"""
tools/discord_worker/register_command.py
==========================================
디스코드 슬래시 커맨드(/영상)를 글로벌로 한 번 등록합니다. Interactions Endpoint
URL을 설정한 뒤 딱 한 번만 실행하면 됩니다 (이후엔 등록할 필요 없음).

실행:
    python tools/discord_worker/register_command.py <APPLICATION_ID> <BOT_TOKEN>
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def main() -> int:
    if len(sys.argv) != 3:
        print("사용법: python register_command.py <APPLICATION_ID> <BOT_TOKEN>")
        return 1

    app_id, bot_token = sys.argv[1], sys.argv[2]

    command = {
        "name": "영상",
        "description": "다음 정기 생성(매일 20:10) 때 지정한 채널의 영상 주제를 예약합니다",
        "options": [
            {
                "name": "channel",
                "description": "채널 이름",
                "type": 3,  # STRING
                "required": True,
                "choices": [
                    {"name": "웃짬", "value": "웃짬"},
                    {"name": "도개", "value": "도개"},
                ],
            },
            {
                "name": "topic",
                "description": "영상 주제 (자유 텍스트)",
                "type": 3,  # STRING
                "required": True,
            },
        ],
    }

    req = urllib.request.Request(
        f"https://discord.com/api/v10/applications/{app_id}/commands",
        data=json.dumps(command).encode("utf-8"),
        headers={
            "Authorization": f"Bot {bot_token}",
            "Content-Type": "application/json",
            # 파이썬 기본 User-Agent는 클라우드플레어가 봇으로 판단해 403(에러 1010)으로
            # 막습니다 (utils/notify.py의 디스코드 웹훅과 동일한 원인) — 브라우저 UA로 우회.
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            print(resp.status, resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(e.code, e.read().decode("utf-8"))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
