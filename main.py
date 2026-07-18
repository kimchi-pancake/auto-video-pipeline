"""
main.py
=======
Auto Video Pipeline 진입점.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# QtWebEngine(내장 Gemini 브라우저)이 자동화 브라우저로 보이지 않도록.
# 이 플래그가 없으면 navigator.webdriver 가 true 로 노출돼 구글 로그인이
# "이 브라우저는 안전하지 않을 수 있습니다"로 차단됩니다. QApplication이
# 생성되기 전, Qt 관련 모듈을 import 하기 전에 설정해야 적용됩니다.
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-blink-features=AutomationControlled")

# 프로젝트 루트를 sys.path에 추가
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from utils.logger import setup_logging
from utils.config_manager import get_config


def main() -> None:
    cfg = get_config()
    log_dir = ROOT / cfg.get("paths.logs_dir", "logs")
    setup_logging(
        log_dir=log_dir,
        level=cfg.get("logging.level", "DEBUG"),
        max_bytes=cfg.get("logging.max_file_size", 10 * 1024 * 1024),
        backup_count=cfg.get("logging.backup_count", 5),
    )

    from gui.main_window import run_app
    run_app()


if __name__ == "__main__":
    main()
