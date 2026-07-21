"""
utils/status_reporter.py
==========================
core/pipeline.py의 progress_callback에 꽂는 리포터. 채널별 디스코드
스레드(도개/웃짬) 안에 메시지를 딱 하나만 만들고, 단계가 바뀔 때마다 그
메시지를 계속 수정(edit)합니다 — 새 메시지를 추가로 보내지 않습니다
(완료/실패 결과도 같은 메시지를 마지막으로 한 번 더 수정해서 반영).
"""

from __future__ import annotations

from utils.logger import get_logger
from utils.notify import notify_discord_edit, notify_discord_silent

logger = get_logger(__name__)

_BAR_WIDTH = 20


def _ascii_bar(pct: float, width: int = _BAR_WIDTH) -> str:
    pct = max(0.0, min(pct, 100.0))
    filled = round(width * pct / 100)
    return "[" + "■" * filled + "□" * (width - filled) + "]"


class DiscordStatusReporter:
    """core/pipeline.py의 PipelineProgress 콜백 시그니처(단일 인자)를 그대로
    받도록 __call__을 구현합니다. 성공/실패 마무리는 finish()로 따로 호출."""

    def __init__(self, title: str, channel: str, thread_id: str | None = None):
        self._title = title
        self._channel = channel
        self._thread_id = thread_id
        self._message_id: str | None = None
        self._last_stage = -1

    def _render(self, stage: str, stage_index: int, stage_total: int, pct: float, done: bool = False, failed: bool = False, extra: str = "") -> str:
        bar = _ascii_bar(100.0 if done else pct)
        status = "실패" if failed else ("완료" if done else f"{stage} ({stage_index + 1}/{stage_total})")
        text = f"`{bar}` {100 if done else round(pct)}%  **[{self._channel}] {self._title}**\n현재: {status}"
        if extra:
            text += f"\n{extra}"
        return text

    def __call__(self, progress) -> None:
        # 같은 단계 안에서는 갱신하지 않음(예: 이미지 1/22 ~ 22/22) — 단계가
        # 바뀔 때만 한 번 수정해서 API 호출을 아낍니다.
        if progress.stage_index == self._last_stage:
            return
        self._last_stage = progress.stage_index
        text = self._render(progress.stage, progress.stage_index, progress.stage_total, progress.overall_pct)
        try:
            if self._message_id is None:
                self._message_id = notify_discord_silent(text, thread_id=self._thread_id)
            else:
                if not notify_discord_edit(self._message_id, text, thread_id=self._thread_id):
                    logger.warning("DiscordStatusReporter: 진행바 수정 실패 — 다음 단계에서 다시 시도")
        except Exception:
            logger.exception("DiscordStatusReporter 진행바 갱신 실패")

    def finish(self, success: bool, youtube_video_id: str | None = None, error: str | None = None) -> None:
        """영상 하나 처리가 끝났을 때: 새 메시지를 보내지 않고, 같은 진행바
        메시지를 최종 상태로 마지막 수정합니다."""
        try:
            extra = ""
            if success and youtube_video_id:
                extra = f"https://youtu.be/{youtube_video_id}"
            elif not success and error:
                extra = f"사유: {error}"
            final_text = self._render("", 7, 8, 100.0, done=success, failed=not success, extra=extra)
            if self._message_id:
                notify_discord_edit(self._message_id, final_text, thread_id=self._thread_id)
            else:
                # 진행바 메시지가 애초에 안 만들어졌으면(무음 전송 실패 등)
                # 결과라도 새로 하나 남김 — 완전히 조용히 유실되진 않게.
                self._message_id = notify_discord_silent(final_text, thread_id=self._thread_id)
        except Exception:
            logger.exception("DiscordStatusReporter 결과 메시지 전송 실패")
