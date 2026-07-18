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
import urllib.request


def main() -> int:
    if len(sys.argv) != 3:
        print("사용법: python register_command.py <APPLICATION_ID> <BOT_TOKEN>")
        return 1

    app_id, bot_token = sys.argv[1], sys.argv[2]

    command = {
        "name": "영상",
        "description": "지정한 채널에 주제를 넣어 사연 영상을 즉시 생성/업로드합니다",
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
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        print(resp.status, resp.read().decode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
