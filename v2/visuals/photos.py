"""
v2/visuals/photos.py — 커먼즈 사진을 세로 1080x1920 캔버스에 배치.
가로 사진은 '흐린 배경 + 폭에 맞춘 원본'으로 크롭 손실 없이 보여주고(숏폼 편집에서 흔한 방식),
세로 사진은 꽉 채운다. 이후 합성 단계가 캔버스 전체에 천천히 줌/팬을 건다.
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Dict

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from v2.sources.http import get
from v2.visuals.common import H, W


def download(asset: Dict, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = get(asset["url"], timeout=60)
    r.raise_for_status()
    Image.open(BytesIO(r.content)).verify()  # 깨진 파일 차단
    dest.write_bytes(r.content)
    return dest


def compose_canvas(src: Path, out: Path) -> Path:
    im = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    w, h = im.size
    ratio = w / h
    if ratio <= 0.62:  # 이미 세로형 → 꽉 채움
        canvas = ImageOps.fit(im, (W, H), method=Image.LANCZOS, centering=(0.5, 0.45))
    else:
        bg = ImageOps.fit(im, (W, H), method=Image.LANCZOS).filter(ImageFilter.GaussianBlur(38))
        bg = ImageEnhance.Brightness(bg).enhance(0.45)
        fw = W if ratio >= 0.95 else int(W * 0.94)
        fh = int(fw / ratio)
        if fh > H * 0.62:           # 너무 세로로 긴 사진은 높이 기준
            fh = int(H * 0.62)
            fw = int(fh * ratio)
        fg = im.resize((fw, fh), Image.LANCZOS)
        canvas = bg.copy()
        x, y = (W - fw) // 2, int((H - fh) * 0.46)
        shadow = Image.new("RGBA", (fw + 60, fh + 60), (0, 0, 0, 0))
        sh = Image.new("RGBA", (fw, fh), (0, 0, 0, 150))
        shadow.paste(sh, (30, 30))
        shadow = shadow.filter(ImageFilter.GaussianBlur(18))
        canvas = canvas.convert("RGBA")
        canvas.alpha_composite(shadow, (x - 30, y - 30))
        canvas = canvas.convert("RGB")
        canvas.paste(fg, (x, y))
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, "JPEG", quality=92)
    return out
