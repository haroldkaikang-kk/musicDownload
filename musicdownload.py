import sys


# 隔离工作进程必须在导入 Qt 之前进入，避免工作进程创建图形界面。
if "--musicdownload-worker" in sys.argv:
    from musicdownload_core import run_worker_cli

    raise SystemExit(run_worker_cli(sys.argv))


import logging
import shutil
import tempfile
import threading
import time
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
from platformdirs import user_log_dir
from PySide6.QtCore import QObject, QRunnable, QSettings, Qt, QThread, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QIcon, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from musicdownload_core import (
    APP_NAME,
    APP_VERSION,
    DOMESTIC_SOURCE_IDS,
    HEALTH_TEST_KEYWORDS,
    HistoryStore,
    IsolatedWorkerJob,
    NAMING_TEMPLATES,
    SOURCE_DEFINITIONS,
    SOURCE_EN_TO_CN,
    SOURCE_PRESETS,
    artist_text,
    build_track_key,
    clean_text,
    detect_network_proxy,
    display_codec,
    filter_sort_songs,
    finalize_download,
    format_bitrate,
    format_duration,
    format_hz,
    get_value,
    parse_file_size_bytes,
    reveal_in_file_manager,
    safe_float,
    search_format,
)


APP_DISPLAY_NAME = "MusicDownload 音频下载与检查工具"
APP_ORGANIZATION = "MusicDownload"
APP_BUNDLE_ID = "com.musicdownload.desktop"


def resource_path(*parts):
    base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return str(base_dir.joinpath(*parts))


