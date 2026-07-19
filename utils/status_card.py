"""
utils/status_card.py
======================
디스코드에 올릴 "지금 이 영상 만드는 중" 상태 카드를 PIL로 그립니다.
GUI 앱과 같은 다크 테마 카드 느낌(둥근 모서리, 진행률 바, 단계 체크리스트)으로
맞췄습니다 — 텍스트 알림보다 한눈에 보기 좋게 하려는 용도입니다.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_ASSETS_FONTS = Path(__file__).parent.parent / "assets" / "fonts"

_BG = (24, 25, 28)
_CARD = (32, 33, 38)
_ACCENT = (88, 166, 255)       # 진행 중 (파랑)
_ACCENT_DONE = (63, 185, 132)  # 완료 (초록)
_ACCENT_FAIL = (224, 82, 82)   # 실패 (빨강)
_TEXT = (235, 235, 240)
_TEXT_DIM = (140, 142, 150)
_TRACK = (52, 53, 60)

STAGES_KO = ["파싱", "TTS 생성", "이미지 생성", "자막 생성", "영상 합성", "썸네일 생성", "YouTube 업로드", "정리"]

W, H = 900, 400

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = ("bold" if bold else "regular", size)
    if key in _font_cache:
        return _font_cache[key]
    name = "NanumGothicBold" if bold else "NanumGothic"
    path = _ASSETS_FONTS / f"{name}.ttf"
    font = ImageFont.truetype(str(path), size) if path.exists() else ImageFont.load_default()
    _font_cache[key] = font
    return font


def _truncate(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def _fit_title_font(
    draw: ImageDraw.ImageDraw, text: str, max_w: int, max_size: int = 30, min_size: int = 15,
) -> ImageFont.FreeTypeFont:
    """제목을 자르지 않고, 칸(max_w) 안에 들어갈 때까지 글자 크기를 줄여서
    반환합니다. 최소 크기에서도 안 들어가면 그 이하로는 더 줄이지 않고
    최소 크기 폰트를 반환합니다(그림에서 draw.text가 잘리는 건 허용)."""
    size = max_size
    font = _font(size, bold=True)
    while size > min_size and draw.textlength(text, font=font) > max_w:
        size -= 1
        font = _font(size, bold=True)
    return font


def render_status_card(
    title: str,
    channel: str,
    stage_index: int,
    stage_total: int,
    overall_pct: float,
    message: str,
    done: bool = False,
    failed: bool = False,
) -> bytes:
    """상태 카드를 PNG 바이트로 렌더링합니다."""
    img = Image.new("RGB", (W, H), _BG)
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle([16, 16, W - 16, H - 16], radius=20, fill=_CARD)

    badge_color = _ACCENT_FAIL if failed else (_ACCENT_DONE if done else _ACCENT)
    draw.text((44, 38), channel, font=_font(20, bold=True), fill=badge_color)

    status_label = "실패" if failed else ("완료" if done else "진행 중")
    status_w = draw.textlength(status_label, font=_font(18, bold=True))
    draw.rounded_rectangle(
        [W - 44 - status_w - 28, 34, W - 44, 34 + 32], radius=16, fill=badge_color,
    )
    draw.text((W - 44 - status_w - 14, 40), status_label, font=_font(18, bold=True), fill=(20, 20, 22))

    title_font = _fit_title_font(draw, title, W - 88)
    draw.text((44, 78), title, font=title_font, fill=_TEXT)

    bar_x0, bar_y0, bar_x1, bar_y1 = 44, 150, W - 44, 182
    draw.rounded_rectangle([bar_x0, bar_y0, bar_x1, bar_y1], radius=16, fill=_TRACK)
    fill_color = _ACCENT_FAIL if failed else (_ACCENT_DONE if done else _ACCENT)
    pct = 100.0 if done else max(0.0, min(overall_pct, 100.0))
    fill_w = int((bar_x1 - bar_x0) * pct / 100)
    if fill_w > 0:
        draw.rounded_rectangle(
            [bar_x0, bar_y0, bar_x0 + max(fill_w, 32), bar_y1], radius=16, fill=fill_color,
        )
    draw.text((bar_x0, bar_y1 + 10), f"{pct:.0f}%", font=_font(20, bold=True), fill=_TEXT)
    msg_display = _truncate(draw, message, _font(18), 340)
    draw.text((bar_x1 - 340, bar_y1 + 12), msg_display, font=_font(18), fill=_TEXT_DIM)

    step_y = 240
    step_x = 44
    col_w = (W - 88) / 4
    for i, name in enumerate(STAGES_KO):
        col = i % 4
        row = i // 4
        x = step_x + col * col_w
        y = step_y + row * 56
        if failed and i == stage_index:
            dot_color, label_color = _ACCENT_FAIL, _TEXT
        elif i < stage_index or done:
            dot_color, label_color = _ACCENT_DONE, _TEXT
        elif i == stage_index:
            dot_color, label_color = _ACCENT, _TEXT
        else:
            dot_color, label_color = _TRACK, _TEXT_DIM
        draw.ellipse([x, y + 2, x + 14, y + 16], fill=dot_color)
        draw.text((x + 22, y - 2), name, font=_font(17), fill=label_color)

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
