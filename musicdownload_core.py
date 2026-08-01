"""MusicDownload 的非界面核心。

本模块只使用 Python 标准库和音频解析库，既供图形主程序调用，也供
隔离的搜索/下载工作进程调用。工作进程通过内部临时 pickle 文件通信，
不开放任何网络服务。
"""

from __future__ import annotations

import hashlib
import json
import os
import pickle
import re
import signal
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from platformdirs import user_data_dir


APP_NAME = "MusicDownload"
APP_VERSION = "1.2.1"

SOURCE_DEFINITIONS = [
    ("苹果音乐", "AppleMusicClient"),
    ("Deezer", "DeezerMusicClient"),
    ("5sing（原创／翻唱／伴奏）", "FiveSingMusicClient"),
    ("Jamendo", "JamendoMusicClient"),
    ("Joox", "JooxMusicClient"),
    ("酷我音乐", "KuwoMusicClient"),
    ("酷狗音乐", "KugouMusicClient"),
    ("咪咕音乐", "MiguMusicClient"),
    ("网易云音乐", "NeteaseMusicClient"),
    ("QQ音乐", "QQMusicClient"),
    ("千千音乐", "QianqianMusicClient"),
    ("Qobuz", "QobuzMusicClient"),
    ("SoundCloud", "SoundCloudMusicClient"),
    ("StreetVoice", "StreetVoiceMusicClient"),
    ("汽水音乐", "SodaMusicClient"),
    ("Spotify", "SpotifyMusicClient"),
    ("TIDAL", "TIDALMusicClient"),
]

SOURCE_CN_TO_EN = dict(SOURCE_DEFINITIONS)
SOURCE_EN_TO_CN = {value: key for key, value in SOURCE_DEFINITIONS}

SOURCE_PRESETS = {
    "国内主流": [
        "KuwoMusicClient",
        "KugouMusicClient",
        "MiguMusicClient",
        "NeteaseMusicClient",
        "QQMusicClient",
        "QianqianMusicClient",
    ],
    "原创与独立音乐": ["FiveSingMusicClient", "StreetVoiceMusicClient", "JamendoMusicClient"],
    "海外流媒体": [
        "AppleMusicClient",
        "DeezerMusicClient",
        "JooxMusicClient",
        "QobuzMusicClient",
        "SoundCloudMusicClient",
        "SpotifyMusicClient",
        "TIDALMusicClient",
    ],
    "全部17个来源": [value for _, value in SOURCE_DEFINITIONS],
}

DOMESTIC_SOURCE_IDS = {
    "FiveSingMusicClient",
    "KuwoMusicClient",
    "KugouMusicClient",
    "MiguMusicClient",
    "NeteaseMusicClient",
    "QQMusicClient",
    "QianqianMusicClient",
    "SodaMusicClient",
}

HEALTH_TEST_KEYWORDS = {
    "FiveSingMusicClient": "青花瓷 伴奏",
    "StreetVoiceMusicClient": "告五人",
    "JamendoMusicClient": "love",
}

LOSSLESS_EXTENSIONS = {"flac", "wav", "wave", "alac", "ape", "wv", "tta", "dsf", "dff", "aiff", "aif"}
SUPPORTED_AUDIO_EXTENSIONS = LOSSLESS_EXTENSIONS | {"mp3", "m4a", "aac", "ogg", "opus", "wma", "mp4"}

NAMING_TEMPLATES = {
    "歌手 - 歌名": "{artist} - {title}",
    "歌手 - 歌名 - 专辑": "{artist} - {title} - {album}",
    "歌手／专辑／歌名": "{artist}/{album}/{title}",
    "歌名 [格式-采样率-位深]": "{title} [{format}-{sample_rate}-{bit_depth}]",
}


def get_value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        value = obj.get(key, default)
    else:
        value = getattr(obj, key, default)
    return default if value is None else value


