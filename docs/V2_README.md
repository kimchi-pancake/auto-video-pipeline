# V2 — 조회수 중심 재설계 (2026-11-18)

목표: 두 채널(도개·웃짬)의 지속 가능한 유기적 조회수. 업로드 자동화 자체가 목표가 아니다.
이 문서는 "무엇을 만들었고, 어떻게 돌리고, 무엇이 아직 안 됐는지"를 적는다.

## 구조

```
 Worker cron(01:00 KST) ─▶ daily.yml ─▶ python -m v2.run plan      ──▶ data/jobs/*.json (git에 커밋, 삭제 안 함)
                                      └▶ Worker /trigger-assemble ─▶ assemble_daily.yml
                                                                      └ python -m v2.run assemble (produce → publish)
 v2_measure.yml (6시간마다)  ─▶ python -m v2.run measure   ─▶ data/metrics/*.jsonl, data/insights.json
 v2_approve.yml (수동)       ─▶ python -m v2.run approve JOB_ID   (비공개 업로드분을 승인 → 20:00 KST 예약공개)
 v2_run.yml (수동)           ─▶ 임의 단계 실행(시험용)
```
Worker(Cloudflare)는 **수정하지 않았다**: 파일 이름(daily.yml, assemble_daily.yml)을 그대로 트리거하므로 재배포가 필요 없다.

### 영상 하나의 일생 (job 상태 머신, `v2/jobstore.py`)
`NEW → RESEARCHING → VERIFIED → SCRIPT_READY → ASSETS_READY → RENDERING → QUALITY_CHECK → READY_TO_UPLOAD → UPLOADING → SCHEDULED → PUBLISHED`
어디서든 `RETRY_WAIT`(서비스 장애, 자동 재시도·대기시간 증가·4회 후 FAILED) / `NEEDS_REVIEW`(품질·인증 문제, 사람 확인) / `FAILED`.
- 서비스 장애(모델 다운, 429, 5xx)와 콘텐츠 품질 실패(검증·QC 실패)를 **다른 경로**로 처리한다.
- 작업 잠금(lease): 같은 job을 두 러너가 동시에 처리하지 못한다(크래시해도 90분 뒤 만료).
- 업로드 중복 방지: job에 video_id가 있으면 재사용, 없으면 채널 업로드 목록(1유닛)에서 같은 제목을 먼저 찾는다.
- 코드는 job 파일을 **삭제하지 않는다**(예전 큐는 "삭제 = 완료"라서 실패한 대본이 사라졌다).

### 단계별 모듈
| 단계 | 모듈 | 하는 일 |
|---|---|---|
| 소재 | `v2/topics.py` | 후보 발견(큐레이션 seed 90개, 위키백과 일간 인기 문서, Google Trends KR RSS, 성과 좋았던 영상의 후속) → LLM이 채널 적합성 분류 → **설명 가능한 규칙 점수**(관심·적합·신선도·근거·성과prior·탐색보너스) → 상위권에서 점수 비례 추첨 |
| 자료 | `v2/research.py`, `v2/sources/` | 위키백과 본문(ko+en), 커먼즈 사진(**라이선스 허용 목록만**: PD/CC0/CC BY/CC BY-SA/KOGL1), 세계은행 통계 |
| 훅 | `v2/hooks.py` | 서로 다른 유형 6개 생성 → 근거 문장 포함 여부·길이·과장어·중복·유형 다양성으로 평가 → 최고점 채택(근거 없는 훅은 감점) |
| 대본 | `v2/script.py` | JSON 장면 구성. 길이는 소재가 정함(15~60초). 코드 검증: 알파벳·한자·구독 유도·숫자 근거(자료에 있는 숫자만)·주장별 인용·시각 유형 다양성·해석/사실 구분. 통과 못 하면 이유를 알려주며 최대 3번 재작성 |
| 검증 | `research.verify_claims` | 모든 주장에 출처 URL·제목·발행일·확인일·인용문 저장(`job.claims`). 모델이 인용을 못 맞춰도 코드가 근거 문장을 직접 찾아 확인하고, 못 찾으면 탈락 |
| 화면 | `v2/visuals/` | 실제 사진(흐린 배경 합성), 숫자 카드, 비교 막대, 단계 인과, 키네틱 문구, **세계은행 데이터 애니메이션 차트**. 맞는 자료가 없으면 무관한 그림으로 채우지 않고 문구 카드로 대체(많이 대체되면 QC 탈락) |
| 제작 | `v2/render.py` + 기존 `tts/ subtitle/ video/` 재사용 | 채널별 목소리, 어절 단위 자막, 장면별 카메라 무빙·전환, **BGM 자동 덕킹 + 음량 정규화 + 전환 효과음(자체 합성)** |
| 품질 | `v2/qc.py` | 재생·해상도·길이·음성·무음 구간·음량·첫/끝 프레임(검은 화면·무의미)·자막 싱크·주장 검증·사진 라이선스 기록·제목 중복 |
| 업로드 | `v2/publish.py` | 비공개 업로드 → 처리 완료 확인 → (scheduled 모드면) 20:00 KST 예약공개. 시각이 지났으면 즉시 공개하지 않고 다음 날로 미룸. 설명란에 출처·사진 저작자·AI 사용 고지 |
| 성과 | `v2/metrics.py`, `v2/intelligence.py`, `v2/experiments.py` | 1h/6h/24h/72h/7d/28d 스냅샷, 같은 경과시간끼리·채널 안에서만 비교, 표본 수와 신뢰 표시, 실험 균형 배정 |

