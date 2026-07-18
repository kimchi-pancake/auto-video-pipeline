"""
parser/story_parser.py
======================
story.txt 를 파싱하여 구조화된 StoryData 객체로 변환합니다.

입력 형식 예시
--------------
RESOLUTION:
1920x1080

CAST:
나레이터: 나레이터
남자1: 홍길동

BGM:
배경음악.mp3

PHOTO:
배경사진.jpg

THUMBNAIL_LONG:
썸네일에 들어갈 제목 텍스트

THUMBNAIL_SHORTS:
썸네일에 들어갈 제목 텍스트

[SCENE:숲 속 아침, 1920x1080]
나레이터: 오늘은 맑은 날이었습니다.&참 좋은 날씨죠.
남자1: 정말 그렇군요!

[SCENE:...]
...
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from utils.logger import get_logger

logger = get_logger(__name__)


# ─────────────────────────────────────────────
# 데이터 클래스 정의
# ─────────────────────────────────────────────

@dataclass
class DialogueLine:
    """대사 한 줄."""
    speaker: str          # 화자 키 (CAST에서 정의한 키)
    display_name: str     # 표시될 이름 (CAST 매핑값)
    text: str             # 원본 대사 (& 포함)
    segments: List[str]   # & 로 분리된 자막 세그먼트 목록
    line_index: int       # scene 내 줄 번호 (0-based)


@dataclass
class Scene:
    """하나의 [SCENE:...] 블록."""
    index: int                          # 장면 번호 (0-based)
    prompt: str                         # 이미지 생성 프롬프트
    resolution: Optional[str]          # "1920x1080" 형식, 없으면 None
    dialogues: List[DialogueLine] = field(default_factory=list)
    negative_prompt: Optional[str] = None
    seed: int = -1


@dataclass
class ThumbnailInfo:
    """썸네일 정보."""
    image_path: str          # 기반 이미지 파일 경로 (assets/photos/ 기준)
    title_text: str          # 오버레이할 제목 텍스트


@dataclass
class StoryData:
    """파싱 결과 전체."""
    cast: Dict[str, str]              # {"나레이터": "나레이터", "남자1": "홍길동"}
    bgm_files: List[str]              # BGM 파일 경로 목록
    photo_files: List[str]            # 배경 사진 파일 목록
    scenes: List[Scene]
    thumbnail_long: Optional[ThumbnailInfo]
    thumbnail_shorts: Optional[ThumbnailInfo]
    raw_title: str                    # 첫 번째 썸네일에서 추출한 제목
    raw_path: str                     # 원본 story.txt 경로
    resolution: Optional[Tuple[int, int]] = None  # RESOLUTION: 섹션 (예: 1080x1920). 없으면 GUI 설정값 사용


# ─────────────────────────────────────────────
# 파서 클래스
# ─────────────────────────────────────────────

class StoryParser:
    """
    story.txt를 파싱하는 클래스.

    사용 예:
        parser = StoryParser("input/story.txt")
        data = parser.parse()
    """

    # 정규식
    _RE_SCENE_HEADER = re.compile(
        r"^\[SCENE:([^\]]*)\]$",
        re.IGNORECASE,
    )
    _RE_DIALOGUE = re.compile(
        r"^([^\s:：]+)[:\：]\s*(.+)$"
    )
    _RE_RESOLUTION = re.compile(r"(\d{3,4})[xX×](\d{3,4})")
    _RE_NEGATIVE = re.compile(r"NEGATIVE:\s*(.+)", re.IGNORECASE)
    _RE_SEED = re.compile(r"SEED:\s*(-?\d+)", re.IGNORECASE)

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._lines: List[str] = []

    def parse(self) -> StoryData:
        """story.txt를 파싱하여 StoryData를 반환합니다."""
        if not self._path.exists():
            raise FileNotFoundError(f"story.txt not found: {self._path}")

        logger.info("Parsing story file: %s", self._path)
        self._lines = self._read_lines()

        resolution = self._parse_resolution_directive()
        cast = self._parse_section("CAST")
        bgm_files = self._parse_list_section("BGM")
        photo_files = self._parse_list_section("PHOTO")
        thumbnail_long = self._parse_thumbnail_section("THUMBNAIL_LONG")
        thumbnail_shorts = self._parse_thumbnail_section("THUMBNAIL_SHORTS")
        scenes = self._parse_scenes(cast)

        # 제목 추출
        raw_title = ""
        if thumbnail_long:
            raw_title = thumbnail_long.title_text
        elif thumbnail_shorts:
            raw_title = thumbnail_shorts.title_text

        data = StoryData(
            cast=cast,
            bgm_files=bgm_files,
            photo_files=photo_files,
            scenes=scenes,
            thumbnail_long=thumbnail_long,
            thumbnail_shorts=thumbnail_shorts,
            raw_title=raw_title,
            raw_path=str(self._path),
            resolution=resolution,
        )

        self._validate(data)
        logger.info(
            "Parsing complete: %d scenes, %d cast members",
            len(data.scenes),
            len(data.cast),
        )
        return data

    # ─────────────────────────────────────────
    # 섹션 파서
    # ─────────────────────────────────────────

    def _parse_section(self, section_name: str) -> Dict[str, str]:
        """KEY: VALUE 형태의 섹션을 파싱합니다."""
        result: Dict[str, str] = {}
        in_section = False

        for line in self._lines:
            stripped = line.strip()
            if stripped.upper() == f"{section_name.upper()}:":
                in_section = True
                continue
            if in_section:
                if self._is_section_header(stripped) or stripped.startswith("[SCENE"):
                    break
                if not stripped:
                    continue
                m = self._RE_DIALOGUE.match(stripped)
                if m:
                    key = m.group(1).strip()
                    val = m.group(2).strip()
                    result[key] = val

        return result

    def _parse_list_section(self, section_name: str) -> List[str]:
        """리스트 형태의 섹션을 파싱합니다."""
        result: List[str] = []
        in_section = False

        for line in self._lines:
            stripped = line.strip()
            if stripped.upper() == f"{section_name.upper()}:":
                in_section = True
                continue
            if in_section:
                if self._is_section_header(stripped) or stripped.startswith("[SCENE"):
                    break
                if stripped:
                    result.append(stripped)

        return result

    def _parse_resolution_directive(self) -> Optional[Tuple[int, int]]:
        """RESOLUTION: 섹션(예: "1080x1920")을 파싱합니다. 없으면 None (GUI에서 고른 해상도를 씀)."""
        in_section = False
        for line in self._lines:
            stripped = line.strip()
            if stripped.upper() == "RESOLUTION:":
                in_section = True
                continue
            if in_section:
                if self._is_section_header(stripped) or stripped.startswith("[SCENE") or not stripped:
                    if not stripped:
                        continue
                    break
                m = self._RE_RESOLUTION.search(stripped)
                if m:
                    return int(m.group(1)), int(m.group(2))
                break
        return None

    def _parse_thumbnail_section(self, section_name: str) -> Optional[ThumbnailInfo]:
        """THUMBNAIL_XXX 섹션을 파싱합니다.
        썸네일은 텍스트만 사용하므로 그냥 제목 텍스트 한 줄이면 됩니다.
        ("이미지경로 | 제목" 형태도 하위 호환으로 계속 지원하지만 image_path는 쓰이지 않습니다.)
        """
        in_section = False

        for line in self._lines:
            stripped = line.strip()
            if stripped.upper() == f"{section_name.upper()}:":
                in_section = True
                continue
            if in_section:
                if self._is_section_header(stripped) or stripped.startswith("[SCENE"):
                    break
                if not stripped:
                    continue
                if "|" in stripped:
                    parts = stripped.split("|", 1)
                    img = parts[0].strip()
                    text = parts[1].strip()
                else:
                    img = ""
                    text = stripped
                return ThumbnailInfo(image_path=img, title_text=text)

        return None

    def _parse_scenes(self, cast: Dict[str, str]) -> List[Scene]:
        """[SCENE:...] 블록들을 파싱합니다."""
        scenes: List[Scene] = []
        current_scene: Optional[Scene] = None
        scene_index = 0
        dialogue_index = 0

        for line in self._lines:
            stripped = line.strip()

            # SCENE 헤더 감지
            m_scene = self._RE_SCENE_HEADER.match(stripped)
            if m_scene:
                if current_scene is not None:
                    scenes.append(current_scene)

                scene_content = m_scene.group(1).strip()
                prompt, resolution, negative_prompt, seed = self._parse_scene_header(
                    scene_content
                )
                current_scene = Scene(
                    index=scene_index,
                    prompt=prompt,
                    resolution=resolution,
                    negative_prompt=negative_prompt,
                    seed=seed,
                )
                scene_index += 1
                dialogue_index = 0
                continue

            if current_scene is None:
                continue

            # 빈 줄 무시
            if not stripped:
                continue

            # 대사 파싱
            m_dial = self._RE_DIALOGUE.match(stripped)
            if m_dial:
                speaker_key = m_dial.group(1).strip()
                text = m_dial.group(2).strip()

                display_name = cast.get(speaker_key, speaker_key)
                segments = self._split_segments(text)

                dialogue = DialogueLine(
                    speaker=speaker_key,
                    display_name=display_name,
                    text=text,
                    segments=segments,
                    line_index=dialogue_index,
                )
                current_scene.dialogues.append(dialogue)
                dialogue_index += 1

        if current_scene is not None:
            scenes.append(current_scene)

        return scenes

    # ─────────────────────────────────────────
    # 헬퍼
    # ─────────────────────────────────────────

    def _parse_scene_header(
        self, content: str
    ) -> Tuple[str, Optional[str], Optional[str], int]:
        """
        SCENE 헤더 내용을 분리합니다.
        반환: (prompt, resolution_str, negative_prompt, seed)
        """
        # 해상도 추출
        m_res = self._RE_RESOLUTION.search(content)
        resolution = None
        if m_res:
            resolution = f"{m_res.group(1)}x{m_res.group(2)}"
            content = content[:m_res.start()] + content[m_res.end():]

        # NEGATIVE 추출
        m_neg = self._RE_NEGATIVE.search(content)
        negative_prompt = None
        if m_neg:
            negative_prompt = m_neg.group(1).strip()
            content = content[:m_neg.start()] + content[m_neg.end():]

        # SEED 추출
        m_seed = self._RE_SEED.search(content)
        seed = -1
        if m_seed:
            seed = int(m_seed.group(1))
            content = content[:m_seed.start()] + content[m_seed.end():]

        # 남은 내용이 프롬프트
        prompt = content.strip().strip(",").strip()

        return prompt, resolution, negative_prompt, seed

    @staticmethod
    def _split_segments(text: str) -> List[str]:
        """& 기준으로 대사를 자막 세그먼트로 분리합니다."""
        raw_segments = text.split("&")
        return [seg.strip() for seg in raw_segments if seg.strip()]

    @staticmethod
    def _is_section_header(line: str) -> bool:
        """섹션 헤더(CAST:, BGM:, etc.) 인지 확인합니다."""
        headers = {"RESOLUTION:", "CAST:", "BGM:", "PHOTO:", "THUMBNAIL_LONG:", "THUMBNAIL_SHORTS:"}
        return line.upper() in {h.upper() for h in headers}

    def _read_lines(self) -> List[str]:
        """파일을 읽어 줄 목록으로 반환합니다."""
        # BOM 처리
        with open(self._path, "r", encoding="utf-8-sig") as f:
            return f.readlines()

    def _validate(self, data: StoryData) -> None:
        """파싱 결과를 검사하고 경고를 출력합니다."""
        if not data.scenes:
            # 씬이 0개면 TTS/이미지/자막까지 전부 빈 채로 진행되다가 영상 합성
            # 단계에서 IndexError 같은 알아보기 힘든 에러로 뒤늦게 죽습니다.
            # (보통 Claude 응답을 다 복사 못 하고 일부만 저장했을 때 생김)
            # 여기서 바로 명확한 에러로 막아줍니다.
            raise ValueError(
                f"대본에 [SCENE:...] 블록이 하나도 없습니다: {self._path}\n"
                "Claude 응답을 저장할 때 본문이 잘렸을 수 있습니다. 응답 전체를 "
                "다시 복사해서 저장해주세요."
            )

        if not data.cast:
            logger.warning("No CAST section found. Speaker names will be used as-is.")

        for scene in data.scenes:
            if not scene.dialogues:
                logger.warning("Scene %d has no dialogues.", scene.index)
            if not scene.prompt:
                logger.warning("Scene %d has no image prompt.", scene.index)

            for dial in scene.dialogues:
                if not dial.segments:
                    logger.warning(
                        "Scene %d, line %d: empty dialogue segments.",
                        scene.index, dial.line_index,
                    )

        logger.debug("Validation complete for %d scenes.", len(data.scenes))
