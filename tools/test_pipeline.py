"""
tools/test_pipeline.py
======================
GUI 없이 커맨드라인에서 파이프라인 전체를 테스트합니다.
CI/CD 또는 개발 단계 검증에 사용합니다.

사용법:
    python tools/test_pipeline.py input/story_example.txt [--no-upload] [--no-images]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# 프로젝트 루트를 path에 추가
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from utils.logger import setup_logging, get_logger
from utils.config_manager import get_config
from utils.system_checker import SystemChecker
from core.pipeline import Pipeline, PipelineProgress


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Auto Video Pipeline — CLI 테스트 실행기"
    )
    parser.add_argument("story", nargs="?",
                        default="input/story_example.txt",
                        help="story.txt 파일 경로")
    parser.add_argument("--no-upload", action="store_true",
                        help="YouTube 업로드 건너뜀")
    parser.add_argument("--no-images", action="store_true",
                        help="이미지 준비(PHOTO/Pixabay) 건너뜀 (검정 화면 사용)")
    parser.add_argument("--privacy", default="private",
                        choices=["private", "public", "unlisted"],
                        help="YouTube 공개 범위")
    parser.add_argument("--days", type=int, default=0,
                        help="예약 업로드 일수 (0=즉시)")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


def on_progress(p: PipelineProgress) -> None:
    bar_width = 30
    filled = int(bar_width * p.overall_pct / 100)
    bar = "█" * filled + "░" * (bar_width - filled)
    print(
        f"\r[{bar}] {p.overall_pct:5.1f}%  [{p.stage_index+1}/{p.stage_total}] "
        f"{p.stage:<12}  {p.message:<40}",
        end="", flush=True,
    )


def main() -> int:
    args = parse_args()

    cfg = get_config()
    setup_logging(
        log_dir=ROOT / "logs",
        level=args.log_level,
    )
    log = get_logger("test_pipeline")

    # 시스템 점검
    print("=" * 60)
    print("  Auto Video Pipeline — CLI Test Runner")
    print("=" * 60)
    checker = SystemChecker(cfg.all())
    report = checker.run()
    print(report.summary())
    print()

    if not report.can_run:
        print("❌  시스템 요구사항 미충족. 중단합니다.")
        return 1

    # Pixabay 비활성화 옵션 (PHOTO 파일도 없다는 전제하에 검정 화면으로 대체됨)
    if args.no_images:
        cfg.set("image.pixabay_api_key", "")

    story_path = ROOT / args.story
    if not story_path.exists():
        print(f"❌  story.txt 파일 없음: {story_path}")
        return 1

    print(f"📄  story.txt: {story_path}")
    print(f"☁️   YouTube 업로드: {'아니오' if args.no_upload else '예'}")
    print(f"🖼️   이미지 생성: {'아니오(검정화면)' if args.no_images else '예'}")
    print()

    pipeline = Pipeline(cfg)
    pipeline.set_progress_callback(on_progress)

    start = time.time()
    result = pipeline.run(
        story_path=str(story_path),
        upload_to_youtube=not args.no_upload,
        youtube_privacy=args.privacy,
        schedule_days_ahead=args.days,
    )
    print()  # 프로그레스바 개행
    print()

    elapsed = time.time() - start
    print("=" * 60)
    if result.success:
        print(f"✅  완료  (총 {elapsed:.1f}초)")
        print(f"   영상: {result.video_path}")
        if result.thumbnail_long_path:
            print(f"   썸네일(Long): {result.thumbnail_long_path}")
        if result.thumbnail_shorts_path:
            print(f"   썸네일(Shorts): {result.thumbnail_shorts_path}")
        if result.youtube_video_id:
            print(f"   YouTube: https://youtu.be/{result.youtube_video_id}")
        if result.stage_times:
            print()
            print("  단계별 소요:")
            for k, v in result.stage_times.items():
                print(f"    {k:<15} {v:.1f}s")
        return 0
    else:
        print(f"❌  실패: {result.error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
