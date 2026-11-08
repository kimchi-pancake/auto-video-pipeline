"""
image/nvidia_direct.py
=======================
Worker가 못 그려서 pending_images에 안 올라온 씬을, 조립 러너가 NVIDIA FLUX를
직접 호출해서 채웁니다.

Cloudflare Worker는 ctx.waitUntil() 안에서 돌기 때문에 NVIDIA가 느리게 500을 내면
(실측: 60초 뒤 500) 재시도나 Workers AI 폴백까지 가기 전에 실행 한도로 잘려서
그 씬은 영원히 안 올라옵니다(2026-11-08: 프로브 1장도 5분 안에 안 올라왔고, 쇼츠
5편 중 여러 편이 6장 중 3장만 받은 채 15분을 기다림). 러너는 그런 시간 제한이 없고,
그 사이 Pixabay 키는 403이라 폴백이 아예 막혀 있었습니다.

NVIDIA_API_KEY가 없으면 조용히 아무것도 안 합니다(기존 동작 그대로 Pixabay로 폴백).
프롬프트 구성/해상도는 tools/discord_worker/index.js와 같은 값을 써야 영상 안에서
Worker가 그린 장면과 톤이 안 어긋납니다.
"""

from __future__ import annotations

import base64
import os
import re
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List

import requests

from parser.story_parser import Scene
from utils.logger import get_logger

logger = get_logger(__name__)

_MODEL = "black-forest-labs/flux.1-dev"
_URL = f"https://ai.api.nvidia.com/v1/genai/{_MODEL}"
_WIDTH, _HEIGHT = 768, 1344

# 세계 정세·경제 채널(world_econ)은 Worker의 "따뜻한 수채화"가 분위기에 안 맞아서
# (Worker 스타일은 대시보드 수동 배포라 못 바꿈) Worker를 아예 건너뛰고 러너에서 이
# 화풍으로 전부 직접 그린다. 대본 파일명에 _econ이 들어 있으면 그 채널(ai_script_generator).
_DOCUMENTARY_SUFFIX = (
    ", cinematic documentary photograph, photorealistic, muted desaturated color grading, "
    "moody overcast lighting, high contrast, sharp focus, 35mm film grain, serious atmosphere, "
    "no text, no letters, no numbers, no logo, no watermark"
)


# 2026-11-08: 국뽕 쇼츠도 "따뜻한 수채화 동화" 그림이 사실 전달 영상과 안 어울리고 그럴싸해
# 보이지 않는다는 지적으로, 실사 시네마틱 사진풍으로 러너에서 직접 그린다(파일명 _photo).
_PHOTO_SUFFIX = (
    ", cinematic photograph, photorealistic, natural vivid colors, dramatic natural lighting, "
    "shallow depth of field, sharp focus, highly detailed, shot on full-frame camera, "
    "no text, no letters, no numbers, no logo, no watermark"
)


def runner_only(run_id: str) -> bool:
    """이 run_id는 Worker를 쓰지 않고 러너가 NVIDIA로 전부 직접 그린다."""
    rid = run_id or ""
    return "_econ" in rid or "_photo" in rid


def _style_for(run_id: str) -> str:
    rid = run_id or ""
    if "_econ" in rid:
        return "documentary"
    if "_photo" in rid:
        return "photo"
    return "watercolor"


# index.js의 _IMAGE_STYLE_SUFFIX / _FACE_QUALITY_BOOST / _PERSON_KEYWORDS와 동일해야 함.
_STYLE_SUFFIX = (
    ", warm watercolor illustration, soft muted color palette, gentle golden hour lighting, "
    "visible paper texture, loose expressive brushstrokes, soft blended edges between colors, "
    "warm beige and amber undertones, cozy nostalgic atmosphere, hand-painted storybook feel, "
    "soft focus background, no harsh outlines, emotionally warm and comforting mood, "
    "traditional watercolor paper grain, delicate color washes"
)
_FACE_BOOST = (
    ", detailed symmetrical face, sharp clear eyes, correct facial anatomy, close-up portrait, "
    "headshot framing, hands not visible, wearing modest crew-neck clothing, fully clothed, covered shoulders"
)
_PERSON_RE = re.compile(
    r"\b(woman|women|man|men|person|people|elderly|grandmother|grandma|grandfather|grandpa|doctor|"
    r"patient|nurse|child|children|boy|girl|family|couple|human|face|portrait|lady|gentleman|"
    r"senior|adult)\b",
    re.IGNORECASE,
)