def song_to_dict(song: Any) -> dict[str, Any]:
    if isinstance(song, dict):
        return dict(song)
    if hasattr(song, "todict"):
        return song.todict()
    fields = (
        "raw_data", "source", "root_source", "song_name", "singers", "album",
        "ext", "file_size_bytes", "file_size", "duration_s", "duration",
        "bitrate", "codec", "samplerate", "channels", "lyric", "cover_url",
        "episodes", "download_url", "download_url_status", "default_download_headers",
        "default_download_cookies", "downloaded_contents", "chunk_size", "protocol",
        "work_dir", "_save_path", "identifier",
    )
    return {field: getattr(song, field, None) for field in fields}


def artist_text(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return " & ".join(str(item).strip() for item in value if str(item).strip())
    text = str(value or "").strip()
    return "" if text.lower() in {"null", "none"} else text


def clean_text(value: Any, fallback: str = "") -> str:
    text = str(value or "").strip()
    return fallback if not text or text.lower() in {"null", "none"} else text


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return float(default)


def display_codec(value: Any) -> str:
    codec = clean_text(value)
    if not codec:
        return "未知"
    lowered = codec.casefold()
    aliases = {
        "flac": "FLAC",
        "alac": "ALAC",
        "aac": "AAC",
        "aac_latm": "AAC LATM",
        "mp3": "MP3",
        "mp3float": "MP3",
        "mp3adu": "MP3",
        "opus": "Opus",
        "vorbis": "Vorbis",
        "wavpack": "WavPack",
        "ape": "APE",
    }
    if lowered in aliases:
        return aliases[lowered]
    if lowered.startswith("pcm_"):
        return "PCM " + lowered.removeprefix("pcm_").upper()
    return codec.upper()


def sanitize_component(value: Any, fallback: str = "未命名") -> str:
    text = clean_text(value, fallback)
    text = re.sub(r"[\\:*?\"<>|]", "_", text)
    text = re.sub(r"[\x00-\x1f]", "", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return (text or fallback)[:160]


def normalize_key_part(value: Any) -> str:
    return re.sub(r"\s+", "", clean_text(value).casefold())


def build_track_key(song: Any) -> str:
    source = clean_text(get_value(song, "source", ""))
    identifier = clean_text(get_value(song, "identifier", ""))
    if source and identifier:
        raw = f"id|{source}|{identifier}"
    else:
        raw = "meta|{}|{}|{}".format(
            normalize_key_part(get_value(song, "song_name", "")),
            normalize_key_part(artist_text(get_value(song, "singers", ""))),
            normalize_key_part(get_value(song, "album", "")),
        )
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def format_hz(value: Any) -> str:
    try:
        hz = int(float(value))
    except (TypeError, ValueError):
        return "未知"
    if hz <= 0:
        return "未知"
    return f"{hz / 1000:g}kHz"


def format_bitrate(value: Any) -> str:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return "未知"
    if number <= 0:
        return "未知"
    kbps = number / 1000 if number > 10000 else number
    return f"{kbps:.0f}kbps"


def format_duration(seconds: Any) -> str:
    try:
        total = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        return "未知"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def song_duration_seconds(song: Any) -> float:
    value = safe_float(get_value(song, "duration_s", 0))
    if value > 0:
        return value
    text = clean_text(get_value(song, "duration", ""))
    parts = text.split(":")
    if 2 <= len(parts) <= 3:
        try:
            numbers = [float(part) for part in parts]
            if len(numbers) == 2:
                return numbers[0] * 60 + numbers[1]
            return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
        except ValueError:
            pass
    return 0.0


def parse_file_size_bytes(song: Any) -> int:
    raw = get_value(song, "file_size_bytes", 0)
    try:
        value = int(float(raw))
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    text = clean_text(get_value(song, "file_size", "")).upper().replace(" ", "")
    match = re.match(r"([\d.]+)(KIB|MIB|GIB|KB|MB|GB|B)?", text)
    if not match:
        return 0
    number, unit = float(match.group(1)), match.group(2) or "B"
    return int(number * {
        "B": 1,
        "KB": 1024,
        "KIB": 1024,
        "MB": 1024**2,
        "MIB": 1024**2,
        "GB": 1024**3,
        "GIB": 1024**3,
    }[unit])


def search_format(song: Any) -> str:
    ext = clean_text(get_value(song, "ext", "")).lstrip(".").upper()
    if ext:
        return ext
    url = clean_text(get_value(song, "download_url", "")).lower().split("?", 1)[0]
    suffix = Path(url).suffix.lstrip(".").upper()
    return suffix if suffix else "未知"


def search_quality_rank(song: Any, mode: str = "全部") -> tuple:
    fmt = search_format(song).lower()
    bitrate = get_value(song, "bitrate", 0)
    try:
        bitrate_num = float(bitrate or 0)
        if bitrate_num > 10000:
            bitrate_num /= 1000
    except (TypeError, ValueError):
        bitrate_num = 0
    lossless = fmt in LOSSLESS_EXTENSIONS
    if mode == "无损优先":
        return (1 if lossless else 0, parse_file_size_bytes(song), bitrate_num)
    if mode == "MP3 320k优先":
        return (1 if fmt == "mp3" and bitrate_num >= 300 else 0, bitrate_num, parse_file_size_bytes(song))
    return (0, 0, 0)


def filter_sort_songs(
    songs: list[Any],
    singer_filter: str = "",
    album_filter: str = "",
    quality_mode: str = "全部",
    source_filter: str = "全部来源",
    sort_mode: str = "默认顺序",
) -> list[Any]:
    singer_query = singer_filter.strip().casefold()
    album_query = album_filter.strip().casefold()
    filtered = []
    for song in songs:
        singer = artist_text(get_value(song, "singers", "")).casefold()
        album = clean_text(get_value(song, "album", "")).casefold()
        source = SOURCE_EN_TO_CN.get(clean_text(get_value(song, "source", "")), clean_text(get_value(song, "source", "")))
        fmt = search_format(song)
        if singer_query and singer_query not in singer:
            continue
        if album_query and album_query not in album:
            continue
        if source_filter != "全部来源" and source != source_filter:
            continue
        if quality_mode == "仅 FLAC" and fmt != "FLAC":
            continue
        filtered.append(song)

    reverse = sort_mode not in {"默认顺序", "来源"}
    if quality_mode in {"无损优先", "MP3 320k优先"} and sort_mode == "默认顺序":
        return sorted(filtered, key=lambda item: search_quality_rank(item, quality_mode), reverse=True)

    key_functions = {
        "格式": lambda item: search_format(item),
        "采样率": lambda item: safe_float(get_value(item, "samplerate", 0)),
        "比特率": lambda item: safe_float(get_value(item, "bitrate", 0)),
        "文件大小": parse_file_size_bytes,
        "来源": lambda item: SOURCE_EN_TO_CN.get(clean_text(get_value(item, "source", "")), ""),
    }
    key_func = key_functions.get(sort_mode)
    return sorted(filtered, key=key_func, reverse=reverse) if key_func else filtered


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 10000):
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.stem}-{uuid.uuid4().hex[:8]}{path.suffix}")


