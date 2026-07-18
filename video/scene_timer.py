"""
video/scene_timer.py
====================
씬 오디오 데이터를 받아서 영상 타임라인 타이밍을 계산합니다.
자막, 비디오 클립 길이, 크로스페이드 오프셋 등을 통합 제공합니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from tts.tts_builder import SceneAudio, SegmentAudio


@dataclass
class SegmentTiming:
    """자막 세그먼트 하나의 타임라인 상 타이밍."""
    scene_idx: int
    dialogue_idx: int
    segment_idx: int
    speaker: str
    text: str
    # 전체 타임라인 기준 절대 시각
    abs_start: float
    abs_end: float
    # 씬 내 상대 시각
    rel_start: float
    rel_end: float

    @property
    def duration(self) -> float:
        return self.abs_end - self.abs_start


@dataclass
class SceneTiming:
    """씬 하나의 타임라인 타이밍."""
    scene_idx: int
    abs_start: float        # 전체 타임라인에서의 시작 시각
    abs_end: float          # 전체 타임라인에서의 종료 시각
    clip_duration: float    # 비디오 클립 길이 (min_duration 보정 포함)
    segments: List[SegmentTiming] = field(default_factory=list)

    @property
    def audio_duration(self) -> float:
        return self.abs_end - self.abs_start


@dataclass
class TimelineData:
    """전체 타임라인 데이터."""
    scenes: List[SceneTiming]
    total_duration: float
    segment_timings: List[SegmentTiming]  # 플랫 리스트 (자막 생성용)


class SceneTimer:
    """
    SceneAudio 목록으로부터 TimelineData를 계산합니다.

    사용 예:
        timer = SceneTimer(min_scene_duration=3.0, gap=0.3)
        timeline = timer.compute(scene_audios)
    """

    def __init__(
        self,
        min_scene_duration: float = 3.0,
        inter_scene_gap: float = 0.3,
    ):
        self._min_dur = min_scene_duration
        self._gap = inter_scene_gap

    def compute(self, scene_audios: List[SceneAudio]) -> TimelineData:
        """
        씬 오디오 목록을 받아 전체 타임라인 타이밍을 반환합니다.
        """
        scene_timings: List[SceneTiming] = []
        all_segments: List[SegmentTiming] = []
        cursor = 0.0

        for sa in scene_audios:
            scene_start = cursor
            seg_cursor = 0.0  # 씬 내 상대 시각

            seg_timings: List[SegmentTiming] = []
            for seg in sa.segments:
                if not seg.success or not seg.text.strip():
                    seg_cursor += seg.duration
                    cursor += seg.duration
                    continue

                dur = max(seg.duration, 0.1)
                st = SegmentTiming(
                    scene_idx=sa.scene_idx,
                    dialogue_idx=seg.dialogue_idx,
                    segment_idx=seg.segment_idx,
                    speaker=seg.speaker,
                    text=seg.text,
                    abs_start=cursor,
                    abs_end=cursor + dur,
                    rel_start=seg_cursor,
                    rel_end=seg_cursor + dur,
                )
                seg_timings.append(st)
                all_segments.append(st)
                seg_cursor += dur
                cursor += dur

            # 씬 최소 길이 보정
            raw_audio_dur = cursor - scene_start
            clip_duration = max(raw_audio_dur, self._min_dur)
            scene_end = cursor

            scene_timing = SceneTiming(
                scene_idx=sa.scene_idx,
                abs_start=scene_start,
                abs_end=scene_end,
                clip_duration=clip_duration,
                segments=seg_timings,
            )
            scene_timings.append(scene_timing)

            # 씬 간 갭
            cursor += self._gap

        total = cursor
        return TimelineData(
            scenes=scene_timings,
            total_duration=total,
            segment_timings=all_segments,
        )
