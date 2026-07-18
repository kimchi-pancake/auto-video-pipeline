"""
image/pixabay_client.py
=======================
Pixabay API를 사용해 씬 프롬프트로 이미지를 자동 검색·다운로드합니다.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import List, Optional
from urllib import request as urllib_request
from urllib.parse import urlencode

import json

from utils.logger import get_logger

logger = get_logger(__name__)

PIXABAY_API_URL = "https://pixabay.com/api/"


class PixabayClient:
    """
    Pixabay API 클라이언트.

    사용 예:
        client = PixabayClient(api_key="your_key")
        path = client.download_for_prompt("beautiful forest morning", save_dir="temp/images")
    """

    def __init__(
        self,
        api_key: str,
        image_type: str = "photo",
        orientation: str = "horizontal",
        min_width: int = 1280,
        min_height: int = 720,
        safe_search: bool = True,
        per_page: int = 5,
        cache_dir: Optional[str | Path] = None,
    ):
        self._api_key     = api_key
        self._image_type  = image_type
        self._orientation = orientation
        self._min_width   = min_width
        self._min_height  = min_height
        self._safe_search = safe_search
        self._per_page    = per_page
        self._cache_dir   = Path(cache_dir) if cache_dir else None
        if self._cache_dir:
            self._cache_dir.mkdir(parents=True, exist_ok=True)

    # ─────────────────────────────────────────
    # 공개 API
    # ─────────────────────────────────────────

    def download_for_prompt(
        self,
        prompt: str,
        save_dir: str | Path,
        filename: Optional[str] = None,
        index: int = 0,
    ) -> Optional[Path]:
        """
        프롬프트로 Pixabay 검색 후 첫 번째 이미지를 다운로드합니다.

        Args:
            prompt: 검색 키워드 (영어 권장)
            save_dir: 저장 디렉터리
            filename: 저장 파일명 (None이면 자동 생성)
            index: 검색 결과 중 몇 번째 이미지를 사용할지 (0-based)

        Returns:
            저장된 파일 경로, 실패 시 None
        """
        save_dir = Path(save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)

        # 캐시 확인
        cache_key = self._cache_key(prompt, index)
        if self._cache_dir:
            cached = self._find_cache(cache_key)
            if cached:
                logger.debug("[Pixabay] Cache hit: %s", prompt[:40])
                dest = save_dir / (filename or cached.name)
                import shutil
                shutil.copy2(cached, dest)
                return dest

        # 키워드 정제 (프롬프트에서 핵심 단어만 추출)
        keywords = self._extract_keywords(prompt)
        logger.info("[Pixabay] Searching: %r", keywords)

        hits = self._search(keywords)
        if not hits:
            # 키워드 줄여서 재시도
            short = " ".join(keywords.split()[:2])
            logger.warning("[Pixabay] No results for %r, retrying with %r", keywords, short)
            hits = self._search(short)

        if not hits:
            logger.error("[Pixabay] No images found for prompt: %s", prompt[:60])
            return None

        # index 범위 내에서 선택
        hit = hits[min(index, len(hits) - 1)]
        image_url = hit.get("largeImageURL") or hit.get("webformatURL")
        if not image_url:
            logger.error("[Pixabay] No image URL in hit: %s", hit)
            return None

        # 다운로드
        ext = Path(image_url.split("?")[0]).suffix or ".jpg"
        fname = filename or f"{cache_key}{ext}"
        dest = save_dir / fname

        if not self._download(image_url, dest):
            return None

        # 캐시 저장
        if self._cache_dir:
            cache_path = self._cache_dir / f"{cache_key}{ext}"
            import shutil
            shutil.copy2(dest, cache_path)

        logger.info("[Pixabay] Downloaded: %s → %s", image_url[:60], dest.name)
        return dest

    def test_api_key(self) -> bool:
        """API 키가 유효한지 확인합니다."""
        try:
            results = self._search("nature", per_page=1)
            return results is not None
        except Exception as e:
            logger.error("[Pixabay] API key test failed: %s", e)
            return False

    # ─────────────────────────────────────────
    # 내부 구현
    # ─────────────────────────────────────────

    def _search(self, query: str, per_page: Optional[int] = None) -> Optional[List[dict]]:
        """Pixabay API 검색 요청을 보내고 hits 목록을 반환합니다."""
        params = {
            "key":         self._api_key,
            "q":           query,
            "image_type":  self._image_type,
            "orientation": self._orientation,
            "min_width":   self._min_width,
            "min_height":  self._min_height,
            "safesearch":  "true" if self._safe_search else "false",
            "per_page":    per_page or self._per_page,
            "lang":        "en",
        }
        url = PIXABAY_API_URL + "?" + urlencode(params)

        try:
            with urllib_request.urlopen(url, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                hits = data.get("hits", [])
                logger.debug("[Pixabay] Search %r → %d hits", query, len(hits))
                return hits
        except Exception as e:
            logger.error("[Pixabay] Search error: %s", e)
            return None

    def _download(self, url: str, dest: Path) -> bool:
        """URL에서 파일을 다운로드합니다."""
        try:
            req = urllib_request.Request(
                url,
                headers={"User-Agent": "AutoVideoPipeline/1.0"},
            )
            with urllib_request.urlopen(req, timeout=30) as resp:
                dest.write_bytes(resp.read())
            return True
        except Exception as e:
            logger.error("[Pixabay] Download failed %s: %s", url[:60], e)
            return False

    @staticmethod
    def _extract_keywords(prompt: str) -> str:
        """
        프롬프트에서 핵심 키워드를 추출합니다.
        숫자, 해상도, 특수문자 등 불필요한 내용을 제거합니다.
        """
        import re
        # 해상도 제거 (1920x1080 등)
        text = re.sub(r"\d{3,4}[xX×]\d{3,4}", "", prompt)
        # 기술적 단어 제거
        stop = {
            "photorealistic", "cinematic", "8k", "4k", "hd", "uhd",
            "detailed", "high", "quality", "ultra", "render", "cgi",
            "trending", "artstation", "unreal", "engine", "dramatic",
            "lighting", "professional", "shot", "photo",
        }
        words = [
            w for w in re.sub(r"[^\w\s]", " ", text).split()
            if w.lower() not in stop and not w.isdigit() and len(w) > 2
        ]
        # 최대 4단어
        return " ".join(words[:4]) or "nature landscape"

    @staticmethod
    def _cache_key(prompt: str, index: int) -> str:
        raw = f"{prompt}_{index}"
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    def _find_cache(self, key: str) -> Optional[Path]:
        if not self._cache_dir:
            return None
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            p = self._cache_dir / f"{key}{ext}"
            if p.exists():
                return p
        return None
