"""
subtitle/subtitle_builder.py
============================
SceneTimer 타이밍 기반 SRT / ASS 자막 파일 생성기.
& 로 분리된 세그먼트마다 독립 자막 항목을 만듭니다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from tts.tts_builder import SceneAudio
from video.scene_timer import SceneTimer, SegmentTiming, TimelineData
from utils.logger import get_logger

logger = get_logger(__name__)

# 노랑 강조할 단어 — 썸네일 강조 키워드와 같은 목록을 씁니다(숫자가 든 어절도 강조).
try:
    from image.thumbnail_generator import _KEYWORD_RE as _HIGHLIGHT_RE
except Exception:  # Pillow 등이 없어도 자막은 나와야 함
    _HIGHLIGHT_RE = re.compile(r"(?!x)x")
_DIGIT_RE = re.compile(r"\d")
_TRAIL_PUNCT_RE = re.compile(r"[,.·…~]+$")
_BREAK_AFTER_RE = re.compile(r"[,.?!…~]$")


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
        # 쇼츠(세로)는 화면이 훨씬 좁고 손에 가까이 보게 되는 만큼 자막을
        # 더 크게 써야 잘 읽힙니다.
        self._font_size    = config.get("font_size_shorts", 96) if is_portrait else config.get("font_size", 76)
        self._font_color   = config.get("font_color", "&H00FFFFFF")
        self._outline_color= config.get("outline_color", "&H00000000")
        self._shadow_color = config.get("shadow_color", "&H80000000")
        self._outline_w    = config.get("outline_width", 4)
        self._shadow_d     = config.get("shadow_depth", 2)
        # 쇼츠(세로)는 화면 하단에 유튜브 자체 UI(설명/좋아요/댓글 버튼 등)가
        # 고정 픽셀 높이로 깔리기 때문에, 롱폼과 같은 작은 여백(margin_v=60)을
        # 그대로 쓰면 그 UI에 가려 자막이 안 보입니다 — 그래서 margin_v_shorts를
        # 훨씬 크게(220) 잡아서 하단 중앙(alignment=2)에 두되 UI보다 위에 뜨게
        # 합니다. (2026-07-21에 한 번 더 나아가 아예 화면 위쪽(alignment=8)으로
        # 옮긴 적이 있었는데, 2026-09-28 사용자 피드백으로 다시 하단으로
        # 되돌림 — margin_v_shorts는 그대로 두고 alignment만 하단으로.)
        self._margin_v     = config.get("margin_v_shorts", 220) if is_portrait else config.get("margin_v", 60)
        self._margin_h     = config.get("margin_h", 40)
        self._alignment    = config.get("alignment_shorts", 8) if is_portrait else config.get("alignment", 2)
        self._bold         = config.get("bold", -1)
        self._italic       = config.get("italic", 0)

        # 2026-11-08 "편집족 스타일": 문장 통째로 한 번에 띡 뜨던 자막 대신, 말하는 속도에
        # 맞춰 2~3어절씩 끊어서 살짝 튀어나오듯(pop) 띄우고 핵심 단어는 노랑으로 강조한다.
        # 세로(쇼츠)일 땐 화면 위쪽에 제목도 고정으로 띄운다. dynamic=false면 예전 방식.
        self._dynamic = bool(config.get("dynamic", True))
        self._is_portrait = is_portrait
        if self._dynamic:
            self._font_name = config.get("dynamic_font_name", "NanumGothic")
            self._font_size = (
                config.get("dynamic_font_size_shorts", 100) if is_portrait
                else config.get("dynamic_font_size", 84)
            )
            self._outline_w = config.get("dynamic_outline_width", 7)
            self._shadow_d = config.get("dynamic_shadow_depth", 3)
            # 하단 유튜브 UI(제목·채널명)보다 확실히 위, 화면 3/4 지점쯤.
            if is_portrait:
                self._margin_v = config.get("dynamic_margin_v_shorts", 500)
            self._margin_h = config.get("dynamic_margin_h", 60)
        self._chunk_max_chars = int(config.get("chunk_max_chars", 11))
        self._chunk_max_words = int(config.get("chunk_max_words", 3))
        self._highlight_color = config.get("highlight_color", "&H00E5FF&")  # BGR → 노랑(#FFE500)
        self._base_color_inline = "&H" + self._font_color.replace("&H", "")[-6:] + "&"

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
        title: Optional[str] = None,
    ) -> Tuple[Path, Path]:
        """SRT / ASS 자막 파일을 생성하고 (srt_path, ass_path) 를 반환합니다.
        title을 주면(세로 영상 + dynamic 모드일 때) 화면 위쪽에 영상 내내 고정 표시합니다."""
        timeline = self._timer.compute(scene_audios)
        entries = self._build_entries(timeline)

        srt_path = self._output_dir / srt_filename
        ass_path = self._output_dir / ass_filename

        # SRT는 업로드용 자막 파일이라 문장 단위 그대로 둔다(끊어 띄우기는 화면용 ASS만).
        self._write_srt(entries, srt_path)
        if self._dynamic:
            chunks = self._build_chunks(timeline)
            show_title = title if (self._is_portrait and title) else None
            self._write_ass_dynamic(chunks, ass_path, show_title, timeline.total_duration)
        else:
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

    # "정확한 진단과 치료는 전문의와 상담하세요" 같은 안전 고지 문구는 내용상
    # 반드시 남겨야 하지만(2026-08-11 사용자 확인 — 의료 오정보 리스크 때문에
    # 완전 삭제는 안 함), 화면에서는 눈에 덜 띄게 작게 표시합니다. "전문의"라는
    # 단어가 들어간 자막 줄에만 ASS 인라인 태그로 폰트 크기를 줄입니다.
    _DISCLAIMER_KEYWORD = "전문의"
    _DISCLAIMER_FONT_SCALE = 0.55

    def _ass_dialogue(self, entry: SubtitleEntry) -> str:
        text = entry.text.replace("\n", "\\N")
        if self._DISCLAIMER_KEYWORD in entry.text:
            small_size = max(int(self._font_size * self._DISCLAIMER_FONT_SCALE), 1)
            text = f"{{\\fs{small_size}}}{text}"
        return (
            f"Dialogue: 0,"
            f"{_ass_time(entry.start)},"
            f"{_ass_time(entry.end)},"
            f"Default,{entry.speaker},0,0,0,,{text}\n"
        )

    # ─────────────────────────────────────────
    # 편집 스타일(dynamic) 자막
    # ─────────────────────────────────────────

    @staticmethod
    def _token_times(seg: SegmentTiming, tokens: List[str]) -> List[Tuple[float, float]]:
        """원문 어절(tokens)마다 세그먼트 기준 (시작, 끝) 시각을 정합니다.
        TTS 단어 타이밍 개수가 원문 어절 수와 같으면 그대로 짝지어 쓰고(보통 이 경우 —
        Edge-TTS는 어절 단위로 주되 문장부호는 뺌), 다르면(숫자·영문을 읽는 방식 차이로
        쪼개지거나 합쳐진 경우) 말하는 구간 안에서 글자 수 비례로 나눕니다."""
        words = seg.words or []
        if words and len(words) == len(tokens):
            return [(w[0], w[1]) for w in words]
        if words:
            span_start, span_end = words[0][0], words[-1][1]
        else:
            span_start, span_end = 0.0, seg.duration * 0.95
        weights = [len(t) + 1 for t in tokens]
        total = float(sum(weights)) or 1.0
        out = []
        cur = span_start
        for w in weights:
            nxt = cur + (span_end - span_start) * w / total
            out.append((cur, nxt))
            cur = nxt
        return out

    def _build_chunks(self, timeline: TimelineData) -> List[Tuple[float, float, List[str], bool]]:
        """[(절대 시작, 절대 끝, 어절 목록, 작은글씨여부)] — 2~3어절(최대 N글자)씩 끊은 자막 덩어리."""
        raw: List[Tuple[float, float, List[str], bool]] = []
        for seg in timeline.segment_timings:
            tokens = seg.text.split()
            if not tokens:
                continue
            small = self._DISCLAIMER_KEYWORD in seg.text
            times = self._token_times(seg, tokens)
            groups: List[List[int]] = []
            cur: List[int] = []
            for i, tok in enumerate(tokens):
                cand = cur + [i]
                length = sum(len(tokens[j]) for j in cand) + len(cand) - 1
                if cur and (length > self._chunk_max_chars or len(cand) > self._chunk_max_words):
                    groups.append(cur)
                    cur = [i]
                else:
                    cur = cand
                if _BREAK_AFTER_RE.search(tok):
                    groups.append(cur)
                    cur = []
            if cur:
                groups.append(cur)

            # "그", "이" 같은 한두 글자 어절이 혼자 0.2초 번쩍이고 사라지면 거슬린다 —
            # 다음 덩어리와 합쳐도 너무 길지 않으면 붙인다.
            merged: List[List[int]] = []
            k = 0
            while k < len(groups):
                g = groups[k]
                chars = sum(len(tokens[j]) for j in g)
                if chars <= 2 and k + 1 < len(groups):
                    nxt = g + groups[k + 1]
                    if sum(len(tokens[j]) for j in nxt) + len(nxt) - 1 <= self._chunk_max_chars + 3:
                        merged.append(nxt)
                        k += 2
                        continue
                merged.append(g)
                k += 1

            for g in merged:
                st = seg.abs_start + times[g[0]][0]
                en = seg.abs_start + times[g[-1]][1]
                raw.append((st, min(en, seg.abs_end), [tokens[j] for j in g], small))

        # 끝 시각 정리: 다음 덩어리가 곧(0.6초 안) 나오면 그때까지 들고 있어서 화면이
        # 깜빡이지 않게, 쉬는 구간이 길면 말 끝나고 0.3초 뒤에 내린다.
        chunks: List[Tuple[float, float, List[str], bool]] = []
        for i, (st, en, toks, small) in enumerate(raw):
            if i + 1 < len(raw):
                nxt_st = raw[i + 1][0]
                end = nxt_st if nxt_st - en <= 0.6 else en + 0.3
                end = min(end, nxt_st)
            else:
                end = en + 0.4
            chunks.append((st, max(end, st + 0.15), toks, small))
        return chunks

    def _style_line(self, name: str, size: int, outline: float, shadow: float,
                    alignment: int, margin_v: int, back: str) -> str:
        return (
            f"Style: {name},{self._font_name},{size},"
            f"{self._font_color},&H000000FF,{self._outline_color},{back},"
            f"{self._bold},0,0,0,100,100,0,0,1,{outline},{shadow},"
            f"{alignment},{self._margin_h},{self._margin_h},{margin_v},1\n"
        )

    def _chunk_text(self, toks: List[str], small: bool) -> str:
        parts = []
        last = len(toks) - 1
        for i, tok in enumerate(toks):
            shown = _TRAIL_PUNCT_RE.sub("", tok) if i == last else tok.rstrip(",")
            if not shown:
                continue
            shown = shown.replace("{", "(").replace("}", ")")
            if _HIGHLIGHT_RE.search(shown) or _DIGIT_RE.search(shown):
                shown = f"{{\\c{self._highlight_color}}}{shown}{{\\c{self._base_color_inline}}}"
            parts.append(shown)
        text = " ".join(parts)
        # 살짝 작게 시작해서 톡 튀어나오는 팝인 + 아주 짧은 페이드 — 너무 통통 튀면 유치해서 폭은 작게.
        anim = "{\\fad(60,0)\\fscx86\\fscy86\\t(0,110,\\fscx104\\fscy104)\\t(110,180,\\fscx100\\fscy100)}"
        if small:
            anim += f"{{\\fs{max(int(self._font_size * self._DISCLAIMER_FONT_SCALE), 1)}}}"
        return anim + text

    @staticmethod
    def _wrap_title(title: str, max_line: int = 11) -> List[str]:
        title = " ".join(title.replace("\\n", " ").replace("|", " ").split())
        if len(title) <= max_line or " " not in title:
            return [title]
        # 쉼표 뒤에서 끊는 게 제일 자연스럽고, 아니면 가운데에 가장 가까운 띄어쓰기에서 끊는다.
        if "," in title:
            a, b = title.split(",", 1)
            if a.strip() and b.strip() and max(len(a), len(b)) <= max_line + 4:
                return [a.strip() + ",", b.strip()]
        spaces = [i for i, c in enumerate(title) if c == " "]
        mid = len(title) / 2
        cut = min(spaces, key=lambda i: abs(i - mid))
        return [title[:cut].strip(), title[cut + 1:].strip()]

    def _write_ass_dynamic(self, chunks, path: Path, title: Optional[str], total: float) -> None:
        title_size = int(self._cfg.get("title_font_size_shorts", 88))
        header = (
            "[Script Info]\n"
            "ScriptType: v4.00+\n"
            f"PlayResX: {self._video_width}\n"
            f"PlayResY: {self._video_height}\n"
            "WrapStyle: 0\n"
            "ScaledBorderAndShadow: yes\n"
            "\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            + self._style_line("Default", self._font_size, self._outline_w, self._shadow_d,
                               self._alignment, self._margin_v, "&H99000000")
            + self._style_line("Title", title_size, 8, 4, 8,
                               int(self._cfg.get("title_margin_v_shorts", 190)), "&H99000000")
            + "\n[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        )
        lines: List[str] = []
        if title:
            wrapped = [w.replace("{", "(").replace("}", ")") for w in self._wrap_title(title)]
            # 두 줄이면 둘째 줄을 노랑으로 — 편집 쇼츠의 흔한 상단 제목 모양.
            if len(wrapped) == 2:
                body = f"{wrapped[0]}\\N{{\\c{self._highlight_color}}}{wrapped[1]}"
            else:
                body = wrapped[0]
            lines.append(
                f"Dialogue: 1,{_ass_time(0)},{_ass_time(total + 2)},Title,,0,0,0,,"
                f"{{\\fad(250,0)}}{body}\n"
            )
        for st, en, toks, small in chunks:
            lines.append(
                f"Dialogue: 0,{_ass_time(st)},{_ass_time(en)},Default,,0,0,0,,"
                f"{self._chunk_text(toks, small)}\n"
            )
        path.write_text(header + "".join(lines), encoding="utf-8-sig")
        logger.debug("ASS(dynamic) written: %s (%d chunks, title=%s)", path, len(chunks), bool(title))
