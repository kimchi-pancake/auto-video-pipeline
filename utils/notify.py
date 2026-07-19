"""
utils/notify.py
================
Windows 알림(트레이 풍선말) + 폰 푸시 알림(ntfy.sh) 헬퍼. GUI(Qt) 앱이 안 켜져
있어도 쓸 수 있게 PowerShell을 서브프로세스로 띄워서 표시합니다 —
scripts/daily_generate.py 같은 헤드리스(작업 스케줄러) 실행 경로에서 씁니다.

폰 알림은 ntfy.sh(무료, 가입 불필요)를 씁니다. 갤럭시에 ntfy 앱 설치하고
NTFY_TOPIC 값으로 구독하면 됨. 토픽 이름을 아는 사람은 누구나 그 채널에
메시지를 보내거나 읽을 수 있으니, 이미 충분히 예측 불가능한 랜덤 문자열로
만들어뒀지만 남한테 공유하지 마세요.
"""

from __future__ import annotations

import json
import subprocess
import urllib.request

NTFY_TOPIC = "auto-video-pipeline-88ed1167dd7f"
NTFY_PUBLISH_URL = "https://ntfy.sh/"

# 오라클 A1 재시도 현황을 올리는 디스코드 채널의 웹훅. URL을 아는 사람은 누구나
# 이 채널에 메시지를 보낼 수 있으니 남한테 공유하지 마세요.
# discord.com(구 discordapp.com 말고 최신 도메인)을 씁니다 — discordapp.com은
# 클라우드플레어가 "브라우저 아님"으로 보고 403(에러 1010)으로 막아버립니다.
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1528006867110854817/j7g5IHvpIBrONliuToWRfGhsKdYG1LPM1AcEW-vqMNdbgp676AO2meP4hXpNB2ulj63K"

# 파이썬 urllib 기본 User-Agent("Python-urllib/x.x")도 클라우드플레어가 봇으로
# 판단해서 막습니다 — 브라우저처럼 보이는 UA로 우회합니다.
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def notify_discord(message: str, webhook_url: str = DISCORD_WEBHOOK_URL) -> None:
    """디스코드 웹훅으로 메시지 하나를 보냅니다. 실패해도 조용히 넘어갑니다."""
    try:
        body = json.dumps({"content": message}).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": _BROWSER_UA},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def notify_discord_status_create(embed: dict, image_bytes: bytes, filename: str = "status.png") -> str | None:
    """이미지(진행률 카드)가 첨부된 임베드 메시지를 새로 올리고, 나중에
    수정(edit)할 수 있도록 메시지 id를 반환합니다. 실패하면 None."""
    import requests
    try:
        payload = {"embeds": [{**embed, "image": {"url": f"attachment://{filename}"}}]}
        resp = requests.post(
            f"{DISCORD_WEBHOOK_URL}?wait=true",
            data={"payload_json": json.dumps(payload)},
            files={"file": (filename, image_bytes, "image/png")},
            headers={"User-Agent": _BROWSER_UA},
            timeout=15,
        )
        if resp.status_code in (200, 201):
            return resp.json().get("id")
    except Exception:
        pass
    return None


def notify_discord_status_update(message_id: str, embed: dict, image_bytes: bytes, filename: str = "status.png") -> bool:
    """notify_discord_status_create()로 만든 메시지를 새 진행률 카드로 갈아
    끼웁니다(같은 메시지를 계속 수정 — 채널에 메시지가 쌓이지 않음)."""
    import requests
    try:
        payload = {"embeds": [{**embed, "image": {"url": f"attachment://{filename}"}}]}
        resp = requests.patch(
            f"{DISCORD_WEBHOOK_URL}/messages/{message_id}",
            data={"payload_json": json.dumps(payload)},
            files={"file": (filename, image_bytes, "image/png")},
            headers={"User-Agent": _BROWSER_UA},
            timeout=15,
        )
        return resp.status_code in (200, 201)
    except Exception:
        return False


def notify_phone(title: str, message: str, priority: int = 4) -> None:
    """ntfy.sh로 폰 푸시 알림을 보냅니다. 헤더 대신 JSON 본문을 써서 한글
    제목/내용도 그대로 보냅니다. 실패해도 조용히 넘어갑니다.

    priority는 반드시 정수(1=min ~ 5=max, 4=high)여야 합니다 — "high" 같은
    문자열을 보내면 ntfy가 400을 뱉는데, 이 함수는 실패를 삼키게 만들어놔서
    한동안 계속 조용히 실패했던 원인이었습니다."""
    try:
        body = json.dumps({
            "topic": NTFY_TOPIC,
            "title": title,
            "message": message,
            "priority": priority,
        }).encode("utf-8")
        req = urllib.request.Request(
            NTFY_PUBLISH_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def notify(title: str, message: str, duration_ms: int = 6000) -> None:
    """Windows 알림 풍선말을 띄웁니다. 알림은 부가 기능이라, 실패해도 예외를
    삼키고 조용히 넘어갑니다 (본 작업을 막으면 안 됨)."""
    safe_title = title.replace("'", "''")
    safe_message = message.replace("'", "''")
    script = f"""
Add-Type -AssemblyName System.Windows.Forms
$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = [System.Drawing.SystemIcons]::Information
$notify.Visible = $true
$notify.ShowBalloonTip({duration_ms}, '{safe_title}', '{safe_message}', [System.Windows.Forms.ToolTipIcon]::Info)
Start-Sleep -Milliseconds {duration_ms + 500}
$notify.Dispose()
"""
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        pass
