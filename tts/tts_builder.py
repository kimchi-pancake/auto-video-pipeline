"""
tts/tts_builder.py
==================
StoryData로부터 TTSRequest 목록을 생성하고,
생성 결과를 장면·대사·세그먼트 단위로 정리합니다.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from parser.story_parser import DialogueLine, Scene, StoryData
from tts.tts_engine import TTSEngine, TTSRequest, TTSResult
from utils.logger import get_logger

logger = get_logger(__name__)


# ─────────────────────────────────────────────
# 결과 구조체
# ─────────────────────────────────────────────

class SegmentAudio:
    """자막 세그먼트 한 개에 대응하는 오디오 정보."""

    def __init__(
        self,
        scene_idx: int,
        dialogue_idx: int,
        segment_idx: int,
        speaker: str,
        text: str,
        audio_path: str,
        duration: float,
        success: bool,
    ):
        self.scene_idx = scene_idx
        self.dialogue_idx = dialogue_idx
        self.segment_idx = segment_idx
        self.speaker = speaker
        self.text = text
        self.audio_path = audio_path
        self.duration = duration
        self.success = success

    @property
    def request_id(self) -> str:
        return f"s{self.scene_idx:03d}_d{self.dialogue_idx:03d}_g{self.segment_idx:03d}"

    def __repr__(self) -> str:
        return (
            f"SegmentAudio({self.request_id}, "
            f"speaker={self.speaker!r}, "
            f"dur={self.duration:.2f}s, ok={self.success})"
        )


class SceneAudio:
    """한 씬의 모든 오디오 세그먼트 묶음."""

    def __init__(self, scene_idx: int, scene: Scene):
        self.scene_idx = scene_idx
        self.scene = scene
        self.segments: List[SegmentAudio] = []

    @property
    def total_duration(self) -> float:
        return sum(seg.duration for seg in self.segments)

    @property
    def all_success(self) -> bool:
        return all(seg.success for seg in self.segments)


# ─────────────────────────────────────────────
# TTS 빌더
# ─────────────────────────────────────────────

class TTSBuilder:
    """
    StoryData → SceneAudio 목록 생성기.

    사용 예:
        builder = TTSBuilder(config["tts"], temp_dir)
        scene_audios = builder.build(story_data)
    """

    def __init__(
        self,
        tts_config: dict,
        temp_dir: str | Path,
        progress_callback: Optional[Callable] = None,
    ):
        self._config = tts_config
        self._temp_dir = Path(temp_dir)
        self._progress_callback = progress_callback
        self._engine = TTSEngine(tts_config, progress_callback=self._on_tts_progress)
        self._total_requests = 0
        self._completed_requests = 0

    def build(self, story: StoryData) -> List[SceneAudio]:
        """
        전체 씬의 TTS를 생성하고 SceneAudio 목록을 반환합니다.
        """
        logger.info("Building TTS requests from story data...")
        requests, meta = self._create_requests(story)

        self._total_requests = len(requests)
        self._completed_requests = 0

        if not requests:
            logger.warning("No TTS requests created.")
            return []

        logger.info("Generating %d TTS audio segments...", len(requests))
        results: List[TTSResult] = self._engine.generate_all(requests)

        # 결과를 id 기반으로 매핑
        result_map: Dict[str, TTSResult] = {r.request_id: r for r in results}

        # SceneAudio 조립
        scene_audios = self._assemble(story, meta, result_map)

        total_dur = sum(sa.total_duration for sa in scene_audios)
        logger.info(
            "TTS build complete: %d scenes, total_duration=%.1fs",
            len(scene_audios),
            total_dur,
        )
        return scene_audios

    # ─────────────────────────────────────────
    # 내부 구현
    # ─────────────────────────────────────────

    def _create_requests(
        self,
        story: StoryData,
    ) -> Tuple[List[TTSRequest], List[Tuple[int, int, int]]]:
        """
        TTSRequest 목록과 (scene_idx, dialogue_idx, segment_idx) 메타 목록을
        같은 순서로 반환합니다.
        """
        requests: List[TTSRequest] = []
        meta: List[Tuple[int, int, int]] = []

        for scene in story.scenes:
            for dial in scene.dialogues:
                for seg_idx, seg_text in enumerate(dial.segments):
                    req_id = f"s{scene.index:03d}_d{dial.line_index:03d}_g{seg_idx:03d}"
                    out_path = str(
                        self._temp_dir
                        / f"{req_id}.mp3"
                    )

                    req = self._engine.build_request(
                        request_id=req_id,
                        speaker=dial.speaker,
                        text=seg_text,
                        output_path=out_path,
                    )
                    requests.append(req)
                    meta.append((scene.index, dial.line_index, seg_idx))

        return requests, meta

    def _assemble(
        self,
        story: StoryData,
        meta: List[Tuple[int, int, int]],
        result_map: Dict[str, TTSResult],
    ) -> List[SceneAudio]:
        """TTSResult를 SceneAudio 구조로 조립합니다."""
        # scene_idx → SceneAudio
        scene_audio_map: Dict[int, SceneAudio] = {}
        for scene in story.scenes:
            scene_audio_map[scene.index] = SceneAudio(scene.index, scene)

        for (scene_idx, dial_idx, seg_idx), req_id in zip(
            meta,
            [
                f"s{si:03d}_d{di:03d}_g{gi:03d}"
                for si, di, gi in meta
            ],
        ):
            result = result_map.get(req_id)
            scene = story.scenes[scene_idx]
            dial = scene.dialogues[dial_idx]
            seg_text = dial.segments[seg_idx] if seg_idx < len(dial.segments) else ""

            if result is None:
                logger.error("Missing TTS result for %s", req_id)
                seg_audio = SegmentAudio(
                    scene_idx=scene_idx,
                    dialogue_idx=dial_idx,
                    segment_idx=seg_idx,
                    speaker=dial.speaker,
                    text=seg_text,
                    audio_path="",
                    duration=0.0,
                    success=False,
                )
            else:
                seg_audio = SegmentAudio(
                    scene_idx=scene_idx,
                    dialogue_idx=dial_idx,
                    segment_idx=seg_idx,
                    speaker=dial.speaker,
                    text=seg_text,
                    audio_path=result.output_path,
                    duration=result.duration,
                    success=result.success,
                )

            scene_audio_map[scene_idx].segments.append(seg_audio)

        return [scene_audio_map[s.index] for s in story.scenes if s.index in scene_audio_map]

    def _on_tts_progress(self, completed: int, total: int, req_id: str) -> None:
        self._completed_requests = completed
        if self._progress_callback:
            self._progress_callback(completed, total, req_id)