def render_output_path(target_dir: str | Path, template: str, song: Any, analysis: dict[str, Any], extension: str) -> Path:
    ext = sanitize_component(extension.lstrip(".").lower(), "mp3")
    values = {
        "artist": sanitize_component(artist_text(get_value(song, "singers", "")), "未知歌手"),
        "title": sanitize_component(get_value(song, "song_name", ""), "未知歌曲"),
        "album": sanitize_component(get_value(song, "album", ""), "未知专辑"),
        "source": sanitize_component(SOURCE_EN_TO_CN.get(clean_text(get_value(song, "source", "")), clean_text(get_value(song, "source", ""))), "未知来源"),
        "format": sanitize_component((analysis.get("format") or ext).upper(), ext.upper()),
        "sample_rate": sanitize_component(format_hz(analysis.get("sample_rate_hz")), "未知采样率"),
        "bit_depth": sanitize_component(f"{analysis['bit_depth']}bit" if analysis.get("bit_depth") else "未知位深", "未知位深"),
        "bitrate": sanitize_component(format_bitrate(analysis.get("bitrate_bps")), "未知比特率"),
    }
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", value)
    rendered = re.sub(r"\{[^{}]+\}", "", rendered)
    raw_parts = [part for part in re.split(r"[/\\]+", rendered) if part.strip()]
    safe_parts = [sanitize_component(part) for part in raw_parts] or [values["title"]]
    base = Path(target_dir).expanduser().resolve()
    relative = Path(*safe_parts[:-1], f"{safe_parts[-1]}.{ext}")
    result = (base / relative).resolve()
    if os.path.commonpath([str(base), str(result)]) != str(base):
        raise ValueError("命名模板生成了保存目录之外的路径")
    return result


