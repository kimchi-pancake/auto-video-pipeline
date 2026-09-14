"""
tts/tts_engine.py
=================
Edge-TTS 또는 OpenAI TTS를 사용한 음성 합성 엔진(config의 tts.provider로 선택).
캐릭터별 보이스, 속도, 피치, 볼륨 설정 지원.
병렬 생성 + 자동 재시도 지원.

2026-09-14: Edge-TTS(무료)의 부자연스러움 때문에 tts.provider="openai"를
추가했습니다 — OPENAI_API_KEY(.env)가 필요합니다. OpenAI TTS API는
pitch/volume 파라미터가 없어서(속도만 조절 가능), rate("+20%" 같은 edge-tts
표기)를 speed(0.25~4.0 배율)로 환산해서 씁니다.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

import edge_tts
from dotenv import load_dotenv
from openai import AsyncOpenAI

from utils.logger import get_logger

logger = get_logger(__name__)

_ENV_PATH = Path(__file__).parent.parent / ".env"


# ─────────────────────────────────────────────
# 데이터 클래스
# ─────────────────────────────────────────────

@dataclass
class TTSRequest:
    """TTS 생성 요청 단위."""
    request_id: str          # 고유 식별자 (예: "scene_0_dial_0_seg_0")
    speaker: str             # 화자 키
    text: str                # 생성할 텍스트
    output_path: str         # 저장할 MP3 경로
    voice: str = "ko-KR-SunHiNeural"
    rate: str = "+0%"
    pitch: str = "+0Hz"
    volume: str = "+0%"


@dataclass
class TTSResult:
    """TTS 생성 결과."""
    request_id: str
    output_path: str
    duration: float          # 오디오 길이 (초), 실패 시 0.0
    success: bool
    error: Optional[str] = None
    elapsed: float = 0.0     # 생성 소요 시간 (초)


# ─────────────────────────────────────────────
# TTS 엔진
# ─────────────────────────────────────────────

class TTSEngine:
    """
    Edge-TTS 기반 음성 합성 엔진.

    사용 예:
        engine = TTSEngine(config)
        results = engine.generate_all(requests)
    """

    def __init__(self, config: dict, progress_callback: Optional[Callable] = None):
        """
        Args:
            config: config.json 의 "tts" 섹션 딕셔너리
            progress_callback: (completed, total, request_id) → None
        """
        self._cfg = config
        self._voices: Dict[str, dict] = config.get("voices", {})
        self._default_voice = config.get("default_voice", "ko-KR-SunHiNeural")
        self._default_rate = config.get("default_rate", "+0%")
        self._default_pitch = config.get("default_pitch", "+0Hz")
        self._default_volume = config.get("default_volume", "+0%")
        self._retry_count = config.get("retry_count", 3)
        self._retry_delay = config.get("retry_delay", 2)
        self._max_workers = config.get("max_workers", 4)
        self._progress_callback = progress_callback

        self._provider = config.get("provider", "edge")
        self._openai_model = config.get("openai_model", "gpt-4o-mini-tts")
        self._openai_default_voice = config.get("openai_default_voice", "onyx")
        self._openai_client: Optional[AsyncOpenAI] = None
        if self._provider == "openai":
            load_dotenv(_ENV_PATH)
            api_key = os.environ.get("OPENAI_API_KEY", "").strip()
            if not api_key:
                # 키가 없다고 영상 생성 전체를 멈추면 안 됨(이 파이프라인의 기존
                # 원칙 — AI 이미지도 NVIDIA가 죽으면 Pixabay로 조용히 폴백함) —
                # Edge-TTS(무료, 키 불필요)로 조용히 내려가고 경고만 남깁니다.
                logger.warning(
                    "tts.provider=openai인데 OPENAI_API_KEY(.env)가 없습니다 — "
                    "Edge-TTS로 폴백합니다."
                )
                self._provider = "edge"
            else:
                self._openai_client = AsyncOpenAI(api_key=api_key)

    def build_request(
        self,
        request_id: str,
        speaker: str,
        text: str,
        output_path: str,
    ) -> TTSRequest:
        """화자 설정을 config에서 조회하여 TTSRequest를 생성합니다."""
        voice_cfg = self._voices.get(speaker, {})
        if self._provider == "openai":
            voice = voice_cfg.get("openai_voice", self._openai_default_voice)
        else:
            voice = voice_cfg.get("voice", self._default_voice)
        return TTSRequest(
            request_id=request_id,
            speaker=speaker,
            text=text,
            output_path=output_path,
            voice=voice,
            rate=voice_cfg.get("rate", self._default_rate),
            pitch=voice_cfg.get("pitch", self._default_pitch),
            volume=voice_cfg.get("volume", self._default_volume),
        )

    def generate_all(self, requests: List[TTSRequest]) -> List[TTSResult]:
        """
        요청 목록을 병렬로 처리하여 결과를 반환합니다.
        순서는 입력 순서대로 보장됩니다.
        """
        if not requests:
            return []

        logger.info("Starting TTS generation: %d requests", len(requests))
        results = asyncio.run(self._run_all(requests))

        success = sum(1 for r in results if r.success)
        logger.info(
            "TTS complete: %d/%d succeeded", success, len(results)
        )
        return results

    # ─────────────────────────────────────────
    # 비동기 내부 구현
    # ─────────────────────────────────────────

    async def _run_all(self, requests: List[TTSRequest]) -> List[TTSResult]:
        semaphore = asyncio.Semaphore(self._max_workers)
        completed = 0
        total = len(requests)
        results: List[Optional[TTSResult]] = [None] * total

        async def worker(index: int, req: TTSRequest):
            nonlocal completed
            async with semaphore:
                result = await self._generate_with_retry(req)
                results[index] = result
                completed += 1
                if self._progress_callback:
                    try:
                        self._progress_callback(completed, total, req.request_id)
                    except Exception:
                        pass

        tasks = [worker(i, req) for i, req in enumerate(requests)]
        await asyncio.gather(*tasks)
        return results  # type: ignore

    async def _generate_with_retry(self, req: TTSRequest) -> TTSResult:
        """재시도 로직 포함 TTS 생성."""
        Path(req.output_path).parent.mkdir(parents=True, exist_ok=True)

        last_error = ""
        for attempt in range(1, self._retry_count + 1):
            try:
                start = time.time()
                await self._generate_once(req)
                elapsed = time.time() - start

                duration = self._get_audio_duration(req.output_path)
                logger.debug(
                    "[TTS] %s → %.2fs (attempt %d)", req.request_id, duration, attempt
                )
                return TTSResult(
                    request_id=req.request_id,
                    output_path=req.output_path,
                    duration=duration,
                    success=True,
                    elapsed=elapsed,
                )
            except Exception as e:
                last_error = str(e)
                logger.warning(
                    "[TTS] %s failed (attempt %d/%d): %s",
                    req.request_id, attempt, self._retry_count, e,
                )
                if attempt < self._retry_count:
                    await asyncio.sleep(self._retry_delay)

        logger.error("[TTS] %s permanently failed: %s", req.request_id, last_error)
        return TTSResult(
            request_id=req.request_id,
            output_path=req.output_path,
            duration=0.0,
            success=False,
            error=last_error,
        )

    async def _generate_once(self, req: TTSRequest) -> None:
        """설정된 provider로 한 번 생성합니다."""
        if self._provider == "openai":
            await self._generate_once_openai(req)
        else:
            await self._generate_once_edge(req)

    async def _generate_once_edge(self, req: TTSRequest) -> None:
        """Edge-TTS로 한 번 생성합니다."""
        communicate = edge_tts.Communicate(
            text=req.text,
            voice=req.voice,
            rate=req.rate,
            pitch=req.pitch,
            volume=req.volume,
        )
        await communicate.save(req.output_path)

    async def _generate_once_openai(self, req: TTSRequest) -> None:
        """OpenAI TTS API로 한 번 생성합니다. pitch/volume은 이 API에 없어서
        무시되고, rate만 speed로 환산해서 반영됩니다."""
        speed = self._rate_to_speed(req.rate)
        async with self._openai_client.audio.speech.with_streaming_response.create(
            model=self._openai_model,
            voice=req.voice,
            input=req.text,
            response_format="mp3",
            speed=speed,
        ) as response:
            await response.stream_to_file(req.output_path)

    @staticmethod
    def _rate_to_speed(rate: str) -> float:
        """edge-tts 스타일 rate 문자열("+20%")을 OpenAI TTS의 speed(0.25~4.0)로
        환산합니다."""
        try:
            pct = float(rate.strip().rstrip("%"))
        except (ValueError, AttributeError):
            pct = 0.0
        return max(0.25, min(4.0, 1.0 + pct / 100.0))

    @staticmethod
    def _get_audio_duration(path: str) -> float:
        """
        MP3 파일의 길이를 반환합니다.
        ffprobe 없이 간단히 파일 크기 기반 추정 (128kbps 기준).
        정확도를 위해 FFmpeg 래퍼에서 덮어쓸 수 있습니다.
        """
        try:
            size_bytes = os.path.getsize(path)
            # 128 kbps = 16000 bytes/sec
            return size_bytes / 16000.0
        except Exception:
            return 3.0  # fallback
