"""
v2/visuals/common.py — 공용 그리기 도구(Pillow), 애니메이션 → mp4 변환(ffmpeg 파이프).
화면 구역(1080x1920): 상단 제목 ~y 140-420(자막 빌더가 그림), 내용 y 440-1250, 하단 자막 ~y 1300-1500.
카드/차트는 내용 구역 안에만 그린다.
"""
from __future__ import annotations

import math
import subprocess
from pathlib import Path
from typing import Callable, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from v2 import ROOT

W, H = 1080, 1920
CONTENT_TOP, CONTENT_BOTTOM = 470, 1270
FONT_BOLD = ROOT / "assets" / "fonts" / "NanumGothicBold.ttf"
FONT_REG = ROOT / "assets" / "fonts" / "NanumGothic.ttf"

_font_cache: dict = {}


def font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(str(FONT_BOLD if bold else FONT_REG), size)
    return _font_cache[key]


def hex_rgb(h: str) -> Tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def ease_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def ease_in_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 0.5 - 0.5 * math.cos(math.pi * t)


def gradient_bg(top: str, bottom: str = None, accent: str = None) -> Image.Image:
    """세로 그라디언트 + 은은한 대각 포인트 도형."""
    t = hex_rgb(top)
    b = hex_rgb(bottom) if bottom else tuple(min(255, int(c * 1.9) + 6) for c in t)
    base = Image.new("RGB", (1, H))
    px = base.load()
    for y in range(H):
        k = y / (H - 1)
        px[0, y] = tuple(int(t[i] + (b[i] - t[i]) * k) for i in range(3))
    img = base.resize((W, H))
    if accent:
        ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(ov)
        ar = hex_rgb(accent)
        d.polygon([(0, 1180), (W, 880), (W, 1000), (0, 1300)], fill=(*ar, 22))
        d.polygon([(0, 260), (W, 120), (W, 170), (0, 320)], fill=(*ar, 28))
        img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
    return img


def text_w(draw: ImageDraw.ImageDraw, s: str, f) -> int:
    return int(draw.textlength(s, font=f))


def fit_font(draw: ImageDraw.ImageDraw, s: str, max_w: int, start: int, min_size: int = 28, bold: bool = True):
    size = start
    while size > min_size:
        f = font(size, bold)
        if text_w(draw, s, f) <= max_w:
            return f
        size -= 4
    return font(min_size, bold)


def wrap(draw: ImageDraw.ImageDraw, s: str, f, max_w: int) -> list:
    """한국어 어절 단위 줄바꿈(어절이 너무 길면 글자 단위)."""
    lines, cur = [], ""
    for word in s.split():
        cand = (cur + " " + word).strip()
        if text_w(draw, cand, f) <= max_w:
            cur = cand
            continue
        if cur:
            lines.append(cur)
        if text_w(draw, word, f) > max_w:
            part = ""
            for ch in word:
                if text_w(draw, part + ch, f) > max_w:
                    lines.append(part)
                    part = ch
                else:
                    part += ch
            cur = part
        else:
            cur = word
    if cur:
        lines.append(cur)
    return lines


def draw_text_center(d: ImageDraw.ImageDraw, xy: Tuple[int, int], s: str, f, fill, stroke: int = 0, stroke_fill=(0, 0, 0), alpha: float = 1.0):
    x, y = xy
    w = text_w(d, s, f)
    kw = {"stroke_width": stroke, "stroke_fill": stroke_fill} if stroke else {}
    d.text((x - w // 2, y), s, font=f, fill=fill, **kw)


def ffmpeg_bin() -> str:
    try:
        from utils.config_manager import get_config
        return get_config().get("video.ffmpeg_path", "ffmpeg") or "ffmpeg"
    except Exception:
        return "ffmpeg"


def render_animation(draw_fn: Callable[[float], Image.Image], seconds: float, out_path: Path, hold: float = 8.0, fps: int = 30) -> Path:
    """draw_fn(progress 0→1) 로 프레임을 그려 mp4로 만든다. 애니메이션이 끝난 뒤 마지막 프레임을 hold초 유지한다
    (장면 길이가 더 길어도 반복 재생되지 않게 — 합성 단계는 'anim' 소스를 마지막 프레임으로 연장한다)."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg_bin(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps),
           "-i", "-", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-pix_fmt", "yuv420p", str(out_path)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    n = max(int(seconds * fps), 2)
    last = b""
    try:
        for i in range(n):
            frame = draw_fn(i / (n - 1)).convert("RGB")
            last = frame.tobytes()
            p.stdin.write(last)
        for _ in range(int(hold * fps)):
            p.stdin.write(last)
        p.stdin.close()
        err = p.stderr.read().decode("utf-8", "replace")
        if p.wait() != 0:
            raise RuntimeError(f"ffmpeg anim failed: {err[:300]}")
    finally:
        if p.poll() is None:
            p.kill()
    return out_path


def save_png(img: Image.Image, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(path, "PNG")
    return path
