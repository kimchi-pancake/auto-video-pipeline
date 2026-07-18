"""
utils/logger.py
===============
중앙 집중식 로깅 시스템.
파일 + 콘솔 + GUI 핸들러를 모두 지원합니다.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import colorlog
    _HAS_COLORLOG = True
except ImportError:
    _HAS_COLORLOG = False


# ─────────────────────────────────────────────
# GUI 핸들러 (PySide6 Signal 없이도 동작)
# ─────────────────────────────────────────────
class GUILogHandler(logging.Handler):
    """GUI 로그창으로 메시지를 전달하는 핸들러."""

    def __init__(self, callback=None):
        super().__init__()
        self._callback = callback

    def set_callback(self, callback):
        self._callback = callback

    def emit(self, record: logging.LogRecord):
        if self._callback:
            try:
                msg = self.format(record)
                self._callback(record.levelno, msg)
            except Exception:
                self.handleError(record)


# ─────────────────────────────────────────────
# 싱글톤 핸들러 저장소
# ─────────────────────────────────────────────
_gui_handler: Optional[GUILogHandler] = None
_initialized: bool = False


def setup_logging(
    log_dir: str | Path = "logs",
    level: str = "DEBUG",
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
    fmt: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    date_fmt: str = "%Y-%m-%d %H:%M:%S",
) -> None:
    """
    애플리케이션 전체 로깅을 초기화합니다.

    Args:
        log_dir: 로그 파일 저장 디렉터리
        level: 로깅 레벨 문자열 (DEBUG/INFO/WARNING/ERROR/CRITICAL)
        max_bytes: 로테이션 파일 최대 크기 (bytes)
        backup_count: 보관할 백업 파일 수
        fmt: 로그 포맷 문자열
        date_fmt: 날짜 포맷 문자열
    """
    global _gui_handler, _initialized

    if _initialized:
        return

    # 콘솔 인코딩이 UTF-8이 아닌 환경(cp949 등)에서 이모지/특수문자 출력 시
    # UnicodeEncodeError 로 로깅이 깨지는 것을 방지
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    numeric_level = getattr(logging, level.upper(), logging.DEBUG)
    root = logging.getLogger()
    root.setLevel(numeric_level)

    # 기존 핸들러 제거
    root.handlers.clear()

    # ── 파일 핸들러 (로테이션) ──────────────────
    today = datetime.now().strftime("%Y%m%d")
    log_file = log_dir / f"pipeline_{today}.log"
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(logging.Formatter(fmt, datefmt=date_fmt))
    root.addHandler(file_handler)

    # 에러 전용 파일 핸들러
    error_file = log_dir / f"error_{today}.log"
    error_handler = logging.handlers.RotatingFileHandler(
        error_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(logging.Formatter(fmt, datefmt=date_fmt))
    root.addHandler(error_handler)

    # ── 콘솔 핸들러 ────────────────────────────
    if _HAS_COLORLOG:
        console_fmt = colorlog.ColoredFormatter(
            "%(log_color)s%(asctime)s [%(levelname)s]%(reset)s %(name)s: %(message)s",
            datefmt=date_fmt,
            log_colors={
                "DEBUG": "cyan",
                "INFO": "green",
                "WARNING": "yellow",
                "ERROR": "red",
                "CRITICAL": "bold_red",
            },
        )
    else:
        console_fmt = logging.Formatter(fmt, datefmt=date_fmt)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(console_fmt)
    root.addHandler(console_handler)

    # ── GUI 핸들러 (콜백 미설정 상태로 등록) ───
    _gui_handler = GUILogHandler()
    _gui_handler.setLevel(numeric_level)
    _gui_handler.setFormatter(logging.Formatter(fmt, datefmt=date_fmt))
    root.addHandler(_gui_handler)

    _initialized = True

    logging.getLogger(__name__).debug(
        "Logging initialized → %s  (level=%s)", log_file, level
    )


def set_gui_log_callback(callback) -> None:
    """GUI 로그창 콜백을 런타임에 등록합니다."""
    global _gui_handler
    if _gui_handler is not None:
        _gui_handler.set_callback(callback)


def get_logger(name: str) -> logging.Logger:
    """모듈별 로거를 반환합니다."""
    return logging.getLogger(name)
