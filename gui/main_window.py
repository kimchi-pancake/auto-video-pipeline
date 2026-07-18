"""
gui/main_window.py
==================
Auto Video Pipeline 메인 윈도우.
- 왼쪽: Claude 대본 작성 패널 (넓게 — 작성한 응답을 바로 채널 대기열로 가져옴)
- 오른쪽: 채널 설정 / 업로드 설정 / 실행 카드 (좁게, 고정폭 — 채널마다 대기열을 자동으로 처리)
"""

from __future__ import annotations

import json
import sys
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    Qt, QThread, Signal, QObject, QPointF, QRectF, QSize, QTimer,
    Property, QPropertyAnimation, QEasingCurve,
)
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QAbstractButton, QApplication, QCheckBox, QComboBox, QFileDialog, QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout, QLabel,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QScrollArea,
    QSpinBox, QSystemTrayIcon, QVBoxLayout, QWidget,
    QSplitter,
)

from core.pipeline import Pipeline, PipelineProgress, PipelineResult
from core.batch_runner import BatchRunner
from gui.claude_panel import ClaudePanel
from gui.log_widget import LogWidget
from utils.config_manager import get_config
from utils.logger import get_logger, set_gui_log_callback
from utils.system_checker import SystemChecker

logger = get_logger(__name__)


# ─────────────────────────────────────────────
# 해상도 프리셋
# ─────────────────────────────────────────────

RESOLUTION_PRESETS = {
    "롱폼 1920×1080": (1920, 1080),
    "롱폼 1280×720":  (1280, 720),
    "숏폼 1080×1920": (1080, 1920),
    "숏폼 720×1280":  (720,  1280),
    "정방형 1080×1080": (1080, 1080),
}


def _make_app_icon() -> QIcon:
    """외부 아이콘 파일 없이, 코드로 재생 버튼 모양의 앱 아이콘을 그립니다.
    (트레이 아이콘 / 완료 알림 / 창 아이콘에 공통으로 씀)"""
    size = 64
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#005fb8"))
    painter.drawRoundedRect(2, 2, size - 4, size - 4, 14, 14)
    painter.setBrush(QColor("#ffffff"))
    triangle = QPolygonF([
        QPointF(size * 0.38, size * 0.28),
        QPointF(size * 0.38, size * 0.72),
        QPointF(size * 0.74, size * 0.5),
    ])
    painter.drawPolygon(triangle)
    painter.end()
    return QIcon(pixmap)


