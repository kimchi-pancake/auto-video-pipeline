"""
image/thumbnail_generator.py
=============================
Pillow를 사용해 썸네일(LONG 1280x720, SHORTS 1080x1920)을 생성합니다.
장면 이미지가 있으면 그걸 배경으로 꽉 채우고 어둡게 깔아서 그 위에 제목
텍스트를 큼직하게 얹습니다 (텍스트 뒤에는 반투명 밴드를 깔아 가독성 확보).
장면 이미지가 없으면 기존처럼 단색 배경 + 텍스트로 대체합니다.
"""

from __future__ import annotations

import re
import zlib
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

from parser.story_parser import ThumbnailInfo
from utils.logger import get_logger

logger = get_logger(__name__)

# 제목에서 "충격 키워드"를 찾아 항상 text_colors[0](빨강)으로 강조합니다.
# 나머지 단어는 기본 text_colors[-1](하양)로 쓰고, text_colors[1](파랑)은
# 거의 안 쓰고 아주 가끔 포인트로만 섞습니다(2026-10-01 사용자 지시: "빨강은
# 강조에만, 파랑은 거의 쓰지 말고") — 아래 _draw_text의 색 배정 로직 참고.
# 2026-09-17엔 이 목록이 건강 정보 채널의 위협 어휘였는데, 2026-09-28에
# 채널을 썰(사연) 이야기로 바꾼 뒤로는 이 단어들이 제목에 거의 안 나와서
# has_keyword가 항상 False로 떨어져 강조 자체가 작동을 안 하고 있었음
# (2026-10-01 발견) — 썰 제목에 실제로 반복 등장하는 충격·반전 어휘로 교체.
_KEYWORD_PATTERNS = [
    "들통", "들켰", "들킨", "배신", "복수", "사이다", "충격", "몰래", "훔친",
    "훔쳐", "협박", "징계", "잠수", "무너", "폭로", "반전", "거짓말", "사기",
    "갑질", "분노", "억울", "비밀", "진실", "소름", "경악",
]
_KEYWORD_RE = re.compile("|".join(re.escape(p) for p in _KEYWORD_PATTERNS))


