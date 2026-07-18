"""
scripts/oci_a1_monitor_gui.py
==============================
scripts/oci_a1_retry.py(작업 스케줄러가 10분마다 돌리는 A1 재시도 스크립트)의
상태를 보여주는 아주 간단한 창. 5초마다 자동 새로고침하고, "지금 재시도" 버튼으로
스케줄을 안 기다리고 바로 한 번 더 시도해볼 수 있습니다.

실행: python scripts/oci_a1_monitor_gui.py
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QObject, QThread, QTimer, Signal
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QPushButton, QVBoxLayout, QWidget

# 성공했을 때 창 배경을 이 두 색 사이로 깜빡이게 합니다.
_FLASH_ON_COLOR = "#2ecc71"   # 초록
_FLASH_OFF_COLOR = "#1e1f22"  # 원래 배경(진회색)


def _flash_taskbar(hwnd: int, count: int = 20) -> None:
    """작업표시줄 아이콘을 깜빡여서 눈길을 끕니다 (Windows 네이티브 FlashWindowEx)."""
    class FLASHWINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_uint),
            ("hwnd", ctypes.c_void_p),
            ("dwFlags", ctypes.c_uint),
            ("uCount", ctypes.c_uint),
            ("dwTimeout", ctypes.c_uint),
        ]

    FLASHW_ALL = 3       # 캡션 + 트레이 아이콘 둘 다
    FLASHW_TIMERNOFG = 12  # 사용자가 창 클릭할 때까지 계속

    try:
        info = FLASHWINFO(
            ctypes.sizeof(FLASHWINFO), hwnd, FLASHW_ALL | FLASHW_TIMERNOFG, count, 0
        )
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        pass

from scripts.oci_a1_retry import STATE_PATH, _load_state, main as run_retry_once


class _RetryWorker(QObject):
    finished = Signal()

    def run(self) -> None:
        try:
            run_retry_once()
        except Exception:
            pass
        self.finished.emit()


class MonitorWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Oracle A1 재시도 모니터")
        self.resize(360, 220)

        self._thread: QThread | None = None
        self._worker: _RetryWorker | None = None
        self._already_flashed = False
        self._blink_on = False
        self._blink_timer = QTimer(self)
        self._blink_timer.timeout.connect(self._toggle_blink)

        central = QWidget()
        self._central = central
        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

        title = QLabel("Ampere A1 자동 재시도")
        title.setStyleSheet("font-size: 16px; font-weight: bold;")
        layout.addWidget(title)

        self._status_label = QLabel("...")
        self._status_label.setStyleSheet("font-size: 14px;")
        layout.addWidget(self._status_label)

        self._attempts_label = QLabel("...")
        layout.addWidget(self._attempts_label)

        self._last_attempt_label = QLabel("...")
        layout.addWidget(self._last_attempt_label)

        self._retry_btn = QPushButton("지금 바로 재시도")
        self._retry_btn.clicked.connect(self._retry_now)
        layout.addWidget(self._retry_btn)

        layout.addStretch()
        self.setCentralWidget(central)

        self._refresh()
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._refresh)
        self._poll_timer.start(5000)

    def _refresh(self) -> None:
        state = _load_state()
        status = state.get("status", "waiting")
        status_text = {
            "waiting": "🟡 대기 중 (자리 없음, 계속 재시도)",
            "succeeded": "🟢 성공! 서버 확보됨",
            "error": "🔴 오류 발생",
        }.get(status, status)
        self._status_label.setText(status_text)
        self._attempts_label.setText(f"시도 횟수: {state.get('attempts', 0)}회")
        self._last_attempt_label.setText(f"마지막 시도: {state.get('last_attempt') or '아직 없음'}")

        if status == "succeeded":
            self._retry_btn.setEnabled(False)
            self._retry_btn.setText("완료됨")
            if not self._already_flashed:
                self._already_flashed = True
                self._celebrate()
        elif state.get("last_error"):
            self._last_attempt_label.setText(
                self._last_attempt_label.text() + f"\n오류: {state['last_error'][:80]}"
            )

    def _celebrate(self) -> None:
        """성공했을 때: 작업표시줄 깜빡임 + 창 배경 초록색으로 반짝반짝."""
        _flash_taskbar(int(self.winId()))
        self._blink_timer.start(500)

    def _toggle_blink(self) -> None:
        self._blink_on = not self._blink_on
        color = _FLASH_ON_COLOR if self._blink_on else _FLASH_OFF_COLOR
        self._central.setStyleSheet(f"background-color: {color};")

    def _stop_blink(self) -> None:
        if self._blink_timer.isActive():
            self._blink_timer.stop()
            self._central.setStyleSheet("")

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._stop_blink()
        super().mousePressEvent(event)

    def _retry_now(self) -> None:
        if self._thread is not None:
            return
        self._retry_btn.setEnabled(False)
        self._retry_btn.setText("시도 중...")

        self._worker = _RetryWorker()
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._on_retry_done)
        self._thread.start()

    def _on_retry_done(self) -> None:
        self._thread = None
        self._worker = None
        self._retry_btn.setEnabled(True)
        self._retry_btn.setText("지금 바로 재시도")
        self._refresh()


def main() -> None:
    app = QApplication(sys.argv)
    win = MonitorWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