class ToggleSwitch(QAbstractButton):
    """iOS 스타일 슬라이딩 토글 스위치.

    Qt 스타일시트(QSS)는 CSS의 transition을 지원하지 않아서, QCheckBox를
    ::indicator로만 스타일링하면 켜고 끌 때 색이 순간적으로 툭 튀어 투박해
    보입니다. 그래서 직접 그리고(paintEvent) 노브 위치를 QPropertyAnimation으로
    부드럽게 움직이는 커스텀 위젯으로 만들었습니다 (디자인 목업의
    `transition: left 0.15s` / `transition: background 0.15s` 를 그대로 재현).
    """

    def __init__(self, width: int = 40, height: int = 22, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(width, height)
        self._knob_pos = 0.0  # 0.0 = 꺼짐(왼쪽), 1.0 = 켜짐(오른쪽)
        self._off_color = QColor("#4a4d55")
        self._on_color = QColor("#5b8def")
        self._anim = QPropertyAnimation(self, b"knobPos", self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.toggled.connect(self._animate_to_state)

    def _animate_to_state(self, checked: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._knob_pos)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def set_colors(self, off_color: str, on_color: str) -> None:
        """테마(다크/라이트) 전환 시 트랙 색을 갱신합니다."""
        self._off_color = QColor(off_color)
        self._on_color = QColor(on_color)
        self.update()

    def _get_knob_pos(self) -> float:
        return self._knob_pos

    def _set_knob_pos(self, value: float) -> None:
        self._knob_pos = value
        self.update()

    knobPos = Property(float, _get_knob_pos, _set_knob_pos)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()
        track_r = rect.height() / 2

        t = self._knob_pos
        track_color = QColor(
            int(self._off_color.red()   + (self._on_color.red()   - self._off_color.red())   * t),
            int(self._off_color.green() + (self._on_color.green() - self._off_color.green()) * t),
            int(self._off_color.blue()  + (self._on_color.blue()  - self._off_color.blue())  * t),
        )
        painter.setPen(Qt.NoPen)
        painter.setBrush(track_color)
        painter.drawRoundedRect(QRectF(rect), track_r, track_r)

        knob_d = rect.height() - 4
        knob_x = 2 + t * (rect.width() - knob_d - 4)
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(QRectF(knob_x, 2, knob_d, knob_d))
        painter.end()


class Spinner(QWidget):
    """작은 회전 로딩 스피너 ("영상 만드는 중" 표시용). start()/stop()으로 켜고 끕니다
    (안 쓸 때는 애니메이션 타이머 자체를 멈춰서 불필요하게 CPU를 쓰지 않음)."""

    def __init__(self, diameter: int = 18, parent=None):
        super().__init__(parent)
        self._diameter = diameter
        self.setFixedSize(diameter, diameter)
        self._angle = 0.0
        self._color = QColor("#5b8def")
        self._anim = QPropertyAnimation(self, b"angle", self)
        self._anim.setDuration(900)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(360.0)
        self._anim.setLoopCount(-1)
        self.hide()

    def set_color(self, color: str) -> None:
        self._color = QColor(color)
        self.update()

    def start(self) -> None:
        self.show()
        self._anim.start()

    def stop(self) -> None:
        self._anim.stop()
        self.hide()

    def _get_angle(self) -> float:
        return self._angle

    def _set_angle(self, value: float) -> None:
        self._angle = value
        self.update()

    angle = Property(float, _get_angle, _set_angle)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        pen_width = max(2, self._diameter // 8)
        rect = QRectF(pen_width / 2, pen_width / 2, self.width() - pen_width, self.height() - pen_width)
        pen = QPen(self._color, pen_width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        start_angle = int(-self._angle * 16)
        span_angle = int(270 * 16)
        painter.drawArc(rect, start_angle, span_angle)
        painter.end()


def _apply_elevation(widget: QWidget, blur: int = 28, y_offset: int = 4, alpha: int = 70) -> None:
    """카드/패널에 은은한 드롭섀도우를 얹어 Fluent/macOS 스타일 입체감을 줍니다."""
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, y_offset)
    effect.setColor(QColor(0, 0, 0, alpha))
    widget.setGraphicsEffect(effect)


# ─────────────────────────────────────────────
# 파이프라인 워커
# ─────────────────────────────────────────────

class PipelineWorker(QObject):
    progress   = Signal(object)
    finished   = Signal(object)
    batch_done = Signal(int, int)
    log_signal = Signal(int, str)

    def __init__(
        self,
        upload: bool,
        privacy: str,
        days: int,
        width: int,
        height: int,
        story_path: str = "",
        batch_count: int = 0,
        input_dir: str = "input",
        youtube_credentials_file: str = "",
        archive_subdir: str = "",
        also_shorts: bool = False,
    ):
        super().__init__()
        self._story_path  = story_path
        self._upload      = upload
        self._privacy     = privacy
        self._days        = days
        self._width       = width
        self._height      = height
        self._batch_count = batch_count
        self._input_dir   = input_dir
        self._yt_creds    = youtube_credentials_file
        self._archive_subdir = archive_subdir
        self._also_shorts = also_shorts
        self._pipeline: Optional[Pipeline] = None
        self._batch: Optional[BatchRunner] = None

    def run(self) -> None:
        cfg = get_config()
        # 해상도 런타임 적용
        cfg.set("video.width",          self._width)
        cfg.set("video.height",         self._height)
        cfg.set("image.default_width",  self._width)
        cfg.set("image.default_height", self._height)

        if self._batch_count > 0:
            self._batch = BatchRunner(
                config=cfg,
                input_dir=self._input_dir,
                progress_callback=self._on_progress,
                on_file_done=self._on_file_done,
                youtube_credentials_file=self._yt_creds or None,
                archive_subdir=self._archive_subdir,
            )
            try:
                results = self._batch.run(
                    count=self._batch_count,
                    upload_to_youtube=self._upload,
                    youtube_privacy=self._privacy,
                    schedule_days_ahead=self._days,
                    also_make_shorts=self._also_shorts,
                )
                success = sum(1 for r in results if r.success)
                self.batch_done.emit(success, len(results))
                last = results[-1] if results else PipelineResult(success=False, error="결과 없음")
                self.finished.emit(last)
            except ValueError as e:
                self.finished.emit(PipelineResult(success=False, error=str(e)))
            return

        self._pipeline = Pipeline(cfg)
        self._pipeline.set_progress_callback(self._on_progress)
        result = self._pipeline.run(
            story_path=self._story_path,
            upload_to_youtube=self._upload,
            youtube_privacy=self._privacy,
            schedule_days_ahead=self._days,
            youtube_credentials_file=self._yt_creds or None,
            also_make_shorts=self._also_shorts,
        )
        self.finished.emit(result)

    def stop(self) -> None:
        if self._pipeline:
            self._pipeline.request_stop()
        if self._batch:
            self._batch.request_stop()

    def _on_progress(self, p) -> None:
        self.progress.emit(p)

    def _on_file_done(self, path: str, result: PipelineResult) -> None:
        name = Path(path).name
        if result.success:
            self.log_signal.emit(logging.INFO, f"완료: {name}")
        else:
            self.log_signal.emit(logging.ERROR, f"실패: {name} → {result.error}")


# ─────────────────────────────────────────────
# 채널 카드 (왼쪽 패널) — 채널마다 하나씩, 동시에 여러 채널을 병렬 처리
# ─────────────────────────────────────────────

class ChannelCardWidget(QWidget):
    """
    채널 하나를 실행하는 컴팩트한 카드 — 여러 채널을 동시에(병렬로) 돌릴 수
    있도록 우측 컬럼에 채널당 하나씩 여러 개가 나란히 쌓일 수 있습니다.
    story.txt 를 따로 지정하지 않으면 그 채널의 대본 폴더에서 자동으로
    하나씩 꺼내 처리합니다.
    (해상도 / YouTube 업로드 설정은 "업로드 설정" 카드에서 공유해서 씁니다.)
    """

    running_changed = Signal(bool)
    result_ready = Signal(object)  # PipelineResult — 실행 하나가 끝날 때마다 발생 (알림용)
    closed = Signal()              # 사용자가 카드를 닫기(제거) 버튼으로 지웠을 때

    _MAX_RESULT_ROWS = 6  # 컴팩트 카드라 최근 몇 개만 유지

    def __init__(
        self, channel: dict, get_settings, claim_next_file, release_file,
        get_pending_count, claim_shorts_sibling=None, big: bool = False, parent=None,
    ):
        super().__init__(parent)
        self._channel = dict(channel)
        self._big = big
        self._get_settings = get_settings          # () -> (upload, privacy, days, w, h, also_shorts)
        self._claim_next_file = claim_next_file     # (input_dir) -> Optional[Path]
        self._release_file = release_file           # (path_str, success) -> None
        self._get_pending_count = get_pending_count  # (input_dir) -> int
        self._claim_shorts_sibling = claim_shorts_sibling  # (long_path, input_dir) -> Optional[Path]
        self._worker: Optional[PipelineWorker] = None
        self._thread: Optional[QThread] = None
        self._auto_claimed = False
        self._override_path = ""
        self._active_path = ""
        self._pending_shorts_path = ""  # 짝 쇼츠 대본 점유해둔 경로 (다음 차례에 진짜로 처리)
        self._forced_next_path = ""     # 짝 쇼츠를 다음 _start()에서 강제로 처리하기 위한 경로
        self._setup_ui()
        self._set_state("idle")

    @staticmethod
    def _title_for(channel: dict) -> str:
        return channel.get("name") or "기본 채널"

    def channel_name(self) -> str:
        return self._channel.get("name", "")

    def update_channel(self, channel: dict) -> None:
        """채널(credentials/폴더)로 갱신합니다. 실행 중에는 바꾸지 않습니다."""
        self._channel = dict(channel)
        self._title_label.setText(self._title_for(self._channel))

    def _setup_ui(self) -> None:
        self._exec_card = QWidget()
        self._exec_card.setObjectName("executionCard")
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        card_v = QVBoxLayout(self._exec_card)
        card_v.setContentsMargins(16, 14, 16, 16)
        card_v.setSpacing(10)
        v.addWidget(self._exec_card)

        # ── 채널명 + 대기 배지 + 닫기 ──
        top_row = QHBoxLayout()
        self._title_label = QLabel(self._title_for(self._channel))
        self._title_label.setObjectName("cardTitle")
        top_row.addWidget(self._title_label)
        self._pending_badge = QLabel("")
        self._pending_badge.setObjectName("queueBadge")
        top_row.addWidget(self._pending_badge)
        top_row.addStretch()
        self._close_btn = QPushButton("✕")
        self._close_btn.setObjectName("ghostBtn")
        self._close_btn.setFixedSize(22, 22)
        self._close_btn.setToolTip("이 카드 닫기 (실행 중에는 닫을 수 없음)")
        self._close_btn.clicked.connect(self._on_close_clicked)
        top_row.addWidget(self._close_btn)
        card_v.addLayout(top_row)

        # ── 컴팩트 실행 제어: 자동이어서 체크 + 파일선택/시작/정지 ──
        self._auto_continue_cb = QCheckBox("완료 후 자동 이어서")
        self._auto_continue_cb.setObjectName("cardAutoCb")
        self._auto_continue_cb.setToolTip(
            "체크하면 성공적으로 끝날 때마다 대기열에 남은 파일을 자동으로 이어서 처리합니다."
        )
        card_v.addWidget(self._auto_continue_cb)

        self._override_row = QHBoxLayout()
        self._override_label = QLabel("")
        self._override_label.setObjectName("overrideLabel")
        self._override_label.setWordWrap(True)
        clear_override_btn = QPushButton("✕")
        clear_override_btn.setFixedWidth(20)
        clear_override_btn.setToolTip("지정 해제 (자동 선택으로 되돌리기)")
        clear_override_btn.clicked.connect(self._clear_override)
        self._override_row.addWidget(self._override_label, 1)
        self._override_row.addWidget(clear_override_btn)
        self._override_widget = QWidget()
        self._override_widget.setLayout(self._override_row)
        self._override_widget.hide()
        card_v.addWidget(self._override_widget)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self._file_btn = QPushButton("파일 선택")
        self._file_btn.setObjectName("fileBtn")
        self._file_btn.setToolTip("특정 story.txt 를 지정하려면 클릭 (기본은 대본 폴더에서 자동 선택)")
        self._file_btn.clicked.connect(self._browse)
        btn_row.addWidget(self._file_btn, 1)
        self._start_btn = QPushButton("▶  시작")
        self._start_btn.setObjectName("btn_start")
        self._start_btn.setMinimumHeight(34)
        self._start_btn.clicked.connect(self._start)
        btn_row.addWidget(self._start_btn, 1)
        self._stop_btn = QPushButton("■  정지")
        self._stop_btn.setObjectName("btn_stop")
        self._stop_btn.setMinimumHeight(34)
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._stop)
        btn_row.addWidget(self._stop_btn, 1)
        card_v.addLayout(btn_row)

        # ── 진행 상태: 스피너 + 단계 텍스트 + % , 얇은 진행률 바 ──
        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        self._spinner = Spinner(16)
        status_row.addWidget(self._spinner)
        self._stage_label = QLabel("대기 중")
        self._stage_label.setObjectName("progressStageLabel")
        status_row.addWidget(self._stage_label, 1)
        self._pct_label = QLabel("")
        self._pct_label.setObjectName("progressPctLabel")
        status_row.addWidget(self._pct_label)
        card_v.addLayout(status_row)

        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setFixedHeight(5)
        self._bar.setTextVisible(False)
        card_v.addWidget(self._bar)
        self._bar_anim = QPropertyAnimation(self._bar, b"value", self)
        self._bar_anim.setDuration(280)
        self._bar_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._next_up_label = QLabel("")
        self._next_up_label.setObjectName("resultLink")
        self._next_up_label.setWordWrap(True)
        self._next_up_label.hide()
        card_v.addWidget(self._next_up_label)

        # ── 처리 결과: 한 줄씩 컴팩트하게 ──
        results_title = QLabel("최근 결과")
        results_title.setObjectName("fieldLabel")
        card_v.addWidget(results_title)

        self._results_list = QVBoxLayout()
        self._results_list.setSpacing(4)
        results_container = QWidget()
        results_container.setLayout(self._results_list)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setMaximumHeight(110)
        scroll.setWidget(results_container)
        card_v.addWidget(scroll)

        self._no_results_label = QLabel("아직 처리한 파일이 없습니다.")
        self._no_results_label.setObjectName("fieldLabel")
        self._results_list.addWidget(self._no_results_label)

        _apply_elevation(self._exec_card)
        self.refresh_pending()

    def _on_close_clicked(self) -> None:
        if self.is_running() or self._pending_shorts_path or self._forced_next_path:
            return
        self.closed.emit()

    def _add_result_row(self, result: PipelineResult) -> None:
        """최근 처리 결과를 카드 맨 위에 한 줄(시간 + 링크/오류만) 추가합니다
        (최대 _MAX_RESULT_ROWS 개까지만 유지 — 컴팩트 카드라 상세 내역 대신
        요약만 보여주고, 자세한 내용은 로그 드로어에서 확인합니다)."""
        if self._no_results_label is not None:
            self._no_results_label.setParent(None)
            self._no_results_label = None

        row = QWidget()
        row.setObjectName("resultRow")
        h = QHBoxLayout(row)
        h.setContentsMargins(8, 5, 8, 5)
        h.setSpacing(8)

        time_label = QLabel(datetime.now().strftime("%m-%d %H:%M"))
        time_label.setObjectName("resultTime")
        h.addWidget(time_label)

        if result.success:
            parts = []
            if result.youtube_video_id:
                url = f"https://youtu.be/{result.youtube_video_id}"
                parts.append(f'<a href="{url}">유튜브</a>')
            if result.shorts_video_path:
                if result.youtube_shorts_video_id:
                    s_url = f"https://youtu.be/{result.youtube_shorts_video_id}"
                    parts.append(f'<a href="{s_url}">쇼츠</a>')
                else:
                    parts.append("쇼츠✓")
            summary = QLabel(" · ".join(parts) if parts else "완료")
            summary.setObjectName("resultLink" if parts else "resultOk")
            summary.setTextInteractionFlags(Qt.TextBrowserInteraction)
            summary.setOpenExternalLinks(True)
            h.addWidget(summary, 1)
        else:
            err_text = (result.error or "오류")[:40]
            err_label = QLabel(err_text)
            err_label.setObjectName("resultError")
            err_label.setToolTip(result.error or "")
            h.addWidget(err_label, 1)

        self._results_list.insertWidget(0, row)

        # 오래된 기록 정리 (맨 위가 최신이므로 끝에서부터 잘라냄)
        while self._results_list.count() > self._MAX_RESULT_ROWS:
            item = self._results_list.takeAt(self._results_list.count() - 1)
            if item.widget():
                item.widget().deleteLater()

    def _set_state(self, state: str) -> None:
        """실행 카드 테두리 색으로 상태(대기/실행/완료/실패)를 한눈에 구분할 수 있게 합니다."""
        self._exec_card.setProperty("state", state)
        self._exec_card.style().unpolish(self._exec_card)
        self._exec_card.style().polish(self._exec_card)

    def _animate_bar_to(self, value: int) -> None:
        """진행률 바가 값이 바뀔 때 툭 튀지 않고 부드럽게 채워지도록 애니메이션합니다."""
        self._bar_anim.stop()
        self._bar_anim.setStartValue(self._bar.value())
        self._bar_anim.setEndValue(value)
        self._bar_anim.start()

    def _update_next_up_label(self) -> None:
        """짝 쇼츠 대본이 이어서 처리될 예정이면 표시합니다 — "쇼츠도 함께"를
        켰는데 정작 쇼츠가 안 만들어지는 것처럼 보이는 원인이, 사실은 롱폼이
        끝난 뒤에야 이어서 시작되는 걸 몰라서 기다리다 지쳐 꺼버리는 경우가
        많아서, 지금 예정돼 있다는 걸 눈에 보이게 합니다."""
        if self._pending_shorts_path:
            self._next_up_label.setText("다음: 쇼츠 대본 이어서 처리 예정")
            self._next_up_label.show()
        else:
            self._next_up_label.hide()

    def is_running(self) -> bool:
        return self._thread is not None

    def start_run(self, silent: bool = False) -> None:
        """MainWindow의 "이 채널 시작" 버튼에서 호출하는 공개 진입점입니다."""
        self._start(silent=silent)

    def set_runnable(self, enabled: bool) -> None:
        """시스템 점검 실패 등으로 실행 자체를 막아야 할 때 사용합니다."""
        self._start_btn.setEnabled(enabled and not self.is_running())

    def refresh_pending(self) -> None:
        n = self._get_pending_count(self._channel.get("input_dir", ""))
        self._pending_badge.setText(f"대기 중 {n}개")

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "story.txt 선택", "", "Text Files (*.txt);;All Files (*)"
        )
        if path:
            self._override_path = path
            self._override_label.setText(f"지정됨: {Path(path).name}")
            self._override_widget.show()

    def _clear_override(self) -> None:
        self._override_path = ""
        self._override_widget.hide()

    def _start(self, silent: bool = False) -> None:
        if self.is_running():
            return

        story_path = self._override_path
        self._auto_claimed = False
        chan_label = self._channel.get("name") or "기본 채널"

        forced_path = self._forced_next_path
        self._forced_next_path = ""

        if forced_path:
            # 직전 실행에서 짝으로 점유해둔 '_shorts.txt' 를 이어서 처리하는
            # 차례 — 점유된 파일이므로 끝나면 release(archive 이동)가 필요해
            # 자동 점유된 것과 동일하게 취급합니다.
            story_path = forced_path
            self._auto_claimed = True
        elif not story_path:
            _, _, _, _, _, also_shorts_peek = self._get_settings()
            claimed = self._claim_next_file(self._channel.get("input_dir", ""))
            if claimed is None:
                if not silent:
                    QMessageBox.information(
                        self, "대기 파일 없음",
                        f"'{chan_label}' 채널 폴더에 처리할 story.txt 가 없습니다."
                    )
                return
            story_path = str(claimed)
            self._auto_claimed = True
            # "쇼츠도 함께"가 켜져 있고 방금 점유한 파일이 '_long.txt' 라면,
            # 롱폼 씬을 잘라 쇼츠를 만드는 대신 짝이 되는 '_shorts.txt'(콤보
            # 프롬프트로 같이 받은 진짜 쇼츠 대본)가 대기열에 있는지 찾아서
            # 같이 점유해둡니다 — 있으면 이번 실행이 끝난 뒤 바로 이어서
            # 그 파일을 (진짜 쇼츠 대본으로) 처리합니다.
            if also_shorts_peek and self._claim_shorts_sibling:
                sibling = self._claim_shorts_sibling(claimed, self._channel.get("input_dir", ""))
                if sibling:
                    self._pending_shorts_path = str(sibling)
            self.refresh_pending()
        elif not Path(story_path).exists():
            QMessageBox.warning(self, "경고", f"'{chan_label}' 파일이 없습니다:\n{story_path}")
            return

        self._active_path = story_path
        upload, privacy, days, w, h, also_shorts = self._get_settings()
        if forced_path or self._pending_shorts_path:
            # 이번 실행 자체는 진짜 쇼츠 대본을 짝으로 따로 처리하니(또는 지금
            # 처리 중인 파일 자체가 그 쇼츠 대본이니) 롱폼 씬 재사용-트리밍은
            # 필요 없습니다.
            also_shorts = False

        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._file_btn.setEnabled(False)
        self._close_btn.setEnabled(False)
        self._bar.setValue(0)
        self._stage_label.setText(f"시작 중... ({Path(story_path).name})")
        self._pct_label.setText("0%")
        self._set_state("running")
        self._spinner.start()
        self._update_next_up_label()
        self.running_changed.emit(True)

        self._worker = PipelineWorker(
            upload=upload, privacy=privacy, days=days,
            width=w, height=h,
            story_path=story_path,
            also_shorts=also_shorts,
            youtube_credentials_file=self._channel.get("credentials_file", ""),
        )
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._thread.deleteLater)
        # _on_finished 와 thread.quit 은 둘 다 finished 시그널에 걸려 있고
        # 큐잉되는 순서상 _on_finished 가 먼저 실행됩니다. 그 안에서 바로
        # self._thread를 None으로 지워버리면, quit()이 아직 처리되지 않아
        # 스레드가 실제로는 여전히 실행 중인 상태에서 마지막 파이썬 참조가
        # 사라져 Qt가 "실행 중인 QThread"를 그대로 파괴 → 프로그램이 통째로
        # 죽습니다. 그래서 스레드가 완전히 멈췄다는 finished 신호를 받은
        # 뒤에만(_on_thread_stopped) 참조를 정리합니다.
        self._thread.finished.connect(self._on_thread_stopped)
        self._thread.start()

    def _stop(self) -> None:
        if self._worker:
            self._worker.stop()
        self._stop_btn.setEnabled(False)
        self._stage_label.setText("중지 요청됨...")

    def _on_progress(self, p: PipelineProgress) -> None:
        self._animate_bar_to(int(p.overall_pct))
        self._stage_label.setText(p.stage)
        self._pct_label.setText(f"{int(p.overall_pct)}%")

    def _on_thread_stopped(self) -> None:
        """QThread가 실제로 멈춘 뒤에만 참조를 정리합니다 (위 _start의 주석 참고)."""
        self._thread = None
        self._worker = None

    def _on_finished(self, result: PipelineResult) -> None:
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._file_btn.setEnabled(True)
        self._spinner.stop()
        self.running_changed.emit(False)
        was_auto = self._auto_claimed
        if result.success:
            self._animate_bar_to(100)
            self._pct_label.setText("100%")
            self._stage_label.setText(f"완료 ({result.elapsed:.1f}초)")
            self._set_state("success")
        else:
            self._stage_label.setText("실패")
            self._set_state("error")
        self._add_result_row(result)

        if was_auto:
            self._release_file(self._active_path, result.success)
            self._auto_claimed = False
            self._override_path = ""
            self._override_widget.hide()
        self._active_path = ""
        self.refresh_pending()
        self.result_ready.emit(result)

        # 짝으로 점유해둔 쇼츠 대본이 있으면, "자동 반복" 체크 여부와 무관하게
        # 바로 이어서 처리합니다 (방금 처리한 롱폼의 성공/실패와도 무관 — 짝
        # 쇼츠는 독립된 대본이라 그 자체로 처리해야 점유된 채로 안 남습니다).
        if self._pending_shorts_path:
            self._forced_next_path = self._pending_shorts_path
            self._pending_shorts_path = ""
            self._update_next_up_label()
            self._close_btn.setEnabled(False)  # 곧 다시 시작되므로 그 사이에 닫히지 않게
            QTimer.singleShot(400, lambda: self._start(silent=True))
        else:
            self._close_btn.setEnabled(True)
            # 자동 반복: 성공했고 체크돼 있으면 대기열의 다음 파일을 이어서 처리
            # (실패 시에는 반복 실패를 막기 위해 자동으로 이어가지 않음)
            if result.success and was_auto and self._auto_continue_cb.isChecked():
                self._close_btn.setEnabled(False)
                QTimer.singleShot(400, lambda: self._start(silent=True))


