"""
tools/upload_from_archive.py
=============================
유튜브 업로드가 실패했지만(인증 만료 등) 영상 자체는 이미 렌더링된 경우를
복구하는 스크립트. daily.yml이 매 실행마다 archive/ 폴더 전체를 GitHub
Actions artifact로 올려두므로(3일 보관), 그 artifact를 내려받아 압축을 푼
뒤 이 스크립트로 재업로드만 하면 됩니다 — 대본/이미지/TTS를 처음부터
다시 만들 필요가 없습니다.

사용법
------
1. 실패했던 daily.yml 실행의 GitHub Actions 페이지 → 맨 아래 Artifacts →
   "produced-videos-<run_id>" 다운로드 → 압축 해제.
2. 아래 실행 (채널명은 config.json에 등록된 이름과 정확히 일치해야 함):

    python tools/upload_from_archive.py <압축해제한 폴더 경로> 웃짬
    python tools/upload_from_archive.py <압축해제한 폴더 경로> 도개

찾은 영상 목록을 먼저 보여주고 확인(y/N)을 받은 뒤에만 업로드합니다.
전부 "비공개(private)"로 올라가니, 업로드 후 유튜브 스튜디오에서 직접
공개 전환/예약하면 됩니다(원래 파이프라인의 자동 예약공개 로직은 여기서
관여하지 않음 — 복구 목적상 안전하게 비공개로만 처리).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from utils.config_manager import get_config
from youtube.youtube_uploader import YouTubeUploader

_TS_SUFFIX = re.compile(r"_\d{8}_\d{6}$")


def _title_from_folder(folder_name: str) -> str:
    """'제목_20260724_081257' 형태의 아카이브 폴더명에서 타임스탬프를 뗍니다."""
    return _TS_SUFFIX.sub("", folder_name) or folder_name


def _find_runs(archive_root: Path) -> list[dict]:
    """archive_root 아래를 재귀로 뒤져서, 영상이 있는 각 실행 폴더를 찾습니다."""
    runs: list[dict] = []
    for mp4 in sorted(archive_root.rglob("*.mp4")):
        if mp4.name.endswith("_shorts.mp4"):
            continue  # 롱폼 mp4 기준으로 순회하고, 쇼츠는 같은 폴더에서 같이 찾음
        run_dir = mp4.parent
        title = _title_from_folder(run_dir.name)
        shorts_mp4 = next(run_dir.glob("*_shorts.mp4"), None)
        thumb_long = run_dir / "thumbnail_long.jpg"
        thumb_shorts = run_dir / "thumbnail_shorts.jpg"
        runs.append({
            "dir": run_dir,
            "title": title,
            "long_video": mp4,
            "shorts_video": shorts_mp4,
            "thumb_long": thumb_long if thumb_long.exists() else None,
            "thumb_shorts": thumb_shorts if thumb_shorts.exists() else None,
        })
    # 롱폼 mp4가 없고 쇼츠 mp4만 있는 폴더(쇼츠 단독 생성)도 놓치지 않게 별도 확인
    seen_dirs = {r["dir"] for r in runs}
    for mp4 in sorted(archive_root.rglob("*_shorts.mp4")):
        run_dir = mp4.parent
        if run_dir in seen_dirs:
            continue
        title = _title_from_folder(run_dir.name)
        thumb_shorts = run_dir / "thumbnail_shorts.jpg"
        runs.append({
            "dir": run_dir,
            "title": title,
            "long_video": None,
            "shorts_video": mp4,
            "thumb_long": None,
            "thumb_shorts": thumb_shorts if thumb_shorts.exists() else None,
        })
    return runs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive_dir", help="압축 해제한 artifact 폴더 경로")
    parser.add_argument("channel", help="config.json에 등록된 채널명 (예: 웃짬)")
    parser.add_argument("--yes", action="store_true", help="확인 프롬프트 없이 바로 업로드")
    args = parser.parse_args()

    archive_root = Path(args.archive_dir)
    if not archive_root.exists():
        print(f"경로가 존재하지 않습니다: {archive_root}")
        return 1

    cfg = get_config()
    channels = cfg.get("youtube.channels", []) or []
    chan = next((c for c in channels if c.get("name") == args.channel), None)
    if not chan:
        print(f"채널 '{args.channel}' 을(를) config.json 에서 찾지 못했습니다.")
        print("등록된 채널:", ", ".join(c.get("name", "?") for c in channels))
        return 1

    runs = _find_runs(archive_root)
    if not runs:
        print(f"{archive_root} 아래에서 mp4를 하나도 못 찾았습니다.")
        return 1

    print(f"채널 '{args.channel}' 대상, 발견된 영상 {len(runs)}개:")
    for r in runs:
        kinds = []
        if r["long_video"]:
            kinds.append("롱폼")
        if r["shorts_video"]:
            kinds.append("쇼츠")
        print(f"  - {r['title']}  [{', '.join(kinds)}]  ({r['dir']})")

    if not args.yes:
        ans = input(f"\n위 {len(runs)}개를 '{args.channel}' 채널에 비공개로 업로드할까요? (y/N): ")
        if ans.strip().lower() != "y":
            print("취소했습니다.")
            return 0

    yt_cfg = cfg.section("youtube")
    yt_cfg["credentials_file"] = chan.get("credentials_file", "")
    uploader = YouTubeUploader(yt_cfg)
    if not uploader.authenticate():
        print("유튜브 인증에 실패했습니다 — 먼저 tools/reauthorize_youtube.py 로 재인증하세요.")
        return 1

    default_tags = list(cfg.get("youtube.default_tags", []))
    shorts_tags = list(cfg.get("youtube.shorts_tags", [])) or default_tags
    description_of = lambda t: f"{t}\n\n이 영상은 Auto Video Pipeline으로 자동 생성되었습니다."

    ok, fail = 0, 0
    for r in runs:
        if r["long_video"]:
            vid = uploader.upload(
                video_path=r["long_video"],
                title=r["title"],
                description=description_of(r["title"]),
                tags=default_tags,
                privacy="private",
                thumbnail_path=r["thumb_long"],
            )
            print(f"[롱폼] {r['title']} -> {'https://youtu.be/' + vid if vid else '실패'}")
            ok += 1 if vid else 0
            fail += 0 if vid else 1
        if r["shorts_video"]:
            vid = uploader.upload(
                video_path=r["shorts_video"],
                title=f"{r['title']} #Shorts",
                description=description_of(r["title"]),
                tags=shorts_tags,
                privacy="private",
                thumbnail_path=r["thumb_shorts"],
            )
            print(f"[쇼츠] {r['title']} -> {'https://youtu.be/' + vid if vid else '실패'}")
            ok += 1 if vid else 0
            fail += 0 if vid else 1

    print(f"\n완료: 성공 {ok}개, 실패 {fail}개. 전부 비공개로 올라갔으니 유튜브 스튜디오에서 공개 전환하세요.")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