def setup_logging():
    try:
        log_dir = Path(user_log_dir(APP_NAME, appauthor=False))
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        log_dir = Path(tempfile.gettempdir()) / APP_NAME
        log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "musicdownload.log"
    handler = RotatingFileHandler(
        log_path,
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(threadName)s | %(message)s"))
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False
    logger.info("%s %s 启动", APP_NAME, APP_VERSION)
    return logger, str(log_dir), str(log_path)


LOGGER, LOG_DIR, LOG_PATH = setup_logging()
DOWNLOAD_SESSION_LOCK = threading.Lock()


def handle_uncaught_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    LOGGER.critical("未处理异常", exc_info=(exc_type, exc_value, exc_traceback))
    if QApplication.instance() is not None:
        QMessageBox.critical(None, "MusicDownload 发生错误", f"程序遇到未处理的错误。\n日志：{LOG_PATH}")


def ensure_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    return list(value)


def song_title(song):
    return clean_text(get_value(song, "song_name", ""), "未知歌曲")


def song_artist(song):
    return artist_text(get_value(song, "singers", "")) or "未知歌手"


def source_label(song_or_source):
    source = song_or_source if isinstance(song_or_source, str) else clean_text(get_value(song_or_source, "source", ""))
    return SOURCE_EN_TO_CN.get(source, source or "未知来源")


class NumericItem(QTableWidgetItem):
    def __lt__(self, other):
        try:
            return float(self.data(Qt.ItemDataRole.UserRole) or 0) < float(other.data(Qt.ItemDataRole.UserRole) or 0)
        except Exception:
            return super().__lt__(other)


class ImageSignals(QObject):
    finished = Signal(str, QPixmap)
    error = Signal(str)


class ImageDownloadTask(QRunnable):
    def __init__(self, track_key, image_url):
        super().__init__()
        self.track_key = track_key
        self.image_url = image_url
        self.signals = ImageSignals()

    def run(self):
        try:
            response = requests.get(self.image_url, timeout=(4, 7), headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
            pixmap = QPixmap()
            if not pixmap.loadFromData(response.content):
                raise ValueError("无法读取封面图片")
            self.signals.finished.emit(
                self.track_key,
                pixmap.scaled(44, 44, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation),
            )
        except Exception:
            self.signals.error.emit(self.track_key)


class SearchCoordinator(QThread):
    source_update = Signal(str, str, int, float, str)
    completed = Signal(object, object, bool)

    def __init__(self, sources, keyword, limit, mode="search", timeout_seconds=35, max_parallel=4, keyword_map=None, parent=None):
        super().__init__(parent)
        self.sources = list(sources)
        self.keyword = keyword
        self.limit = int(limit)
        self.mode = mode
        self.timeout_seconds = int(timeout_seconds)
        self.max_parallel = max(1, int(max_parallel))
        self.keyword_map = dict(keyword_map or {})
        self._cancel_event = threading.Event()
        self._jobs_lock = threading.Lock()
        self._jobs = {}

    def cancel(self):
        self._cancel_event.set()
        with self._jobs_lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            job.terminate()

    def run(self):
        results = {source: [] for source in self.sources}
        statuses = {}
        try:
            self._run_search(results, statuses)
        except BaseException as exc:
            LOGGER.exception("来源协调器发生未处理错误")
            detail = f"协调器错误：{type(exc).__name__}: {exc}"
            for source in self.sources:
                if source not in statuses:
                    statuses[source] = {"status": "失败", "count": 0, "elapsed": 0.0, "detail": detail}
                    self.source_update.emit(source, "失败", 0, 0.0, detail)
        finally:
            self.completed.emit(results, statuses, self._cancel_event.is_set())

    def _run_search(self, results, statuses):
        pending = list(self.sources)
        work_root = Path(tempfile.mkdtemp(prefix="musicdownload-search-"))
        running = {}
        try:
            while pending or running:
                if self._cancel_event.is_set():
                    for job in running.values():
                        job.terminate()
                    break

                while pending and len(running) < self.max_parallel and not self._cancel_event.is_set():
                    source = pending.pop(0)
                    payload = {
                        "source": source,
                        "keyword": self.keyword_map.get(source, self.keyword),
                        "limit": self.limit,
                        "mode": "playlist" if self.mode == "playlist" else "search",
                        "work_dir": str(work_root / source),
                    }
                    job = None
                    try:
                        job = IsolatedWorkerJob("search", payload, prefix=f"musicdownload-{source}-")
                        job.start()
                        running[source] = job
                        with self._jobs_lock:
                            self._jobs[source] = job
                        self.source_update.emit(source, "检测中" if self.keyword_map else "搜索中", 0, 0.0, "")
                    except Exception as exc:
                        if job:
                            job.cleanup()
                        statuses[source] = {"status": "失败", "count": 0, "elapsed": 0.0, "detail": str(exc)}
                        self.source_update.emit(source, "失败", 0, 0.0, str(exc))

                for source, job in list(running.items()):
                    if job.elapsed > self.timeout_seconds and job.poll() is None:
                        job.terminate()
                        statuses[source] = {
                            "status": "超时",
                            "count": 0,
                            "elapsed": job.elapsed,
                            "detail": f"超过 {self.timeout_seconds} 秒，已自动跳过",
                        }
                        self.source_update.emit(source, "超时", 0, job.elapsed, statuses[source]["detail"])
                        job.cleanup()
                        running.pop(source, None)
                        with self._jobs_lock:
                            self._jobs.pop(source, None)
                        continue
                    if job.poll() is None:
                        continue
                    payload = job.collect()
                    elapsed = float(payload.get("elapsed") or job.elapsed)
                    if payload.get("ok"):
                        songs = list(payload.get("songs") or [])
                        results[source] = songs
                        status = "正常" if songs else "无结果"
                        detail = "接口返回正常" if songs else "接口完成请求，但测试关键词没有可用结果"
                        statuses[source] = {"status": status, "count": len(songs), "elapsed": elapsed, "detail": detail}
                    else:
                        error = clean_text(payload.get("error"), "未知错误")
                        statuses[source] = {"status": "失败", "count": 0, "elapsed": elapsed, "detail": error}
                    item = statuses[source]
                    self.source_update.emit(source, item["status"], item["count"], item["elapsed"], item["detail"])
                    job.cleanup()
                    running.pop(source, None)
                    with self._jobs_lock:
                        self._jobs.pop(source, None)
                time.sleep(0.08)

            if self._cancel_event.is_set():
                for source in pending:
                    statuses[source] = {"status": "已取消", "count": 0, "elapsed": 0.0, "detail": "未开始"}
                    self.source_update.emit(source, "已取消", 0, 0.0, "未开始")
                for source, job in list(running.items()):
                    job.terminate()
                    statuses[source] = {"status": "已取消", "count": 0, "elapsed": job.elapsed, "detail": "用户取消"}
                    self.source_update.emit(source, "已取消", 0, job.elapsed, "用户取消")
                    job.cleanup()
        finally:
            with self._jobs_lock:
                self._jobs.clear()
            shutil.rmtree(work_root, ignore_errors=True)


class DownloadCoordinator(QThread):
    item_update = Signal(int, str, str)
    duplicate_request = Signal(str, object, object)
    completed = Signal(object, bool)

    def __init__(self, songs, target_dir, naming_template, duplicate_policy, timeout_seconds, history_store, parent=None):
        super().__init__(parent)
        self.songs = list(songs)
        self.target_dir = target_dir
        self.naming_template = naming_template
        self.duplicate_policy = duplicate_policy
        self.timeout_seconds = int(timeout_seconds)
        self.history_store = history_store
        self._cancel_event = threading.Event()
        self._pause_event = threading.Event()
        self._active_lock = threading.Lock()
        self._active_job = None
        self._decision_condition = threading.Condition()
        self._decisions = {}

    def cancel(self):
        self._cancel_event.set()
        with self._decision_condition:
            self._decision_condition.notify_all()
        with self._active_lock:
            job = self._active_job
        if job:
            job.terminate()

    def set_paused(self, paused):
        if paused:
            self._pause_event.set()
        else:
            self._pause_event.clear()

    def resolve_duplicate(self, token, choice):
        with self._decision_condition:
            self._decisions[token] = choice
            self._decision_condition.notify_all()

    def _ask_duplicate(self, song, existing):
        token = uuid.uuid4().hex
        with self._decision_condition:
            self._decisions[token] = None
        self.duplicate_request.emit(token, song, existing)
        with self._decision_condition:
            while self._decisions.get(token) is None and not self._cancel_event.is_set():
                self._decision_condition.wait(timeout=0.2)
            return self._decisions.pop(token, None) or "skip"

    def _wait_while_paused(self, index):
        announced = False
        while self._pause_event.is_set() and not self._cancel_event.is_set():
            if not announced:
                self.item_update.emit(index, "已暂停", "等待继续")
                announced = True
            time.sleep(0.1)
        return not self._cancel_event.is_set()

    def _find_existing(self, song):
        try:
            return self.history_store.find_existing(song)
        except Exception:
            LOGGER.exception("读取下载历史失败，将继续当前下载")
            return None

    def _record_history(self, song, status, path="", analysis=None, error=""):
        try:
            self.history_store.record(song, status, path, analysis, error)
        except Exception:
            LOGGER.exception("写入下载历史失败：%s - %s", song_title(song), song_artist(song))

    def run(self):
        reports = []
        try:
            self._run_queue(reports)
        except BaseException as exc:
            LOGGER.exception("下载队列发生未处理错误")
            reason = f"队列内部错误：{type(exc).__name__}: {exc}"
            for index in range(len(reports), len(self.songs)):
                song = self.songs[index]
                reports.append({"song": song, "status": "失败", "reason": reason, "path": "", "analysis": {}})
                self.item_update.emit(index, "失败", reason)
        finally:
            if self._cancel_event.is_set():
                completed_count = len(reports)
                for index in range(completed_count, len(self.songs)):
                    song = self.songs[index]
                    reports.append({"song": song, "status": "已取消", "reason": "任务尚未开始", "path": "", "analysis": {}})
                    self.item_update.emit(index, "已取消", "任务尚未开始")
            self.completed.emit(reports, self._cancel_event.is_set())

    def _run_queue(self, reports):
        for index, song in enumerate(self.songs):
            if self._cancel_event.is_set():
                break
            if not self._wait_while_paused(index):
                break

            existing = self._find_existing(song)
            collision_policy = "keep_both"
            if existing:
                choice = self.duplicate_policy
                if choice == "ask":
                    choice = self._ask_duplicate(song, existing)
                if choice in {"skip", "open"}:
                    reports.append({
                        "song": song,
                        "status": "已存在" if choice == "open" else "已跳过",
                        "reason": "历史记录中已有可用文件",
                        "path": existing.get("path", ""),
                        "analysis": {},
                    })
                    self.item_update.emit(index, reports[-1]["status"], existing.get("path", ""))
                    continue
                collision_policy = "replace" if choice == "replace" else "keep_both"

            task_root = Path(tempfile.mkdtemp(prefix="musicdownload-download-"))
            payload = {"song": song, "work_dir": str(task_root / "raw")}
            job = None
            try:
                self.item_update.emit(index, "下载及检查中", f"{source_label(song)} · 超时包含完整音频解码")
                job = IsolatedWorkerJob("download", payload, prefix="musicdownload-song-")
                job.start()
                with self._active_lock:
                    self._active_job = job
                while job.poll() is None:
                    if self._cancel_event.is_set():
                        job.terminate()
                        raise InterruptedError("用户取消")
                    if job.elapsed > self.timeout_seconds:
                        job.terminate()
                        raise TimeoutError(f"超过 {self.timeout_seconds} 秒，已停止此项目")
                    time.sleep(0.1)
                worker_result = job.collect()
                if not worker_result.get("ok"):
                    raise RuntimeError(worker_result.get("error") or "来源没有返回文件")
                self.item_update.emit(index, "整理文件", "根据真实音频参数命名并写入历史")
                finalized = finalize_download(
                    worker_result,
                    self.target_dir,
                    self.naming_template,
                    collision_policy=collision_policy,
                    existing_path=existing.get("path", "") if existing else "",
                )
                analysis = finalized["analysis"]
                status = "成功" if analysis.get("decodable") else "文件异常"
                reason = "解码正常" if analysis.get("decodable") else clean_text(analysis.get("error"), "音频无法完整解码")
                report = {
                    "song": finalized["song"],
                    "status": status,
                    "reason": reason,
                    "path": finalized["path"],
                    "analysis": analysis,
                }
                reports.append(report)
                self._record_history(finalized["song"], status, finalized["path"], analysis, reason if status != "成功" else "")
                self.item_update.emit(index, status, f"{reason} · {finalized['path']}")
            except InterruptedError as exc:
                report = {"song": song, "status": "已取消", "reason": str(exc), "path": "", "analysis": {}}
                reports.append(report)
                self.item_update.emit(index, "已取消", str(exc))
            except TimeoutError as exc:
                report = {"song": song, "status": "超时", "reason": str(exc), "path": "", "analysis": {}}
                reports.append(report)
                self._record_history(song, "超时", error=str(exc))
                self.item_update.emit(index, "超时", str(exc))
            except Exception as exc:
                LOGGER.exception("下载项目失败：%s - %s", song_title(song), song_artist(song))
                report = {"song": song, "status": "失败", "reason": str(exc), "path": "", "analysis": {}}
                reports.append(report)
                self._record_history(song, "失败", error=str(exc))
                self.item_update.emit(index, "失败", str(exc))
            finally:
                with self._active_lock:
                    self._active_job = None
                if job:
                    job.cleanup()
                shutil.rmtree(task_root, ignore_errors=True)



class SourceProgressDialog(QDialog):
    cancel_requested = Signal()

    def __init__(self, title, sources, keep_open=False, parent=None):
        super().__init__(parent)
        self.keep_open = keep_open
        self.active = True
        self.setWindowTitle(title)
        self.resize(760, 460)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        layout = QVBoxLayout(self)
        self.summary = QLabel("正在逐个检测；单个来源不会阻塞其他来源。")
        layout.addWidget(self.summary)
        self.table = QTableWidget(len(sources), 5)
        self.table.setHorizontalHeaderLabels(["来源", "状态", "结果数", "耗时", "说明"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.rows = {}
        for row, source in enumerate(sources):
            self.rows[source] = row
            self.table.setItem(row, 0, QTableWidgetItem(source_label(source)))
            self.table.setItem(row, 1, QTableWidgetItem("等待"))
            self.table.setItem(row, 2, QTableWidgetItem("0"))
            self.table.setItem(row, 3, QTableWidgetItem("—"))
            self.table.setItem(row, 4, QTableWidgetItem(""))
        layout.addWidget(self.table)
        self.progress = QProgressBar()
        self.progress.setRange(0, len(sources))
        layout.addWidget(self.progress)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self.cancel_requested.emit)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)
        self.completed_count = 0

    def update_source(self, source, status, count, elapsed, detail):
        row = self.rows.get(source)
        if row is None:
            return
        previous = self.table.item(row, 1).text()
        self.table.item(row, 1).setText(status)
        self.table.item(row, 2).setText(str(count))
        self.table.item(row, 3).setText(f"{elapsed:.1f}s" if elapsed else "—")
        self.table.item(row, 4).setText(detail)
        colors = {"正常": "#15803d", "无结果": "#b45309", "失败": "#dc2626", "超时": "#dc2626", "已取消": "#6b7280"}
        self.table.item(row, 1).setForeground(QColor(colors.get(status, "#2563eb")))
        if previous in {"等待", "搜索中", "检测中"} and status not in {"搜索中", "检测中"}:
            self.completed_count += 1
            self.progress.setValue(self.completed_count)

    def finish(self, statuses, canceled):
        self.active = False
        normal = sum(1 for item in statuses.values() if item["status"] == "正常")
        failed = sum(1 for item in statuses.values() if item["status"] in {"失败", "超时"})
        self.summary.setText(f"完成：正常 {normal} 个，异常/超时 {failed} 个" + ("；任务已取消" if canceled else ""))
        if self.keep_open:
            self.cancel_button.setText("关闭")
            try:
                self.cancel_button.clicked.disconnect()
            except RuntimeError:
                pass
            self.cancel_button.clicked.connect(self.accept)
        else:
            self.accept()

    def closeEvent(self, event):
        if self.active:
            self.summary.setText("正在安全取消各来源工作进程……")
            self.cancel_button.setEnabled(False)
            self.cancel_requested.emit()
            event.ignore()
            return
        super().closeEvent(event)


class DownloadReportDialog(QDialog):
    retry_requested = Signal(object)

    def __init__(self, reports, parent=None):
        super().__init__(parent)
        self.reports = list(reports)
        self.setWindowTitle("下载与音频检查报告")
        self.resize(1220, 620)
        layout = QVBoxLayout(self)
        success = sum(1 for item in reports if item["status"] == "成功")
        abnormal = sum(1 for item in reports if item["status"] == "文件异常")
        failed = sum(1 for item in reports if item["status"] in {"失败", "超时"})
        layout.addWidget(QLabel(f"完成：成功 {success}，文件异常 {abnormal}，失败/超时 {failed}。双击成功项目可在访达中显示。"))
        self.table = QTableWidget(len(reports), 13)
        self.table.setHorizontalHeaderLabels([
            "状态", "歌曲", "歌手", "来源", "格式", "编码", "采样率", "位深", "比特率", "声道", "时长", "解码", "文件/原因"
        ])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(12, QHeaderView.ResizeMode.Stretch)
        for row, report in enumerate(reports):
            song = report["song"]
            analysis = report.get("analysis") or {}
            values = [
                report["status"], song_title(song), song_artist(song), source_label(song),
                analysis.get("format") or "—", display_codec(analysis.get("codec")), format_hz(analysis.get("sample_rate_hz")),
                f"{analysis['bit_depth']}bit" if analysis.get("bit_depth") else "—",
                format_bitrate(analysis.get("bitrate_bps")), str(analysis.get("channels") or "—"),
                format_duration(analysis.get("duration_seconds")), "正常" if analysis.get("decodable") else "异常/未检查",
                report.get("path") or report.get("reason") or "—",
            ]
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))
        self.table.doubleClicked.connect(self.reveal_selected)
        layout.addWidget(self.table)
        buttons = QHBoxLayout()
        self.retry_button = QPushButton("只重试失败项目")
        retry_items = [item["song"] for item in reports if item["status"] in {"失败", "超时", "文件异常"}]
        self.retry_button.setEnabled(bool(retry_items))
        self.retry_button.clicked.connect(lambda: self.retry_requested.emit(retry_items))
        reveal_button = QPushButton("在访达中显示")
        reveal_button.clicked.connect(self.reveal_selected)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.accept)
        buttons.addWidget(self.retry_button)
        buttons.addStretch()
        buttons.addWidget(reveal_button)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

    def reveal_selected(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self.reports):
            return
        path = self.reports[row].get("path")
        if path:
            reveal_in_file_manager(path)