# ─────────────────────────────────────────────
# 다크 / 라이트 팔레트 & 스타일
# ─────────────────────────────────────────────

_UI_PREFS_FILE = Path(__file__).parent.parent / "config" / "ui_prefs.json"


def _load_ui_prefs() -> dict:
    try:
        if _UI_PREFS_FILE.exists():
            return json.loads(_UI_PREFS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_ui_prefs(**updates) -> None:
    """기존 키를 지우지 않고 병합해서 저장합니다 (theme/last_channel 등 공유 파일)."""
    data = _load_ui_prefs()
    data.update(updates)
    try:
        _UI_PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _UI_PREFS_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def load_theme_pref() -> str:
    theme = _load_ui_prefs().get("theme")
    return theme if theme in ("dark", "light") else "light"


def save_theme_pref(theme: str) -> None:
    _save_ui_prefs(theme=theme)


def load_last_channel_pref() -> str:
    return _load_ui_prefs().get("last_channel", "")


def save_last_channel_pref(name: str) -> None:
    _save_ui_prefs(last_channel=name)


# Claude 디자인 목업(YouTube 자동 생성 프로그램 UI)에서 그대로 가져온 색상표.
_DARK = dict(
    bg_app="#1e1f22", bg_panel="#26282c", bg_panel_alt="#2c2e33", bg_input="#1b1c1f",
    border="#3a3c42", border_strong="#4a4d55",
    text_primary="#e8e9eb", text_secondary="#9a9da3", text_muted="#6d6f76",
    accent="#5b8def", accent_hover="#4a7ce0",
    success="#3dd68c", danger="#e5484d", warning="#f5a623",
    queue_badge_bg="rgba(91,141,239,0.15)",
)
_LIGHT = dict(
    bg_app="#f2f3f5", bg_panel="#ffffff", bg_panel_alt="#f7f8f9", bg_input="#ffffff",
    border="#dcdee2", border_strong="#c5c8cd",
    text_primary="#1e1f22", text_secondary="#6b6e76", text_muted="#9a9da3",
    accent="#3b6fd6", accent_hover="#2f5cc0",
    success="#1f9d63", danger="#d93c42", warning="#c97a12",
    queue_badge_bg="rgba(59,111,214,0.1)",
)


def _dark_palette() -> QPalette:
    p = QPalette()
    t = _DARK
    p.setColor(QPalette.Window,          QColor(t["bg_app"]))
    p.setColor(QPalette.WindowText,      QColor(t["text_primary"]))
    p.setColor(QPalette.Base,            QColor(t["bg_input"]))
    p.setColor(QPalette.AlternateBase,   QColor(t["bg_panel_alt"]))
    p.setColor(QPalette.Text,            QColor(t["text_primary"]))
    p.setColor(QPalette.Button,          QColor(t["bg_panel_alt"]))
    p.setColor(QPalette.ButtonText,      QColor(t["text_primary"]))
    p.setColor(QPalette.Highlight,       QColor(t["accent"]))
    p.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    p.setColor(QPalette.Disabled, QPalette.Text,       QColor(t["text_muted"]))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(t["text_muted"]))
    return p


