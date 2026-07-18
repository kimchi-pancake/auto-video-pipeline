"""
core/batch_runner.py
=====================
input/ 폴더에 쌓인 story.txt 파일들을 순서대로 Pipeline 에 넘겨
연속 처리하는 배치 실행기.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, List, Optional

from core.pipeline import Pipeline, PipelineProgress, PipelineResult
from utils.config_manager import ConfigManager
from utils.file_utils import safe_move
from utils.logger import get_logger

logger = get_logger(__name__)


class BatchRunner:
    def __init__(
        self,
        config: ConfigManager,
        input_dir: str | Path,
        progress_callback: Optional[Callable[[PipelineProgress], None]] = None,
        on_file_done: Optional[Callable[[str, PipelineResult], None]] = None,
        youtube_credentials_file: Optional[str] = None,
        archive_subdir: str = "",
    ):
        self._cfg = config
        self._input_dir = Path(input_dir)
        self._progress_cb = progress_callback
        self._on_file_done = on_file_done
        self._stop_requested = False
        self._yt_creds = youtube_credentials_file

        root = Path(__file__).parent.parent
        self._archive_dir = root / self._cfg.get("paths.archive_dir", "archive")
        if archive_subdir:
            self._archive_dir = self._archive_dir / archive_subdir

    def get_pending_files(self) -> List[Path]:
        """input/ 의 .txt 파일들을 생성일(오래된 순) 기준으로 정렬해 반환합니다.

        수정 시간(mtime) 대신 생성 시간(ctime)을 쓰는 이유: 대본을 나중에
        열어서 수정해도 큐 순서(먼저 넣은 것부터)가 바뀌지 않도록 하기 위함.
        """
        if not self._input_dir.exists():
            return []
        return sorted(
            self._input_dir.glob("*.txt"),
            key=lambda p: p.stat().st_ctime,
        )

    def request_stop(self) -> None:
        self._stop_requested = True
        logger.info("Batch stop requested.")

    def run(
        self,
        count: int,
        upload_to_youtube: bool = True,
        youtube_privacy: str = "private",
        schedule_days_ahead: int = 0,
        also_make_shorts: bool = False,
    ) -> List[PipelineResult]:
        self._stop_requested = False
        pending = self.get_pending_files()
        if len(pending) < count:
            raise ValueError(
                f"요청 수량: {count}개, input/ 폴더에는 {len(pending)}개만 있습니다."
            )

        targets = pending[:count]
        results: List[PipelineResult] = []

        for i, story_path in enumerate(targets):
            if self._stop_requested:
                logger.info("Batch stopped before processing: %s", story_path)
                break

            logger.info("▶ 배치 %d/%d 시작: %s", i + 1, len(targets), story_path.name)
            pipeline = Pipeline(self._cfg)
            if self._progress_cb:
                pipeline.set_progress_callback(self._progress_cb)

            result = pipeline.run(
                story_path=story_path,
                upload_to_youtube=upload_to_youtube,
                youtube_privacy=youtube_privacy,
                schedule_days_ahead=schedule_days_ahead + i,
                youtube_credentials_file=self._yt_creds or None,
                also_make_shorts=also_make_shorts,
            )
            results.append(result)
            self._archive_input(story_path, result.success)

            if self._on_file_done:
                try:
                    self._on_file_done(str(story_path), result)
                except Exception:
                    logger.exception("on_file_done callback 오류")

        return results

    def _archive_input(self, story_path: Path, success: bool) -> None:
        sub = "input_processed" if success else "input_failed"
        dst_dir = self._archive_dir / sub
        try:
            safe_move(story_path, dst_dir / story_path.name)
        except Exception:
            logger.exception("입력 파일 이동 실패: %s", story_path)
