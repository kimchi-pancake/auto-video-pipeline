"""
utils/config_manager.py
=======================
설정 파일(config.json)을 로드·저장·병합하는 매니저.
default_config.json 을 기반으로 사용자 설정을 덮어씁니다.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from utils.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config" / "default_config.json"
_USER_CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.json"


def _deep_merge(base: dict, override: dict) -> dict:
    """base 딕셔너리에 override를 재귀적으로 병합합니다."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class ConfigManager:
    """
    설정 관리 클래스.

    사용 예:
        cfg = ConfigManager()
        key = cfg.get("image.pixabay_api_key")
        cfg.set("image.pixabay_api_key", "your_key")
        cfg.save()
    """

    def __init__(
        self,
        default_path: str | Path = _DEFAULT_CONFIG_PATH,
        user_path: str | Path = _USER_CONFIG_PATH,
    ):
        self._default_path = Path(default_path)
        self._user_path = Path(user_path)
        self._config: dict = {}
        self.load()

    # ─────────────────────────────────────────
    # 공개 API
    # ─────────────────────────────────────────

    def load(self) -> None:
        """default → user 순으로 병합하여 설정을 로드합니다."""
        default = self._load_json(self._default_path)
        user = self._load_json(self._user_path) if self._user_path.exists() else {}
        self._config = _deep_merge(default, user)
        logger.debug("Config loaded. user_config=%s", self._user_path.exists())

    def save(self, path: str | Path | None = None) -> None:
        """현재 설정을 JSON 파일로 저장합니다."""
        target = Path(path) if path else self._user_path
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            json.dump(self._config, f, ensure_ascii=False, indent=2)
        logger.info("Config saved → %s", target)

    def get(self, key: str, default: Any = None) -> Any:
        """
        점(.) 구분 키로 값을 가져옵니다.

        예: cfg.get("tts.default_voice")
        """
        parts = key.split(".")
        node = self._config
        for part in parts:
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def set(self, key: str, value: Any) -> None:
        """
        점(.) 구분 키로 값을 설정합니다.

        예: cfg.set("video.gpu_encoding", True)
        """
        parts = key.split(".")
        node = self._config
        for part in parts[:-1]:
            if part not in node or not isinstance(node[part], dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = value

    def section(self, name: str) -> dict:
        """섹션 딕셔너리를 반환합니다. 없으면 빈 딕셔너리."""
        return copy.deepcopy(self._config.get(name, {}))

    def all(self) -> dict:
        """전체 설정 딕셔너리의 깊은 복사본을 반환합니다."""
        return copy.deepcopy(self._config)

    def update_section(self, name: str, values: dict) -> None:
        """특정 섹션을 딕셔너리로 업데이트합니다."""
        if name not in self._config:
            self._config[name] = {}
        self._config[name] = _deep_merge(self._config[name], values)

    # ─────────────────────────────────────────
    # 내부 헬퍼
    # ─────────────────────────────────────────

    @staticmethod
    def _load_json(path: Path) -> dict:
        if not path.exists():
            logger.warning("Config file not found: %s", path)
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            logger.error("JSON parse error in %s: %s", path, e)
            return {}


# ─────────────────────────────────────────────
# 전역 싱글톤
# ─────────────────────────────────────────────
_instance: ConfigManager | None = None


def get_config() -> ConfigManager:
    """애플리케이션 전역 ConfigManager 싱글톤을 반환합니다."""
    global _instance
    if _instance is None:
        _instance = ConfigManager()
    return _instance