def move_to_trash(path: str | Path) -> bool:
    source = Path(path)
    if not source.exists():
        return True
    if sys.platform != "darwin":
        return False
    trash = Path.home() / ".Trash"
    try:
        trash.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(unique_path(trash / source.name)))
        return True
    except Exception:
        return False


def analyze_audio(path: str | Path) -> dict[str, Any]:
    """读取真实媒体参数，并尝试完整解码音频流。"""
    audio_path = Path(path)
    result: dict[str, Any] = {
        "path": str(audio_path),
        "format": audio_path.suffix.lstrip(".").upper() or "未知",
        "codec": None,
        "sample_rate_hz": None,
        "bit_depth": None,
        "bitrate_bps": None,
        "channels": None,
        "duration_seconds": None,
        "file_size_bytes": audio_path.stat().st_size if audio_path.exists() else 0,
        "decodable": False,
        "decode_engine": None,
        "error": None,
    }
    if not audio_path.exists() or result["file_size_bytes"] <= 0:
        result["error"] = "文件不存在或大小为0"
        return result

    mutagen_error = None
    try:
        from mutagen import File as MutagenFile

        media = MutagenFile(str(audio_path))
        info = getattr(media, "info", None) if media is not None else None
        if info is not None:
            result["codec"] = clean_text(getattr(info, "codec", None)) or media.__class__.__name__
            result["sample_rate_hz"] = int(getattr(info, "sample_rate", 0) or getattr(info, "samplerate", 0) or 0) or None
            result["bit_depth"] = int(getattr(info, "bits_per_sample", 0) or 0) or None
            result["bitrate_bps"] = int(getattr(info, "bitrate", 0) or 0) or None
            result["channels"] = int(getattr(info, "channels", 0) or 0) or None
            result["duration_seconds"] = float(getattr(info, "length", 0) or 0) or None
    except Exception as exc:
        mutagen_error = str(exc)

    try:
        import av

        frame_count = 0
        with av.open(str(audio_path), mode="r") as container:
            streams = [stream for stream in container.streams if stream.type == "audio"]
            if not streams:
                raise ValueError("没有可解码的音频流")
            stream = streams[0]
            context = stream.codec_context
            result["codec"] = clean_text(getattr(context, "name", None)) or result["codec"]
            result["sample_rate_hz"] = int(getattr(context, "sample_rate", 0) or result["sample_rate_hz"] or 0) or None
            layout = getattr(context, "layout", None)
            layout_channels = getattr(layout, "nb_channels", None) if layout is not None else None
            result["channels"] = int(getattr(context, "channels", 0) or layout_channels or result["channels"] or 0) or None
            raw_bits = int(getattr(context, "bits_per_raw_sample", 0) or getattr(context, "bits_per_coded_sample", 0) or 0)
            codec_name = clean_text(result.get("codec")).casefold()
            lossless_codec = codec_name in {"alac", "flac", "wavpack", "ape", "tta", "dsd_lsbf", "dsd_msbf"} or codec_name.startswith("pcm_")
            if raw_bits > 0 and (audio_path.suffix.lower().lstrip(".") in LOSSLESS_EXTENSIONS or lossless_codec):
                result["bit_depth"] = raw_bits
            stream_rate = int(getattr(stream, "bit_rate", 0) or getattr(context, "bit_rate", 0) or 0)
            if stream_rate > 0:
                result["bitrate_bps"] = stream_rate
            if stream.duration is not None and stream.time_base is not None:
                result["duration_seconds"] = float(stream.duration * stream.time_base)
            elif getattr(container, "duration", None):
                result["duration_seconds"] = float(container.duration / 1_000_000)
            for frame in container.decode(stream):
                frame_count += 1
            if frame_count <= 0:
                raise ValueError("解码器没有输出任何音频帧")
        result["decodable"] = True
        result["decode_engine"] = "PyAV"
    except ImportError:
        result["decodable"] = bool(result["codec"] and result["duration_seconds"])
        result["decode_engine"] = "Mutagen"
        if not result["decodable"]:
            result["error"] = mutagen_error or "无法读取音频参数"
    except Exception as exc:
        result["error"] = f"解码失败：{exc}"
        result["decode_engine"] = "PyAV"

    codec_name = clean_text(result.get("codec")).casefold()
    lossless_codec = codec_name in {"alac", "flac", "wavpack", "ape", "tta", "dsd_lsbf", "dsd_msbf"} or codec_name.startswith("pcm_")
    if audio_path.suffix.lower().lstrip(".") not in LOSSLESS_EXTENSIONS and not lossless_codec:
        # MP3/AAC 等有损编码不存在与 PCM 相同含义的文件位深，避免把解码器
        # 输出的浮点格式误标成原始位深。
        result["bit_depth"] = None
    return result


