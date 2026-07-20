"""
tools/push_ci_config.py
=========================
config/config.json을 GitHub Actions의 CONFIG_JSON 시크릿으로 올릴 때 반드시
이 스크립트를 거쳐서 올립니다 — `gh secret set CONFIG_JSON < config/config.json`을
직접 쓰면 로컬 전용 값(특히 video.ffmpeg_path/ffprobe_path의 Windows 절대
경로, 예: winget으로 설치된 C:\\Users\\...\\ffmpeg.exe)이 그대로 CI(Linux
러너)로 새어 들어가서 ffmpeg를 못 찾아 영상 합성이 통째로 실패합니다
(2026-07-20 실제로 겪은 사고 — 3번의 daily.yml 실행이 전부 이 이유로 실패).

사용법: python tools/push_ci_config.py
  (내부에서 `gh secret set CONFIG_JSON`을 호출하므로 gh CLI 로그인 상태 필요)
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
CONFIG_PATH = ROOT / "config" / "config.json"

# 로컬 환경에서만 유효하고 CI(Linux 러너)에는 절대 넘기면 안 되는 값들.
# CI는 apt-get install ffmpeg로 PATH에 깔아두므로 이름만 지정하면 됩니다.
_CI_OVERRIDES = {
    ("video", "ffmpeg_path"): "ffmpeg",
    ("video", "ffprobe_path"): "ffprobe",
}


def main() -> int:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    changed = []
    for (section, key), ci_value in _CI_OVERRIDES.items():
        current = cfg.get(section, {}).get(key)
        if current != ci_value:
            cfg.setdefault(section, {})[key] = ci_value
            changed.append(f"{section}.{key}: {current!r} → {ci_value!r}")

    if changed:
        print("CI용으로 아래 값을 덮어씀:")
        for line in changed:
            print(f"  - {line}")

    payload = json.dumps(cfg, ensure_ascii=False, indent=2)
    result = subprocess.run(
        ["gh", "secret", "set", "CONFIG_JSON"],
        input=payload.encode("utf-8"),
        cwd=ROOT,
    )
    if result.returncode != 0:
        print("gh secret set 실패", file=sys.stderr)
        return 1
    print("CONFIG_JSON 시크릿 갱신 완료.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
