"""
image/thumbnail_generator.py
=============================
Pillow를 사용해 썸네일(LONG 1280x720, SHORTS 1080x1920)을 생성합니다.
장면 이미지가 있으면 그걸 배경으로 꽉 채우고 어둡게 깔아서 그 위에 제목
텍스트를 큼직하게 얹습니다 (텍스트 뒤에는 반투명 밴드를 깔아 가독성 확보).
장면 이미지가 없으면 기존처럼 단색 배경 + 텍스트로 대체합니다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

from parser.story_parser import ThumbnailInfo
from utils.logger import get_logger

logger = get_logger(__name__)


class ThumbnailGenerator:
    """
    장면 이미지를 배경으로 깔고(없으면 단색 배경) 제목 텍스트를 큼직하게
    얹는 썸네일을 생성합니다. 줄마다 색상(기본: 빨강 → 파랑 → 하양 순환)을
    바꿔가며 그리고, 두꺼운 외곽선 + 텍스트 뒤 반투명 밴드로 어떤 배경 위에서도
    가독성을 확보합니다.

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

        stroke_w = max(2, size // 22)
        for i, line in enumerate(lines):
            bbox = draw.textbbox((0, 0), line, font=font)
            tw = bbox[2] - bbox[0]
            # 강제로 쪼갠 글자 단위 조각이 그래도 max_w를 넘는 극단적인
            # 경우(글자 하나가 max_w보다 넓은 경우)엔 0으로 클램프해서
            # 캔버스 밖으로 삐져나가지 않게 합니다.
            x = max(0, (width - tw) // 2)
            color = self._text_colors[i % len(self._text_colors)]
            draw.text(
                (x, y), line, font=font, fill=color,
                stroke_width=stroke_w, stroke_fill=(0, 0, 0, 255),
            )
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
