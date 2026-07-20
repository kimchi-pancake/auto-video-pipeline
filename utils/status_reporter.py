"""
utils/status_reporter.py
==========================
core/pipeline.py의 progress_callback으로 꽂는 리포터. edit도 안 쓰고, 진행
중에 알림도 안 울리게 하면서 "과정은 다 보이게" 하기 위해 알림/표시를
분리했습니다(Push vs Pull):

  - 진행 중 8단계: 새 텍스트 메시지를 매번 보내되, Discord의
    SUPPRESS_NOTIFICATIONS 플래그로 조용히 보냅니다 — 채널에 쌓여서
    스크롤하면 전 과정이 다 보이지만, 알림/뱃지는 안 뜹니다.
  - 완료 시: 진행률 카드(이미지) 1장을 새로 올리고, 이건 소리 나게
    보냅니다 — "다 됐다"는 사실만 알림으로 부릅니다.
  - 실패 시에도 즉시 소리 나는 카드로 알립니다.

메시지를 edit하지 않고 매번 새로 보내는 이유: edit 자체는 원래 무음이지만
edit 실패 시 새 메시지로 폴백하던 예전 로직이 반복 알림처럼 느껴졌던
원인이었고, 애초에 edit을 아예 안 쓰면 그 문제 자체가 성립하지 않습니다.
"""

from __future__ import annotations

from utils.logger import get_logger
from utils.notify import notify_discord_silent, notify_discord_status_create
from utils.status_card import render_status_card

logger = get_logger(__name__)

_COLOR_DONE = 0x3FB984
_COLOR_FAIL = 0xE05252


class DiscordStatusReporter:
    """core/pipeline.py의 PipelineProgress 콜백 시그니처(단일 인자)를 그대로
    받도록 __call__을 구현합니다. 성공/실패 마무리는 finish()로 따로 호출."""

    def __init__(self, title: str, channel: str):
        self._title = title
        self._channel = channel
        self._last_stage = -1

    def __call__(self, progress) -> None:
        # 같은 단계 안에서는 갱신하지 않음(예: 이미지 1/22 ~ 22/22) — 단계가
        # 바뀔 때만 한 줄 보내서 메시지 수를 아낍니다.
        if progress.stage_index == self._last_stage:
            return
        self._last_stage = progress.stage_index
        try:
            notify_discord_silent(
                f"⏳ [{self._channel}] {progress.stage} — {self._title} "
                f"({progress.stage_index + 1}/{progress.stage_total})"
            )
        except Exception:
            logger.exception("DiscordStatusReporter 진행 메시지 전송 실패")

    def finish(self, success: bool, youtube_video_id: str | None = None, error: str | None = None) -> None:
        """영상 하나 처리가 끝났을 때(성공/실패) 카드를 새로 올립니다(소리 남)."""
        message = "완료" if success else (error or "실패")
        if success and youtube_video_id:
            message = f"https://youtu.be/{youtube_video_id}"
        try:
            image_bytes = render_status_card(
                title=self._title, channel=self._channel,
                stage_index=7, stage_total=8, overall_pct=100.0,
                message=message, done=success, failed=not success,
            )
            embed = {
                "title": f"🎬 {self._channel}",
                "color": _COLOR_DONE if success else _COLOR_FAIL,
            }
            notify_discord_status_create(embed, image_bytes)
        except Exception:
            logger.exception("DiscordStatusReporter 결과 카드 전송 실패")