class HistoryStore:
    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            try:
                data_dir = Path(user_data_dir(APP_NAME, appauthor=False))
                data_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                data_dir = Path(tempfile.gettempdir()) / APP_NAME
                data_dir.mkdir(parents=True, exist_ok=True)
            db_path = data_dir / "download-history.sqlite3"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(str(self.db_path), timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    track_key TEXT NOT NULL,
                    downloaded_at TEXT NOT NULL,
                    source TEXT,
                    identifier TEXT,
                    title TEXT,
                    artist TEXT,
                    album TEXT,
                    path TEXT,
                    format TEXT,
                    codec TEXT,
                    sample_rate_hz INTEGER,
                    bit_depth INTEGER,
                    bitrate_bps INTEGER,
                    channels INTEGER,
                    duration_seconds REAL,
                    decodable INTEGER,
                    file_size_bytes INTEGER,
                    status TEXT NOT NULL,
                    error TEXT,
                    details_json TEXT
                )
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(downloads)").fetchall()}
            if "codec" not in columns:
                connection.execute("ALTER TABLE downloads ADD COLUMN codec TEXT")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_downloads_track_key ON downloads(track_key)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_downloads_date ON downloads(downloaded_at DESC)")

    def record(self, song: Any, status: str, path: str = "", analysis: dict[str, Any] | None = None, error: str = "") -> int:
        analysis = analysis or {}
        payload = (
            build_track_key(song),
            datetime.now().astimezone().isoformat(timespec="seconds"),
            clean_text(get_value(song, "source", "")),
            clean_text(get_value(song, "identifier", "")),
            clean_text(get_value(song, "song_name", "")),
            artist_text(get_value(song, "singers", "")),
            clean_text(get_value(song, "album", "")),
            str(path or ""),
            clean_text(analysis.get("format")),
            display_codec(analysis.get("codec")) if analysis.get("codec") else "",
            analysis.get("sample_rate_hz"),
            analysis.get("bit_depth"),
            analysis.get("bitrate_bps"),
            analysis.get("channels"),
            analysis.get("duration_seconds"),
            1 if analysis.get("decodable") else 0,
            analysis.get("file_size_bytes") or 0,
            status,
            str(error or analysis.get("error") or ""),
            json.dumps(analysis, ensure_ascii=False, default=str),
        )
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO downloads (
                    track_key, downloaded_at, source, identifier, title, artist,
                    album, path, format, codec, sample_rate_hz, bit_depth, bitrate_bps,
                    channels, duration_seconds, decodable, file_size_bytes,
                    status, error, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
            return int(cursor.lastrowid)

    def find_existing(self, song: Any) -> dict[str, Any] | None:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM downloads WHERE track_key = ? AND status = '成功' ORDER BY id DESC",
                (build_track_key(song),),
            ).fetchall()
        for row in rows:
            data = dict(row)
            if data.get("path") and Path(data["path"]).exists():
                return data

        # 不同来源通常使用不同 identifier。仅在歌名和歌手完全一致，并且
        # 时长接近（或专辑也完全一致）时，才把跨来源结果视为同一首，避免
        # 把现场版、伴奏版等仅同名作品自动判成重复。
        title_key = normalize_key_part(get_value(song, "song_name", ""))
        artist_key = normalize_key_part(artist_text(get_value(song, "singers", "")))
        album_key = normalize_key_part(get_value(song, "album", ""))
        wanted_duration = song_duration_seconds(song)
        if not title_key or not artist_key:
            return None
        with self._connect() as connection:
            candidates = connection.execute(
                "SELECT * FROM downloads WHERE status = '成功' ORDER BY id DESC LIMIT 1000"
            ).fetchall()
        for row in candidates:
            data = dict(row)
            if not data.get("path") or not Path(data["path"]).exists():
                continue
            if normalize_key_part(data.get("title")) != title_key or normalize_key_part(data.get("artist")) != artist_key:
                continue
            existing_duration = safe_float(data.get("duration_seconds"))
            same_duration = wanted_duration > 0 and existing_duration > 0 and abs(wanted_duration - existing_duration) <= 4
            same_album = bool(album_key) and normalize_key_part(data.get("album")) == album_key
            if same_duration or same_album:
                return data
        return None

    def list_records(self, query: str = "", limit: int = 1000) -> list[dict[str, Any]]:
        query = query.strip()
        with self._connect() as connection:
            if query:
                pattern = f"%{query}%"
                rows = connection.execute(
                    """
                    SELECT * FROM downloads
                    WHERE title LIKE ? OR artist LIKE ? OR album LIKE ? OR source LIKE ? OR path LIKE ?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (pattern, pattern, pattern, pattern, pattern, int(limit)),
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM downloads ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(row) for row in rows]

    def remove_missing_records(self) -> int:
        records = self.list_records(limit=100000)
        missing_ids = [item["id"] for item in records if item.get("path") and not Path(item["path"]).exists()]
        if not missing_ids:
            return 0
        placeholders = ",".join("?" for _ in missing_ids)
        with self._connect() as connection:
            connection.execute(f"DELETE FROM downloads WHERE id IN ({placeholders})", missing_ids)
        return len(missing_ids)


def detect_network_proxy() -> dict[str, Any]:
    details: list[str] = []
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        if os.environ.get(key):
            details.append(f"环境变量 {key}")
    if sys.platform == "darwin":
        try:
            output = subprocess.run(
                ["/usr/sbin/scutil", "--proxy"],
                capture_output=True,
                text=True,
                timeout=4,
                check=False,
            ).stdout
            for name, label in (("HTTPEnable", "HTTP代理"), ("HTTPSEnable", "HTTPS代理"), ("SOCKSEnable", "SOCKS代理")):
                if re.search(rf"\b{name}\s*:\s*1\b", output):
                    details.append(label)
        except Exception:
            pass
        try:
            output = subprocess.run(
                ["/usr/sbin/scutil", "--nc", "list"],
                capture_output=True,
                text=True,
                timeout=4,
                check=False,
            ).stdout
            if "(Connected)" in output:
                details.append("系统VPN连接")
        except Exception:
            pass
    return {"active": bool(details), "details": sorted(set(details))}


def reveal_in_file_manager(path: str | Path) -> bool:
    target = Path(path).expanduser()
    try:
        if sys.platform == "darwin":
            command = ["open", "-R", str(target)] if target.is_file() else ["open", str(target)]
        elif sys.platform.startswith("win"):
            command = ["explorer", "/select,", str(target)] if target.is_file() else ["explorer", str(target)]
        else:
            command = ["xdg-open", str(target.parent if target.is_file() else target)]
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def atomic_pickle_dump(path: str | Path, value: Any):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_name(destination.name + f".{uuid.uuid4().hex}.tmp")
    with temp_path.open("wb") as handle:
        pickle.dump(value, handle, protocol=pickle.HIGHEST_PROTOCOL)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, destination)


def pickle_load(path: str | Path) -> Any:
    with Path(path).open("rb") as handle:
        return pickle.load(handle)


def _worker_search(payload: dict[str, Any]) -> dict[str, Any]:
    from musicdl import musicdl

    source = payload["source"]
    limit = max(1, int(payload.get("limit", 1)))
    work_dir = payload["work_dir"]
    config = {
        source: {
            "search_size_per_source": limit,
            "search_size_per_page": min(limit, 10),
            "strict_limit_search_size_per_page": True,
            "work_dir": work_dir,
            "max_retries": 1,
        }
    }
    client = musicdl.MusicClient(
        music_sources=[source],
        init_music_clients_cfg=config,
        clients_threadings={source: min(3, limit)},
    )
    started = time.monotonic()
    if payload.get("mode") == "playlist":
        parsed = client.parseplaylist(payload["keyword"])
        songs = list(parsed or [])
    else:
        result = client.search(keyword=payload["keyword"])
        songs = list(result.get(source, []) or [])
    return {"ok": True, "source": source, "songs": songs, "elapsed": time.monotonic() - started}


def _worker_download(payload: dict[str, Any]) -> dict[str, Any]:
    from musicdl import musicdl
    from musicdl.modules import SongInfo

    song = payload["song"]
    if isinstance(song, dict):
        song = SongInfo.fromdict(song)
    source = clean_text(get_value(song, "source", ""))
    if not source:
        raise ValueError("歌曲没有来源标识")
    work_dir = payload["work_dir"]
    Path(work_dir).mkdir(parents=True, exist_ok=True)
    song.work_dir = work_dir
    if hasattr(song, "_save_path"):
        song._save_path = None
    config = {
        source: {
            "search_size_per_source": 1,
            "work_dir": work_dir,
            "max_retries": 1,
        }
    }
    client = musicdl.MusicClient(
        music_sources=[source],
        init_music_clients_cfg=config,
        clients_threadings={source: 1},
    )
    started = time.monotonic()
    downloaded = list(client.download(song_infos=[song]) or [])
    valid = []
    for item in downloaded:
        save_path = clean_text(get_value(item, "save_path", ""))
        if save_path and Path(save_path).exists():
            valid.append((item, save_path))
    if not valid:
        raise RuntimeError("来源没有返回可用的下载文件")
    item, save_path = valid[0]
    analysis = analyze_audio(save_path)
    return {
        "ok": True,
        "source": source,
        "song": item,
        "save_path": save_path,
        "analysis": analysis,
        "elapsed": time.monotonic() - started,
    }


def run_worker_cli(argv: list[str]) -> int:
    try:
        marker_index = argv.index("--musicdownload-worker")
        mode, input_path, output_path = argv[marker_index + 1 : marker_index + 4]
    except (ValueError, IndexError):
        return 2
    try:
        payload = pickle_load(input_path)
        if mode == "search":
            result = _worker_search(payload)
        elif mode == "download":
            result = _worker_download(payload)
        else:
            raise ValueError(f"未知工作模式：{mode}")
    except BaseException as exc:
        result = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
    try:
        atomic_pickle_dump(output_path, result)
        return 0 if result.get("ok") else 1
    except Exception:
        return 3


def worker_command(mode: str, input_path: str | Path, output_path: str | Path) -> list[str]:
    args = ["--musicdownload-worker", mode, str(input_path), str(output_path)]
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    main_script = Path(__file__).resolve().with_name("musicdownload.py")
    return [sys.executable, str(main_script), *args]


@dataclass
class IsolatedWorkerJob:
    mode: str
    payload: dict[str, Any]
    prefix: str = "musicdownload-job-"

    def __post_init__(self):
        self.directory = Path(tempfile.mkdtemp(prefix=self.prefix))
        self.input_path = self.directory / "input.pkl"
        self.output_path = self.directory / "output.pkl"
        self.log_path = self.directory / "worker.log"
        self.process: subprocess.Popen | None = None
        self.log_handle = None
        self.started_at = 0.0
        atomic_pickle_dump(self.input_path, self.payload)

    def start(self):
        self.log_handle = self.log_path.open("wb")
        try:
            self.process = subprocess.Popen(
                worker_command(self.mode, self.input_path, self.output_path),
                stdin=subprocess.DEVNULL,
                stdout=self.log_handle,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=os.name == "posix",
            )
        except Exception:
            self.log_handle.close()
            self.log_handle = None
            raise
        self.started_at = time.monotonic()
        return self

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at if self.started_at else 0.0

    def poll(self):
        return self.process.poll() if self.process else None

    def terminate(self):
        if not self.process or self.process.poll() is not None:
            return
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                self.process.terminate()
        else:
            self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                try:
                    os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    self.process.kill()
            else:
                self.process.kill()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    def collect(self) -> dict[str, Any]:
        if self.process and self.process.poll() is None:
            raise RuntimeError("工作进程尚未结束")
        if self.log_handle:
            self.log_handle.close()
            self.log_handle = None
        if self.output_path.exists():
            return pickle_load(self.output_path)
        details = ""
        try:
            details = self.log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
        except Exception:
            pass
        code = self.process.returncode if self.process else "未知"
        return {"ok": False, "error": f"工作进程异常退出（代码 {code}）", "traceback": details}

    def cleanup(self):
        if self.log_handle:
            self.log_handle.close()
            self.log_handle = None
        shutil.rmtree(self.directory, ignore_errors=True)


def finalize_download(
    worker_result: dict[str, Any],
    target_dir: str | Path,
    naming_template: str,
    collision_policy: str = "keep_both",
    existing_path: str = "",
) -> dict[str, Any]:
    if not worker_result.get("ok"):
        raise RuntimeError(worker_result.get("error") or "下载工作进程失败")
    raw_path = Path(worker_result["save_path"])
    song = worker_result["song"]
    if not raw_path.exists():
        raise FileNotFoundError(f"下载文件不存在：{raw_path}")
    analysis = dict(worker_result.get("analysis") or analyze_audio(raw_path))
    extension = raw_path.suffix.lstrip(".") or clean_text(get_value(song, "ext", ""), "mp3")
    final_path = render_output_path(target_dir, naming_template, song, analysis, extension)

    if collision_policy == "replace" and existing_path:
        old_path = Path(existing_path)
        if old_path.exists() and not move_to_trash(old_path):
            raise RuntimeError("无法把旧文件移到废纸篓，因此没有覆盖它")
        old_lrc = old_path.with_suffix(".lrc")
        if old_lrc.exists():
            move_to_trash(old_lrc)
    if final_path.exists():
        if collision_policy == "replace":
            if not move_to_trash(final_path):
                raise RuntimeError("无法把同名文件移到废纸篓，因此没有覆盖它")
        else:
            final_path = unique_path(final_path)

    final_path.parent.mkdir(parents=True, exist_ok=True)
    old_lrc_path = raw_path.with_suffix(".lrc")
    final_lrc_path = final_path.with_suffix(".lrc")
    shutil.move(str(raw_path), str(final_path))
    if old_lrc_path.exists():
        if final_lrc_path.exists():
            final_lrc_path = unique_path(final_lrc_path)
        shutil.move(str(old_lrc_path), str(final_lrc_path))
    analysis["path"] = str(final_path)
    analysis["file_size_bytes"] = final_path.stat().st_size
    return {"song": song, "path": str(final_path), "analysis": analysis}
