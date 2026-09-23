"""
image/ai_image_kickoff.py
==========================
Cloudflare Worker에 씬 이미지 생성을 비동기로 요청합니다. Worker는
ctx.waitUntil()로 GitHub Actions 잡 시간과 무관하게 백그라운드에서
Pollinations AI 이미지를 순차 생성해 assets/pending_images/{run_id}/ 에
커밋해둡니다. 합성 단계(image_generator.py)에서 git pull 후 파일이 있으면
쓰고, 없으면(아직 생성 중이거나 Worker 미설정) Pixabay로 폴백합니다.

DISCORD_WORKER_URL / IMAGE_GEN_SECRET 환경변수가 없으면 조용히 건너뜁니다 —
AI 이미지는 있으면 더 좋고 없어도 파이프라인은 기존 Pixabay 경로로 정상
동작해야 합니다(2026-08-04).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import List

from parser.story_parser import Scene
from utils.logger import get_logger

logger = get_logger(__name__)

# 한 번의 Worker 호출에 넘길 씬 개수. 씬 목록을 통째로 한 번에 넘기면 Worker가
# 호출 하나의 실행 예산(CPU·서브리퀘스트) 안에서 그걸 다 그리려다 초반에 죽습니다 —
# 2026-09-23 실측으로 32씬 요청에 3장, 28씬 요청에 4장만 커밋되고 나머지는 아무리
# 기다려도(대기 900초) 올라오지 않았고, 커밋 시각이 전부 킥오프 직후 몇 초 안에
# 몰려 있었습니다(Worker 동시처리를 4→8로 올려도 동일). 요청을 쪼개면 호출마다
# 예산이 새로 생기므로, 여기서 나눠 보냅니다. Worker 쪽에도 같은 크기의 안전망
# (자기 자신 재호출)이 있지만, 이 분할이 정상 경로입니다.
_SCENES_PER_REQUEST = 8


def kickoff_ai_images(run_id: str, scenes: List[Scene]) -> None:
    """Worker에 생성 요청만 던지고 응답을 기다리지 않고 바로 리턴합니다.
    씬이 많으면 _SCENES_PER_REQUEST개씩 나눠 여러 번 POST합니다.
    실패해도 예외를 삼키고 조용히 넘어갑니다 — Pixabay 폴백이 항상 있으므로
    이 호출이 파이프라인을 막으면 안 됩니다."""
    worker_url = os.environ.get("DISCORD_WORKER_URL", "").strip()
    secret = os.environ.get("IMAGE_GEN_SECRET", "").strip()
    if not worker_url or not secret:
        logger.info("[AIImage] DISCORD_WORKER_URL/IMAGE_GEN_SECRET 미설정 — AI 이미지 생성 건너뜀 (Pixabay만 사용)")
        return
    scene_payload = [{"index": s.index, "prompt": s.prompt} for s in scenes if s.prompt]
    if not scene_payload:
        return

    endpoint = f"{worker_url.rstrip('/')}/generate-images-x9k3m2"
    chunks = [
        scene_payload[i:i + _SCENES_PER_REQUEST]
        for i in range(0, len(scene_payload), _SCENES_PER_REQUEST)
    ]
    sent = 0
    try:
        import requests
    except Exception as e:  # requests 자체가 없으면 조용히 포기
        logger.warning("[AIImage] requests 임포트 실패(무시하고 계속 진행): %s", e)
        return

    for n, chunk in enumerate(chunks, 1):
        try:
            resp = requests.post(
                endpoint,
                json={"run_id": run_id, "scenes": chunk},
                headers={"X-Secret": secret},
                timeout=10,
            )
            if resp.status_code == 200:
                sent += len(chunk)
            else:
                logger.warning(
                    "[AIImage] Worker 요청 실패 (%d/%d, %d): %s",
                    n, len(chunks), resp.status_code, resp.text[:200],
                )
        except Exception as e:
            logger.warning("[AIImage] Worker 요청 중 에러 (%d/%d, 무시하고 계속 진행): %s", n, len(chunks), e)

    if sent:
        logger.info(
            "[AIImage] Worker에 %d개 씬 생성 요청 완료 (요청 %d번으로 분할, run_id=%s)",
            sent, len(chunks), run_id,
        )


def pull_pending_images(repo_root: Path) -> None:
    """미리 생성돼 커밋된 이미지를 받기 위한 git pull. 실패(충돌, 오프라인
    등)해도 조용히 넘어갑니다 — 이번 런은 그냥 Pixabay로 폴백됩니다."""
    try:
        subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=repo_root, capture_output=True, timeout=30, check=False,
        )
    except Exception as e:
        logger.warning("[AIImage] git pull 실패(무시): %s", e)


def pending_image_path(repo_root: Path, run_id: str, scene_index: int) -> Path:
    return repo_root / "assets" / "pending_images" / run_id / f"scene_{scene_index:04d}.jpg"


def wait_for_pending_images(
    repo_root: Path,
    run_id: str,
    scene_indices: List[int],
    timeout_sec: float = 1200.0,
    poll_interval_sec: float = 15.0,
) -> int:
    """"모든 사진을 AI로 대체"가 목표이므로, 그냥 한 번 pull해보고 없으면
    바로 Pixabay로 넘기는 게 아니라 — Worker가 백그라운드에서 다 그릴
    때까지 (timeout_sec 한도 내에서) 주기적으로 pull하며 기다립니다.
    이 대기는 stage_images가 stage_tts와 같은 스레드풀에서 동시에 돌기
    때문에 TTS 시간만큼은 공짜로 겹쳐지지만, 씬이 많으면(Pollinations가
    씬당 수십 초씩 순차 처리) 그 이상으로 GH Actions 시간이 실제로
    늘어날 수 있습니다 — "전부 AI로" 요구사항의 직접적인 트레이드오프입니다
    (2026-08-04). 끝까지 안 채워진 씬은 호출부(image_generator.py)에서
    Pixabay로 폴백합니다(완전 실패 방지용 안전망일 뿐, 정상 경로가 아님).
    반환값: timeout 시점까지 준비된 씬 개수."""
    total = len(scene_indices)
    if total == 0:
        return 0
    deadline = time.time() + timeout_sec
    ready = 0
    while True:
        pull_pending_images(repo_root)
        ready = sum(
            1 for idx in scene_indices
            if pending_image_path(repo_root, run_id, idx).exists()
        )
        logger.info("[AIImage] 준비된 씬 %d/%d (run_id=%s)", ready, total, run_id)
        if ready >= total:
            break
        if time.time() >= deadline:
            logger.warning(
                "[AIImage] 대기 시간(%ds) 초과 — %d/%d개만 준비됨, 나머지는 Pixabay 폴백",
                int(timeout_sec), ready, total,
            )
            break
        time.sleep(min(poll_interval_sec, max(deadline - time.time(), 0)))
    return ready
