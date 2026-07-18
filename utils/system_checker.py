"""
utils/system_checker.py
=======================
파이프라인 실행 전 시스템 요구사항을 점검합니다.
FFmpeg, FFprobe, Python 패키지 등을 확인하고
결과를 CheckReport 로 반환합니다.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import List, Optional

from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class CheckItem:
    name: str
    ok: bool
    message: str
    critical: bool = True   # False 면 경고만, True 면 실행 불가


@dataclass
class CheckReport:
    items: List[CheckItem] = field(default_factory=list)

    @property
    def can_run(self) -> bool:
        return all(item.ok for item in self.items if item.critical)

    @property
    def warnings(self) -> List[CheckItem]:
        return [i for i in self.items if not i.ok and not i.critical]

    @property
    def errors(self) -> List[CheckItem]:
        return [i for i in self.items if not i.ok and i.critical]

    def summary(self) -> str:
        lines = []
        for item in self.items:
            tag = "OK" if item.ok else ("경고" if not item.critical else "실패")
            lines.append(f"[{tag}] {item.name}: {item.message}")
        return "\n".join(lines)


class SystemChecker:
    """
    사용 예:
        checker = SystemChecker(config)
        report = checker.run()
        if not report.can_run:
            print(report.summary())
            sys.exit(1)
    """

    _REQUIRED_PACKAGES = [
        "edge_tts",
        "PIL",          # Pillow
        "PySide6",
        "google.auth",
        "googleapiclient",
        "pytz",
        "tenacity",
    ]

    def __init__(self, config: Optional[dict] = None):
        self._cfg = config or {}

    def run(self) -> CheckReport:
        report = CheckReport()
        report.items += [
            self._check_python_version(),
            self._check_ffmpeg(),
            self._check_ffprobe(),
            self._check_packages(),
            self._check_client_secrets(),
        ]
        logger.info("System check complete:\n%s", report.summary())
        return report

    # ─────────────────────────────────────────
    # 개별 점검
    # ─────────────────────────────────────────

    def _check_python_version(self) -> CheckItem:
        major, minor = sys.version_info[:2]
        ok = major == 3 and minor >= 10
        return CheckItem(
            name="Python 버전",
            ok=ok,
            message=f"Python {major}.{minor} (3.10+ 필요)",
            critical=True,
        )

    def _check_ffmpeg(self) -> CheckItem:
        ffmpeg_path = self._cfg.get("video", {}).get("ffmpeg_path", "ffmpeg")
        found = shutil.which(ffmpeg_path)
        if found:
            try:
                result = subprocess.run(
                    [ffmpeg_path, "-version"],
                    capture_output=True, text=True, timeout=5,
                    encoding="utf-8", errors="replace",
                )
                ver_line = result.stdout.splitlines()[0] if result.stdout else "unknown"
                return CheckItem(name="FFmpeg", ok=True, message=ver_line[:60], critical=True)
            except Exception as e:
                return CheckItem(name="FFmpeg", ok=False, message=str(e), critical=True)
        return CheckItem(
            name="FFmpeg",
            ok=False,
            message=f"'{ffmpeg_path}' 를 찾을 수 없습니다. PATH에 추가하거나 config에서 경로를 지정하세요.",
            critical=True,
        )

    def _check_ffprobe(self) -> CheckItem:
        ffprobe_path = self._cfg.get("video", {}).get("ffprobe_path", "ffprobe")
        found = shutil.which(ffprobe_path)
        if found:
            return CheckItem(name="FFprobe", ok=True, message=found, critical=True)
        return CheckItem(
            name="FFprobe",
            ok=False,
            message=f"'{ffprobe_path}' 를 찾을 수 없습니다.",
            critical=True,
        )

    def _check_packages(self) -> CheckItem:
        missing = []
        for pkg in self._REQUIRED_PACKAGES:
            try:
                importlib.import_module(pkg)
            except ImportError:
                missing.append(pkg)
        if missing:
            return CheckItem(
                name="Python 패키지",
                ok=False,
                message=f"누락: {', '.join(missing)}. pip install -r requirements.txt 실행",
                critical=True,
            )
        return CheckItem(name="Python 패키지", ok=True, message="모두 설치됨", critical=True)

    def _check_client_secrets(self) -> CheckItem:
        import os
        secrets = self._cfg.get("youtube", {}).get(
            "client_secrets_file", "config/client_secrets.json"
        )
        from pathlib import Path
        root = Path(__file__).parent.parent
        path = root / secrets
        if path.exists():
            return CheckItem(
                name="YouTube client_secrets.json",
                ok=True,
                message=str(path),
                critical=False,
            )
        return CheckItem(
            name="YouTube client_secrets.json",
            ok=False,
            message=f"{path} 없음 — YouTube 업로드가 비활성화됩니다.",
            critical=False,
        )
