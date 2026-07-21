"""
subtitle/subtitle_builder.py
============================
SceneTimer 타이밍 기반 SRT / ASS 자막 파일 생성기.
& 로 분리된 세그먼트마다 독립 자막 항목을 만듭니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

from tts.tts_builder import SceneAudio
from video.scene_timer import SceneTimer, SegmentTiming, TimelineData
from utils.logger import get_logger

logger = get_logger(__name__)


# ─────────────────────────────────────────────
# 타임코드 변환
# ─────────────────────────────────────────────

def _srt_time(seconds: float) -> str:
    """초 → SRT 타임코드 (00:00:00,000)"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds % 1) * 1000))
    if ms >= 1000:
        ms = 999
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ass_time(seconds: float) -> str:
    """초 → ASS 타임코드 (H:MM:SS.cc)"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds % 1) * 100))
    if cs >= 100:
        cs = 99
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


# ─────────────────────────────────────────────
# 자막 항목
# ─────────────────────────────────────────────

@dataclass
class SubtitleEntry:
    index: int
    start: float
    end: float
    text: str
    speaker: str


# ─────────────────────────────────────────────
# 빌더
# ─────────────────────────────────────────────

class SubtitleBuilder:
    """
    사용 예:
        builder = SubtitleBuilder(config["subtitle"], output_dir)
        srt_path, ass_path = builder.build(scene_audios)
    """

    def __init__(
        self,
        config: dict,
        output_dir: str | Path,
        min_scene_duration: float = 3.0,
        video_width: int = 1920,
        video_height: int = 1080,
    ):
        self._cfg = config
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

        self._video_width  = video_width
        self._video_height = video_height
        is_portrait = video_height > video_width

        self._font_name    = config.get("font_name", "Arial")
        self._font_size    = config.get("font_size", 76)
        self._font_color   = config.get("font_color", "&H00FFFFFF")
        self._outline_color= config.get("outline_color", "&H00000000")
        self._shadow_color = config.get("shadow_color", "&H80000000")
        self._outline_w    = config.get("outline_width", 4)
        self._shadow_d     = config.get("shadow_depth", 2)
        # 쇼츠(세로)는 화면 하단에 유튜브 자체 UI(설명/좋아요/댓글 버튼 등)가
        # 고정 픽셀 높이로 깔려서, 롱폼과 같은 margin_v를 쓰면 그 UI에 가려
        # 자막이 안 보입니다 — 세로 영상은 훨씬 큰 여백을 기본값으로 씁니다.
        self._margin_v     = config.get("margin_v_shorts", 220) if is_portrait else config.get("margin_v", 60)
        self._margin_h     = config.get("margin_h", 40)
        self._alignment    = config.get("alignment", 2)
        self._bold         = config.get("bold", -1)
        self._italic       = config.get("italic", 0)

        # inter_scene_gap=0: VideoComposer와 동일한 이유로, 실제 렌더링에
        # 반영되지 않는 가짜 간격을 자막 타이밍에 섞지 않습니다.
        self._timer = SceneTimer(
            min_scene_duration=min_scene_duration,
            inter_scene_gap=0.0,
        )

    def build(
        self,
        scene_audios: List[SceneAudio],
        srt_filename: str = "subtitles.srt",
        ass_filename: str = "subtitles.ass",
    ) -> Tuple[Path, Path]:
        """SRT / ASS 자막 파일을 생성하고 (srt_path, ass_path) 를 반환합니다."""
        timeline = self._timer.compute(scene_audios)
        entries = self._build_entries(timeline)

        srt_path = self._output_dir / srt_filename
        ass_path = self._output_dir / ass_filename

        self._write_srt(entries, srt_path)
        self._write_ass(entries, ass_path)

        logger.info(
            "Subtitles: %d entries, total=%.1fs → %s, %s",
            len(entries), timeline.total_duration,
            srt_path.name, ass_path.name,
        )
        return srt_path, ass_path

    def get_timeline(self, scene_audios: List[SceneAudio]) -> TimelineData:
        """타이밍 데이터를 반환합니다 (VideoComposer 에서 재사용)."""
        return self._timer.compute(scene_audios)

    # ─────────────────────────────────────────
    # 엔트리 생성
    # ─────────────────────────────────────────

    def _build_entries(self, timeline: TimelineData) -> List[SubtitleEntry]:
        entries: List[SubtitleEntry] = []
        for idx, seg in enumerate(timeline.segment_timings, start=1):
            if not seg.text.strip():
                continue
            entries.append(SubtitleEntry(
                index=idx,
                start=seg.abs_start,
                end=seg.abs_end,
                text=seg.text,
                speaker=seg.speaker,
            ))
        return entries

    # ─────────────────────────────────────────
    # SRT 출력
    # ─────────────────────────────────────────

    def _write_srt(self, entries: List[SubtitleEntry], path: Path) -> None:
        lines: List[str] = []
        for e in entries:
            lines.append(str(e.index))
            lines.append(f"{_srt_time(e.start)} --> {_srt_time(e.end)}")
            lines.append(e.text)
            lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8-sig")
        logger.debug("SRT written: %s (%d entries)", path, len(entries))

    # ─────────────────────────────────────────
    # ASS 출력
    # ─────────────────────────────────────────

    def _write_ass(self, entries: List[SubtitleEntry], path: Path) -> None:
        content = self._ass_header() + "".join(
            self._ass_dialogue(e) for e in entries
        )
        path.write_text(content, encoding="utf-8-sig")
        logger.debug("ASS written: %s (%d entries)", path, len(entries))

    def _ass_header(self) -> str:
        return (
            "[Script Info]\n"
            "ScriptType: v4.00+\n"
            f"PlayResX: {self._video_width}\n"
            f"PlayResY: {self._video_height}\n"
            "ScaledBorderAndShadow: yes\n"
            "\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Default,{self._font_name},{self._font_size},"
            f"{self._font_color},&H000000FF,"
            f"{self._outline_color},{self._shadow_color},"
            f"{self._bold},{self._italic},0,0,"
            f"100,100,0,0,1,"
            f"{self._outline_w},{self._shadow_d},"
            f"{self._alignment},"
            f"{self._margin_h},{self._margin_h},{self._margin_v},1\n"
            "\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        )

    def _ass_dialogue(self, entry: SubtitleEntry) -> str:
        text = entry.text.replace("\n", "\\N")
        return (
            f"Dialogue: 0,"
            f"{_ass_time(entry.start)},"
            f"{_ass_time(entry.end)},"
            f"Default,{entry.speaker},0,0,0,,{text}\n"
        )
