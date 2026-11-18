"""
v2/visuals/cards.py — 숫자 카드(stat), 키네틱 문구(text), 인과 단계(steps), 비교 막대(compare), 차트(chart).
모두 애니메이션 mp4로 렌더링한다. 모든 값은 호출자가 검증한 것만 받는다(여기서 숫자를 만들지 않는다).
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from PIL import Image, ImageDraw

from v2.sources.worldbank import COUNTRIES
from v2.visuals.common import (CONTENT_BOTTOM, CONTENT_TOP, H, W, draw_text_center, ease_in_out, ease_out, fit_font,
                               font, gradient_bg, hex_rgb, render_animation, wrap)

WHITE = (245, 247, 250)
MUTED = (150, 160, 178)
SERIES_COLORS = ["#FFE500", "#4FD1FF", "#FF7A59"]


_BG_CACHE: dict = {}


def _bg(profile) -> Image.Image:
    """그라디언트 배경은 만드는 데 비싸서(프레임마다 새로 만들면 차트 한 개에 1분) 한 번만 만들고 복사해 쓴다."""
    key = (profile.palette_bg, profile.accent_color)
    if key not in _BG_CACHE:
        _BG_CACHE[key] = gradient_bg(profile.palette_bg, None, profile.accent_color)
    return _BG_CACHE[key].copy()


def _source_tag(d: ImageDraw.ImageDraw, text: str):
    f = font(30, False)
    draw_text_center(d, (W // 2, CONTENT_BOTTOM + 34), text, f, MUTED)


def stat(profile, big: str, small: str, out: Path, source: str = "") -> Path:
    acc = hex_rgb(profile.accent_color)

    def draw(t: float) -> Image.Image:
        img = _bg(profile)
        d = ImageDraw.Draw(img)
        k = ease_out(t / 0.45)
        f = fit_font(d, big, 900, 280, 90)
        scale_y = CONTENT_TOP + 230 - int((1 - k) * 40)
        alpha_col = tuple(int(c * k + 20 * (1 - k)) for c in acc)
        draw_text_center(d, (W // 2, scale_y), big, f, alpha_col, stroke=6, stroke_fill=(10, 12, 18))
        bar_w = int(560 * ease_out((t - 0.15) / 0.4))
        d.rounded_rectangle((W // 2 - bar_w // 2, scale_y + f.size + 60, W // 2 + bar_w // 2, scale_y + f.size + 70), 5, fill=acc)
        k2 = ease_out((t - 0.3) / 0.4)
        sf = fit_font(d, small, 900, 58, 34, bold=True)
        draw_text_center(d, (W // 2, scale_y + f.size + 105 + int((1 - k2) * 20)), small, sf, tuple(int(c * k2) for c in WHITE))
        if source:
            _source_tag(d, source)
        return img
    return render_animation(draw, 1.4, out)


def kinetic_text(profile, headline: str, sub: str, out: Path) -> Path:
    acc = hex_rgb(profile.accent_color)
    words = headline.split()

    def draw(t: float) -> Image.Image:
        img = _bg(profile)
        d = ImageDraw.Draw(img)
        f = fit_font(d, headline, 900, 110, 64)
        # 단어 단위로 줄바꿈 후 단어별 순차 등장
        lines = wrap(d, headline, f, 900)
        line_h = int(f.size * 1.35)
        y0 = CONTENT_TOP + 160 - (len(lines) * line_h) // 2 + 150
        idx = 0
        total_words = max(len(words), 1)
        for li, line in enumerate(lines):
            lw = [w for w in line.split()]
            x = W // 2 - int(d.textlength(line, font=f)) // 2
            for w in lw:
                prog = ease_out((t * 1.25 * total_words - idx) / 1.0)
                col = tuple(int(c * prog + 14 * (1 - prog)) for c in (acc if idx == total_words - 1 else WHITE))
                d.text((x, y0 + li * line_h + int((1 - prog) * 24)), w, font=f, fill=col, stroke_width=4, stroke_fill=(10, 12, 18))
                x += int(d.textlength(w + " ", font=f))
                idx += 1
        if sub:
            k = ease_out((t - 0.55) / 0.35)
            sf = fit_font(d, sub, 900, 48, 30, bold=False)
            draw_text_center(d, (W // 2, y0 + len(lines) * line_h + 40), sub, sf, tuple(int(c * k) for c in MUTED))
        return img
    return render_animation(draw, 1.6, out)


def steps(profile, title: str, items: List[str], out: Path) -> Path:
    acc = hex_rgb(profile.accent_color)
    n = len(items)

    def draw(t: float) -> Image.Image:
        img = _bg(profile)
        d = ImageDraw.Draw(img)
        if title:
            tf = fit_font(d, title, 900, 54, 32)
            draw_text_center(d, (W // 2, CONTENT_TOP - 10), title, tf, MUTED)
        box_h, gap = 150, 58
        total = n * box_h + (n - 1) * gap
        y = CONTENT_TOP + 70 + (CONTENT_BOTTOM - CONTENT_TOP - 70 - total) // 2
        for i, it in enumerate(items):
            p = ease_out((t * (n + 0.6) - i) / 0.9)
            if p <= 0:
                continue
            x0, x1 = 90, W - 90
            off = int((1 - p) * 60)
            ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            od = ImageDraw.Draw(ov)
            fill = (255, 255, 255, int(22 * p)) if i < n - 1 else (*acc, int(60 * p))
            od.rounded_rectangle((x0, y + off, x1, y + off + box_h), 28, fill=fill, outline=(*acc, int(200 * p)), width=3)
            img.paste(Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB"))
            d = ImageDraw.Draw(img)
            nf = font(56)
            d.text((x0 + 36, y + off + box_h // 2 - 34), str(i + 1), font=nf, fill=tuple(int(c * p) for c in acc))
            f = fit_font(d, it, x1 - x0 - 190, 56, 30)
            d.text((x0 + 120, y + off + box_h // 2 - f.size // 2 - 4), it, font=f, fill=tuple(int(c * p) for c in WHITE))
            if i < n - 1:
                ay = y + box_h + 8
                ap = ease_out((t * (n + 0.6) - i - 0.6) / 0.5)
                if ap > 0:
                    cx = W // 2
                    d.polygon([(cx - 22, ay + 6), (cx + 22, ay + 6), (cx, ay + 40)], fill=tuple(int(c * ap) for c in acc))
            y += box_h + gap
        return img
    return render_animation(draw, 1.2 + 0.6 * n, out)


def compare(profile, title: str, a: Dict, b: Dict, unit: str, out: Path, source: str = "") -> Path:
    acc = hex_rgb(profile.accent_color)
    va, vb = float(a["value"]), float(b["value"])
    vmax = max(abs(va), abs(vb), 1e-9)
    base_y = CONTENT_BOTTOM - 120
    max_h = 520

    def fmt(v: float) -> str:
        s = f"{v:,.1f}" if abs(v) < 100 and v != int(v) else f"{v:,.0f}"
        return f"{s}{unit}" if unit else s

    def draw(t: float) -> Image.Image:
        img = _bg(profile)
        d = ImageDraw.Draw(img)
        if title:
            tf = fit_font(d, title, 900, 52, 30)
            draw_text_center(d, (W // 2, CONTENT_TOP), title, tf, MUTED)
        k = ease_in_out(t / 0.7)
        for i, (side, v, col) in enumerate(((a, va, (110, 150, 255)), (b, vb, acc))):
            cx = W // 2 + (-230 if i == 0 else 230)
            h = int(max_h * abs(v) / vmax * k)
            d.rounded_rectangle((cx - 120, base_y - h, cx + 120, base_y), 18, fill=col)
            vf = fit_font(d, fmt(v * k if k < 1 else v), 260, 56, 30)
            draw_text_center(d, (cx, base_y - h - 76), fmt(v * k if k < 1 else v), vf, WHITE, stroke=3, stroke_fill=(10, 12, 18))
            lf = fit_font(d, side["label"], 260, 44, 26)
            draw_text_center(d, (cx, base_y + 18), side["label"], lf, WHITE)
        d.line((120, base_y, W - 120, base_y), fill=(70, 80, 100), width=3)
        if source:
            _source_tag(d, source)
        return img
    return render_animation(draw, 1.6, out)


def line_chart(profile, data: Dict, out: Path, headline: str = "") -> Path:
    """data = worldbank.fetch_series() 결과. 선이 왼쪽에서 오른쪽으로 그려지고 끝점에 값이 표시된다."""
    series = data["series"]
    allpts = [p for pts in series.values() for p in pts]
    y0 = min(p[0] for p in allpts)
    y1 = max(p[0] for p in allpts)
    vmin = min(p[1] for p in allpts)
    vmax = max(p[1] for p in allpts)
    if vmax == vmin:
        vmax += 1
    pad = (vmax - vmin) * 0.12
    vmin_p, vmax_p = vmin - pad, vmax + pad
    L, R, T, B = 130, W - 90, CONTENT_TOP + 120, CONTENT_BOTTOM - 110
    codes = list(series.keys())
    colors = [hex_rgb(SERIES_COLORS[i % 3]) for i in range(len(codes))]

    def xy(year: int, v: float):
        return (L + (year - y0) / max(y1 - y0, 1) * (R - L), B - (v - vmin_p) / (vmax_p - vmin_p) * (B - T))

    def fmt(v: float) -> str:
        unit = data["unit"]
        if abs(v) >= 1000:
            s = f"{v:,.0f}"
        else:
            s = f"{v:.1f}"
        return s + (unit if unit in ("%",) else "")

    # 움직이지 않는 부분(배경·격자·축·범례)은 한 번만 그린다.
    static = _bg(profile)
    sd = ImageDraw.Draw(static)
    label = f"{data['label']}" + (f" ({data['unit']})" if data["unit"] and data["unit"] != "%" else "")
    draw_text_center(sd, (W // 2, CONTENT_TOP - 20), label, fit_font(sd, label, 900, 52, 30), WHITE)
    for i in range(5):
        gy = T + (B - T) * i / 4
        sd.line((L, gy, R, gy), fill=(48, 58, 78), width=2)
        val = vmax_p - (vmax_p - vmin_p) * i / 4
        sd.text((12, gy - 16), f"{val:,.0f}" if abs(val) >= 100 else f"{val:.1f}", font=font(28, False), fill=(120, 132, 152))
    if vmin_p < 0 < vmax_p:
        zy = xy(y0, 0)[1]
        sd.line((L, zy, R, zy), fill=(120, 132, 152), width=3)
    for yr in (y0, (y0 + y1) // 2, y1):
        x, _ = xy(yr, vmin_p)
        sd.text((x - 30, B + 14), str(yr), font=font(30, False), fill=(150, 160, 178))
    lx = L
    for c, col in zip(codes, colors):
        name = COUNTRIES.get(c, c)
        sd.rounded_rectangle((lx, B + 80, lx + 26, B + 106), 6, fill=col)
        sd.text((lx + 36, B + 74), name, font=font(34), fill=WHITE)
        lx += 60 + int(sd.textlength(name, font=font(34)))
    _source_tag(sd, "자료: 세계은행")

    def draw(t: float) -> Image.Image:
        img = static.copy()
        d = ImageDraw.Draw(img)
        prog_year = y0 + (y1 - y0) * ease_in_out(t)
        ends = []
        for c, col in zip(codes, colors):
            pts = [p for p in series[c] if p[0] <= prog_year]
            if len(pts) >= 2:
                d.line([xy(*p) for p in pts], fill=col, width=7, joint="curve")
            if pts:
                ex, ey = xy(*pts[-1])
                d.ellipse((ex - 11, ey - 11, ex + 11, ey + 11), fill=col, outline=(10, 12, 18), width=3)
                ends.append([ex, ey - 62, fmt(pts[-1][1]), col])
        # 끝값 라벨이 서로 겹치지 않게 위에서부터 최소 간격을 강제
        ends.sort(key=lambda e: e[1])
        for i in range(1, len(ends)):
            if ends[i][1] - ends[i - 1][1] < 48:
                ends[i][1] = ends[i - 1][1] + 48
        for ex, ly, vt, col in ends:
            tw = int(d.textlength(vt, font=font(40)))
            d.text((min(max(ex - tw // 2, 10), W - tw - 10), ly), vt, font=font(40), fill=col, stroke_width=3, stroke_fill=(10, 12, 18))
        d.text((W - 90 - 140, B + 14), f"{int(prog_year)}", font=font(30), fill=(220, 226, 236))
        return img
    return render_animation(draw, 3.8, out)
