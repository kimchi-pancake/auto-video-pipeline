"""
utils/status_reporter.py
==========================
core/pipeline.py의 progress_callback에 꽂는 리포터. 채널별 디스코드
스레드(도개/웃짬) 안에 진행 상황 메시지를 올립니다.

2026-08-01: 이미지 카드(Pillow 렌더링) 대신 코드블록 텍스트로 되돌렸습니다 —
이미지 생성/업로드 없이 메시지 하나만 보내면 되니 훨씬 단순하고 빠릅니다.
같은 메시지를 PATCH로 수정하지 않고, 매번 이전 메시지를 삭제하고 새로
올립니다(요청사항). 모든 전송은 무음(알림/뱃지 없음)입니다.
"""

from __future__ import annotations

from utils.logger import get_logger
from utils.notify import notify_discord_delete, notify_discord_silent

logger = get_logger(__name__)

STAGES_KO = ["파싱", "TTS", "이미지", "자막", "합성", "썸네일", "업로드", "정리"]
_BAR_WIDTH = 24


def _bar(pct: float, width: int = _BAR_WIDTH) -> str:
    pct = max(0.0, min(pct, 100.0))
    filled = round(width * pct / 100)
    return "█" * filled + "░" * (width - filled)


def _stage_grid(stage_index: int, done: bool, failed: bool) -> str:
    cells = []
    for i, name in enumerate(STAGES_KO):
        if failed and i == stage_index:
            mark = "✕"
        elif i < stage_index or done:
            mark = "✓"
        elif i == stage_index:
            mark = "›"
        else:
            mark = "○"
        cells.append(f"{mark} {name}")
    rows = [cells[i:i + 4] for i in range(0, len(cells), 4)]
    return "\n".join("  ".join(f"{c:<8}" for c in row) for row in rows)


class DiscordStatusReporter:
    """core/pipeline.py의 PipelineProgress 콜백 시그니처(단일 인자)를 그대로
    받도록 __call__을 구현합니다. 성공/실패 마무리는 finish()로 따로 호출."""

    def __init__(self, title: str, channel: str, thread_id: str | None = None):
        self._title = title
        self._channel = channel
        self._thread_id = thread_id
        self._message_id: str | None = None
        self._last_stage = -1

    def _render(self, stage_index: int, pct: float, done: bool = False, failed: bool = False, extra: str = "") -> str:
        status_word = "FAILED" if failed else ("DONE" if done else "RUNNING")
        pct = 100.0 if done else max(0.0, min(pct, 100.0))
        if failed:
            status_line = "현재 단계: 실패"
        elif done:
            status_line = "현재 단계: 완료"
        else:
            stage_name = STAGES_KO[stage_index] if 0 <= stage_index < len(STAGES_KO) else ""
            status_line = f"현재 단계: {stage_name} 중..."

        body = "\n".join([
            f"[{self._channel}] ● {status_word}",
            self._title,
            "",
            f"{_bar(pct)}  {pct:.0f}%",
            status_line,
            "",
            _stage_grid(stage_index, done, failed),
        ])
        text = f"```\n{body}\n```"
        if extra:
            # URL은 코드블록 밖에 있어야 디스코드가 링크로 인식합니다.
            text += f"\n{extra}"
        return text

    def _swap(self, text: str) -> None:
        """이전 메시지를 지우고(있으면) 새 메시지를 무음으로 올려서 message_id를
        갱신합니다. 삭제가 실패해도(이미 지워졌거나 네트워크 오류) 새로 올리는
        건 계속 진행합니다 — 진행 표시가 끊기는 것보다 메시지 하나 안 지워지고
        남는 게 낫습니다."""
        if self._message_id:
            notify_discord_delete(self._message_id, thread_id=self._thread_id)
        self._message_id = notify_discord_silent(text, thread_id=self._thread_id)

    def __call__(self, progress) -> None:
        # 같은 단계 안에서는 갱신하지 않음(예: 이미지 1/22 ~ 22/22) — 단계가
        # 바뀔 때만 한 번 갈아끼워서 API 호출을 아낍니다.
        if progress.stage_index == self._last_stage:
            return
        self._last_stage = progress.stage_index
        text = self._render(progress.stage_index, progress.overall_pct)
        try:
            self._swap(text)
        except Exception:
            logger.exception("DiscordStatusReporter 진행 메시지 갱신 실패")

    def finish(self, success: bool, youtube_video_id: str | None = None, error: str | None = None) -> None:
        """영상 하나 처리가 끝났을 때: 이전 진행 메시지를 지우고, 최종 상태
        메시지를 새로 하나 올립니다."""
        try:
            extra = ""
            if success and youtube_video_id:
                extra = f"https://youtu.be/{youtube_video_id}"
            elif not success and error:
                extra = f"사유: {error}"
            text = self._render(len(STAGES_KO) - 1, 100.0, done=success, failed=not success, extra=extra)
            self._swap(text)
        except Exception:
            logger.exception("DiscordStatusReporter 결과 메시지 전송 실패")
