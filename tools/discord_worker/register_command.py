"""
tools/discord_worker/register_command.py
==========================================
디스코드 슬래시 커맨드(/영상) 등록은 이 스크립트가 아니라 Worker 자체의
/register-command-x7k2m9 경로에서 이뤄집니다 — 로컬/GitHub Actions 양쪽 다
디스코드 커맨드 등록 API 호출이 클라우드플레어에 막혀서(403, "internal
network error" / 에러 1010), Worker(Cloudflare 네트워크)를 통해서만 등록이
성공했기 때문입니다. 커맨드 정의(예약/목록/취소/분석/주제생성/상태/로그/
재생성/cta설정)는 tools/discord_worker/index.js의 registerCommand()
함수 안에 있습니다 — 정의를 바꾸려면 그 파일을 수정하세요.

등록 절차 (커맨드 정의를 바꿨을 때만 필요):
  1. Cloudflare Worker 환경변수에 DISCORD_BOT_TOKEN, DISCORD_APP_ID 잠깐 추가
  2. index.js 최신 내용 배포
  3. https://<worker-url>/register-command-x7k2m9 접속 (200 응답 확인)
  4. 두 환경변수 다시 제거해도 됨
"""