_ATTEMPTS = 4          # 씬당 최대 시도 횟수
_REQUEST_TIMEOUT = 100  # 느린 500이 60초쯤 걸려서 그보다 넉넉히
_CONCURRENCY = 4        # NVIDIA는 동시 16개부터 429가 늘어서 여유 있게


def _key() -> str:
    key = os.environ.get("NVIDIA_API_KEY", "").strip()
    if not key:  # on_demand 워크플로우는 키를 .env로만 써준다
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except Exception:  # noqa: BLE001
            pass
        key = os.environ.get("NVIDIA_API_KEY", "").strip()
    return key


def available() -> bool:
    return bool(_key())


def _generate_one(scene: Scene, seed: int, dest: Path, key: str, style: str = "watercolor") -> bool:
    if style == "documentary":
        prompt = f"{scene.prompt}{_DOCUMENTARY_SUFFIX}"
    elif style == "photo":
        prompt = f"{scene.prompt}{_PHOTO_SUFFIX}"
    else:
        boost = _FACE_BOOST if _PERSON_RE.search(scene.prompt) else ""
        prompt = f"{scene.prompt}{boost}{_STYLE_SUFFIX}"
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            resp = requests.post(
                _URL,
                headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                json={"prompt": prompt, "width": _WIDTH, "height": _HEIGHT, "steps": 25, "seed": seed},
                timeout=_REQUEST_TIMEOUT,
            )
            if resp.ok:
                artifact = (resp.json().get("artifacts") or [{}])[0]
                b64 = artifact.get("base64")
                reason = artifact.get("finishReason")
                if b64 and (not reason or reason == "SUCCESS"):
                    dest.write_bytes(base64.b64decode(b64.split(",", 1)[-1]))
                    return True
                # 필터링되면 새까만 이미지가 오니 seed를 바꿔서 다시 시도
                logger.warning("[NvidiaDirect] 씬 %d 응답 이상(%s) — 재시도", scene.index, reason)
                seed += 1
            else:
                logger.warning("[NvidiaDirect] 씬 %d HTTP %d (시도 %d/%d)", scene.index, resp.status_code, attempt, _ATTEMPTS)
        except Exception as e:  # noqa: BLE001 — 한 씬 실패가 전체를 막으면 안 됨
            logger.warning("[NvidiaDirect] 씬 %d 에러(시도 %d/%d): %s", scene.index, attempt, _ATTEMPTS, e)
        if attempt < _ATTEMPTS:
            time.sleep(5 * attempt)
    return False


def fill_missing(run_id: str, scenes: List[Scene], dest_dir: Path) -> Dict[int, Path]:
    """scenes를 NVIDIA로 직접 그려 dest_dir에 저장하고 {scene.index: 파일경로}를 돌려줍니다.
    그리지 못한 씬은 결과에서 빠집니다(호출부가 Pixabay로 폴백)."""
    key = _key()
    if not key or not scenes:
        return {}
    dest_dir.mkdir(parents=True, exist_ok=True)
    logger.info("[NvidiaDirect] Worker가 못 그린 %d개 씬을 러너에서 직접 생성합니다", len(scenes))

    def _job(scene: Scene):
        seed = zlib.crc32(f"{run_id}:{scene.index}".encode("utf-8")) % 1_000_000_000
        dest = dest_dir / f"scene_{scene.index:04d}_direct.jpg"
        return scene.index, dest, _generate_one(scene, seed, dest, key, style=_style_for(run_id))

    out: Dict[int, Path] = {}
    with ThreadPoolExecutor(max_workers=_CONCURRENCY) as pool:
        for idx, dest, ok in pool.map(_job, scenes):
            if ok:
                out[idx] = dest
    logger.info("[NvidiaDirect] %d/%d개 생성 성공", len(out), len(scenes))
    return out