class DownloadQueueDialog(QDialog):
    def __init__(self, songs, target_dir, naming_template, duplicate_policy, timeout_seconds, history_store, auto_start=False, parent=None):
        super().__init__(parent)
        self.songs = list(songs)
        self.target_dir = target_dir
        self.naming_template = naming_template
        self.duplicate_policy = duplicate_policy
        self.timeout_seconds = timeout_seconds
        self.history_store = history_store
        self.coordinator = None
        self.reports = []
        self.active = False
        self.paused = False
        self.close_when_finished = False
        self.owns_session_lock = False
        self.setWindowTitle("下载队列")
        self.resize(900, 560)
        layout = QVBoxLayout(self)
        self.summary = QLabel("可在开始前调整顺序。暂停会在当前歌曲处理完成后生效。")
        layout.addWidget(self.summary)
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["顺序", "歌曲", "歌手", "来源", "状态", "详细信息"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)
        self.progress = QProgressBar()
        self.progress.setRange(0, len(self.songs))
        layout.addWidget(self.progress)
        buttons = QHBoxLayout()
        self.up_button = QPushButton("上移")
        self.down_button = QPushButton("下移")
        self.start_button = QPushButton("开始下载")
        self.pause_button = QPushButton("暂停")
        self.cancel_button = QPushButton("取消全部")
        self.open_button = QPushButton("打开下载文件夹")
        self.close_button = QPushButton("关闭")
        self.pause_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.close_button.setEnabled(True)
        self.up_button.clicked.connect(lambda: self.move_selected(-1))
        self.down_button.clicked.connect(lambda: self.move_selected(1))
        self.start_button.clicked.connect(self.start_download)
        self.pause_button.clicked.connect(self.toggle_pause)
        self.cancel_button.clicked.connect(self.cancel_download)
        self.open_button.clicked.connect(lambda: reveal_in_file_manager(self.target_dir))
        self.close_button.clicked.connect(self.close)
        for button in (self.up_button, self.down_button, self.start_button, self.pause_button, self.cancel_button):
            buttons.addWidget(button)
        buttons.addStretch()
        buttons.addWidget(self.open_button)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)
        self.rebuild_table()
        if auto_start:
            QTimer.singleShot(0, self.start_download)

    def rebuild_table(self):
        self.table.setRowCount(len(self.songs))
        for row, song in enumerate(self.songs):
            values = [str(row + 1), song_title(song), song_artist(song), source_label(song), "等待", ""]
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))

    def move_selected(self, direction):
        if self.active:
            return
        row = self.table.currentRow()
        target = row + direction
        if row < 0 or target < 0 or target >= len(self.songs):
            return
        self.songs[row], self.songs[target] = self.songs[target], self.songs[row]
        self.rebuild_table()
        self.table.selectRow(target)

    def start_download(self):
        if self.active or not self.songs:
            return
        if not DOWNLOAD_SESSION_LOCK.acquire(blocking=False):
            QMessageBox.information(self, "已有下载队列", "另一个下载队列正在运行。请先等待它完成或将其取消。")
            return
        self.owns_session_lock = True
        self.active = True
        self.paused = False
        self.progress.setRange(0, len(self.songs))
        self.progress.setValue(0)
        self.start_button.setEnabled(False)
        self.up_button.setEnabled(False)
        self.down_button.setEnabled(False)
        self.pause_button.setEnabled(True)
        self.cancel_button.setEnabled(True)
        self.close_button.setEnabled(False)
        self.summary.setText(f"正在处理 {len(self.songs)} 首歌曲；保存到：{self.target_dir}")
        self.coordinator = DownloadCoordinator(
            self.songs,
            self.target_dir,
            self.naming_template,
            self.duplicate_policy,
            self.timeout_seconds,
            self.history_store,
            self,
        )
        self.coordinator.item_update.connect(self.on_item_update)
        self.coordinator.duplicate_request.connect(self.on_duplicate_request)
        self.coordinator.completed.connect(self.on_completed)
        try:
            self.coordinator.start()
        except Exception as exc:
            self.active = False
            if self.owns_session_lock:
                DOWNLOAD_SESSION_LOCK.release()
                self.owns_session_lock = False
            self.start_button.setEnabled(True)
            self.up_button.setEnabled(True)
            self.down_button.setEnabled(True)
            self.pause_button.setEnabled(False)
            self.cancel_button.setEnabled(False)
            self.close_button.setEnabled(True)
            QMessageBox.critical(self, "无法启动下载", str(exc))

    def toggle_pause(self):
        if not self.coordinator:
            return
        self.paused = not self.paused
        self.coordinator.set_paused(self.paused)
        self.pause_button.setText("继续" if self.paused else "暂停")
        self.summary.setText("将在当前歌曲处理完成后暂停。" if self.paused else "下载队列继续运行。")

    def cancel_download(self):
        if self.coordinator and self.active:
            self.summary.setText("正在安全取消当前工作进程和剩余队列……")
            self.coordinator.cancel()
            self.cancel_button.setEnabled(False)

    def on_item_update(self, row, status, detail):
        if 0 <= row < self.table.rowCount():
            self.table.item(row, 4).setText(status)
            self.table.item(row, 5).setText(detail)
            completed_states = {"成功", "文件异常", "失败", "超时", "已存在", "已跳过", "已取消"}
            completed = sum(1 for index in range(self.table.rowCount()) if self.table.item(index, 4).text() in completed_states)
            self.progress.setValue(completed)

    def on_duplicate_request(self, token, song, existing):
        box = QMessageBox(self)
        box.setWindowTitle("发现重复歌曲")
        box.setText(
            f"这首歌曲已经下载过：\n\n{song_title(song)} - {song_artist(song)}\n{existing.get('path', '')}\n\n请选择处理方式。"
        )
        open_button = box.addButton("打开原文件", QMessageBox.ButtonRole.ActionRole)
        replace_button = box.addButton("重新下载", QMessageBox.ButtonRole.DestructiveRole)
        keep_button = box.addButton("保留两个版本", QMessageBox.ButtonRole.AcceptRole)
        skip_button = box.addButton("跳过", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is open_button:
            reveal_in_file_manager(existing.get("path", ""))
            choice = "open"
        elif clicked is replace_button:
            choice = "replace"
        elif clicked is keep_button:
            choice = "keep_both"
        else:
            choice = "skip"
        if self.coordinator:
            self.coordinator.resolve_duplicate(token, choice)

    def on_completed(self, reports, canceled):
        self.reports = list(reports)
        self.active = False
        if self.owns_session_lock:
            DOWNLOAD_SESSION_LOCK.release()
            self.owns_session_lock = False
        self.pause_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.close_button.setEnabled(True)
        self.summary.setText("队列已取消。" if canceled else "队列处理完成。")
        if self.close_when_finished:
            self.accept()
            return
        report_dialog = DownloadReportDialog(self.reports, self)
        report_dialog.retry_requested.connect(self.retry_items)
        report_dialog.exec()

    def retry_items(self, songs):
        if not songs:
            return
        self.songs = list(songs)
        self.reports = []
        self.rebuild_table()
        self.start_button.setEnabled(True)
        self.up_button.setEnabled(True)
        self.down_button.setEnabled(True)
        self.summary.setText(f"已载入 {len(self.songs)} 个失败/异常项目，点击“开始下载”重试。")

    def closeEvent(self, event):
        if self.active:
            reply = QMessageBox.question(self, "任务进行中", "关闭队列窗口将取消当前下载，是否继续？")
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.cancel_download()
            self.close_when_finished = True
            event.ignore()
            return
        super().closeEvent(event)


class HistoryDialog(QDialog):
    def __init__(self, history_store, parent=None):
        super().__init__(parent)
        self.history_store = history_store
        self.records = []
        self.setWindowTitle("下载历史与音频参数")
        self.resize(1180, 650)
        layout = QVBoxLayout(self)
        search_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("筛选歌曲、歌手、专辑、来源或路径")
        self.search_edit.returnPressed.connect(self.reload)
        search_button = QPushButton("筛选")
        search_button.clicked.connect(self.reload)
        clean_button = QPushButton("清理失效记录")
        clean_button.clicked.connect(self.clean_missing)
        search_row.addWidget(self.search_edit)
        search_row.addWidget(search_button)
        search_row.addWidget(clean_button)
        layout.addLayout(search_row)
        self.table = QTableWidget()
        self.table.setColumnCount(14)
        self.table.setHorizontalHeaderLabels([
            "日期", "状态", "歌曲", "歌手", "来源", "格式", "编码", "采样率", "位深", "比特率", "声道", "时长", "解码", "路径/错误"
        ])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(13, QHeaderView.ResizeMode.Stretch)
        self.table.doubleClicked.connect(self.reveal_selected)
        layout.addWidget(self.table)
        bottom = QHBoxLayout()
        reveal_button = QPushButton("在访达中显示")
        reveal_button.clicked.connect(self.reveal_selected)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.accept)
        bottom.addWidget(reveal_button)
        bottom.addStretch()
        bottom.addWidget(close_button)
        layout.addLayout(bottom)
        self.reload()

    def reload(self):
        self.records = self.history_store.list_records(self.search_edit.text())
        self.table.setRowCount(len(self.records))
        for row, item in enumerate(self.records):
            values = [
                item.get("downloaded_at", "").replace("T", " "), item.get("status", ""), item.get("title", ""),
                item.get("artist", ""), source_label(item.get("source", "")), item.get("format") or "—",
                item.get("codec") or "—", format_hz(item.get("sample_rate_hz")), f"{item['bit_depth']}bit" if item.get("bit_depth") else "—",
                format_bitrate(item.get("bitrate_bps")), str(item.get("channels") or "—"),
                format_duration(item.get("duration_seconds")), "正常" if item.get("decodable") else "异常/未检查",
                item.get("path") or item.get("error") or "—",
            ]
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))

    def clean_missing(self):
        count = self.history_store.remove_missing_records()
        QMessageBox.information(self, "清理完成", f"已移除 {count} 条文件已不存在的历史记录。")
        self.reload()

    def reveal_selected(self):
        row = self.table.currentRow()
        if 0 <= row < len(self.records) and self.records[row].get("path"):
            reveal_in_file_manager(self.records[row]["path"])


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("专业设置")
        self.resize(560, 340)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.naming_combo = QComboBox()
        self.naming_combo.addItems(list(NAMING_TEMPLATES.keys()) + ["自定义"])
        current_label = settings.value("naming_label", "歌手 - 歌名")
        self.naming_combo.setCurrentText(current_label if current_label in [self.naming_combo.itemText(i) for i in range(self.naming_combo.count())] else "自定义")
        self.custom_naming = QLineEdit(settings.value("custom_naming", "{artist} - {title}"))
        self.custom_naming.setPlaceholderText("可用：{artist} {title} {album} {source} {format} {sample_rate} {bit_depth} {bitrate}")
        self.duplicate_combo = QComboBox()
        self.duplicate_combo.addItem("每次询问", "ask")
        self.duplicate_combo.addItem("自动跳过", "skip")
        self.duplicate_combo.addItem("自动保留两个版本", "keep_both")
        self.duplicate_combo.addItem("重新下载，旧文件移到废纸篓", "replace")
        current_policy = settings.value("duplicate_policy", "ask")
        self.duplicate_combo.setCurrentIndex(max(0, self.duplicate_combo.findData(current_policy)))
        self.search_timeout = QSpinBox()
        self.search_timeout.setRange(10, 120)
        self.search_timeout.setValue(int(settings.value("search_timeout", 35)))
        self.search_timeout.setSuffix(" 秒/来源")
        self.download_timeout = QSpinBox()
        self.download_timeout.setRange(60, 1800)
        self.download_timeout.setValue(int(settings.value("download_timeout", 360)))
        self.download_timeout.setSuffix(" 秒/歌曲")
        self.parallel_sources = QSpinBox()
        self.parallel_sources.setRange(1, 6)
        self.parallel_sources.setValue(int(settings.value("parallel_sources", 4)))
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("浅色", "light")
        self.theme_combo.addItem("深色", "dark")
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(settings.value("theme", "light"))))
        self.remember_sources = QCheckBox("下次启动恢复我手动选择的音源")
        self.remember_sources.setChecked(settings.value("remember_sources", False, type=bool))
        form.addRow("命名规则：", self.naming_combo)
        form.addRow("自定义模板：", self.custom_naming)
        form.addRow("重复歌曲：", self.duplicate_combo)
        form.addRow("搜索超时：", self.search_timeout)
        form.addRow("下载超时：", self.download_timeout)
        form.addRow("并行来源数：", self.parallel_sources)
        form.addRow("界面主题：", self.theme_combo)
        form.addRow("音源记忆：", self.remember_sources)
        layout.addLayout(form)
        note = QLabel("位深仅对 FLAC/WAV/ALAC 等无损/PCM 文件显示；不会把 MP3/AAC 的解码输出格式误写成文件位深。")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("取消")
        save = QPushButton("保存")
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self.save_and_accept)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def save_and_accept(self):
        template = self.custom_naming.text().strip()
        if self.naming_combo.currentText() == "自定义" and not template:
            QMessageBox.warning(self, "模板为空", "请输入自定义命名模板。")
            return
        self.settings.setValue("naming_label", self.naming_combo.currentText())
        self.settings.setValue("custom_naming", template)
        self.settings.setValue("duplicate_policy", self.duplicate_combo.currentData())
        self.settings.setValue("search_timeout", self.search_timeout.value())
        self.settings.setValue("download_timeout", self.download_timeout.value())
        self.settings.setValue("parallel_sources", self.parallel_sources.value())
        self.settings.setValue("theme", self.theme_combo.currentData())
        self.settings.setValue("remember_sources", self.remember_sources.isChecked())
        self.settings.sync()
        self.accept()


