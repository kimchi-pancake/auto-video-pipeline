"""
v2/qc.py — 업로드 전 품질 게이트. 하나라도 실패하면 공개하지 않는다(NEEDS_REVIEW).
점검: 재생 가능·해상도·길이·음성 존재/무음 구간/음량, 첫·끝 프레임(검은 화면·무의미한 화면), 자막 타이밍,
      주장 검증, 사진 라이선스 기록, 제목 중복.
"""
from __future__ import annotations

import io
import re
import subprocess
from pathlib import Path
from typing import Dict, List

from PIL import Image, ImageStat

from utils.config_manager import get_config
from v2.topics import max_similarity


def _bins():
    cfg = get_config()
    return cfg.get("video.ffmpeg_path", "ffmpeg"), cfg.get("video.ffprobe_path", "ffprobe")


def _run(cmd: List[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def _frame(video: str, t: float) -> Image.Image | None:
    ff, _ = _bins()
    r = subprocess.run([ff, "-loglevel", "error", "-ss", f"{t:.2f}", "-i", video, "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                       capture_output=True, timeout=60)
    if r.returncode != 0 or not r.stdout:
        return None
    return Image.open(io.BytesIO(r.stdout)).convert("RGB")


def _frame_ok(im: Image.Image | None) -> tuple[bool, str]:
    if im is None:
        return False, "프레임을 읽지 못함"
    g = im.convert("L").resize((108, 192))
    st = ImageStat.Stat(g)
    mean, std = st.mean[0], st.stddev[0]
    if mean < 16:
        return False, f"거의 검은 화면(밝기 {mean:.0f})"
    if std < 9:
        return False, f"내용 없는 단색에 가까운 화면(편차 {std:.0f})"
    return True, f"밝기 {mean:.0f}/편차 {std:.0f}"


def check_video(video: str, ass: str) -> List[Dict]:
    ff, fp = _bins()
    checks: List[Dict] = []

    def add(name: str, ok: bool, detail: str = ""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    import json as _json
    p = _run([fp, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", video])
    if p.returncode != 0:
        add("playable", False, p.stderr[:120])
        return checks
    info = _json.loads(p.stdout)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    dur = float(info["format"].get("duration", 0))
    add("playable", True, f"{dur:.1f}s")
    add("resolution", bool(v) and (v["width"], v["height"]) == (1080, 1920), f"{v and v['width']}x{v and v['height']}")
    add("duration", 8 <= dur <= 75, f"{dur:.1f}s (허용 8~75초)")
    add("audio_stream", a is not None, "")

    first = [_frame(video, t) for t in (0.15, 0.7)]
    oks = [_frame_ok(f) for f in first]
    add("first_frame", all(o[0] for o in oks), "; ".join(o[1] for o in oks))
    last_ok = _frame_ok(_frame(video, max(dur - 0.6, 0.1)))
    add("last_frame", last_ok[0], last_ok[1])

    sil = _run([ff, "-hide_banner", "-i", video, "-af", "silencedetect=noise=-42dB:d=1.6", "-f", "null", "-"], timeout=180)
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", sil.stderr)]
    internal = [s for s in starts if 0.8 < s < dur - 1.5]
    add("no_long_silence", not internal, f"무음 시작 {internal[:3]}" if internal else "")
    vol = _run([ff, "-hide_banner", "-i", video, "-af", "volumedetect", "-f", "null", "-"], timeout=180)
    m = re.search(r"mean_volume: (-?[\d.]+) dB", vol.stderr)
    mean_db = float(m.group(1)) if m else -99.0
    add("audible", mean_db > -33, f"평균 {mean_db:.1f}dB")

    # 자막 타이밍
    try:
        events = re.findall(r"Dialogue: \d+,(\d+):(\d+):([\d.]+),(\d+):(\d+):([\d.]+),(\w+)", Path(ass).read_text(encoding="utf-8-sig"))
        body = [e for e in events if e[6] == "Default"]
        ends = [int(e[3]) * 3600 + int(e[4]) * 60 + float(e[5]) for e in body]
        starts_ = [int(e[0]) * 3600 + int(e[1]) * 60 + float(e[2]) for e in body]
        add("subtitle_sync", bool(body) and max(ends) <= dur + 0.8 and max(ends) >= dur * 0.8 and starts_ == sorted(starts_),
            f"자막 {len(body)}개, 마지막 {max(ends) if ends else 0:.1f}s / 영상 {dur:.1f}s")
    except Exception as e:  # noqa: BLE001
        add("subtitle_sync", False, str(e)[:80])
    return checks


def check_job(job: Dict, recent_titles: List[str]) -> List[Dict]:
    checks: List[Dict] = []
    claims = job.get("claims") or []
    unver = [c for c in claims if not c.get("verified")]
    checks.append({"name": "claims_verified", "ok": bool(claims) and not unver, "detail": f"{len(claims)}개 중 미검증 {len(unver)}개"})
    photos = [a for a in (job.get("assets") or []) if a.get("meta", {}).get("photo")]
    bad = [a for a in photos if not a["meta"]["photo"].get("license") or not a["meta"]["photo"].get("page")]
    checks.append({"name": "photo_licenses", "ok": not bad, "detail": f"사진 {len(photos)}장, 라이선스 기록 누락 {len(bad)}장"})
    title = (job.get("script") or {}).get("title", "")
    sim = max_similarity(title, recent_titles)
    checks.append({"name": "not_duplicate", "ok": sim < 0.8, "detail": f"최근 제목과 최대 유사도 {sim:.2f}"})
    fb = [a for a in (job.get("assets") or []) if a.get("meta", {}).get("fallback_from")]
    checks.append({"name": "visual_fallbacks", "ok": len(fb) <= 2, "detail": f"대체된 장면 {len(fb)}개"})
    return checks


def summarize(checks: List[Dict]) -> Dict:
    failed = [c for c in checks if not c["ok"]]
    return {"passed": not failed, "failed": [c["name"] for c in failed], "checks": checks}
