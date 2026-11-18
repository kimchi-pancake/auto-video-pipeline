"""
video/sfx.py — 장면 전환 효과음(휙 소리)을 외부 파일 없이 ffmpeg 필터로 합성해 시간 위치에 맞게 깔아 둔 트랙을 만든다.
과하지 않게 낮은 음량, 장면 경계 0.12초 전에 배치. 저작권 문제가 없는 자체 생성 소리다.
"""
from __future__ import annotations

from pathlib import Path
from typing import List

from video.ffmpeg_wrapper import FFmpegWrapper


def build_sfx_track(ff: FFmpegWrapper, times: List[float], total: float, workdir: Path, volume: float = 0.35) -> str:
    workdir = Path(workdir)
    whoosh = workdir / "whoosh.wav"
    if not whoosh.exists():
        ff.run([
            "-f", "lavfi", "-i", "anoisesrc=d=0.55:c=pink:r=44100:a=0.6",
            "-af", "highpass=f=250,lowpass=f=5200,afade=t=in:d=0.2,afade=t=out:st=0.22:d=0.33,volume=0.9",
            "-ac", "2", str(whoosh),
        ])
    out = workdir / "sfx_track.wav"
    times = [t for t in times if 0.3 < t < total - 0.3]
    if not times:
        ff.run(["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo", "-t", f"{total:.2f}", str(out)])
        return str(out)
    n = len(times)
    inputs: List[str] = []
    for _ in range(n):
        inputs += ["-i", str(whoosh)]
    parts = []
    for i, t in enumerate(times):
        ms = max(int((t - 0.12) * 1000), 0)
        parts.append(f"[{i}:a]adelay={ms}|{ms},volume={volume}[d{i}]")
    mix = "".join(f"[d{i}]" for i in range(n))
    parts.append(f"{mix}amix=inputs={n}:duration=longest:normalize=0,apad=whole_dur={total:.2f}[out]")
    ff.run([*inputs, "-filter_complex", ";".join(parts), "-map", "[out]", "-t", f"{total:.2f}", "-ac", "2", "-ar", "44100", str(out)])
    return str(out)
