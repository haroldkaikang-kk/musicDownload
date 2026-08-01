from __future__ import annotations

import math
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from musicdownload_core import (  # noqa: E402
    HistoryStore,
    analyze_audio,
    display_codec,
    filter_sort_songs,
    finalize_download,
    render_output_path,
)


def make_test_wav(path: Path, seconds: float = 0.12) -> None:
    sample_rate = 44_100
    frame_count = int(sample_rate * seconds)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        frames = bytearray()
        for index in range(frame_count):
            sample = int(12_000 * math.sin(2 * math.pi * 440 * index / sample_rate))
            frames.extend(struct.pack("<hh", sample, sample))
        output.writeframes(frames)


class CoreFeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="musicdownload-selftest-")
        self.root = Path(self.temp.name)
        self.song = {
            "source": "KuwoMusicClient",
            "identifier": "selftest-001",
            "song_name": "测试歌曲",
            "singers": "测试歌手",
            "album": "测试专辑",
            "ext": "wav",
            "samplerate": "unknown",
            "bitrate": None,
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_real_audio_decode_and_parameters(self):
        path = self.root / "tone.wav"
        make_test_wav(path)
        result = analyze_audio(path)
        self.assertTrue(result["decodable"], result)
        self.assertEqual(result["sample_rate_hz"], 44_100)
        self.assertEqual(result["bit_depth"], 16)
        self.assertEqual(result["channels"], 2)
        self.assertEqual(display_codec(result["codec"]), "PCM S16LE")

    def test_naming_history_and_duplicate_lookup(self):
        raw = self.root / "raw.wav"
        make_test_wav(raw)
        result = finalize_download(
            {"ok": True, "song": self.song, "save_path": str(raw)},
            self.root / "library",
            "{artist}/{album}/{title} [{format}-{sample_rate}-{bit_depth}]",
        )
        final_path = Path(result["path"])
        self.assertTrue(final_path.exists())
        self.assertTrue(final_path.resolve().is_relative_to((self.root / "library").resolve()))

        store = HistoryStore(self.root / "history.sqlite3")
        store.record(self.song, "成功", str(final_path), result["analysis"])
        existing = store.find_existing(self.song)
        self.assertIsNotNone(existing)
        self.assertEqual(existing["codec"], "PCM S16LE")
        same_song_other_source = dict(self.song, source="MiguMusicClient", identifier="another-id")
        cross_source = store.find_existing(same_song_other_source)
        self.assertIsNotNone(cross_source)
        self.assertEqual(cross_source["path"], str(final_path))

    def test_filters_tolerate_unknown_numbers(self):
        songs = filter_sort_songs([self.song], sort_mode="采样率")
        self.assertEqual(songs, [self.song])
        rendered = render_output_path(
            self.root / "safe",
            "../../{artist}/{title}",
            self.song,
            {"format": "WAV"},
            "wav",
        )
        self.assertTrue(rendered.resolve().is_relative_to((self.root / "safe").resolve()))

    def test_macos_style_path_alias_is_not_a_failure(self):
        actual_root = self.root / "private-style-root"
        actual_root.mkdir()
        alias_root = self.root / "var-style-alias"
        alias_root.symlink_to(actual_root, target_is_directory=True)
        rendered = render_output_path(
            alias_root,
            "{artist}/{title}",
            self.song,
            {"format": "WAV"},
            "wav",
        )
        self.assertTrue(rendered.resolve().is_relative_to(alias_root.resolve()))


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(CoreFeatureTests)
    outcome = unittest.TextTestRunner(verbosity=2).run(suite)
    if not outcome.wasSuccessful():
        raise SystemExit(1)
    print("MusicDownload 1.2.1 专业功能自检通过。")
