"""
tools/reauthorize_youtube.py
============================
유튜브 채널 자격증명(credentials json)을 로컬에서 다시 발급받는 일회성 헬퍼.

언제 쓰나
---------
- 디코봇/파이프라인에 "could not locate runnable browser" 또는
  "deleted_client / invalid_grant / 자격증명 무효" 가 떠서 업로드가 전부
  실패할 때. 이건 구글 OAuth 리프레시가 죽었다는 뜻이라, 브라우저로 다시
  로그인해서 새 자격증명을 받아야 합니다.

사전 준비 (구글 클라우드 콘솔)
------------------------------
1) console.cloud.google.com → 해당 프로젝트 → API 및 서비스 → 사용 설정된 API
   에서 "YouTube Data API v3" 가 켜져 있는지 확인.
2) "사용자 인증 정보" → 사용자 인증 정보 만들기 → OAuth 클라이언트 ID →
   애플리케이션 유형 "데스크톱 앱" 으로 새로 생성 → JSON 다운로드 →
   그 파일을 이 저장소의  config/client_secrets.json  으로 덮어쓰기.
   (deleted_client 에러는 기존 OAuth 클라이언트가 삭제됐다는 뜻이라 새로 만들어야 함)
3) OAuth 동의 화면이 "테스트" 상태면, 각 채널의 구글 계정을 "테스트 사용자"에
   추가해두기(아니면 로그인 단계에서 막힘). 게시됨(In production) 상태면 그대로 OK.

사용법
------
    python tools/reauthorize_youtube.py 웃짬
    python tools/reauthorize_youtube.py 도개

채널 이름을 빼고 실행하면 config.json 에 등록된 모든 채널을 순서대로 처리합니다:
    python tools/reauthorize_youtube.py

각 채널마다 브라우저가 열립니다 → 그 채널을 소유한 구글 계정으로 로그인 →
권한 허용. 그러면 config/channels/<채널>_credentials.json 이 새로 써집니다.

마무리 (GitHub Secrets 갱신)
----------------------------
로컬에서 새로 발급된 파일 3개를 그대로 GitHub Secrets 에 붙여넣어야 CI(daily.yml)가
새 자격증명을 씁니다. 아래 명령을 그대로 실행하면 됩니다(gh CLI 필요):

    gh secret set CLIENT_SECRETS_JSON < config/client_secrets.json
    gh secret set YT_CHANNEL_UJJJAM_CREDENTIALS < "config/channels/웃짬_credentials.json"
    gh secret set YT_CHANNEL_DOGAE_CREDENTIALS < "config/channels/도개_credentials.json"
"""

from __future__ import annotations

import sys
from pathlib import Path

# Windows 콘솔(cp949)은 이모지/em-dash 같은 문자를 못 찍어서 print()가 그대로
# 죽습니다(UnicodeEncodeError) — 개별 문자를 하나씩 빼는 대신 stdout/stderr
# 자체를 UTF-8로 강제해서 이 종류의 문제를 다 없앱니다.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from utils.config_manager import get_config
from youtube.youtube_uploader import YouTubeUploader


def _channels(cfg) -> list[dict]:
    chans = cfg.get("youtube.channels", []) or []
    if not chans:
        # 단일 채널 구성(채널 목록이 비었을 때)도 지원.
        return [{"name": "default", "credentials_file": cfg.get("youtube.credentials_file", "")}]
    return chans


def reauthorize_one(cfg, chan: dict) -> bool:
    name = chan.get("name") or "(이름없음)"
    cred_file = chan.get("credentials_file") or cfg.get("youtube.credentials_file", "")
    if not cred_file:
        print(f"[{name}] credentials_file 경로를 알 수 없습니다 — config.json 확인 필요.")
        return False

    cred_path = ROOT / cred_file if not Path(cred_file).is_absolute() else Path(cred_file)
    # 죽은 기존 자격증명을 지워서, authenticate()가 곧바로 브라우저 인증 흐름으로
    # 가도록 합니다(안 지우면 만료 리프레시부터 시도하다 실패 로그가 섞임).
    if cred_path.exists():
        cred_path.unlink()
        print(f"[{name}] 기존(무효) 자격증명 삭제: {cred_path}")

    yt_cfg = cfg.section("youtube")
    yt_cfg["credentials_file"] = str(cred_path)

    print(f"\n=== [{name}] 브라우저가 열립니다. 이 채널을 소유한 구글 계정으로 로그인하세요. ===")
    uploader = YouTubeUploader(yt_cfg)
    try:
        ok = uploader.authenticate()
    except Exception as e:
        print(f"[{name}] 인증 실패: {e}")
        return False

    if ok and cred_path.exists():
        print(f"[{name}] 재인증 완료 — 새 자격증명 저장됨: {cred_path}")
        return True
    print(f"[{name}] 재인증 실패.")
    return False


def main() -> int:
    cfg = get_config()
    target = sys.argv[1] if len(sys.argv) > 1 else None

    chans = _channels(cfg)
    if target:
        chans = [c for c in chans if (c.get("name") or "") == target]
        if not chans:
            print(f"채널 '{target}' 을(를) config.json 에서 찾지 못했습니다.")
            print("등록된 채널:", ", ".join(c.get("name", "?") for c in _channels(cfg)))
            return 1

    results = {c.get("name", "?"): reauthorize_one(cfg, c) for c in chans}

    print("\n=== 결과 ===")
    for name, ok in results.items():
        print(f"  {name}: {'성공' if ok else '실패'}")
    if all(results.values()):
        print(
            "\n이제 GitHub Secrets 를 갱신하세요 (docstring 하단의 gh secret set 명령 참고). "
            "안 하면 CI(daily.yml)는 여전히 옛 자격증명을 씁니다."
        )
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