class MusicDownloader(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings(APP_ORGANIZATION, APP_NAME)
        self.history_store = HistoryStore()
        self.search_thread = None
        self.search_dialog = None
        self.health_thread = None
        self.health_dialog = None
        self.queue_dialogs = []
        self.all_songs = []
        self.search_statuses = {}
        self.checked_track_keys = set()
        self.song_id_map = {}
        self.current_right_click_row = -1
        self.thread_pool = QThreadPool.globalInstance()
        self.thread_pool.setMaxThreadCount(8)
        self.media_player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.7)
        self.media_player.setAudioOutput(self.audio_output)
        self.media_player.errorOccurred.connect(self.preview_error)

        self.setWindowTitle(APP_DISPLAY_NAME)
        self.setMinimumSize(1100, 760)
        self.resize(1320, 880)
        icon_path = resource_path("assets", "MusicDownload.png")
        if Path(icon_path).exists():
            icon = QIcon(icon_path)
            self.setWindowIcon(icon)
            QApplication.instance().setWindowIcon(icon)
        QApplication.setFont(QFont("PingFang SC" if sys.platform == "darwin" else "Microsoft YaHei", 10))

        downloads_dir = Path.home() / "Downloads"
        default_root = downloads_dir if downloads_dir.is_dir() else Path.home()
        self.default_save_dir = str(default_root / "已下载音乐")
        self.save_dir = str(self.settings.value("save_dir", self.default_save_dir))
        try:
            Path(self.save_dir).mkdir(parents=True, exist_ok=True)
        except Exception:
            self.save_dir = self.default_save_dir
            Path(self.save_dir).mkdir(parents=True, exist_ok=True)

        central = QWidget()
        central.setObjectName("CentralWidget")
        self.setCentralWidget(central)
        self.main_layout = QVBoxLayout(central)
        self.main_layout.setContentsMargins(14, 14, 14, 14)
        self.main_layout.setSpacing(10)
        self.setup_sources()
        self.setup_search_controls()
        self.setup_filters()
        self.setup_results_table()
        self.setup_menu()
        self.apply_theme(self.settings.value("theme", "light"))
        self.refresh_network_notice()
        self.statusBar().showMessage(f"就绪 · {APP_DISPLAY_NAME} {APP_VERSION}")
        geometry = self.settings.value("window_geometry")
        if geometry:
            self.restoreGeometry(geometry)

    def setup_sources(self):
        group = QGroupBox("音乐来源（启动时不做任何默认勾选）")
        group_layout = QVBoxLayout(group)
        controls = QHBoxLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.addItem("选择预设……")
        self.preset_combo.addItems(SOURCE_PRESETS.keys())
        apply_preset = QPushButton("应用预设")
        apply_preset.clicked.connect(self.apply_source_preset)
        select_all = QPushButton("全选")
        select_all.clicked.connect(lambda: self.set_all_sources(True))
        clear_all = QPushButton("清空")
        clear_all.clicked.connect(lambda: self.set_all_sources(False))
        self.health_button = QPushButton("检测音源")
        self.health_button.clicked.connect(self.check_sources)
        controls.addWidget(self.preset_combo)
        controls.addWidget(apply_preset)
        controls.addWidget(select_all)
        controls.addWidget(clear_all)
        controls.addStretch()
        controls.addWidget(self.health_button)
        group_layout.addLayout(controls)

        grid = QGridLayout()
        self.source_checkboxes = {}
        remember_sources = self.settings.value("remember_sources", False, type=bool)
        saved_sources = set(ensure_list(self.settings.value("sources_v12", []))) if remember_sources else set()
        for index, (label, source) in enumerate(SOURCE_DEFINITIONS):
            checkbox = QCheckBox(label)
            checkbox.setProperty("source_id", source)
            checkbox.setChecked(source in saved_sources)
            checkbox.setToolTip("尚未检测")
            self.source_checkboxes[source] = checkbox
            grid.addWidget(checkbox, index // 4, index % 4)
        group_layout.addLayout(grid)
        self.network_notice = QLabel()
        self.network_notice.setWordWrap(True)
        group_layout.addWidget(self.network_notice)
        self.main_layout.addWidget(group)

    def setup_search_controls(self):
        group = QGroupBox("搜索与保存")
        layout = QVBoxLayout(group)
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("单源数量："))
        self.spin_limit = QSpinBox()
        self.spin_limit.setRange(1, 100)
        self.spin_limit.setValue(int(self.settings.value("search_limit", 10)))
        self.spin_limit.setSuffix(" 条")
        row1.addWidget(self.spin_limit)
        row1.addWidget(QLabel("保存目录："))
        self.save_edit = QLineEdit(self.save_dir)
        self.save_edit.setReadOnly(True)
        row1.addWidget(self.save_edit, 1)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self.browse_save_dir)
        row1.addWidget(browse)
        row1.addWidget(QLabel("命名规则："))
        self.naming_combo = QComboBox()
        self.naming_combo.addItems(list(NAMING_TEMPLATES.keys()) + ["自定义"])
        naming_label = self.settings.value("naming_label", "歌手 - 歌名")
        self.naming_combo.setCurrentText(naming_label if self.naming_combo.findText(naming_label) >= 0 else "自定义")
        row1.addWidget(self.naming_combo)
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        self.search_mode = QComboBox()
        self.search_mode.addItems(["搜索歌曲", "解析歌单链接"])
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("输入歌曲/歌手关键词，或切换到歌单链接模式")
        self.search_edit.returnPressed.connect(self.start_search)
        self.search_button = QPushButton("立即搜索")
        self.search_button.setObjectName("SearchButton")
        self.search_button.clicked.connect(self.start_search)
        self.cancel_search_button = QPushButton("取消搜索")
        self.cancel_search_button.setEnabled(False)
        self.cancel_search_button.clicked.connect(self.cancel_search)
        self.auto_download = QCheckBox("搜索后把全部结果加入下载队列")
        self.auto_download.setChecked(self.settings.value("auto_download", False, type=bool))
        row2.addWidget(self.search_mode)
        row2.addWidget(self.search_edit, 1)
        row2.addWidget(self.search_button)
        row2.addWidget(self.cancel_search_button)
        row2.addWidget(self.auto_download)
        layout.addLayout(row2)
        self.main_layout.addWidget(group)

    def setup_filters(self):
        group = QGroupBox("结果过滤与排序")
        row = QHBoxLayout(group)
        self.singer_filter = QLineEdit()
        self.singer_filter.setPlaceholderText("过滤歌手")
        self.album_filter = QLineEdit()
        self.album_filter.setPlaceholderText("过滤专辑")
        self.quality_filter = QComboBox()
        self.quality_filter.addItems(["全部", "无损优先", "仅 FLAC", "MP3 320k优先"])
        self.source_filter = QComboBox()
        self.source_filter.addItem("全部来源")
        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["默认顺序", "格式", "采样率", "比特率", "文件大小", "来源"])
        clear = QPushButton("清除过滤")
        clear.clicked.connect(self.clear_filters)
        for widget in (self.singer_filter, self.album_filter):
            widget.textChanged.connect(self.apply_filters)
        for widget in (self.quality_filter, self.source_filter, self.sort_combo):
            widget.currentTextChanged.connect(self.apply_filters)
        row.addWidget(self.singer_filter)
        row.addWidget(self.album_filter)
        row.addWidget(QLabel("音质："))
        row.addWidget(self.quality_filter)
        row.addWidget(QLabel("来源："))
        row.addWidget(self.source_filter)
        row.addWidget(QLabel("排序："))
        row.addWidget(self.sort_combo)
        row.addWidget(clear)
        self.main_layout.addWidget(group)

    def setup_results_table(self):
        controls = QHBoxLayout()
        self.download_scope = QComboBox()
        self.download_scope.addItems(["勾选", "全部可见", "未勾选"])
        select_all = QPushButton("勾选全部可见")
        select_all.clicked.connect(lambda: self.set_visible_checks(True))
        deselect_all = QPushButton("取消全部可见")
        deselect_all.clicked.connect(lambda: self.set_visible_checks(False))
        preview_stop = QPushButton("停止试听")
        preview_stop.clicked.connect(self.stop_preview)
        preview_selected = QPushButton("试听选中行")
        preview_selected.clicked.connect(self.preview_selected_row)
        open_folder = QPushButton("打开下载文件夹")
        open_folder.clicked.connect(lambda: reveal_in_file_manager(self.save_dir))
        self.download_button = QPushButton("加入下载队列")
        self.download_button.setEnabled(False)
        self.download_button.clicked.connect(self.add_selected_to_queue)
        controls.addWidget(QLabel("下载范围："))
        controls.addWidget(self.download_scope)
        controls.addWidget(select_all)
        controls.addWidget(deselect_all)
        controls.addStretch()
        controls.addWidget(preview_selected)
        controls.addWidget(preview_stop)
        controls.addWidget(open_folder)
        controls.addWidget(self.download_button)
        self.main_layout.addLayout(controls)

        self.results_table = QTableWidget()
        self.results_table.setColumnCount(11)
        self.results_table.setHorizontalHeaderLabels([
            "选择", "封面", "歌曲名", "歌手", "专辑", "格式", "采样率", "比特率", "大小", "时长", "来源"
        ])
        self.results_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.results_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.results_table.setAlternatingRowColors(True)
        self.results_table.verticalHeader().setVisible(False)
        self.results_table.verticalHeader().setDefaultSectionSize(54)
        self.results_table.setSortingEnabled(True)
        self.results_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.results_table.customContextMenuRequested.connect(self.show_result_menu)
        header = self.results_table.horizontalHeader()
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.results_table.setColumnWidth(0, 46)
        self.results_table.setColumnWidth(1, 58)
        for column, width in {3: 150, 5: 70, 6: 80, 7: 80, 8: 80, 9: 72, 10: 120}.items():
            self.results_table.setColumnWidth(column, width)
        self.main_layout.addWidget(self.results_table, 1)

    def setup_menu(self):
        file_menu = self.menuBar().addMenu("文件")
        open_downloads = QAction("打开下载文件夹", self)
        open_downloads.triggered.connect(lambda: reveal_in_file_manager(self.save_dir))
        history = QAction("下载历史与音频参数", self)
        history.triggered.connect(self.show_history)
        logs = QAction("打开日志文件夹", self)
        logs.triggered.connect(lambda: reveal_in_file_manager(LOG_DIR))
        settings_action = QAction("专业设置", self)
        settings_action.triggered.connect(self.show_settings)
        reset_action = QAction("恢复默认设置", self)
        reset_action.triggered.connect(self.reset_settings)
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.close)
        for action in (open_downloads, history, logs, settings_action, reset_action):
            file_menu.addAction(action)
        file_menu.addSeparator()
        file_menu.addAction(quit_action)

        appearance = self.menuBar().addMenu("外观")
        theme_group = QActionGroup(self)
        theme_group.setExclusive(True)
        self.theme_actions = {}
        for label, theme in (("浅色", "light"), ("深色", "dark")):
            action = QAction(label, self, checkable=True)
            action.setData(theme)
            action.setChecked(self.settings.value("theme", "light") == theme)
            action.triggered.connect(lambda checked=False, value=theme: self.change_theme(value))
            theme_group.addAction(action)
            appearance.addAction(action)
            self.theme_actions[theme] = action

        help_menu = self.menuBar().addMenu("帮助")
        about = QAction("关于", self)
        about.triggered.connect(self.show_about)
        help_menu.addAction(about)

    def apply_source_preset(self):
        name = self.preset_combo.currentText()
        if name not in SOURCE_PRESETS:
            return
        selected = set(SOURCE_PRESETS[name])
        for source, checkbox in self.source_checkboxes.items():
            checkbox.setChecked(source in selected)

    def set_all_sources(self, checked):
        for checkbox in self.source_checkboxes.values():
            checkbox.setChecked(checked)

    def selected_sources(self):
        return [source for source, checkbox in self.source_checkboxes.items() if checkbox.isChecked()]

    def check_sources(self):
        if self.health_thread and self.health_thread.isRunning():
            return
        if self.search_thread and self.search_thread.isRunning():
            QMessageBox.information(self, "搜索进行中", "请先等待当前搜索完成或取消搜索，再检测音源。")
            return
        sources = self.selected_sources()
        if not sources:
            reply = QMessageBox.question(self, "未选择来源", "当前没有勾选来源。是否检测全部17个来源？")
            if reply != QMessageBox.StandardButton.Yes:
                return
            sources = list(self.source_checkboxes.keys())
        self.health_button.setEnabled(False)
        self.health_dialog = SourceProgressDialog("音源检测", sources, keep_open=True, parent=self)
        self.health_thread = SearchCoordinator(
            sources,
            "晴天",
            1,
            timeout_seconds=int(self.settings.value("search_timeout", 35)),
            max_parallel=int(self.settings.value("parallel_sources", 4)),
            keyword_map={source: HEALTH_TEST_KEYWORDS.get(source, "周杰伦 晴天") for source in sources},
            parent=self,
        )
        self.health_thread.source_update.connect(self.health_dialog.update_source)
        self.health_dialog.cancel_requested.connect(self.health_thread.cancel)

        def completed(results, statuses, canceled):
            for source, item in statuses.items():
                checkbox = self.source_checkboxes.get(source)
                if checkbox:
                    checkbox.setProperty("health_status", item["status"])
                    checkbox.setToolTip(f"最近检测：{item['status']} · {item['detail']}")
                    self.refresh_source_style(source)
            self.health_dialog.finish(statuses, canceled)
            self.health_button.setEnabled(True)

        self.health_thread.completed.connect(completed)
        self.health_dialog.show()
        self.health_thread.start()

    def browse_save_dir(self):
        selected = QFileDialog.getExistingDirectory(self, "选择保存目录", self.save_dir)
        if selected:
            self.save_dir = selected
            self.save_edit.setText(selected)

    def refresh_network_notice(self):
        state = detect_network_proxy()
        self.network_proxy_active = bool(state["active"])
        if state["active"]:
            self.network_notice.setText("网络提示：检测到 " + "、".join(state["details"]) + "。国内来源异常时可自行切换线路；程序不会关闭或修改你的 VPN/代理。")
            self.network_notice.setStyleSheet("color: #b45309; font-weight: bold;")
        else:
            self.network_notice.setText("网络提示：未检测到系统级代理/VPN。此提示只作诊断，不会修改网络。")
            self.network_notice.setStyleSheet("color: #64748b;")
        for source in self.source_checkboxes:
            self.refresh_source_style(source)

    def refresh_source_style(self, source):
        checkbox = self.source_checkboxes.get(source)
        if not checkbox:
            return
        health_status = checkbox.property("health_status")
        if health_status:
            color = "#15803d" if health_status == "正常" else "#b45309" if health_status == "无结果" else "#dc2626"
            checkbox.setStyleSheet(f"color: {color}; font-weight: bold;")
        elif getattr(self, "network_proxy_active", False) and source in DOMESTIC_SOURCE_IDS:
            checkbox.setStyleSheet("color: #b45309; font-weight: bold;")
            checkbox.setToolTip("检测到代理/VPN：仅作橙色提示；程序不会修改网络。可点击“检测音源”实际测试。")
        else:
            checkbox.setStyleSheet("")
            checkbox.setToolTip("尚未检测")

    def start_search(self):
        keyword = self.search_edit.text().strip()
        if not keyword:
            QMessageBox.warning(self, "缺少内容", "请输入歌曲关键词或歌单链接。")
            return
        sources = self.selected_sources()
        if not sources:
            QMessageBox.warning(self, "未选择来源", "本版本不做默认勾选，请先选择至少一个音乐来源。")
            return
        if self.search_thread and self.search_thread.isRunning():
            return
        if self.health_thread and self.health_thread.isRunning():
            QMessageBox.information(self, "音源检测进行中", "请先等待音源检测完成或取消检测，再开始搜索。")
            return
        self.save_preferences()
        self.search_button.setEnabled(False)
        self.cancel_search_button.setEnabled(True)
        self.search_dialog = SourceProgressDialog("搜索进度", sources, keep_open=False, parent=self)
        mode = "playlist" if self.search_mode.currentText() == "解析歌单链接" else "search"
        self.search_thread = SearchCoordinator(
            sources,
            keyword,
            self.spin_limit.value(),
            mode=mode,
            timeout_seconds=int(self.settings.value("search_timeout", 35)),
            max_parallel=int(self.settings.value("parallel_sources", 4)),
            parent=self,
        )
        self.search_thread.source_update.connect(self.search_dialog.update_source)
        self.search_dialog.cancel_requested.connect(self.cancel_search)
        self.search_thread.completed.connect(self.search_completed)
        self.search_dialog.show()
        self.statusBar().showMessage(f"正在搜索：{keyword}")
        self.search_thread.start()

    def cancel_search(self):
        if self.search_thread and self.search_thread.isRunning():
            self.statusBar().showMessage("正在安全取消各来源工作进程……")
            self.search_thread.cancel()
            self.cancel_search_button.setEnabled(False)

    def search_completed(self, results, statuses, canceled):
        self.search_button.setEnabled(True)
        self.cancel_search_button.setEnabled(False)
        self.search_statuses = statuses
        if self.search_dialog:
            self.search_dialog.finish(statuses, canceled)
        self.all_songs = []
        # 使用发起搜索时的结果字典顺序；即使用户在搜索期间改变勾选，
        # 已经返回的来源也不会因此被静默丢弃。
        for source_songs in results.values():
            self.all_songs.extend(source_songs or [])
        self.checked_track_keys.clear()
        self.refresh_source_filter()
        self.apply_filters()
        failures = [source_label(source) for source, item in statuses.items() if item["status"] in {"失败", "超时"}]
        message = f"搜索完成，共 {len(self.all_songs)} 首"
        if failures:
            message += "；异常来源：" + "、".join(failures)
        if canceled:
            message += "；用户已取消，保留已返回结果"
        self.statusBar().showMessage(message)
        if not self.all_songs:
            QMessageBox.information(self, "没有可用结果", message + "。可点击“检测音源”查看各来源状态。")
        elif self.auto_download.isChecked() and not canceled:
            self.open_download_queue(self.all_songs, auto_start=True)

    def refresh_source_filter(self):
        current = self.source_filter.currentText()
        labels = []
        for song in self.all_songs:
            label = source_label(song)
            if label not in labels:
                labels.append(label)
        self.source_filter.blockSignals(True)
        self.source_filter.clear()
        self.source_filter.addItem("全部来源")
        self.source_filter.addItems(labels)
        self.source_filter.setCurrentText(current if current in labels else "全部来源")
        self.source_filter.blockSignals(False)

    def clear_filters(self):
        self.singer_filter.clear()
        self.album_filter.clear()
        self.quality_filter.setCurrentText("全部")
        self.source_filter.setCurrentText("全部来源")
        self.sort_combo.setCurrentText("默认顺序")
        self.apply_filters()

    def get_cover_url(self, song):
        for field in ("cover_url", "cover", "album_cover", "pic", "picture", "img", "image", "album_pic"):
            value = clean_text(get_value(song, field, ""))
            if value.startswith("http"):
                return value
        return ""

    def apply_filters(self):
        if not hasattr(self, "results_table"):
            return
        songs = filter_sort_songs(
            self.all_songs,
            self.singer_filter.text(),
            self.album_filter.text(),
            self.quality_filter.currentText(),
            self.source_filter.currentText(),
            self.sort_combo.currentText(),
        )
        self.populate_results(songs)

    def populate_results(self, songs):
        self.thread_pool.clear()
        table = self.results_table
        table.setSortingEnabled(False)
        table.setRowCount(len(songs))
        self.song_id_map = {}
        for row, song in enumerate(songs):
            key = build_track_key(song)
            self.song_id_map[key] = song
            cell = QWidget()
            cell_layout = QHBoxLayout(cell)
            checkbox = QCheckBox()
            checkbox.song_info = song
            checkbox.track_key = key
            checkbox.setChecked(key in self.checked_track_keys)
            checkbox.stateChanged.connect(lambda state, item_key=key: self.update_checked_key(item_key, state))
            cell_layout.addWidget(checkbox)
            cell_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            table.setCellWidget(row, 0, cell)
            placeholder = QLabel("♪")
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            table.setCellWidget(row, 1, placeholder)
            samplerate = get_value(song, "samplerate", 0)
            bitrate = get_value(song, "bitrate", 0)
            duration_seconds = get_value(song, "duration_s", 0)
            values = [
                (2, song_title(song), None),
                (3, song_artist(song), None),
                (4, clean_text(get_value(song, "album", "")), None),
                (5, search_format(song), None),
                (6, format_hz(samplerate), safe_float(samplerate)),
                (7, format_bitrate(bitrate), safe_float(bitrate)),
                (8, clean_text(get_value(song, "file_size", ""), "未知"), parse_file_size_bytes(song)),
                (9, clean_text(get_value(song, "duration", ""), format_duration(duration_seconds)), safe_float(duration_seconds)),
                (10, source_label(song), None),
            ]
            for column, text, number in values:
                item = NumericItem(str(text)) if number is not None else QTableWidgetItem(str(text))
                if number is not None:
                    item.setData(Qt.ItemDataRole.UserRole, number)
                table.setItem(row, column, item)
            cover_url = self.get_cover_url(song)
            if cover_url:
                task = ImageDownloadTask(key, cover_url)
                task.signals.finished.connect(self.cover_loaded)
                self.thread_pool.start(task)
        table.setSortingEnabled(True)
        self.download_button.setEnabled(bool(songs))
        self.statusBar().showMessage(f"显示 {len(songs)} / {len(self.all_songs)} 首结果")

    def update_checked_key(self, key, state):
        if state == Qt.CheckState.Checked.value:
            self.checked_track_keys.add(key)
        else:
            self.checked_track_keys.discard(key)

    def cover_loaded(self, key, pixmap):
        for row in range(self.results_table.rowCount()):
            checkbox = self.row_checkbox(row)
            if checkbox and getattr(checkbox, "track_key", None) == key:
                label = QLabel()
                label.setPixmap(pixmap)
                label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                self.results_table.setCellWidget(row, 1, label)

    def row_checkbox(self, row):
        widget = self.results_table.cellWidget(row, 0)
        return widget.findChild(QCheckBox) if widget else None

    def set_visible_checks(self, checked):
        for row in range(self.results_table.rowCount()):
            checkbox = self.row_checkbox(row)
            if checkbox:
                checkbox.setChecked(checked)

    def songs_for_scope(self):
        scope = self.download_scope.currentText()
        songs = []
        for row in range(self.results_table.rowCount()):
            checkbox = self.row_checkbox(row)
            if not checkbox:
                continue
            checked = checkbox.isChecked()
            if scope == "全部可见" or (scope == "勾选" and checked) or (scope == "未勾选" and not checked):
                songs.append(checkbox.song_info)
        return songs

    def show_result_menu(self, pos):
        item = self.results_table.itemAt(pos)
        if not item:
            return
        row = item.row()
        checkbox = self.row_checkbox(row)
        if not checkbox:
            return
        song = checkbox.song_info
        menu = QMenu(self)
        download_action = QAction(f"加入队列：{song_title(song)}", self)
        download_action.triggered.connect(lambda: self.open_download_queue([song]))
        preview_action = QAction("试听此结果", self)
        preview_action.triggered.connect(lambda: self.preview_song(song))
        stop_action = QAction("停止试听", self)
        stop_action.triggered.connect(self.stop_preview)
        reveal_existing = QAction("查找已下载文件", self)
        reveal_existing.triggered.connect(lambda: self.reveal_existing(song))
        for action in (download_action, preview_action, stop_action, reveal_existing):
            menu.addAction(action)
        menu.exec(self.results_table.mapToGlobal(pos))

    def preview_song(self, song):
        url = get_value(song, "download_url", "")
        if not isinstance(url, str) or not url.startswith("http"):
            existing = self.history_store.find_existing(song)
            if existing:
                url = existing["path"]
            else:
                QMessageBox.information(self, "无法试听", "该结果没有可直接播放的地址，也没有已下载的本地文件。")
                return
        self.media_player.stop()
        self.media_player.setSource(QUrl(url) if str(url).startswith("http") else QUrl.fromLocalFile(str(url)))
        self.media_player.play()
        self.statusBar().showMessage(f"正在试听：{song_title(song)}（不会自动播放下一首）")

    def preview_selected_row(self):
        row = self.results_table.currentRow()
        checkbox = self.row_checkbox(row) if row >= 0 else None
        if not checkbox:
            QMessageBox.information(self, "尚未选择", "请先在结果表中点选一行，再点击试听。")
            return
        self.preview_song(checkbox.song_info)

    def preview_error(self, error, error_string):
        if error != QMediaPlayer.Error.NoError:
            detail = clean_text(error_string, "该来源的试听地址可能需要专用请求头或已失效")
            self.statusBar().showMessage(f"试听失败：{detail}")

    def stop_preview(self):
        self.media_player.stop()
        self.statusBar().showMessage("试听已停止")

    def reveal_existing(self, song):
        existing = self.history_store.find_existing(song)
        if existing:
            reveal_in_file_manager(existing["path"])
        else:
            QMessageBox.information(self, "没有历史文件", "下载历史中没有找到仍然存在的同一首歌曲。")

    def add_selected_to_queue(self):
        songs = self.songs_for_scope()
        if not songs:
            QMessageBox.warning(self, "没有项目", "没有符合当前下载范围的歌曲。")
            return
        self.open_download_queue(songs)

    def current_naming_template(self):
        label = self.naming_combo.currentText()
        if label == "自定义":
            return clean_text(self.settings.value("custom_naming", "{artist} - {title}"), "{artist} - {title}")
        return NAMING_TEMPLATES.get(label, "{artist} - {title}")

    def open_download_queue(self, songs, auto_start=False):
        dialog = DownloadQueueDialog(
            songs,
            self.save_dir,
            self.current_naming_template(),
            self.settings.value("duplicate_policy", "ask"),
            int(self.settings.value("download_timeout", 360)),
            self.history_store,
            auto_start=auto_start,
            parent=self,
        )
        self.queue_dialogs.append(dialog)
        dialog.finished.connect(lambda _: self.queue_dialogs.remove(dialog) if dialog in self.queue_dialogs else None)
        dialog.show()

    def show_history(self):
        HistoryDialog(self.history_store, self).exec()

    def show_settings(self):
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.naming_combo.setCurrentText(self.settings.value("naming_label", "歌手 - 歌名"))
            self.apply_theme(self.settings.value("theme", "light"))

    def reset_settings(self):
        reply = QMessageBox.question(self, "恢复默认设置", "将重置来源选择、保存目录、超时、命名和界面设置。下载历史及已下载文件不会删除。是否继续？")
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.settings.clear()
        self.set_all_sources(False)
        self.preset_combo.setCurrentIndex(0)
        for checkbox in self.source_checkboxes.values():
            checkbox.setProperty("health_status", None)
        self.spin_limit.setValue(10)
        self.auto_download.setChecked(False)
        self.save_dir = self.default_save_dir
        Path(self.save_dir).mkdir(parents=True, exist_ok=True)
        self.save_edit.setText(self.save_dir)
        self.naming_combo.setCurrentText("歌手 - 歌名")
        self.clear_filters()
        self.apply_theme("light")
        self.refresh_network_notice()
        self.statusBar().showMessage("默认设置已恢复；下载历史和音乐文件未删除")

    def show_about(self):
        QMessageBox.about(
            self,
            "关于 MusicDownload",
            f"<h3>{APP_DISPLAY_NAME}</h3><p>版本 {APP_VERSION}</p>"
            "<p>适配 Intel Mac 与 macOS 12.7.6。</p>"
            "<p>支持来源隔离、硬超时、取消任务、下载队列、真实音频参数检测、下载历史和重复管理。</p>"
            "<p>MP3/AAC 不显示伪造的“位深”；音频状态来自下载后的实际文件检查。</p>",
        )

    def change_theme(self, theme):
        self.settings.setValue("theme", theme)
        self.apply_theme(theme)

    def apply_theme(self, theme):
        dark = theme == "dark"
        background = "#111827" if dark else "#f3f4f6"
        panel = "#1f2937" if dark else "#ffffff"
        text = "#e5e7eb" if dark else "#1f2937"
        muted = "#9ca3af" if dark else "#4b5563"
        border = "#374151" if dark else "#d1d5db"
        alternate = "#182235" if dark else "#f9fafb"
        input_bg = "#111827" if dark else "#ffffff"
        self.setStyleSheet(f"""
            #CentralWidget {{ background: {background}; }}
            QMainWindow, QDialog {{ background: {background}; color: {text}; }}
            QGroupBox {{ color: {text}; background: {panel}; border: 1px solid {border}; border-radius: 8px; margin-top: 12px; padding-top: 13px; font-weight: bold; }}
            QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 5px; color: #3b82f6; }}
            QLabel, QCheckBox {{ color: {text}; }}
            QLineEdit, QComboBox, QSpinBox {{ color: {text}; background: {input_bg}; border: 1px solid {border}; border-radius: 6px; padding: 5px 8px; min-height: 24px; }}
            QPushButton {{ background: #2563eb; color: white; border: none; border-radius: 6px; padding: 7px 13px; font-weight: bold; }}
            QPushButton:hover {{ background: #1d4ed8; }}
            QPushButton:disabled {{ background: #6b7280; color: #d1d5db; }}
            QPushButton#SearchButton {{ background: #059669; }}
            QTableWidget {{ color: {text}; background: {panel}; alternate-background-color: {alternate}; border: 1px solid {border}; selection-background-color: #1d4ed8; selection-color: white; }}
            QHeaderView::section {{ color: {muted}; background: {background}; border: none; border-right: 1px solid {border}; border-bottom: 1px solid {border}; padding: 6px; font-weight: bold; }}
            QMenuBar, QMenu {{ background: {panel}; color: {text}; }}
            QMenu::item:selected {{ background: #2563eb; color: white; }}
            QStatusBar {{ color: {text}; background: {panel}; }}
        """)
        if hasattr(self, "theme_actions"):
            for value, action in self.theme_actions.items():
                action.setChecked(value == theme)

    def save_preferences(self):
        self.settings.setValue("sources_v12", self.selected_sources())
        self.settings.setValue("save_dir", self.save_dir)
        self.settings.setValue("search_limit", self.spin_limit.value())
        self.settings.setValue("auto_download", self.auto_download.isChecked())
        self.settings.setValue("naming_label", self.naming_combo.currentText())
        self.settings.setValue("window_geometry", self.saveGeometry())
        self.settings.sync()

    def closeEvent(self, event):
        active_search = self.search_thread and self.search_thread.isRunning()
        active_health = self.health_thread and self.health_thread.isRunning()
        active_queues = [dialog for dialog in self.queue_dialogs if dialog.active]
        if active_search or active_health or active_queues:
            reply = QMessageBox.question(self, "任务进行中", "仍有搜索、检测或下载任务。是否安全取消所有任务并退出？")
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            if active_search:
                self.search_thread.cancel()
            if active_health:
                self.health_thread.cancel()
            for dialog in active_queues:
                dialog.cancel_download()
            threads = [thread for thread in (self.search_thread, self.health_thread) if thread and thread.isRunning()]
            threads.extend(dialog.coordinator for dialog in active_queues if dialog.coordinator and dialog.coordinator.isRunning())
            if any(not thread.wait(7000) for thread in threads):
                QMessageBox.warning(self, "仍在取消", "部分工作进程仍在退出，请稍候再关闭程序。")
                event.ignore()
                return
        self.stop_preview()
        self.save_preferences()
        LOGGER.info("%s 正常退出", APP_NAME)
        super().closeEvent(event)


def launch_app():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_DISPLAY_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(APP_ORGANIZATION)
    sys.excepthook = handle_uncaught_exception
    LOGGER.info("Python %s | 平台 %s", sys.version.split()[0], sys.platform)
    window = MusicDownloader()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(launch_app())
