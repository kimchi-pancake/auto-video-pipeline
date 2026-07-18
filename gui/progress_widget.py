"""
gui/progress_widget.py
======================
재사용 가능한 진행률 위젯.
단계 이름, 전체 프로그레스바, 현재 단계 프로그레스바, 상세 메시지를 표시합니다.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QGroupBox, QLabel, QProgressBar, QVBoxLayout, QWidget,
)

from core.pipeline import PipelineProgress, STAGES


class ProgressWidget(QWidget):
    """
    파이프라인 진행 상태를 시각화합니다.

    사용 예:
        pw = ProgressWidget()
        pw.update_progress(pipeline_progress)
        pw.reset()
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        group = QGroupBox("진행 상태")
        inner = QVBoxLayout(group)
        inner.setSpacing(6)

        # 단계 레이블
        self._stage_label = QLabel("대기 중...")
        self._stage_label.setAlignment(Qt.AlignCenter)
        font = QFont()
        font.setPointSize(13)
        font.setBold(True)
        self._stage_label.setFont(font)

        # 전체 프로그레스바
        self._overall_bar = QProgressBar()
        self._overall_bar.setRange(0, 100)
        self._overall_bar.setValue(0)
        self._overall_bar.setFormat("전체 진행률: %p%")
        self._overall_bar.setMinimumHeight(24)

        # 단계 내 프로그레스바
        self._step_bar = QProgressBar()
        self._step_bar.setRange(0, 100)
        self._step_bar.setValue(0)
        self._step_bar.setFormat("현재 단계: %p%")
        self._step_bar.setMinimumHeight(20)

        # 상세 메시지
        self._detail_label = QLabel("")
        self._detail_label.setAlignment(Qt.AlignCenter)
        self._detail_label.setStyleSheet("color: #888; font-size: 11px;")
        self._detail_label.setWordWrap(True)

        # 단계 인디케이터
        self._stage_indicator = QLabel("")
        self._stage_indicator.setAlignment(Qt.AlignCenter)
        self._stage_indicator.setStyleSheet("color: #666; font-size: 10px;")

        inner.addWidget(self._stage_label)
        inner.addWidget(self._overall_bar)
        inner.addWidget(self._step_bar)
        inner.addWidget(self._detail_label)
        inner.addWidget(self._stage_indicator)

        layout.addWidget(group)

    # ─────────────────────────────────────────
    # 공개 API
    # ─────────────────────────────────────────

    def update_progress(self, p: PipelineProgress) -> None:
        """PipelineProgress 객체로 위젯을 업데이트합니다."""
        self._stage_label.setText(f"[{p.stage_index + 1}/{p.stage_total}]  {p.stage}")
        self._overall_bar.setValue(int(p.overall_pct))
        step_pct = int((p.step / max(p.step_total, 1)) * 100)
        self._step_bar.setValue(step_pct)
        self._detail_label.setText(p.message)
        # 단계 인디케이터 업데이트
        indicator_parts = []
        for i, stage_name in enumerate(STAGES):
            if i < p.stage_index:
                indicator_parts.append(f"[완료] {stage_name}")
            elif i == p.stage_index:
                indicator_parts.append(f"[진행] {stage_name}")
            else:
                indicator_parts.append(f"[대기] {stage_name}")
        self._stage_indicator.setText("  →  ".join(indicator_parts))

    def set_complete(self) -> None:
        """모든 단계 완료 상태로 설정합니다."""
        self._stage_label.setText("완료!")
        self._overall_bar.setValue(100)
        self._step_bar.setValue(100)
        self._detail_label.setText("모든 작업이 완료되었습니다.")

    def set_failed(self, error: str = "") -> None:
        """실패 상태로 설정합니다."""
        self._stage_label.setText("실패")
        self._detail_label.setText(error[:120] if error else "오류가 발생했습니다.")

    def reset(self) -> None:
        """위젯을 초기 상태로 되돌립니다."""
        self._stage_label.setText("대기 중...")
        self._overall_bar.setValue(0)
        self._step_bar.setValue(0)
        self._detail_label.setText("")
        self._stage_indicator.setText("")