def _light_palette() -> QPalette:
    p = QPalette()
    t = _LIGHT
    p.setColor(QPalette.Window,          QColor(t["bg_app"]))
    p.setColor(QPalette.WindowText,      QColor(t["text_primary"]))
    p.setColor(QPalette.Base,            QColor(t["bg_input"]))
    p.setColor(QPalette.AlternateBase,   QColor(t["bg_panel_alt"]))
    p.setColor(QPalette.Text,            QColor(t["text_primary"]))
    p.setColor(QPalette.Button,          QColor(t["bg_panel_alt"]))
    p.setColor(QPalette.ButtonText,      QColor(t["text_primary"]))
    p.setColor(QPalette.Highlight,       QColor(t["accent"]))
    p.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    p.setColor(QPalette.Disabled, QPalette.Text,       QColor(t["text_muted"]))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(t["text_muted"]))
    return p


def _build_qss(t: dict) -> str:
    """Claude 디자인 목업의 색상표(t=_DARK 또는 _LIGHT)로 전체 스타일시트를 만듭니다."""
    return f"""
QMainWindow, QWidget {{
    background: {t["bg_app"]}; color: {t["text_primary"]};
    font-size: 13px; font-family: "Segoe UI", "Malgun Gothic", sans-serif;
}}

/* ── 상단 바 ── */
QWidget#topBar {{ background:{t["bg_panel"]}; border-bottom:1px solid {t["border"]}; }}
QLabel#appTitle {{ font-size:18px; font-weight:600; color:{t["text_primary"]}; }}
QPushButton#topBarBtn {{
    height:30px; padding:0 14px; font-size:12px; font-weight:500;
    color:{t["text_primary"]}; background:transparent; border:1px solid {t["border"]}; border-radius:4px;
}}
QPushButton#topBarBtn:hover {{ background:{t["bg_panel_alt"]}; }}

/* ── 좌측 패널(AI 대본 작성) ── */
QWidget#leftPanel {{ background:{t["bg_app"]}; }}
QLabel#sectionTitle {{ font-size:14px; font-weight:600; color:{t["text_primary"]}; }}
QWidget#browserFrame {{ background:{t["bg_panel_alt"]}; border:1px solid {t["border"]}; border-radius:8px; }}
QPushButton#ghostBtn {{
    height:30px; padding:0 14px; font-size:12px; font-weight:500;
    color:{t["text_primary"]}; background:transparent; border:1px solid {t["border"]}; border-radius:4px;
}}
QPushButton#ghostBtn:hover {{ background:{t["bg_panel_alt"]}; }}
QPushButton#outlineAccentBtn {{
    height:30px; padding:0 14px; font-size:12px; font-weight:600;
    color:{t["accent"]}; background:transparent; border:1px solid {t["accent"]}; border-radius:4px;
}}
QPushButton#outlineAccentBtn:hover {{ background:{t["bg_panel_alt"]}; }}
QLabel#templateLabel {{
    font-size:11px; font-weight:600; color:{t["text_secondary"]};
}}
QPushButton#templateBtn {{
    height:30px; padding:0 12px; font-size:12px; font-weight:500;
    color:{t["text_primary"]}; background:{t["bg_panel_alt"]}; border:1px solid {t["border"]}; border-radius:4px;
}}
QPushButton#templateBtn:hover {{ border-color:{t["accent"]}; }}

/* ── 우측 컬럼 카드 공통 ── */
QScrollArea#rightColumn {{ background:{t["bg_app"]}; border:none; }}
QScrollArea#rightColumn > QWidget > QWidget {{ background:{t["bg_app"]}; }}
QWidget#card {{ background:{t["bg_panel"]}; border:1px solid {t["border"]}; border-radius:8px; }}
QWidget#executionCard {{ background:{t["bg_panel"]}; border:1.5px solid {t["border"]}; border-radius:8px; }}
QWidget#executionCard[state="running"] {{ border-color:{t["accent"]}; }}
QWidget#executionCard[state="success"] {{ border-color:{t["success"]}; }}
QWidget#executionCard[state="error"]   {{ border-color:{t["danger"]}; }}
QLabel#cardTitle {{
    font-size:12px; font-weight:600; color:{t["text_secondary"]};
}}
QLabel#fieldLabel {{ font-size:12px; color:{t["text_secondary"]}; }}

QComboBox, QDateTimeEdit {{
    height:32px; padding:0 10px; font-size:13px;
    color:{t["text_primary"]}; background:{t["bg_input"]};
    border:1px solid {t["border"]}; border-radius:4px;
}}
QComboBox:hover, QDateTimeEdit:hover {{ border-color:{t["accent"]}; }}
QComboBox::drop-down {{ border:none; width:22px; }}
QComboBox QAbstractItemView {{
    background:{t["bg_panel"]}; color:{t["text_primary"]};
    border:1px solid {t["border"]}; selection-background-color:{t["accent"]};
}}

QLabel#queueBadge {{
    font-size:11px; font-weight:600; color:{t["accent"]};
    background:{t["queue_badge_bg"]}; padding:3px 10px; border-radius:999px;
}}
QPushButton#fileBtn {{
    height:32px; font-size:13px; font-weight:500;
    color:{t["text_primary"]}; background:{t["bg_panel_alt"]}; border:1px solid {t["border"]}; border-radius:4px;
}}
QPushButton#fileBtn:hover {{ border-color:{t["accent"]}; }}
QLabel#overrideLabel {{ color:{t["accent"]}; font-size:11px; font-style:italic; }}

QPushButton#btn_start {{
    background:{t["accent"]}; color:#ffffff; font-weight:600;
    font-size:14px; border:none; border-radius:6px;
}}
QPushButton#btn_start:hover    {{ background:{t["accent_hover"]}; }}
QPushButton#btn_start:disabled {{ background:{t["border_strong"]}; color:{t["text_muted"]}; }}
QPushButton#btn_stop {{
    background:{t["bg_panel_alt"]}; color:{t["text_muted"]}; border:1px solid {t["border"]}; border-radius:6px;
}}
QPushButton#btn_stop:enabled {{ background:{t["danger"]}; color:#ffffff; border:none; }}

QProgressBar {{
    background:{t["bg_panel_alt"]}; border:1px solid {t["border"]}; border-radius:4px;
    height:8px; text-align:center; color:transparent;
}}
QProgressBar::chunk {{ background:{t["accent"]}; border-radius:4px; }}
QLabel#progressStageLabel {{ font-size:12px; color:{t["text_secondary"]}; }}
QLabel#progressPctLabel {{ font-size:12px; font-weight:600; color:{t["text_primary"]}; }}
QLabel#cardStatusBig {{ color:{t["text_secondary"]}; font-size:12px; }}

QWidget#resultRow {{ background:{t["bg_panel_alt"]}; border:1px solid {t["border"]}; border-radius:6px; }}
QLabel#resultTime {{ font-size:12px; color:{t["text_secondary"]}; }}
QLabel#resultOk {{ font-size:12px; color:{t["text_primary"]}; }}
QLabel#resultLink {{ font-size:12px; color:{t["accent"]}; }}
QLabel#resultError {{ font-size:12px; color:{t["danger"]}; }}

/* ── 하단 상태 표시줄 ── */
QWidget#statusBarRow {{ background:{t["bg_panel"]}; border-top:1px solid {t["border"]}; }}
QLabel#statusText {{ font-size:11px; color:{t["text_secondary"]}; }}

QSplitter::handle {{ background:{t["border"]}; }}
QSplitter::handle:hover {{ background:{t["accent"]}; }}

/* ── 얇고 모던한 스크롤바 ── */
QScrollBar:vertical {{ background:transparent; width:9px; margin:2px; }}
QScrollBar::handle:vertical {{
    background:{t["border_strong"]}; border-radius:4px; min-height:28px;
}}
QScrollBar::handle:vertical:hover {{ background:{t["accent"]}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; border:none; background:none; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background:transparent; }}
QScrollBar:horizontal {{ background:transparent; height:9px; margin:2px; }}
QScrollBar::handle:horizontal {{
    background:{t["border_strong"]}; border-radius:4px; min-width:28px;
}}
QScrollBar::handle:horizontal:hover {{ background:{t["accent"]}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width:0; border:none; background:none; }}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background:transparent; }}

/* ── 체크박스 (쇼츠도 함께 / 자동 이어서 처리): 작은 사각 체크 ── */
QCheckBox {{ color:{t["text_primary"]}; font-size:13px; }}
QCheckBox::indicator {{
    width:16px; height:16px; border-radius:3px;
    border:1.5px solid {t["border_strong"]}; background:{t["bg_input"]};
}}
QCheckBox::indicator:hover   {{ border-color:{t["accent"]}; }}
QCheckBox::indicator:checked {{ background:{t["accent"]}; border-color:{t["accent"]}; }}
QCheckBox#cardAutoCb {{ color:{t["text_secondary"]}; font-size:11px; }}
/* 테마 전환 / 업로드 여부 토글은 QSS가 아니라 ToggleSwitch(커스텀 위젯, 직접
   그려서 슬라이딩 애니메이션 적용)를 씁니다 — set_colors()로 색을 갱신합니다. */
"""


