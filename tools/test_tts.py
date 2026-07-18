"""
tools/test_tts.py
=================
Edge-TTS 음성 생성을 단독으로 테스트합니다.

사용법:
    python tools/test_tts.py [story.txt 경로]
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from utils.logger import setup_logging
from utils.config_manager import get_config
from parser.story_parser import StoryParser
from tts.tts_builder import TTSBuilder


def main() -> None:
    setup_logging(log_dir=ROOT / "logs", level="DEBUG")
    cfg = get_config()

    story_path = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "input/story_example.txt")

    print(f"Parsing: {story_path}")
    parser = StoryParser(story_path)
    data = parser.parse()

    temp_dir = ROOT / "temp" / "tts_test"
    temp_dir.mkdir(parents=True, exist_ok=True)

    def progress(done, total, rid):
        print(f"  [{done}/{total}] {rid}", end="\r")

    builder = TTSBuilder(
        tts_config=cfg.section("tts"),
        temp_dir=temp_dir,
        progress_callback=progress,
    )

    scene_audios = builder.build(data)
    print()
    print(f"\n✅  TTS 완료: {len(scene_audios)} 씬")
    for sa in scene_audios:
        print(f"  Scene {sa.scene_idx}: {len(sa.segments)} segments, "
              f"total={sa.total_duration:.2f}s, all_ok={sa.all_success}")
        for seg in sa.segments:
            status = "✅" if seg.success else "❌"
            print(f"    {status} [{seg.speaker}] {seg.text!r} ({seg.duration:.2f}s)")


if __name__ == "__main__":
    main()
