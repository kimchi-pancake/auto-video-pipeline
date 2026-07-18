"""
gui/log_widget.py
=================
독립형 로그 뷰어 위젯.
색상 코딩, 자동 스크롤, 필터링, 내보내기를 지원합니다.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QTextCursor, QFont
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QGroupBox,
    QHBoxLayout, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)


_DARK_LEVEL_COLORS = {
    logging.DEBUG:    "#8b8b9e",
    logging.INFO:     "#9cdcfe",
    logging.WARNING:  "#ffd700",
    logging.ERROR:    "#f47d7d",
    logging.CRITICAL: "#ff6b6b",
}
_DARK_TIMESTAMP_COLOR = "#8a8a8a"

# 다크 테마 기준으로 골랐던 색을 그대로 흰 배경에 쓰면 파스텔톤이 거의 안
# 보여서, 라이트 테마에서는 더 진하고 채도 낮은 색으로 따로 씁니다.
_LIGHT_LEVEL_COLORS = {
    logging.DEBUG:    "#6b7280",
    logging.INFO:     "#0969da",
    logging.WARNING:  "#9a6700",
    logging.ERROR:    "#cf222e",
    logging.CRITICAL: "#a40e26",
}
_LIGHT_TIMESTAMP_COLOR = "#6e7781"

_LEVEL_NAMES = {
    logging.DEBUG:    "DEBUG",
    logging.INFO:     "INFO",
    logging.WARNING:  "WARN",
    logging.ERROR:    "ERROR",
    logging.CRITICAL: "CRIT",
}


class LogWidget(QWidget):
    """
    색상 코딩된 로그 뷰어 위젯.

    사용 예:
        lw = LogWidget()
        lw.append_log(logging.INFO, "작업 시작")
        lw.append_log(logging.ERROR, "오류 발생")
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._buffer: list[tuple[int, str, str]] = []   # (level, timestamp, safe_message)
        self._min_level = logging.DEBUG
        self._auto_scroll = True
        self._level_colors = _LIGHT_LEVEL_COLORS
        self._timestamp_color = _LIGHT_TIMESTAMP_COLOR
        self._setup_ui()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        group = QGroupBox("로그")
        inner = QVBoxLayout(group)
        inner.setContentsMargins(6, 6, 6, 6)
        inner.setSpacing(4)

        # 툴바
        toolbar = QHBoxLayout()

        self._level_combo = QComboBox()
        self._level_combo.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self._level_combo.setFixedWidth(90)
        self._level_combo.currentTextChanged.connect(self._on_level_changed)

        self._auto_scroll_cb = QCheckBox("자동 스크롤")
        self._auto_scroll_cb.setChecked(True)
        self._auto_scroll_cb.toggled.connect(lambda v: setattr(self, "_auto_scroll", v))

        clear_btn = QPushButton("지우기")
        clear_btn.setFixedWidth(70)
        clear_btn.clicked.connect(self.clear)

        export_btn = QPushButton("저장")
        export_btn.setFixedWidth(60)
        export_btn.clicked.connect(self._export)

        toolbar.addWidget(self._level_combo)
        toolbar.addWidget(self._auto_scroll_cb)
        toolbar.addStretch()
        toolbar.addWidget(export_btn)
        toolbar.addWidget(clear_btn)

        # 로그 뷰
        self._view = QTextEdit()
        self._view.setReadOnly(True)
        self._view.setMinimumHeight(180)
        font = QFont("Consolas", 11)
        font.setStyleHint(QFont.Monospace)
        self._view.setFont(font)

        inner.addLayout(toolbar)
        inner.addWidget(self._view)
        root.addWidget(group)

    # ─────────────────────────────────────────
    # 공개 API
    # ─────────────────────────────────────────

    def append_log(self, level: int, message: str) -> None:
        """로그 메시지를 추가합니다."""
        ts = datetime.now().strftime("%H:%M:%S")
        safe = (
            message
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        self._buffer.append((level, ts, safe))

        if level >= self._min_level:
            self._view.append(self._render_line(level, ts, safe))
            if self._auto_scroll:
                self._scroll_to_bottom()

    def clear(self) -> None:
        self._view.clear()
        self._buffer.clear()

    def set_min_level(self, level: int) -> None:
        self._min_level = level
        self._redraw()

    def set_theme(self, theme: str) -> None:
        """다크/라이트 배경에 맞춰 로그 글자색을 바꿉니다 (안 바꾸면 한쪽 배경에서 잘 안 보임)."""
        if theme == "dark":
            self._level_colors = _DARK_LEVEL_COLORS
            self._timestamp_color = _DARK_TIMESTAMP_COLOR
        else:
            self._level_colors = _LIGHT_LEVEL_COLORS
            self._timestamp_color = _LIGHT_TIMESTAMP_COLOR
        self._redraw()

    # ─────────────────────────────────────────
    # 내부
    # ─────────────────────────────────────────

    def _render_line(self, level: int, ts: str, safe: str) -> str:
        color = self._level_colors.get(level, "#888")
        level_name = _LEVEL_NAMES.get(level, "????")
        return (
            f'<span style="color:{self._timestamp_color}">{ts}</span> '
            f'<span style="color:{color};font-weight:bold">[{level_name}]</span> '
            f'<span style="color:{color}">{safe}</span>'
        )

    def _scroll_to_bottom(self) -> None:
        cursor = self._view.textCursor()
        cursor.movePosition(QTextCursor.End)
        self._view.setTextCursor(cursor)

    def _on_level_changed(self, text: str) -> None:
        level_map = {
            "DEBUG": logging.DEBUG,
            "INFO": logging.INFO,
            "WARNING": logging.WARNING,
            "ERROR": logging.ERROR,
        }
        self._min_level = level_map.get(text, logging.DEBUG)
        self._redraw()

    def _redraw(self) -> None:
        self._view.clear()
        for level, ts, safe in self._buffer:
            if level >= self._min_level:
                self._view.append(self._render_line(level, ts, safe))
        if self._auto_scroll:
            self._scroll_to_bottom()

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "로그 저장", f"log_{datetime.now():%Y%m%d_%H%M%S}.txt",
            "Text Files (*.txt)"
        )
        if not path:
            return
        lines = [
            f"{ts} [{_LEVEL_NAMES.get(lvl,'?')}] {msg}"
            for lvl, ts, msg in self._buffer
        ]
        Path(path).write_text("\n".join(lines), encoding="utf-8")
