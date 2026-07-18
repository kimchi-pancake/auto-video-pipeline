"""
tools/test_parser.py
====================
StoryParser 단위 테스트.
실제 story_example.txt 를 파싱하여 구조를 출력합니다.

사용법:
    python tools/test_parser.py [story.txt 경로]
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from utils.logger import setup_logging
from parser.story_parser import StoryParser


def main() -> None:
    setup_logging(log_dir=ROOT / "logs", level="DEBUG")

    story_path = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "input/story_example.txt")

    print(f"Parsing: {story_path}")
    print("=" * 60)

    parser = StoryParser(story_path)
    data = parser.parse()

    print(f"CAST ({len(data.cast)}):")
    for k, v in data.cast.items():
        print(f"  {k} → {v}")

    print(f"\nBGM ({len(data.bgm_files)}):")
    for b in data.bgm_files:
        print(f"  {b}")

    print(f"\nTHUMBNAIL_LONG: {data.thumbnail_long}")
    print(f"THUMBNAIL_SHORTS: {data.thumbnail_shorts}")
    print(f"제목: {data.raw_title!r}")

    print(f"\nSCENES ({len(data.scenes)}):")
    for scene in data.scenes:
        print(f"\n  [Scene {scene.index}] prompt={scene.prompt[:60]!r}")
        print(f"    resolution={scene.resolution}  seed={scene.seed}")
        for dial in scene.dialogues:
            print(f"    [{dial.speaker}] segments={dial.segments}")

    print("\n" + "=" * 60)
    print("✅ 파싱 성공")


if __name__ == "__main__":
    main()