_THEMES = {"dark": (_dark_palette, lambda: _build_qss(_DARK)), "light": (_light_palette, lambda: _build_qss(_LIGHT))}


def apply_theme(app: QApplication, theme: str) -> None:
    palette_fn, qss_fn = _THEMES.get(theme, _THEMES["light"])
    app.setPalette(palette_fn())
    app.setStyleSheet(qss_fn())


# ─────────────────────────────────────────────
# 메인 윈도우
# ─────────────────────────────────────────────

class MainWindow(QMainWindow):
    log_signal = Signal(int, str)

    def __init__(self):
        super().__init__()
        self._current_theme = load_theme_pref()
        self._claimed_files: set = set()
        self._system_ok = True
        self._setup_ui()
        self._log_widget.set_theme(self._current_theme)
        # 로그 콜백은 앱 전체에서 단 한 번만 등록 (여러 채널 카드가 동시에
        # 돌아도 스레드 안전하게 하나의 로그창으로 모이도록 Qt 시그널을 경유)
        self.log_signal.connect(self._log_widget.append_log)
        set_gui_log_callback(self.log_signal.emit)
        self._setup_tray_notifications()
        self._run_system_check()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._fade_played:
            self._fade_played = True
            self._fade_anim.start()

    def _setup_tray_notifications(self) -> None:
        """영상 생성이 끝날 때마다(성공/실패 상관없이) 윈도우 알림(토스트)을 띄웁니다."""
        icon = _make_app_icon()
        self.setWindowIcon(icon)
        self._tray_icon = QSystemTrayIcon(icon, self)
        self._tray_icon.setToolTip("Auto Video Pipeline")
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray_icon.show()
        # 카드별 result_ready 연결은 _add_run_card()에서 각 카드가 생성될 때 합니다
        # (카드가 여러 개라 어느 카드가 보낸 신호인지 sender()로 구분합니다).

    def _notify_run_result(self, result: PipelineResult) -> None:
        card = self.sender()
        chan = card.channel_name() if card else ""
        chan = chan or "기본 채널"
        if result.success:
            title = f"[{chan}] 영상 생성 완료"
            parts = []
            if result.youtube_video_id:
                parts.append("YouTube 업로드 완료")
            elif result.youtube_error:
                parts.append("영상은 완성, YouTube 업로드는 실패")
            if result.shorts_video_path:
                parts.append("쇼츠도 함께 생성됨")
            msg = f"{result.elapsed:.0f}초 걸림" + ("\n" + " · ".join(parts) if parts else "")
            icon = QSystemTrayIcon.MessageIcon.Information
        else:
            title = f"[{chan}] 영상 생성 실패"
            msg = result.error or "오류가 발생했습니다."
            icon = QSystemTrayIcon.MessageIcon.Critical
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray_icon.showMessage(title, msg, icon, 8000)

    def _toggle_theme(self) -> None:
        self._current_theme = "light" if self._current_theme == "dark" else "dark"
        app = QApplication.instance()
        apply_theme(app, self._current_theme)
        save_theme_pref(self._current_theme)
        self._theme_toggle.setChecked(self._current_theme == "dark")
        self._apply_toggle_colors()
        self._log_widget.set_theme(self._current_theme)

    def _on_theme_toggle(self, checked: bool) -> None:
        # 디자인의 테마 스위치는 켜짐=다크, 꺼짐=라이트.
        new_theme = "dark" if checked else "light"
        if new_theme == self._current_theme:
            return
        self._current_theme = new_theme
        app = QApplication.instance()
        apply_theme(app, self._current_theme)
        save_theme_pref(self._current_theme)
        self._apply_toggle_colors()
        self._log_widget.set_theme(self._current_theme)

    def _apply_toggle_colors(self) -> None:
        """토글 스위치(테마/업로드)의 트랙 색을 현재 테마에 맞게 갱신합니다."""
        t = _DARK if self._current_theme == "dark" else _LIGHT
        for toggle in (getattr(self, "_theme_toggle", None), getattr(self, "_upload_cb", None)):
            if toggle is not None:
                toggle.set_colors(t["border_strong"], t["accent"])

    def _setup_ui(self) -> None:
        self.setWindowTitle("YouTube 자동 생성 프로그램")
        self.setMinimumSize(QSize(1150, 760))
        self.resize(1480, 920)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 처음 창이 뜰 때 은은하게 페이드인 (showEvent에서 재생 — 자세한 내용은
        # showEvent 참고). central 위젯에 QGraphicsOpacityEffect를 걸면 그
        # 안의 카드들에 이미 걸려있는 QGraphicsDropShadowEffect와 중첩되면서
        # 렌더링이 깨지는(우측 컬럼 전체가 안 그려지는) 문제가 있어서, 위젯
        # 트리 안의 효과가 아니라 창(윈도우) 자체의 투명도를 애니메이션합니다.
        self.setWindowOpacity(0.0)
        self._fade_anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade_anim.setDuration(400)
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_played = False

        root.addWidget(self._build_top_bar())
        status_bar = self._build_status_bar()  # _res_combo populate 시 시그널이 바로 타므로 먼저 생성

        # ── 본문: 좌(대본 작성, 넓게) | 우(채널/업로드 설정 + 실행 카드, 고정폭) ──
        main_splitter = QSplitter(Qt.Horizontal)
        main_splitter.setChildrenCollapsible(False)
        main_splitter.setHandleWidth(1)

        main_splitter.addWidget(self._build_left_panel())
        right_column = self._build_right_column()
        main_splitter.addWidget(right_column)
        main_splitter.setStretchFactor(0, 1)
        main_splitter.setStretchFactor(1, 0)
        main_splitter.setSizes([1100, 380])

        # ── 로그 드로어 (기본은 접혀 있음, 우측 '로그 보기' 버튼으로 펼침) ──
        # LogWidget 내부 QTextEdit에 최소 높이가 걸려 있어 스플리터 크기만으로는
        # 완전히 접히지 않으므로, 아예 숨겨서 접는다 (QSplitter는 숨겨진 자식을
        # 공간을 차지하지 않는 것으로 취급합니다).
        self._v_splitter = QSplitter(Qt.Vertical)
        self._v_splitter.setChildrenCollapsible(False)
        self._v_splitter.addWidget(main_splitter)
        self._log_widget = LogWidget()
        self._v_splitter.addWidget(self._log_widget)
        self._log_widget.hide()
        self._log_drawer_anim = QPropertyAnimation(self._log_widget, b"maximumHeight", self)
        self._log_drawer_anim.setDuration(220)
        self._log_drawer_anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._log_drawer_anim.finished.connect(self._on_log_drawer_anim_finished)
        root.addWidget(self._v_splitter, 1)

        root.addWidget(status_bar)
        self._update_status_bar()
        self._apply_toggle_colors()

        self._refresh_channel_combo()

    def _build_top_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("topBar")
        bar.setFixedHeight(56)
        row = QHBoxLayout(bar)
        row.setContentsMargins(20, 0, 20, 0)
        row.setSpacing(10)

        logo = QLabel()
        logo.setPixmap(_make_app_icon().pixmap(28, 28))
        row.addWidget(logo)
        title = QLabel("YouTube 자동 생성 프로그램")
        title.setObjectName("appTitle")
        row.addWidget(title)
        row.addStretch()

        check_btn = QPushButton("시스템 점검")
        check_btn.setObjectName("topBarBtn")
        check_btn.setToolTip("시스템 점검")
        check_btn.clicked.connect(self._run_system_check_dialog)
        row.addWidget(check_btn)

        settings_btn = QPushButton("설정")
        settings_btn.setObjectName("topBarBtn")
        settings_btn.setToolTip("설정 (채널 관리 포함)")
        settings_btn.clicked.connect(self._open_settings)
        row.addWidget(settings_btn)

        self._theme_toggle = ToggleSwitch(44, 24)
        self._theme_toggle.setToolTip("다크/라이트 모드 전환")
        self._theme_toggle.setChecked(self._current_theme == "dark")
        self._theme_toggle.toggled.connect(self._on_theme_toggle)
        row.addWidget(self._theme_toggle)

        return bar

    def _build_left_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("leftPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        self._claude_panel = ClaudePanel(
            self._get_active_channel, self._resolve_input_dir,
            on_saved=lambda _name: self._refresh_all_pending(),
        )
        layout.addWidget(self._claude_panel, 1)
        return panel

    def _build_right_column(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setObjectName("rightColumn")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(340)
        scroll.setMaximumWidth(420)

        inner = QWidget()
        v = QVBoxLayout(inner)
        v.setContentsMargins(20, 20, 20, 20)
        v.setSpacing(16)

        v.addWidget(self._build_channel_settings_card())
        v.addWidget(self._build_upload_settings_card())

        # 실행 카드는 채널마다 하나씩, "이 채널 시작" 누를 때마다 여기 동적으로
        # 쌓입니다 (여러 채널을 동시에 돌릴 수 있게 — 채널 콤보를 실행 중에도
        # 잠그지 않습니다).
        self._run_cards: dict[str, ChannelCardWidget] = {}
        self._runs_container = QWidget()
        self._runs_layout = QVBoxLayout(self._runs_container)
        self._runs_layout.setContentsMargins(0, 0, 0, 0)
        self._runs_layout.setSpacing(16)
        v.addWidget(self._runs_container)

        self._log_toggle_btn = QPushButton("로그 보기")
        self._log_toggle_btn.setObjectName("fileBtn")
        self._log_toggle_btn.setCheckable(True)
        self._log_toggle_btn.setToolTip("실행 로그 보기/숨기기")
        self._log_toggle_btn.toggled.connect(self._toggle_log_drawer)
        v.addWidget(self._log_toggle_btn)

        v.addStretch()
        scroll.setWidget(inner)
        return scroll

    # ─────────────────────────────────────────
    # 실행 카드 관리 (채널별로 동시에 여러 개)
    # ─────────────────────────────────────────

    def _add_run_card(self) -> None:
        """"채널 설정" 카드의 "이 채널 시작" 버튼: 지금 선택된 채널의 실행
        카드를 찾아서(없으면 새로 만들어서) 바로 시작합니다. 다른 채널을
        골라 다시 누르면 카드가 하나 더 생겨서 여러 채널이 동시에 돌아갑니다."""
        if not self._system_ok:
            QMessageBox.warning(self, "실행 불가", "시스템 점검을 통과하지 못해 실행할 수 없습니다.")
            return

        chan = self._get_active_channel()
        key = chan.get("name", "")
        existing = self._run_cards.get(key)
        if existing is not None:
            if existing.is_running():
                self._set_status(f"'{self._title_for(chan)}' 이미 실행 중입니다.", ok=True)
                return
            existing.start_run()
            return

        card = ChannelCardWidget(
            chan,
            self._get_current_settings,
            self._claim_next_pending_file, self._release_claimed_file,
            self._pending_count_for,
            claim_shorts_sibling=self._claim_shorts_sibling,
        )
        card.set_runnable(self._system_ok)
        card.result_ready.connect(self._notify_run_result)
        card.closed.connect(lambda: self._remove_run_card(key))
        self._run_cards[key] = card
        self._runs_layout.addWidget(card)
        card.start_run()

    def _remove_run_card(self, key: str) -> None:
        card = self._run_cards.pop(key, None)
        if card is None:
            return
        self._runs_layout.removeWidget(card)
        card.deleteLater()

    @staticmethod
    def _title_for(channel: dict) -> str:
        return channel.get("name") or "기본 채널"

    def _build_channel_settings_card(self) -> QWidget:
        card = QWidget()
        card.setObjectName("card")
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(10)

        title = QLabel("채널 설정")
        title.setObjectName("cardTitle")
        v.addWidget(title)

        chan_label = QLabel("채널 선택")
        chan_label.setObjectName("fieldLabel")
        v.addWidget(chan_label)
        self._channel_combo = QComboBox()
        self._channel_combo.setToolTip("실행할 채널 선택")
        self._channel_combo.currentIndexChanged.connect(self._on_channel_selected)
        v.addWidget(self._channel_combo)

        res_label = QLabel("해상도 프리셋")
        res_label.setObjectName("fieldLabel")
        v.addWidget(res_label)
        self._res_combo = QComboBox()
        for label, (w, h) in RESOLUTION_PRESETS.items():
            self._res_combo.addItem(label, (w, h))
        self._res_combo.setToolTip("해상도")
        self._res_combo.currentIndexChanged.connect(self._update_status_bar)
        v.addWidget(self._res_combo)

        self._shorts_cb = QCheckBox("쇼츠도 함께 만들기")
        self._shorts_cb.setToolTip(
            "체크하면 위 해상도로 만드는 영상과 별도로, 세로 쇼츠용 영상도 같은 실행에서 함께 만듭니다.\n"
            "대기열에 이름이 같은 '_shorts.txt' 짝이 있으면 그걸 그대로 쓰고,\n"
            "없으면 방금 만든 영상 앞부분을 재사용해 짧게 잘라 만듭니다."
        )
        v.addWidget(self._shorts_cb)

        start_btn = QPushButton("▶  이 채널 시작")
        start_btn.setObjectName("btn_start")
        start_btn.setMinimumHeight(36)
        start_btn.setToolTip(
            "선택한 채널의 실행 카드를 아래에 추가하고 바로 시작합니다.\n"
            "다른 채널을 골라 다시 누르면 여러 채널을 동시에 돌릴 수 있습니다."
        )
        start_btn.clicked.connect(self._add_run_card)
        v.addWidget(start_btn)

        _apply_elevation(card)
        return card

    def _build_upload_settings_card(self) -> QWidget:
        card = QWidget()
        card.setObjectName("card")
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(10)

        title = QLabel("유튜브 업로드 설정")
        title.setObjectName("cardTitle")
        v.addWidget(title)

        upload_row = QHBoxLayout()
        upload_label = QLabel("업로드 여부")
        upload_row.addWidget(upload_label)
        upload_row.addStretch()
        self._upload_cb = ToggleSwitch(40, 22)
        self._upload_cb.setChecked(True)
        self._upload_cb.toggled.connect(self._on_upload_toggle)
        upload_row.addWidget(self._upload_cb)
        v.addLayout(upload_row)

        priv_label = QLabel("공개범위")
        priv_label.setObjectName("fieldLabel")
        v.addWidget(priv_label)
        self._privacy_combo = QComboBox()
        self._privacy_combo.addItems(["비공개 (private)", "공개 (public)", "미등록 (unlisted)"])
        v.addWidget(self._privacy_combo)

        days_label = QLabel("예약일")
        days_label.setObjectName("fieldLabel")
        v.addWidget(days_label)
        self._days_spin = QSpinBox()
        self._days_spin.setRange(0, 30)
        self._days_spin.setValue(0)
        self._days_spin.setPrefix("예약: ")
        self._days_spin.setSuffix(" 일 후")
        v.addWidget(self._days_spin)

        _apply_elevation(card)
        return card

    def _build_status_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("statusBarRow")
        bar.setFixedHeight(26)
        row = QHBoxLayout(bar)
        row.setContentsMargins(16, 0, 16, 0)
        row.setSpacing(16)

        self._status_dot = QLabel("●")
        row.addWidget(self._status_dot)
        self._status_text = QLabel("준비")
        self._status_text.setObjectName("statusText")
        row.addWidget(self._status_text)
        row.addStretch()
        self._status_res_label = QLabel("")
        self._status_res_label.setObjectName("statusText")
        row.addWidget(self._status_res_label)

        return bar

    def _set_status(self, text: str, ok: bool = True) -> None:
        self._status_text.setText(text)
        t = _DARK if self._current_theme == "dark" else _LIGHT
        self._status_dot.setStyleSheet(f"color:{t['success'] if ok else t['warning']}; font-size:9px;")

    def _update_status_bar(self) -> None:
        w, h = self._res_combo.currentData() or (0, 0)
        self._status_res_label.setText(f"{w}×{h}" if w and h else "")

    # ─────────────────────────────────────────
    # 로그 드로어
    # ─────────────────────────────────────────

    _LOG_DRAWER_HEIGHT = 260

    def _toggle_log_drawer(self, checked: bool) -> None:
        """로그 드로어를 즉시 보이기/숨기기 대신 높이를 부드럽게 펼치고 접습니다."""
        self._log_drawer_anim.stop()
        if checked:
            self._log_widget.setMaximumHeight(0)
            self._log_widget.show()
            self._log_drawer_anim.setStartValue(0)
            self._log_drawer_anim.setEndValue(self._LOG_DRAWER_HEIGHT)
        else:
            self._log_drawer_anim.setStartValue(self._log_widget.height())
            self._log_drawer_anim.setEndValue(0)
        self._log_drawer_anim.start()

    def _on_log_drawer_anim_finished(self) -> None:
        if self._log_toggle_btn.isChecked():
            # 애니메이션용으로 걸어둔 상한을 풀어서, 나중에 스플리터를 손으로
            # 늘렸을 때도 260px 이상으로 커질 수 있게 합니다.
            self._log_widget.setMaximumHeight(16777215)
        else:
            self._log_widget.hide()

    # ─────────────────────────────────────────
    # 업로드 토글
    # ─────────────────────────────────────────

    def _on_upload_toggle(self, checked: bool) -> None:
        self._privacy_combo.setEnabled(checked)
        self._days_spin.setEnabled(checked)

    def _resolve_input_dir(self, input_dir: str = "") -> Path:
        """채널의 input_dir 설정을 실제 경로로 변환합니다. 비어있으면 기본 input/ 폴더."""
        cfg = get_config()
        root = Path(__file__).parent.parent
        rel = input_dir or cfg.get("paths.input_dir", "input")
        path = root / rel
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _pending_count_for(self, input_dir: str = "") -> int:
        cfg = get_config()
        runner = BatchRunner(cfg, input_dir=self._resolve_input_dir(input_dir))
        return len(runner.get_pending_files())

    def _refresh_all_pending(self) -> None:
        for card in self._run_cards.values():
            card.refresh_pending()

    # ─────────────────────────────────────────
    # 채널별 input 폴더 자동 배분
    # ─────────────────────────────────────────

    def _claim_next_pending_file(self, input_dir: str = "") -> Optional[Path]:
        """채널의 대본 폴더에서 아직 어떤 카드도 처리 중이지 않은 파일을 하나 골라
        점유합니다. 점유 즉시 archive/<채널>/input_processing/ 으로 옮겨두므로,
        처리 도중 앱이 꺼지거나 죽어도 파일이 input/ 에 애매하게 남아있다가
        재시작 후 중복 처리되거나 유실되지 않습니다. 최종 결과는
        _release_claimed_file 에서 input_processed/input_failed 로 확정됩니다."""
        cfg = get_config()
        input_root_dir = self._resolve_input_dir(input_dir)
        runner = BatchRunner(cfg, input_dir=input_root_dir)
        root = Path(__file__).parent.parent
        archive_dir = root / cfg.get("paths.archive_dir", "archive")
        input_root = root / cfg.get("paths.input_dir", "input")

        for p in runner.get_pending_files():
            if str(p) in self._claimed_files:
                continue
            chan_name = p.parent.name if p.parent != input_root else ""
            processing_dir = archive_dir / chan_name / "input_processing" if chan_name else archive_dir / "input_processing"
            try:
                from utils.file_utils import safe_move
                claimed = safe_move(p, processing_dir / p.name)
            except Exception:
                logger.exception("대본 파일 점유(이동) 실패: %s", p)
                continue
            self._claimed_files.add(str(claimed))
            self._refresh_all_pending()
            return claimed
        return None

    def _release_claimed_file(self, path_str: str, success: bool) -> None:
        """점유한(=input_processing으로 옮겨둔) 파일을 완료 처리합니다:
        성공하면 input_processed, 실패하면 input_failed 로 옮기고 점유 해제합니다."""
        self._claimed_files.discard(path_str)
        sub = "input_processed" if success else "input_failed"

        # _claim_next_pending_file 에서 이미 archive/<채널>/input_processing/ 으로
        # 옮겨둔 상태이므로, 그 형제 디렉터리(input_processed/input_failed)로만
        # 옮기면 됩니다.
        story_path = Path(path_str)
        dest_dir = story_path.parent.parent / sub

        try:
            from utils.file_utils import safe_move
            safe_move(story_path, dest_dir / story_path.name)
        except Exception:
            logger.exception("자동 배분 파일 정리 실패: %s", path_str)
        self._refresh_all_pending()

    def _claim_shorts_sibling(self, long_path: Path, input_dir: str = "") -> Optional[Path]:
        """방금 점유한 '..._long.txt' 파일과 접두사가 같은 '..._shorts.txt' 파일이
        대기열에 있으면 그것도 같이 점유합니다 (콤보 프롬프트로 롱폼+쇼츠를 한 번에
        받았을 때, 쇼츠를 롱폼에서 잘라내는 대신 Claude가 따로 써준 진짜 쇼츠
        대본으로 만들기 위함). 짝이 없으면 None — 이 경우 호출자가 기존처럼
        롱폼 씬을 재사용하는 방식으로 대체합니다."""
        stem = long_path.stem
        if not stem.endswith("_long"):
            return None
        shorts_name = stem[: -len("_long")] + "_shorts" + long_path.suffix
        input_root_dir = self._resolve_input_dir(input_dir)
        candidate = input_root_dir / shorts_name
        if not candidate.exists() or str(candidate) in self._claimed_files:
            return None

        cfg = get_config()
        root = Path(__file__).parent.parent
        archive_dir = root / cfg.get("paths.archive_dir", "archive")
        input_root = root / cfg.get("paths.input_dir", "input")
        chan_name = candidate.parent.name if candidate.parent != input_root else ""
        processing_dir = archive_dir / chan_name / "input_processing" if chan_name else archive_dir / "input_processing"
        try:
            from utils.file_utils import safe_move
            claimed = safe_move(candidate, processing_dir / candidate.name)
        except Exception:
            logger.exception("짝 쇼츠 대본 점유(이동) 실패: %s", candidate)
            return None
        self._claimed_files.add(str(claimed))
        self._refresh_all_pending()
        return claimed

    # ─────────────────────────────────────────
    # 시스템 점검
    # ─────────────────────────────────────────

    def _run_system_check(self) -> None:
        cfg = get_config()
        checker = SystemChecker(cfg.all())
        report = checker.run()
        for item in report.items:
            level = logging.INFO if item.ok else (logging.ERROR if item.critical else logging.WARNING)
            self._log_widget.append_log(level, f"[시스템] {item.name}: {item.message}")
        self._system_ok = report.can_run
        for card in self._run_cards.values():
            card.set_runnable(self._system_ok)
        if not self._system_ok:
            self._set_status("시스템 요구사항 미충족", ok=False)
        else:
            self._set_status("시스템 점검 완료", ok=True)

    def _run_system_check_dialog(self) -> None:
        cfg = get_config()
        checker = SystemChecker(cfg.all())
        report = checker.run()
        QMessageBox.information(self, "시스템 점검", report.summary())

    # ─────────────────────────────────────────
    # 실행 설정 / 채널
    # ─────────────────────────────────────────

    def _get_current_settings(self):
        """실행 설정 바의 현재 값 (upload, privacy, days, width, height, also_shorts). 채널 카드들이 공유해서 씁니다."""
        privacy_map = {
            "비공개 (private)": "private",
            "공개 (public)": "public",
            "미등록 (unlisted)": "unlisted",
        }
        privacy = privacy_map.get(self._privacy_combo.currentText(), "private")
        days    = self._days_spin.value()
        upload  = self._upload_cb.isChecked()
        w, h    = self._res_combo.currentData() or (1920, 1080)
        also_shorts = self._shorts_cb.isChecked()
        return upload, privacy, days, w, h, also_shorts

    def _open_settings(self) -> None:
        from gui.settings_dialog import SettingsDialog
        dlg = SettingsDialog(self)
        dlg.exec()
        self._refresh_channel_combo()

    def _get_channels(self) -> list:
        cfg = get_config()
        return list(cfg.get("youtube.channels", []) or [])

    def _get_active_channel(self) -> dict:
        return self._channel_combo.currentData() or {"name": "", "credentials_file": "", "input_dir": ""}

    def _refresh_channel_combo(self) -> None:
        """설정의 채널 목록으로 상단 채널 선택 콤보를 채웁니다. '기본 채널'은 항상 존재합니다."""
        is_initial_load = self._channel_combo.count() == 0
        current = self._channel_combo.currentData()["name"] if not is_initial_load else None

        self._channel_combo.blockSignals(True)
        self._channel_combo.clear()
        self._channel_combo.addItem("기본 채널", {"name": "", "credentials_file": "", "input_dir": ""})
        for c in self._get_channels():
            self._channel_combo.addItem(c.get("name", ""), {
                "name": c.get("name", ""),
                "credentials_file": c.get("credentials_file", ""),
                "input_dir": c.get("input_dir", ""),
            })

        if is_initial_load:
            # 앱을 처음 켤 때는 '기본 채널'이 아니라, 마지막으로 쓰던 채널
            # (기록이 없으면 등록된 첫 번째 실제 채널)부터 시작합니다.
            current = load_last_channel_pref()
            if not current and self._channel_combo.count() > 1:
                current = self._channel_combo.itemData(1)["name"]

        if current:
            for i in range(self._channel_combo.count()):
                if self._channel_combo.itemData(i)["name"] == current:
                    self._channel_combo.setCurrentIndex(i)
                    break
        self._channel_combo.blockSignals(False)
        self._on_channel_selected()

    def _on_channel_selected(self) -> None:
        # 채널 콤보는 이제 특정 카드에 매인 게 아니라 "다음에 [이 채널 시작]을
        # 누르면 어느 채널을 쓸지" 고르는 용도라, 마지막 선택 기억 외에는 할
        # 일이 없습니다 (각 채널의 실행 카드는 _add_run_card()가 알아서 관리).
        chan = self._get_active_channel()
        save_last_channel_pref(chan.get("name", ""))


# ─────────────────────────────────────────────
# 앱 실행
# ─────────────────────────────────────────────

def run_app() -> None:
    app = QApplication(sys.argv)
    # 지금 UI는 네이티브 Windows 느낌을 노리는 게 아니라 카드형 다크/라이트
    # 커스텀 디자인(색상·토글 스위치 등을 QSS로 직접 그림)이라, 네이티브
    # windows11 스타일 위에 QSS를 얹으면 일부 컨트롤(인디케이터 등)이 OS
    # 테마와 충돌해 예측이 어려움. Fusion은 QSS를 그대로, 일관되게 반영합니다.
    app.setStyle("Fusion")
    apply_theme(app, load_theme_pref())
    window = MainWindow()
    window.show()
    sys.exit(app.exec())