class ThumbnailGenerator:
    """
    장면 이미지를 배경으로 깔고(없으면 단색 배경) 제목 텍스트를 큼직하게
    얹는 썸네일을 생성합니다. 기본은 하양이고, 충격 키워드만 빨강으로
    강조하며, 파랑은 제목 하나당 한 단어 정도만 포인트로 섞습니다
    (2026-10-01, 아래 _draw_text 참고). 두꺼운 외곽선 + 텍스트 뒤 반투명
    밴드로 어떤 배경 위에서도 가독성을 확보합니다.

    사용 예:
        gen = ThumbnailGenerator(config["thumbnail"], assets_dir)
        long_path  = gen.generate_long(info, output_dir, bg_image_path=...)
        short_path = gen.generate_shorts(info, output_dir, bg_image_path=...)
    """

    def __init__(self, config: dict, assets_dir: str | Path = "assets"):
        self._cfg = config
        self._assets = Path(assets_dir)

        self._long_w = config.get("long_width", 1280)
        self._long_h = config.get("long_height", 720)
        self._shorts_w = config.get("shorts_width", 1080)
        self._shorts_h = config.get("shorts_height", 1920)
        self._font_name = config.get("font_name", "NanumGothicBold")
        self._font_size_long = config.get("font_size_long", 96)
        self._font_size_shorts = config.get("font_size_shorts", 88)
        self._bg_color = tuple(config.get("bg_color", [0, 0, 0]))
        self._text_colors = [
            tuple(c) for c in config.get(
                "text_colors", [[255, 45, 45], [70, 130, 255], [255, 255, 255]]
            )
        ] or [(255, 255, 255)]
        self._quality = config.get("quality", 95)

    # ─────────────────────────────────────────
    # 공개 API
    # ─────────────────────────────────────────

    def generate_long(
        self, info: ThumbnailInfo, output_dir: str | Path, bg_image_path: str | Path | None = None
    ) -> Optional[Path]:
        out = Path(output_dir) / "thumbnail_long.jpg"
        return self._generate(info, out, self._long_w, self._long_h, self._font_size_long, bg_image_path)

    def generate_shorts(
        self, info: ThumbnailInfo, output_dir: str | Path, bg_image_path: str | Path | None = None
    ) -> Optional[Path]:
        out = Path(output_dir) / "thumbnail_shorts.jpg"
        return self._generate(info, out, self._shorts_w, self._shorts_h, self._font_size_shorts, bg_image_path)

    def generate_title_card(
        self,
        title: str,
        width: int,
        height: int,
        output_dir: str | Path,
        filename: str = "title_card.jpg",
    ) -> Optional[Path]:
        """영상 맨 앞에 붙는 인트로 "제목 카드" 이미지를 만듭니다 — 유튜브
        썸네일과 달리 실제 영상 해상도(width x height)에 맞춰서 만들어야
        영상 합성 시 크롭 없이 그대로 씁니다. 검은 배경 위에 제목 텍스트만
        큼직하게 얹는 걸로 충분해서 배경 사진은 안 씁니다."""
        out = Path(output_dir) / filename
        font_size = max(48, int(width * 0.09))
        return self._generate(
            ThumbnailInfo(image_path="", title_text=title),
            out, width, height, font_size, bg_image_path=None,
        )

    # ─────────────────────────────────────────
    # 내부 구현
    # ─────────────────────────────────────────

    def _generate(
        self,
        info: ThumbnailInfo,
        output_path: Path,
        width: int,
        height: int,
        font_size: int,
        bg_image_path: str | Path | None = None,
    ) -> Optional[Path]:
        output_path.parent.mkdir(parents=True, exist_ok=True)

        img = self._build_background(width, height, bg_image_path)
        if info.title_text:
            img = self._draw_text(img, info.title_text, font_size, width, height)

        img.save(str(output_path), "JPEG", quality=self._quality)
        logger.info("Thumbnail saved: %s (%dx%d)", output_path.name, width, height)
        return output_path

    def _build_background(
        self, width: int, height: int, bg_image_path: str | Path | None
    ) -> Image.Image:
        """장면 이미지가 있으면 꽉 채워 자르고 어둡게 깔아 배경으로 씁니다.
        없거나 열기 실패하면 기존 단색 배경으로 대체합니다."""
        if bg_image_path:
            try:
                src = Image.open(bg_image_path).convert("RGB")
                bg = ImageOps.fit(src, (width, height), method=Image.LANCZOS)
                # 살짝 어둡게 + 채도 낮춰서 그 위에 올릴 텍스트가 튀게 만듦
                bg = ImageEnhance.Brightness(bg).enhance(0.55)
                bg = ImageEnhance.Contrast(bg).enhance(1.08)
                return bg
            except Exception as e:
                logger.warning("배경 이미지 로드 실패, 단색 배경으로 대체: %s", e)
        return Image.new("RGB", (width, height), self._bg_color)

    def _draw_text(
        self,
        img: Image.Image,
        text: str,
        font_size: int,
        width: int,
        height: int,
    ) -> Image.Image:
        draw = ImageDraw.Draw(img, "RGBA")
        max_w = int(width * 0.88)
        max_h = int(height * 0.78)

        font, lines = self._fit_text(draw, text, font_size, max_w, max_h)
        size = font.size

        line_h = int(size * 1.25)
        total_h = line_h * len(lines)
        y = (height - total_h) // 2

        # 텍스트 뒤에 반투명 밴드를 깔아 사진이 화려해도 가독성을 확보
        pad_y = int(size * 0.35)
        draw.rectangle(
            [0, y - pad_y, width, y + total_h + pad_y],
            fill=(0, 0, 0, 140),
        )

        # 2026-09-14: 줄 단위가 아니라 "단어" 단위로 색을 바꿔가며 그립니다 —
        # 제목이 짧아서 줄이 1~2개뿐이면 줄 단위 순환으로는 거의 항상 단색으로
        # 보였는데(알록달록한 느낌이 안 남), 단어마다 바꾸면 짧은 제목에서도
        # 색이 확실히 섞여 보입니다.
        # 2026-09-17: 단순 순환이면 정작 중요한 단어가 파랑/노랑으로 묻히는
        # 경우가 많다는 피드백 — 충격 키워드는 항상 text_colors[0](빨강)으로
        # 고정.
        # 2026-10-01: "빨강은 강조에만, 파랑은 거의 쓰지 말고"로 다시 조정 —
        # 나머지 단어는 전부 text_colors[-1](하양)이 기본이고, text_colors[1]
        # (파랑)은 제목 하나당 딱 한 단어에만 포인트로 씀(그마저 키워드가 아닌
        # 단어가 2개 이상 있을 때만 — 안 그러면 "강조 아닌 단어"가 아예 없어서
        # 포인트를 줄 자리가 없음). 어느 단어에 파랑을 줄지는 제목 글자를 해시해
        # 정해서, 매번 같은 제목이면 같은 자리에 포인트가 가고(재현 가능), 제목이
        # 바뀌면 자리도 자연스럽게 바뀝니다.
        stroke_w = max(2, size // 22)
        space_bbox = draw.textbbox((0, 0), " ", font=font)
        space_w = space_bbox[2] - space_bbox[0]

        all_words = [w for line in lines for w in (line.split(" ") if " " in line else [line])]
        keyword_color = self._text_colors[0]
        default_color = self._text_colors[-1]
        accent_color = self._text_colors[1] if len(self._text_colors) > 2 else None

        non_keyword_idx = [i for i, w in enumerate(all_words) if not _KEYWORD_RE.search(w)]
        accent_word_i = None
        if accent_color is not None and len(non_keyword_idx) >= 2:
            # 파이썬 내장 hash()는 보안상 문자열마다 프로세스별로 다른 솔트를
            # 쓰기 때문에(PYTHONHASHSEED 랜덤화) 같은 제목이어도 실행할 때마다
            # 결과가 달라집니다 — zlib.crc32로 고정 해시를 씁니다.
            accent_word_i = non_keyword_idx[zlib.crc32(text.encode("utf-8")) % len(non_keyword_idx)]

        word_i = 0
        for line in lines:
            words = line.split(" ") if " " in line else [line]
            word_widths = [draw.textbbox((0, 0), w, font=font)[2] for w in words]
            total_w = sum(word_widths) + space_w * (len(words) - 1)
            # 강제로 쪼갠 글자 단위 조각이 그래도 max_w를 넘는 극단적인
            # 경우(글자 하나가 max_w보다 넓은 경우)엔 0으로 클램프해서
            # 캔버스 밖으로 삐져나가지 않게 합니다.
            x = max(0, (width - total_w) // 2)
            for w, ww in zip(words, word_widths):
                if _KEYWORD_RE.search(w):
                    color = keyword_color
                elif word_i == accent_word_i:
                    color = accent_color
                else:
                    color = default_color
                draw.text(
                    (x, y), w, font=font, fill=color,
                    stroke_width=stroke_w, stroke_fill=(0, 0, 0, 255),
                )
                x += ww + space_w
                word_i += 1
            y += line_h

        return img

    def _fit_text(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        max_size: int,
        max_w: int,
        max_h: int,
        min_size: int = 32,
    ) -> tuple:
        """줄바꿈한 텍스트가 가로(max_w)/세로(max_h) 안에 다 들어갈 때까지
        글자 크기를 줄여가며 (font, lines)를 반환합니다. 제목이 길어서
        최소 크기에서도 안 들어가면 그 이상은 줄이지 않고 최소 크기를
        반환합니다(그 아래는 가독성이 오히려 떨어짐)."""
        size = max_size
        while True:
            font = self._load_font(size)
            lines = self._wrap_text(text, font, draw, max_w)
            line_h = int(size * 1.25)
            if line_h * len(lines) <= max_h or size <= min_size:
                return font, lines
            size = max(min_size, size - 6)

    def _load_font(self, size: int) -> ImageFont.FreeTypeFont:
        font_candidates = [
            self._assets / "fonts" / f"{self._font_name}.ttf",
            self._assets / "fonts" / "NanumGothicBold.ttf",
            self._assets / "fonts" / "NanumGothic.ttf",
            Path("C:/Windows/Fonts/malgunbd.ttf"),
            Path("C:/Windows/Fonts/malgun.ttf"),
        ]
        for fc in font_candidates:
            if fc.exists():
                try:
                    return ImageFont.truetype(str(fc), size)
                except Exception:
                    pass
        # 폰트 없으면 기본 PIL 폰트
        logger.warning("Font not found, using default PIL font.")
        return ImageFont.load_default()

    @staticmethod
    def _wrap_text(
        text: str,
        font: ImageFont.FreeTypeFont,
        draw: ImageDraw.ImageDraw,
        max_width: int,
    ) -> list:
        def width_of(s: str) -> int:
            bbox = draw.textbbox((0, 0), s, font=font)
            return bbox[2] - bbox[0]

        words = text.split()
        lines = []
        current = ""
        for word in words:
            # 단어 자체가 max_width보다 넓으면(공백 없이 긴 한글 단어 등)
            # split()만으로는 절대 줄바꿈이 안 돼서 그 줄 전체가 캔버스
            # 밖으로 삐져나갑니다 — 글자 단위로 강제로 잘라냅니다.
            while width_of(word) > max_width and len(word) > 1:
                cut = len(word)
                while cut > 1 and width_of(word[:cut]) > max_width:
                    cut -= 1
                piece, word = word[:cut], word[cut:]
                if current:
                    lines.append(current)
                    current = ""
                lines.append(piece)

            test = (current + " " + word).strip()
            if width_of(test) <= max_width:
                current = test
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines if lines else [text]
