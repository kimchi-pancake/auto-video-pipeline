"""
utils/status_reporter.py
==========================
core/pipeline.py의 progress_callback으로 꽂아서, 영상 하나가 시작될 때와
끝날 때 딱 두 번만 디스코드에 "진행률 카드"(이미지)를 올리는 리포터.

예전엔 단계(stage)가 바뀔 때마다(8단계) 메시지를 edit했는데, 실제로 써보니
edit이 실패해서 새 메시지로 재생성되는 경우가 있었고, 결과적으로 단계마다
알림이 여러 번 오는 것처럼 느껴진다는 피드백을 받았습니다. 텍스트 알림도
"시작/끝만" 남기기로 한 것과 동일하게, 카드도 시작 1번 + 완료 1번으로
줄였습니다.
"""

from __future__ import annotations

from utils.logger import get_logger
from utils.notify import notify_discord_status_create, notify_discord_status_update
from utils.status_card import render_status_card

logger = get_logger(__name__)

_COLOR_PROGRESS = 0x58A6FF
_COLOR_DONE = 0x3FB984
_COLOR_FAIL = 0xE05252


class DiscordStatusReporter:
    """core/pipeline.py의 PipelineProgress 콜백 시그니처(단일 인자)를 그대로
    받도록 __call__을 구현합니다. 성공/실패 마무리는 finish()로 따로 호출."""

    def __init__(self, title: str, channel: str):
        self._title = title
        self._channel = channel
        self._message_id: str | None = None

    def __call__(self, progress) -> None:
        # 시작 카드는 딱 한 번만 올림 — 그 이후 단계 진행 상황은 더 이상
        # 갱신하지 않고, finish()가 마지막에 한 번 더 갱신합니다.
        if self._message_id is not None:
            return
        self._push(progress.stage_index, progress.stage_total, progress.overall_pct, progress.message)

    def _push(self, stage_index: int, stage_total: int, overall_pct: float, message: str, done: bool = False, failed: bool = False) -> None:
        try:
            image_bytes = render_status_card(
                title=self._title, channel=self._channel,
                stage_index=stage_index, stage_total=stage_total,
                overall_pct=overall_pct, message=message,
                done=done, failed=failed,
            )
            color = _COLOR_FAIL if failed else (_COLOR_DONE if done else _COLOR_PROGRESS)
            embed = {"title": f"🎬 {self._channel}", "color": color}
            if self._message_id is None:
                self._message_id = notify_discord_status_create(embed, image_bytes)
            else:
                ok = notify_discord_status_update(self._message_id, embed, image_bytes)
                if not ok:
                    # 메시지가 어떤 이유로든 사라졌으면(수동 삭제 등) 새로 만듦
                    self._message_id = notify_discord_status_create(embed, image_bytes)
        except Exception:
            logger.exception("DiscordStatusReporter 갱신 실패")

    def finish(self, success: bool, youtube_video_id: str | None = None, error: str | None = None) -> None:
        """영상 하나 처리가 끝났을 때(성공/실패) 카드를 최종 상태로 갱신합니다."""
        message = "완료" if success else (error or "실패")
        if success and youtube_video_id:
            message = f"https://youtu.be/{youtube_video_id}"
        self._push(7, 8, 100.0, message, done=success, failed=not success)
