"""
image/image_generator.py
========================
씬(Scene)마다 이미지를 준비합니다. PHOTO 파일 → Pixabay 순으로 시도합니다.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from image.pixabay_client import PixabayClient
from parser.story_parser import Scene
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ImageResult:
    """이미지 준비 결과."""
    scene_index: int
    image_path: Optional[str]       # 저장 경로, 실패 시 None
    prompt: str
    success: bool
    error: Optional[str] = None
    elapsed: float = 0.0
    is_video: bool = False          # True면 image_path가 정지 이미지가 아니라 비디오 클립


class ImageGenerator:
    """
    씬 목록에 대해 이미지를 준비합니다.
    story.txt의 PHOTO: 섹션 파일을 순환 배정하고, 없으면 Pixabay에서
    씬 프롬프트로 검색해 다운로드합니다.

    사용 예:
        gen = ImageGenerator(image_config, temp_dir)
        results = gen.generate_all(scenes, photo_files=story.photo_files)
    """

    def __init__(
        self,
        config: dict,
        temp_dir: str | Path,
        progress_callback: Optional[Callable] = None,
    ):
        self._cfg = config
        self._temp_dir = Path(temp_dir)
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        self._progress_callback = progress_callback

        self._default_width = config.get("default_width", 1920)
        self._default_height = config.get("default_height", 1080)

        # Pixabay fallback
        pixabay_key = config.get("pixabay_api_key", "")
        self._use_video = config.get("use_pixabay_video", True)
        self._pixabay: Optional[PixabayClient] = None
        if pixabay_key:
            self._pixabay = PixabayClient(
                api_key=pixabay_key,
                min_width=self._default_width,
                min_height=self._default_height,
                cache_dir=self._temp_dir / "pixabay_cache",
            )
            logger.info(
                "[Image] Pixabay fallback 활성화됨 (video=%s)", self._use_video
            )

    # ─────────────────────────────────────────
    # 공개 API
    # ─────────────────────────────────────────

    def generate_all(
        self,
        scenes: List[Scene],
        photo_files: Optional[List[str]] = None,
        photo_base_dir: str | Path = "assets/photos",
    ) -> List[ImageResult]:
        """
        모든 씬에 대해 이미지를 준비합니다: PHOTO 파일 → Pixabay 순으로 시도.
        반환값: 입력 순서대로 정렬된 ImageResult 목록.
        """
        results: List[ImageResult] = []
        total = len(scenes)

        for idx, scene in enumerate(scenes):
            logger.info("[Image] Scene %d/%d: %s", idx + 1, total, scene.prompt[:60])
            result: Optional[ImageResult] = None

            # 1) PHOTO fallback
            fallback = self._resolve_photo_fallback(idx, photo_files, photo_base_dir)
            if fallback:
                logger.info("[Image] Scene %d: PHOTO 사용 → %s", scene.index, fallback)
                dest = self._temp_dir / f"scene_{scene.index:04d}_fallback.png"
                shutil.copy2(fallback, dest)
                result = ImageResult(
                    scene_index=scene.index,
                    image_path=str(dest),
                    prompt=scene.prompt,
                    success=True,
                )

            # 2) Pixabay 비디오 (활성화 시 사진보다 우선 시도 — 검색 결과가 사진보다
            #    훨씬 적어서, 없으면 자연스럽게 아래 3) 사진 검색으로 폴백됨)
            if result is None and self._pixabay and self._use_video:
                logger.info("[Image] Scene %d: Pixabay 비디오 검색 중...", scene.index)
                pb_video = self._pixabay.download_video_for_prompt(
                    prompt=scene.prompt,
                    save_dir=self._temp_dir,
                    filename=f"scene_{scene.index:04d}_pixabay.mp4",
                    index=idx % 5,
                )
                if pb_video:
                    result = ImageResult(
                        scene_index=scene.index,
                        image_path=str(pb_video),
                        prompt=scene.prompt,
                        success=True,
                        is_video=True,
                    )

            # 3) Pixabay 사진 (비디오가 비활성화됐거나, 이 씬은 비디오 검색 결과가
            #    없었을 때의 최종 폴백)
            if result is None and self._pixabay:
                logger.info("[Image] Scene %d: Pixabay 사진 검색 중...", scene.index)
                pb_path = self._pixabay.download_for_prompt(
                    prompt=scene.prompt,
                    save_dir=self._temp_dir,
                    filename=f"scene_{scene.index:04d}_pixabay.jpg",
                    index=idx % 5,
                )
                if pb_path:
                    result = ImageResult(
                        scene_index=scene.index,
                        image_path=str(pb_path),
                        prompt=scene.prompt,
                        success=True,
                    )

            if result is None:
                result = ImageResult(
                    scene_index=scene.index,
                    image_path=None,
                    prompt=scene.prompt,
                    success=False,
                    error="PHOTO/Pixabay 모두 이미지를 찾지 못했습니다.",
                )

            results.append(result)

            if self._progress_callback:
                try:
                    self._progress_callback(idx + 1, total, scene.index)
                except Exception:
                    pass

        success = sum(1 for r in results if r.success)
        logger.info("Image generation complete: %d/%d succeeded", success, total)
        return results

    def _resolve_photo_fallback(
        self,
        scene_idx: int,
        photo_files: Optional[List[str]],
        base_dir: str | Path,
    ) -> Optional[str]:
        """photo_files 목록에서 scene_idx % len 으로 순환 선택합니다."""
        if not photo_files:
            return None
        base = Path(base_dir)
        name = photo_files[scene_idx % len(photo_files)]
        for candidate in [Path(name), base / name]:
            if candidate.exists():
                return str(candidate)
        logger.warning("[Image] PHOTO fallback file not found: %s", name)
        return None
