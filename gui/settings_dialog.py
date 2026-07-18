"""
gui/settings_dialog.py
======================
설정 다이얼로그. config.json의 주요 항목을 GUI에서 편집합니다.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QScrollArea, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from utils.config_manager import get_config
from utils.logger import get_logger

logger = get_logger(__name__)


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("설정")
        self.setMinimumSize(600, 500)
        self.resize(680, 580)
        self._cfg = get_config()
        self._widgets: dict = {}
        self._channels: list = list(self._cfg.get("youtube.channels", []) or [])
        self._setup_ui()
        self._load_values()
        self._refresh_channel_list()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        tabs = QTabWidget()

        tabs.addTab(self._make_tts_tab(), "TTS")
        tabs.addTab(self._make_image_tab(), "이미지")
        tabs.addTab(self._make_video_tab(), "영상")
        tabs.addTab(self._make_subtitle_tab(), "자막")
        tabs.addTab(self._make_youtube_tab(), "YouTube")

        layout.addWidget(tabs)

        btns = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _scrollable(self, inner: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(inner)
        return area

    # ─────────────────────────────────────────
    # 탭 구성
    # ─────────────────────────────────────────

    def _make_tts_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self._add_line(form, "tts.default_voice", "기본 보이스")
        self._add_line(form, "tts.default_rate", "속도 (+0%)")
        self._add_line(form, "tts.default_pitch", "피치 (+0Hz)")
        self._add_line(form, "tts.default_volume", "볼륨 (+0%)")
        self._add_spin(form, "tts.max_workers", "병렬 워커 수", 1, 8)
        self._add_spin(form, "tts.retry_count", "재시도 횟수", 1, 10)
        return w

    def _make_image_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self._add_line(form, "image.pixabay_api_key", "Pixabay API 키")
        self._add_spin(form, "image.default_width", "너비", 256, 4096)
        self._add_spin(form, "image.default_height", "높이", 256, 4096)
        return w

    def _make_video_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self._add_spin(form, "video.fps", "FPS", 15, 60)
        self._add_spin(form, "video.width", "너비", 640, 3840)
        self._add_spin(form, "video.height", "높이", 360, 2160)
        self._add_line(form, "video.video_bitrate", "영상 비트레이트")
        self._add_line(form, "video.audio_bitrate", "오디오 비트레이트")
        self._add_spin(form, "video.crf", "CRF", 0, 51)
        self._add_check(form, "video.gpu_encoding", "GPU 인코딩 사용")
        self._add_line(form, "video.gpu_codec", "GPU 코덱")
        self._add_dspin(form, "video.bgm_volume", "BGM 볼륨", 0.0, 1.0)
        self._add_check(form, "video.pan_zoom_enabled", "Pan&Zoom 효과")
        self._add_dspin(form, "video.crossfade_duration", "크로스페이드(초)", 0.0, 3.0)
        self._add_dspin(form, "video.min_scene_duration", "최소 씬 길이(초)", 1.0, 30.0)
        self._add_spin(form, "video.threads", "FFmpeg 스레드", 1, 32)
        return w

    def _make_subtitle_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self._add_line(form, "subtitle.font_name", "폰트 이름")
        self._add_spin(form, "subtitle.font_size", "폰트 크기", 10, 200)
        self._add_line(form, "subtitle.font_color", "폰트 색상 (ASS)")
        self._add_line(form, "subtitle.outline_color", "외곽선 색상")
        self._add_spin(form, "subtitle.outline_width", "외곽선 두께", 0, 10)
        self._add_spin(form, "subtitle.shadow_depth", "그림자 깊이", 0, 10)
        self._add_spin(form, "subtitle.margin_v", "하단 여백", 0, 300)
        self._add_spin(form, "subtitle.alignment", "정렬 (ASS)", 1, 9)
        return w

    def _make_youtube_tab(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)

        form_w = QWidget()
        form = QFormLayout(form_w)
        self._add_line(form, "youtube.client_secrets_file", "client_secrets.json 경로")
        self._add_line(form, "youtube.credentials_file", "기본 credentials 캐시 경로")
        self._add_spin(form, "youtube.schedule_hour", "예약 시각(시)", 0, 23)
        self._add_spin(form, "youtube.schedule_minute", "예약 시각(분)", 0, 59)
        self._add_line(form, "youtube.schedule_timezone", "타임존")
        self._add_spin(form, "youtube.retry_count", "재시도 횟수", 1, 10)
        outer.addWidget(form_w)

        # ── 채널 관리 (한 계정의 여러 채널을 개별 credentials로 구분) ──
        chan_group = QGroupBox("채널 관리 (여러 채널 업로드용)")
        chan_layout = QVBoxLayout(chan_group)
        self._channel_list = QListWidget()
        self._channel_list.setToolTip(
            "채널을 추가하면 슬롯에서 선택할 수 있습니다.\n"
            "각 채널은 처음 업로드 시 구글 로그인 창이 뜨며, 그때 원하는 채널로 로그인하면 됩니다."
        )
        chan_layout.addWidget(self._channel_list)

        chan_btn_row = QHBoxLayout()
        add_chan_btn = QPushButton("채널 추가")
        add_chan_btn.clicked.connect(self._add_channel)
        remove_chan_btn = QPushButton("선택 삭제")
        remove_chan_btn.clicked.connect(self._remove_channel)
        chan_btn_row.addWidget(add_chan_btn)
        chan_btn_row.addWidget(remove_chan_btn)
        chan_btn_row.addStretch()
        chan_layout.addLayout(chan_btn_row)

        outer.addWidget(chan_group)
        outer.addStretch()
        return w

    def _add_channel(self) -> None:
        name, ok = QInputDialog.getText(self, "채널 추가", "채널 이름 (구분용):")
        name = name.strip()
        if not ok or not name:
            return
        if any(c["name"] == name for c in self._channels):
            QMessageBox.warning(self, "중복", "이미 있는 채널 이름입니다.")
            return
        safe = "".join(ch for ch in name if ch.isalnum() or ch in "_-") or "channel"
        creds_file = f"config/channels/{safe}_credentials.json"
        input_dir = f"input/{name}"
        self._channels.append({
            "name": name,
            "credentials_file": creds_file,
            "input_dir": input_dir,
        })
        root = Path(__file__).parent.parent
        (root / input_dir).mkdir(parents=True, exist_ok=True)
        self._refresh_channel_list()

    def _remove_channel(self) -> None:
        item = self._channel_list.currentItem()
        if not item:
            return
        name = item.data(Qt.UserRole)
        self._channels = [c for c in self._channels if c["name"] != name]
        self._refresh_channel_list()

    def _refresh_channel_list(self) -> None:
        self._channel_list.clear()
        for c in self._channels:
            item = QListWidgetItem(c["name"])
            item.setData(Qt.UserRole, c["name"])
            item.setToolTip(
                f"credentials: {c['credentials_file']}\n"
                f"대본 폴더: {c.get('input_dir', '')}"
            )
            self._channel_list.addItem(item)

    # ─────────────────────────────────────────
    # 위젯 팩토리
    # ─────────────────────────────────────────

    def _add_line(self, form: QFormLayout, key: str, label: str) -> None:
        w = QLineEdit()
        form.addRow(label + ":", w)
        self._widgets[key] = w

    def _add_spin(self, form: QFormLayout, key: str, label: str, mn: int, mx: int) -> None:
        w = QSpinBox()
        w.setRange(mn, mx)
        form.addRow(label + ":", w)
        self._widgets[key] = w

    def _add_dspin(self, form: QFormLayout, key: str, label: str, mn: float, mx: float) -> None:
        w = QDoubleSpinBox()
        w.setRange(mn, mx)
        w.setSingleStep(0.05)
        w.setDecimals(3)
        form.addRow(label + ":", w)
        self._widgets[key] = w

    def _add_check(self, form: QFormLayout, key: str, label: str) -> None:
        w = QCheckBox()
        form.addRow(label + ":", w)
        self._widgets[key] = w

    # ─────────────────────────────────────────
    # 값 로드 / 저장
    # ─────────────────────────────────────────

    def _load_values(self) -> None:
        for key, widget in self._widgets.items():
            val = self._cfg.get(key)
            if val is None:
                continue
            if isinstance(widget, QLineEdit):
                widget.setText(str(val))
            elif isinstance(widget, (QSpinBox,)):
                widget.setValue(int(val))
            elif isinstance(widget, QDoubleSpinBox):
                widget.setValue(float(val))
            elif isinstance(widget, QCheckBox):
                widget.setChecked(bool(val))

    def _save(self) -> None:
        for key, widget in self._widgets.items():
            if isinstance(widget, QLineEdit):
                self._cfg.set(key, widget.text())
            elif isinstance(widget, QSpinBox):
                self._cfg.set(key, widget.value())
            elif isinstance(widget, QDoubleSpinBox):
                self._cfg.set(key, widget.value())
            elif isinstance(widget, QCheckBox):
                self._cfg.set(key, widget.isChecked())

        self._cfg.set("youtube.channels", self._channels)
        self._cfg.save()
        logger.info("Settings saved.")
        self.accept()