## 설정 (`config/v2.json`, 시크릿 아님)
- `publish_mode`: **`review`(기본)** = 비공개 업로드까지만 하고 사람이 승인. `scheduled` = 승인 없이 20:00 KST 예약공개.
  새 시스템의 첫 영상들을 직접 본 뒤 `scheduled`로 바꾸길 권장.
- `posts_per_day`: 채널당 기본 1편(실험값). 성과를 보고 올린다.
- 기존 `config.json`의 채널 이름·인증파일을 그대로 쓴다(인증 정보는 건드리지 않음).

## 로컬 실행
```
python -m v2.run plan --store tmp_jobs      # 기획
python -m v2.run produce --store tmp_jobs   # 제작(영상은 임시폴더/v2_work)
python -m v2.run status --store tmp_jobs
python -m pytest tests -q                   # 단위 테스트
```

## 롤백 / 이전
- 예전 방식 코드는 그대로 남아 있다. `daily.yml`/`assemble_daily.yml`의 실행 줄을 `python scripts/prepare_daily.py` / `python scripts/daily_generate.py` 로 되돌리면 된다.
- 백업: `backups/2026-11-18/`(로컬, gitignore) — registry, 큐, 상태 파일, 로컬 config.
- 예전 큐(`queue/pending_scripts/`)의 대본 5편(11-11자)은 **삭제하지 않고 남겨 뒀다**. V2는 읽지 않는다.
- 기존 `video_registry.json`(영상 504건)은 그대로다. V2 성과 분석은 V2로 만든 영상만 쓴다.

## 아직 안 된 것 (정직하게)
- **Cloudflare D1/R2 미사용**: 무료 한도(D1 일 500만 읽기/10만 쓰기, R2 10GB)는 충분하지만 API 토큰이 없어서 연결 못 함. 대신 `JobStore` 인터페이스 뒤에 git 파일 백엔드를 구현했고(영상당 파일 1개라 충돌 없음), D1로 바꿀 때 이 클래스만 교체하면 된다.
- **Discord 운영 명령 미확장**: Worker는 수동 배포라 건드리지 않았다. 지금은 GitHub Actions 알림(디스코드 웹훅) + `V2 Approve` 워크플로우로 대신한다. `/영상 성과·추천·일시정지` 같은 명령은 Worker 수정+재배포+명령 재등록이 필요해서 미구현.
- **외부 영상 클립·지도·AI 이미지 장면 미구현**: 화면은 실제 사진 + 자체 그래픽만. AI 이미지 API는 장애라 쓰지 않는다. 지도/구조도는 만들지 않았다(근거 있는 데이터 소스가 없음).
- **Shorts 전용 지표(시청 선택률, 피드 노출, 구간별 유지율)**: 공개 API 문서에 없어 자동 수집하지 않는다. `metrics.import_studio_csv`로 Studio CSV를 수동 입력하는 경로만 있다.
- **시청자 댓글 기반 소재 발굴 미구현**(후보 경로로 설계만 해 둠).
- **합성 미디어 표시(containsSyntheticMedia)**: V2는 실제 사진·자체 그래픽·합성 음성이라 AI 생성 이미지를 쓰지 않는다. 설명란에 AI 사용 고지를 넣는다. 정책 해석은 사람이 확인해야 한다.
- 실제 조회수 개선 여부는 **아직 모른다**. 아래 실험 계획으로 판단한다.

## 초기 운영 권장
1. `publish_mode: review`로 시작, 첫 3~5편을 직접 보고 승인.
2. 하루 채널당 1편. 14일간 유지하며 스냅샷(72h)을 모은다.
3. 소재 쏠림 방지를 위해 탐색 보너스(`exploration_c` 0.35)를 유지, 표본 8편 전에는 성과 prior를 약하게만 반영(자동).

## 조회수 개선 판단 실험 계획
- **비교 단위**: 채널 28일 총 조회수 / 신규 영상 72시간 조회수 중앙값(평균 아님) / 평균 시청 비율. 같은 경과 시간끼리만.
- **기준선**: 구 파이프라인의 최근 28일(구 영상 504건)은 콘텐츠 종류가 달라 직접 비교 불가 → V2 첫 14일을 자체 기준선으로 삼고, 이후 실험으로 변화를 본다.
- **실험 순서**(한 번에 변수 하나, 팔당 6편 이상일 때만 결론): ① 길이(짧게 vs 중간) → ② 훅 유형 선호 → ③ 사진 중심 vs 그래픽 중심. 소재가 서로 달라 인과 단정은 금지(결과에 한계 명시).
- **중단 조건**: 연속 5편 72h 조회수가 채널 최근 중앙값의 30% 미만이면 소재 탐색 비중을 올리고 사람이 검토.
