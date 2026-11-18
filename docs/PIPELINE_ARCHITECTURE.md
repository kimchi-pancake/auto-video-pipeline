# Auto Video Pipeline — 구조 해부 문서

작성일: 2026-11-12 · 기준 커밋: `7e73253` 이후 · 목적: 지금 파이프라인이 **무엇을, 어디서, 어떤 순서로, 왜 그렇게** 하는지 전부 적어둔 현황 문서(재설계의 출발점).

이 문서는 "있는 그대로"를 적었다. 좋게 포장하지 않았고, 마지막 장(§17)에 구조적 약점을 따로 모았다.

---

## 0. 한 문단 요약

두 개의 유튜브 채널(**도개** = 한국 성취 "국뽕" 쇼츠, **웃짬** = 세계 정세·경제 비판 쇼츠)에 **하루 5편(도개 3 + 웃짬 2)** 의 세로 쇼츠를 자동으로 올린다. 사람이 하는 일은 없고, 전부 GitHub Actions + Cloudflare Worker + Python으로 돈다.

흐름: **(1) 무료 LLM이 대본 텍스트 생성 → (2) 대본의 `[SCENE: 영어 묘사]`를 이미지 생성 AI(FLUX)로 그림 → (3) 대사를 Edge-TTS로 읽힘 → (4) 그림 + 음성 + 자막 + BGM을 ffmpeg로 합성 → (5) Pillow로 썸네일 → (6) YouTube Data API로 비공개 업로드 후 20:00 KST 예약공개.**

즉 "AI가 쓴 글 + AI가 그린 정지 그림 + 합성 음성"을 이어 붙인 **나레이션 슬라이드쇼**다. 실제 영상 소스, 실제 사진, 실제 데이터, 사람 목소리, 검증된 자료는 어디에도 없다.

---

## 1. 채널 구성 (2026-11-12 현재)

| 채널 | 콘텐츠 | 하루 편수 | 프로필 | run_id 표식 | 그림 화풍 |
|---|---|---|---|---|---|
| 도개 | 한국의 성취·문화·기술·음식 (국뽕) 쇼츠 | 3 | (없음 = 기본 국뽕) | `_photo` | 실사 시네마틱 사진풍 (러너 직접 생성) |
| 웃짬 | 세계 정세·경제를 "누가 이득/누가 비용"으로 해부하는 약간 비판적 쇼츠 | 2 | `world_econ` | `_econ` | 어두운 다큐 사진풍 (러너 직접 생성) |

- 채널 설정은 `config/config.json`의 `youtube.channels[]` (로컬 전용, gitignore). 키: `name`, `credentials_file`, `input_dir`, `content_profile`, `daily_shorts_count`, `paused`.
- 편수가 3+2인 이유: **두 채널이 같은 Google Cloud 프로젝트(= 같은 `client_secrets`)를 쓰고, YouTube Data API 일일 쿼터가 프로젝트당 10,000 유닛**이기 때문. 업로드 1건 ≈ 1,600유닛 + 썸네일/예약공개 업데이트 각 50유닛 → 하루 약 5~6편이 한계.
- 롱폼(10분짜리 TOP10)은 2026-11-01부터 중단. 코드는 남아 있고 `daily.long_count`(기본 0)로만 막혀 있다.
- 웃짬은 원래 코미디/썰 채널이었고 옛 영상 ~200개가 공개 상태로 남아 있다(이름·배너도 옛 방향).

---

## 2. 전체 그림

```
                       ┌──────────────── Cloudflare Worker (tools/discord_worker/index.js) ────────────────┐
                       │  cron 0 16 * * *  (01:00 KST)  ── 이미 도는 daily.yml 없으면 dispatch              │
                       │  cron 0  9 * * *  (18:00 KST)  ── 안전망: assemble_daily.yml dispatch              │
                       │  cron 59 14 * * * (23:59 KST)  ── 디스코드 채널 청소                               │
                       │  cron */10 * * * *             ── Oracle A1 서버 자리 재시도(이 프로젝트와 무관)   │
                       │  POST /trigger-assemble-x4k8p1 ── daily.yml이 끝나면 호출 → assemble dispatch      │
                       │  POST /generate-images-x9k3m2  ── (레거시) 씬 그림 생성 → GitHub에 jpg 커밋        │
                       │  Discord /영상 슬래시 커맨드    ── 예약/분석/시작/상태/…                           │
                       └──────────────────────────────────────────────────────────────────────────────────┘
                                   │ dispatch                                  ▲ trigger
                                   ▼                                           │
 ┌───────────────── daily.yml  "Daily Script Prepare" (≤90분) ─────────────────┴─────────┐
 │ scripts/prepare_daily.py                                                               │
 │   채널별 → generate_daily_batch → generate_shorts_only × N                             │
 │      프롬프트 조립(script_prompts) → 무료 LLM(OpenRouter→NVIDIA) → 후처리·검수         │
 │      → save_story → queue/pending_scripts/{채널}/claude_{ts}_{NN}_{econ|photo}_shorts.txt│
 │   git commit + push (큐 커밋)  ── 끝나면 Worker /trigger-assemble 호출                  │
 └────────────────────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
 ┌─────────────── assemble_daily.yml  "Daily Video Assemble" (≤350분) ───────────────────┐
 │ scripts/daily_generate.py                                                              │
 │   채널당 1스레드 병렬 → 채널 안에서는 대본을 한 편씩 순차 처리 (BatchRunner)            │
 │     core/pipeline.py:                                                                  │
 │       파싱 → ┌ TTS(Edge-TTS)        ┐                                                  │
 │              └ 이미지(NVIDIA FLUX)  ┘ 병렬 → 자막(ASS) → 합성(ffmpeg) → 썸네일 → 업로드 │
 │   업로드 후 videos.update 로 20:00 KST 예약공개 확정, video_registry.json 기록          │
 │   커밋: 큐 삭제분 + registry (+ 2시간 지난 pending_images 정리)                         │
 └────────────────────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
                   YouTube (도개 / 웃짬)  ──→  collect_stats / weekly_analytics 가 성과 수집
```

---

## 3. 하루의 타임라인

| 시각(KST) | 시각(UTC) | 무슨 일 |
|---|---|---|
| 01:00 | 16:00 | Worker cron → `daily.yml` dispatch (이미 도는 게 있으면 스킵) |
| 01:00~01:30 | 16:00~16:30 | 대본 5편 생성(편당 ~5분, 모델이 느리면 더). 쇼츠 생성 예산 45분 초과 시 남은 건 포기 |
| ~01:30 | ~16:30 | 큐 커밋/푸시 → Worker `/trigger-assemble` → `assemble_daily.yml` 시작 |
| 01:30~ | 16:30~ | 조립: 도개/웃짬이 병렬, 채널 내 순차. 편당 합성 ~40초~1분, 이미지가 정상이면 전체 ~1시간 |
| 업로드 즉시 | | 비공개로 올린 뒤 같은 런에서 `publishAt = 20:00 KST` 예약 확정 (이미 지났으면 즉시 공개) |
| 18:00 | 09:00 | Worker 안전망 cron → 큐가 남아 있으면 조립 재실행, 비어 있으면 조용히 종료 |
| 20:00 | 11:00 | YouTube가 스스로 공개 전환 |
| 23:59 | 14:59 | Worker가 디스코드 채널 비움 |
| 월 07:00 | 일 22:00 | `weekly_analytics.yml` (유일하게 GitHub 자체 cron) |

GitHub 자체 `schedule:`은 최대 1시간 가까이 지연돼서 **정시 트리거는 전부 Worker cron이 담당**한다 (`daily.yml`/`assemble_daily.yml`은 `workflow_dispatch`만 있다).

---

## 4. 저장소 지도

```
auto_video_pipeline/
├─ .github/workflows/        9개 워크플로우 (§5)
├─ config/
│   ├─ default_config.json   기본 설정(커밋됨)
│   ├─ config.json           로컬 실설정 (gitignore) ← CI엔 CONFIG_JSON 시크릿으로 복제
│   ├─ channels/*_credentials.json  채널별 OAuth 토큰 (gitignore; CI엔 시크릿으로)
│   ├─ client_secrets.json   Google OAuth 클라이언트 (gitignore; CI엔 시크릿)
│   ├─ video_registry.json   업로드된 영상 이력 + 통계 (append-only)
│   ├─ topic_queue.json      디스코드로 예약한 주제 큐
│   ├─ daily_generate_state.json  채널별 "오늘 대본 생성함" 날짜(KST)
│   ├─ category_performance.json  주간 분석 결과(카테고리별 시청지속률)
│   └─ cta_settings.json     (Worker가 쓰는 CTA 설정. 쇼츠는 더 이상 안 씀)
├─ core/                     두뇌
│   ├─ script_prompts.py     프롬프트 전부 (1,076줄)
│   ├─ ai_script_generator.py LLM 호출·후처리·저장 (1,324줄)
│   ├─ pipeline.py           한 편을 영상으로 만드는 오케스트레이터 (742줄)
│   ├─ batch_runner.py       폴더의 대본을 순차 처리, 실패 시 큐 복귀
│   ├─ job_manager.py        단계별 재시도/상태 관리
│   ├─ daily_queue.py        queue/pending_scripts 입출력
│   ├─ topic_queue.py, cta_settings.py, video_registry.py
│   ├─ analytics_collector.py, youtube_analytics.py, performance_analysis.py
├─ parser/story_parser.py    대본 DSL 파서
├─ tts/                      tts_engine.py(Edge-TTS/OpenAI), tts_builder.py
├─ image/
│   ├─ ai_image_kickoff.py   Worker에 그림 요청(레거시 경로)
│   ├─ image_generator.py    씬별 이미지 확보(폴백 체인)
│   ├─ nvidia_direct.py      러너에서 NVIDIA FLUX 직접 호출 (현재 주력)
│   ├─ pixabay_client.py     스톡 사진/영상 검색
│   └─ thumbnail_generator.py Pillow 썸네일
├─ subtitle/subtitle_builder.py  SRT + ASS(편집 스타일) 생성
├─ video/                    ffmpeg_wrapper.py, video_composer.py, scene_timer.py
├─ youtube/youtube_uploader.py   OAuth·업로드·썸네일
├─ utils/                    config_manager, logger, notify(Discord), status_reporter, bgm_selector, soft_approval, file_utils, system_checker
├─ scripts/                  Actions에서 부르는 진입점 (prepare_daily, daily_generate, generate_topic(s), collect_stats, weekly_analysis, oci_*)
├─ tools/                    discord_worker/(index.js, register_command.py), push_ci_config.py, reauthorize_youtube.py, upload_from_archive.py, test_*.py
├─ gui/                      PySide6 데스크톱 앱 (로컬 전용, CI 무관)
├─ assets/                   bgm/(6곡), fonts/(NanumGothic ×2), photos/, pending_images/(Worker가 커밋하는 임시 그림)
├─ queue/pending_scripts/{채널}/   대본 큐 (git으로 잡 사이 전달)
└─ input/, output/, temp/, archive/, logs/   런타임 폴더 (gitignore)
```

---

## 5. GitHub Actions 워크플로우 (9개)

| 파일 | 트리거 | 하는 일 | 제한 |
|---|---|---|---|
| `daily.yml` Daily Script Prepare | dispatch (Worker cron) | `prepare_daily.py` → 큐 커밋(5회 재시도 push) → Worker `/trigger-assemble` 호출 | 90분 |
| `assemble_daily.yml` Daily Video Assemble | dispatch (Worker, 안전망 cron) | ffmpeg 설치, 한글 폰트 등록, `daily_generate.py`, `archive/`를 3일짜리 아티팩트로 보관, 큐/레지스트리 커밋 | 350분, `concurrency: assemble-daily` (겹치면 대기) |
| `on_demand.yml` | dispatch (디스코드 `/영상 재생성`) | 채널+주제 입력 → `generate_topic.py`(대본 → 조립 → 업로드 한 번에) | 120분 |
| `generate_topics.yml` | dispatch (`/영상 주제생성`) | 성과 참고해서 주제 N개를 `topic_queue.json`에 예약 | 15분 |
| `weekly_analytics.yml` | cron 월 07:00 KST + dispatch | `weekly_analysis.py` → `category_performance.json` 갱신 | 30분 |
| `collect_stats.yml` | dispatch | 조회수/좋아요/댓글을 `video_registry.json`에 채움 | 30분 |
| `check_worker.yml` | dispatch | Worker `/version` 조회(배포 확인) | 5분 |
| `probe_worker_images.yml` | dispatch | Worker에 씬 1장을 시켜서 커밋되는지 5분 감시 | 10분 |
| `register_discord_command.yml` | dispatch | 디스코드 `/영상` 커맨드 등록 | – |

**시크릿(레포):** `CONFIG_JSON`, `CLIENT_SECRETS_JSON`, `YT_CHANNEL_UJJJAM_CREDENTIALS`(웃짬), `YT_CHANNEL_DOGAE_CREDENTIALS`(도개), `NVIDIA_API_KEY`, `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`(코드 비활성), `DISCORD_WEBHOOK_URL`, `DISCORD_BOT_TOKEN`, `DISCORD_WORKER_URL`, `IMAGE_GEN_SECRET`. (`OPENAI_API_KEY`, `DISCORD_APP_ID`는 워크플로우가 참조하지만 시크릿 목록에 없음 = 빈 값.)

**주의 사항**
- 시크릿은 런마다 파일로 써 넣는다(`config/config.json`, `config/channels/*_credentials.json`, `.env`). UTF-8 BOM은 `sed`로 제거.
- **로컬 `config.json`을 고치면 반드시 `python tools/push_ci_config.py`로 `CONFIG_JSON` 시크릿을 다시 올려야 한다.** 직접 올리면 Windows ffmpeg 절대경로가 CI로 새서 합성이 통째로 실패한다(2026-07-20 사고). 이 스크립트가 로컬 전용 값을 걸러낸다.
- 새 API 키를 파이썬에서 쓰면 `daily.yml`/`on_demand.yml`/`generate_topics.yml`의 `.env` 쓰는 스텝에도 추가해야 한다. 로컬 테스트에선 `.env`가 있어서 누락이 안 보인다.
- Actions 로그에서 **숫자 `0`이 `***`로 마스킹**된다(`9***s` = 900s, `***/5개` = 0/5개). 읽을 때 주의.
- 새 `openai`/`anthropic` 라이브러리는 `httpx`가 아니라 `httpx2`를 깐다. 코드에서 `import httpx` 금지(2026-11-02~08 대본 생성이 이것 때문에 매일 죽었음).

---

## 6. 단계 1 — 대본 생성 (`scripts/prepare_daily.py`)

### 6.1 실행 흐름
1. `config.json`에서 채널 목록을 읽고 `paused` 채널 제외.
2. 채널별로: `daily_generate_state.json`에 **오늘(KST) 날짜가 이미 있으면 건너뜀**(수동 실행이 UTC 날짜 때문에 정기 실행을 막던 사고 이후 KST 기준).
3. `pop_topic(채널, 오늘)`로 디스코드 예약 주제 확인 — **지금은 읽기만 하고 반영 안 함**(콤보 경로가 `custom_topic`을 안 받아서 경고만 찍는다).
4. `generate_daily_batch(queue_dir, channel, long_count=0, extra_shorts_count=채널별 N, profile=채널 content_profile)`.
5. 성공 시 state 갱신.

### 6.2 쇼츠 한 편의 생성 (`generate_shorts_only`)
```
seed_hint = random_topic_seed(profile)          # "이번 영상의 주인공은 반드시 'OO'이고 접근 각도는 'XX'"
prompt    = shorts_script_prompt(None, profile) # 프로필별 프롬프트 (CTA 지시는 안 붙임)
          + [이번 소재 필수 조건] seed_hint
          + [다루지 마라] banned_topics(profile)
          + [오늘 이미 만든 제목] avoid_titles  # 같은 배치 안 중복 방지
최대 2회 반복:
    text = _call_claude(prompt)                 # → _call_openrouter (LLM 체인, §6.4)
    text = _clean_language_if_needed(text)      # 외국어 혼입 제거
    text = _postprocess_script(text)            # 형식 정리
    text, removed = _strip_filler(text)         # 구독·좋아요·하트 씬 문장 단위 삭제
    if _shorts_too_thin(text) is None: return text   # 씬 ≥4, 대사 ≥170자
raise ScriptGenerationError
```
저장: `queue/pending_scripts/{채널}/claude_{YYYYMMDD_HHMMSS}_{NN}{_econ|_photo}_shorts.txt` (+ 맨 앞에 `RUN_ID:` 헤더). **파일명 stem = run_id**이고, `_econ`/`_photo` 접미사가 이후 이미지 경로를 결정한다(§8).

### 6.3 프롬프트 구성 (`core/script_prompts.py`)
쇼츠 프롬프트는 이 블록들을 이어 붙인다.

| 블록 | 내용 |
|---|---|
| 페르소나 | 국뽕 작가 / 비판적 시사 작가 |
| 스토리 구조 | 훅 → 구체적 사실 3~4개(경제는 메커니즘 해부) → 한 줄 정리. 30~40초, 대사 220~300자, SCENE 5~6개 |
| **유도 멘트 전면 금지** | 구독·좋아요·"두 번 눌러"·"진짜 놀라운 건 지금부터"·하트 씬 금지 (2026-11-08에 뒤집음. 그 전엔 이게 필수라 영상의 절반이 군더더기였음) |
| **내용 밀도 규칙** | 주인공을 고유명사로, 씬마다 새 사실 1개, 뜬구름 수식어 금지 |
| 주제/사실 규칙 | 검증 가능한 사실만, 인터뷰·전문가 발언 날조 금지, 타국 비하 금지 / (경제) 최신 사건 아는 척 금지, 정책·제도만 비판, 투자 조언·정치 편들기 금지 |
| 시각 규칙 | `[SCENE:영어 묘사]` — 글자·로고·숫자·실존 인물 얼굴 금지, 씬마다 구도 다르게 |
| 카테고리 규칙 | 국뽕 12개 / 경제 12개 중 하나 |
| 제목 공식 | 15자 이내, 패턴 제시 |
| 문체 | 존댓말, 한 문장 한 정보, **나레이션에 알파벳 금지(한글로 풀어쓰기)** |
| BGM 가이드 | 사용 가능한 6곡 중 선택 (경제는 happy/lovely 금지) |
| 출력 형식 | `RESOLUTION / CATEGORY / CAST / BGM / THUMBNAIL_LONG / THUMBNAIL_SHORTS / [SCENE…] 화자: 대사&대사` |

**소재 다양화 장치:** 프롬프트에 구체 예시를 넣으면 무료 모델이 그대로 베낀다(예시 복사 문제). 그래서 코드가 소재를 먼저 정한다.
- 국뽕 시드: 누리호, 직지, 온돌, 불닭볶음면, K9 자주포, 바라카 원전, 인천공항 등 **구체 대상 45개** × 접근 각도 8개.
- 경제 시드: 달러 패권, 관세 전쟁, 일본 엔저, 1997년 외환위기, 반도체 패권 등 32개 × 각도 10개.
- 카테고리는 더 이상 무작위로 강제하지 않음(소재와 안 맞는 조합이 이상한 대본을 만들었음).

### 6.4 LLM 호출 체인 (`_call_claude` → `_call_openrouter`)
```
1) OpenRouter  qwen/qwen3-235b-a22b-2507   ← 잔액 $0이라 즉시 402 → 건너뜀
2) NVIDIA 직접 openai/gpt-oss-20b          ← 현재 1순위 (느림, 긴 출력에서 타임아웃 잦음)
3) NVIDIA 직접 nvidia/nemotron-3.5-super-vl-120b-a12b (추론 끔)   ← 실질적 일꾼
```
- 사용자 지시: **Claude/유료 모델 금지**(2026-09-29). Anthropic 호출 코드(`_call_anthropic`)는 남아 있지만 `_call_claude`가 안 부른다.
- 스트리밍 + 스레드 기반 데드라인(응답이 안 오면 `openai.APITimeoutError` 발생시켜 다음 모델로).
- 모델별 쿨다운 45분(한 번 402/404/5xx/타임아웃 난 모델은 프로세스 안에서 건너뜀 — gpt-oss가 매번 504라 대본 한 편에 10분씩 허비하던 문제 대응).
- 429는 같은 모델로 20초×… 백오프 3회 후 다음 모델.
- 알려진 한계: 이 모델들은 **프롬프트의 예시 문구를 베끼고, 내가 정한 소재를 무시하기도 하고(모델 편의로 관세로 쏠림), 이름을 모르면 "한 작은 회사" 같은 가짜 주어를 지어낸다.**

### 6.5 후처리 (코드가 모델의 형식 실수를 고침)
| 함수 | 역할 |
|---|---|
| `_normalize_speaker_tags` | "나레이터:" 외 화자 태그를 역할 키로 정규화 |
| `_ensure_sentence_breaks` | 문장 끝마다 `&` 삽입 (자막 단위용) |
| `_clean_scene_headers` | 장면 머리글 괄호 정리 |
| `_purge_foreign_words` | 외국어 혼입 줄만 골라 LLM에 "그 줄만 한국어로" 요청 → 그래도 남으면 외국어 토큰을 코드가 삭제 (TTS가 영어/이탈리아어를 읽는 사고 방지) |
| `_strip_filler` | 구독/좋아요/두 번 눌러/예고만 하는 문장을 문장 단위로 삭제, 하트 씬은 통째 삭제 |
| `_shorts_too_thin` | 지운 뒤 씬 4개·대사 170자 미만이면 폐기 후 재생성 |
| `THUMBNAIL_SHORT:` 오타 보정 | 무료 모델이 헤더를 `THUMBNAIL_SHORT:`로 쓰는 버그 교정 |

### 6.6 (비활성) 롱폼 경로
주제 아이디어 → 제목 후보 10개+자체 평가 → 콤보(롱폼+쇼츠) 프롬프트 → 롱폼이 25씬/5000자 미만이면 확장 호출 최대 2회 → 저장. `long_count=0`이라 지금은 안 돈다. 경제 프로필은 롱폼 프롬프트가 없다.

---

## 7. 대본 DSL (`story.txt`) 와 파서 (`parser/story_parser.py`)

```
RUN_ID:
claude_20261111_161218_01_photo_shorts

RESOLUTION:
1080x1920

CATEGORY:
첨단기술

CAST:
나레이터: 나레이터

BGM:
piano.mp3

THUMBNAIL_LONG:
(제목)
THUMBNAIL_SHORTS:
(제목)

[SCENE:Ancient Korean ondol room with smoke rising…, wide shot, cinematic, 1080x1920]
나레이터: 문장 하나&문장 둘&문장 셋
```
- **`[SCENE:…]`** : 영어 이미지 프롬프트. 끝의 `WxH`는 해상도 지시. (구버전의 `NEGATIVE:`/`SEED:`는 파싱만 하고 안 씀.)
- **`화자: 대사&대사`** : `&`가 자막/TTS 세그먼트 경계. 한 세그먼트당 TTS 호출 1회.
- **세그먼트 30자 초과 방어:** 모델이 `&`를 빠뜨려도 파서가 `_MAX_SEGMENT_CHARS=30`으로 강제 분할.
- 콜론이 들어간 이어지는 줄이 새 화자로 오인되는 걸 막기 위해, **CAST에 없는 화자의 줄은 직전 대사에 이어붙임**.
- `_validate`: 씬 0개면 즉시 예외(본문이 잘린 응답).
- `StoryData`: `cast, bgm_files, photo_files, scenes, thumbnail_long/shorts, raw_title, resolution, category, run_id`.

---

## 8. 단계 2 — 이미지 확보

### 8.1 개념
씬마다 그림 1장. 우선순위 사슬(`image/image_generator.py`):

```
1) PHOTO:   대본에 지정한 로컬 사진 (현재 안 씀)
2) pending_images/{run_id}/scene_XXXX.jpg   Worker가 미리 그려 커밋해 둔 것 (현재 새 대본엔 해당 없음)
2-b) nvidia_direct 가 러너에서 직접 그린 scene_XXXX_direct.jpg
3) Pixabay 비디오 검색
4) Pixabay 사진 검색
5) (2026-11-08 추가) 위 전부 실패한 씬은 가장 가까운 성공 씬의 그림을 빌려 씀
   → 성공한 그림이 한 장도 없으면 RuntimeError("씬 이미지를 한 장도 못 구했습니다")로 그 편을 실패시킴
```

### 8.2 경로 A — Cloudflare Worker (레거시, 이제 새 대본엔 안 씀)
- 대본이 저장되는 순간 `ai_image_kickoff.py`가 씬을 8개씩 쪼개 15초 간격으로 Worker `/generate-images-x9k3m2`에 POST.
- Worker가 `ctx.waitUntil()`로 NVIDIA FLUX.1-dev를 호출, 결과 jpg를 GitHub Contents API로 `assets/pending_images/{run_id}/`에 커밋. 실패 시 `env.AI` 바인딩이 있으면 Workers AI flux-1-schnell로 폴백(**바인딩 존재 여부는 미확인**).
- 화풍은 Worker 코드에 박힌 **"따뜻한 수채화 스토리북"** 접미사(대시보드에 수동 붙여넣기로 배포하는 파일이라 에이전트가 못 바꿈).
- 문제: 실행 시간 한도에 잘려 일부만 올라옴, GitHub 동시 커밋 409 충돌, 그리고 지금은 NVIDIA 이미지 API 자체가 죽어 있음.
- `ai_image_kickoff`는 `_econ`/`_photo` run_id면 요청 자체를 보내지 않고 return.

### 8.3 경로 B — 러너 직접 생성 (`image/nvidia_direct.py`, 현재 주력)
- `runner_only(run_id)` = run_id에 `_econ` 또는 `_photo` 포함. 이 경우 Worker 대기 0초, 전 씬을 러너가 그림.
- 모델 `black-forest-labs/flux.1-dev`, 768×1344, steps 25, 4동시, 씬당 최대 4회(5·10·15초 대기), 요청 타임아웃 100초, 필터링/검은 이미지면 seed+1로 재시도. 같은 run_id+씬은 같은 seed(crc32).
- 화풍 접미사: `_econ` = 어두운 다큐 사진, `_photo` = 실사 시네마틱, (그 외) = 수채화.
- 장애 판정: 한 영상에서 3씬 이상 전부 실패하면 `_outage=True` → 같은 프로세스의 다음 영상은 NVIDIA를 아예 안 두드림.

### 8.4 Pixabay (`image/pixabay_client.py`)
- 씬 프롬프트에서 키워드를 뽑아 검색 → 다운로드(캐시). `urllib`로 호출.
- **러너에서 HTTP 403**(같은 키가 로컬에선 200). 원인 미확정(데이터센터 IP 차단 또는 기본 UA). 사실상 죽은 폴백.

### 8.5 현재 상태 (2026-11-12)
**NVIDIA 이미지 API가 11-08부터 전면 장애**(flux.1-dev 500/타임아웃, flux.1-schnell 타임아웃, sd3는 이 계정에 없음). Worker도 같은 API라 같이 죽음. Pixabay 403, Hugging Face 무료 크레딧 소진(402), Pollinations는 접속 불가. → 11-08 이후 **업로드 0건**. §16 참고.

---

## 9. 단계 3 — TTS (`tts/`)

- 엔진: **Edge-TTS**(`tts.provider` 미설정 = edge). OpenAI TTS 경로가 코드에 있으나 키가 없어 안 씀.
- 목소리: `ko-KR-HyunsuMultilingualNeural`, `rate +18%`(나레이터). 다른 화자 키(남자1/여자1/…)는 설정에 있으나 쇼츠는 나레이터만 씀.
- 요청 단위 = 대사의 `&` 세그먼트 하나. 4동시, 실패 시 3회 재시도(2초).
- **2026-11-08 추가:** `boundary="WordBoundary"`로 호출해 **어절별 (시작, 끝, 단어) 타이밍**을 받아 `SegmentAudio.words`에 저장 → 자막이 말하는 속도에 맞게 끊김(§11). (edge-tts 7.x는 기본이 SentenceBoundary라 명시 필요. 로컬 설치본 7.2.8은 403이 나서 7.3.1로 올려야 동작 — CI는 항상 최신을 깔아서 무관.)
- 길이는 ffprobe로 재측정해서 보정(`_fix_durations`) — 엔진의 추정(128kbps 기준 파일 크기)은 부정확.
- 단계 결과: `List[SceneAudio]` (씬별 세그먼트 오디오 경로·길이·단어 타이밍).

---

## 10. 단계 4 — 타임라인과 자막

### 10.1 `video/scene_timer.py`
세그먼트 길이를 누적해 절대/상대 시각 계산. 씬 클립 길이 = `max(음성 길이, min_scene_duration=3.0)`. 씬 사이 가짜 간격은 0(자막만 밀려서 뒤로 갈수록 싱크가 어긋나던 버그 때문에 제거).

### 10.2 `subtitle/subtitle_builder.py` — 두 모드
**① dynamic(기본, 2026-11-08 "편집 스타일")**
- 문장 통째가 아니라 **2~3어절·최대 11자씩 끊어서**, TTS 어절 타이밍에 맞춰 순서대로 띄운다. 단어 수가 안 맞으면(숫자·영문 읽는 방식 차이) 글자 수 비례로 추정.
- 팝인 애니메이션(86%→104%→100% 스케일 + 60ms 페이드), 반짝임 방지(한두 글자 어절은 다음 덩어리와 병합, 0.6초 안이면 이어서 유지).
- **강조:** 숫자가 든 어절과 `_KEYWORD_RE`(세계·최초·위기·금리·달러·제재 …)는 노랑.
- 폰트 `NanumGothic`(저장소 동봉), 쇼츠 크기 100, 외곽선 7, 위치 화면 약 3/4 지점(margin_v 500 — 유튜브 하단 UI 회피).
- **세로 영상이면 화면 상단에 영상 내내 고정 제목**(쇼츠 썸네일 문구, 두 줄이면 둘째 줄 노랑).
- 같은 파일의 "전문의" 포함 줄은 작게(의료 고지 문구 잔재).
**② 레거시** `subtitle.dynamic=false`: 문장 단위, 하단 고정.
- SRT는 항상 문장 단위로 따로 생성(업로드용이 아니라 보관용).

---

## 11. 단계 5 — 영상 합성 (`video/video_composer.py`, `video/ffmpeg_wrapper.py`)

`compose()` 6단계:
1. **씬 음성 병합** — 세그먼트 mp3를 concat(무음이면 1초 무음).
2. **씬 클립 생성** — 정지 그림 → 영상. 길이 = 클립길이 + 크로스페이드(마지막 씬은 + outro_hold 0.5초).
   - 편집 스타일: 씬마다 **카메라 무빙을 돌려 씀**(첫 씬 줌아웃 → 줌인/오른쪽 팬/위 팬/줌아웃/왼쪽 팬/…), ease-in-out, **2배 업스케일 후 zoompan → 축소**로 떨림 제거. 확대율 1.12.
   - 레거시: 전부 천천히 1.05배 줌인. 가로/세로 비율이 달라도 "꽉 채우고 중앙 크롭".
   - Pixabay 비디오 클립이면 루프/크롭해 길이에 맞춤.
3. **씬 연결** — xfade. 편집 스타일은 전환 0.3초 × 8종 순환(smoothleft, fade, zoomin, smoothup, slideleft, fadeblack, smoothright, fade), 레거시는 0.8초 fade. offset은 "누적 길이 − crossfade"로 계산(뒤쪽 씬이 잘리지 않게).
4. **전체 음성 병합.**
5. **BGM 믹스** — `bgm_volume 0.15`, 루프, `amix duration=first`, 영상 총길이에 맞춤. BGM 파일이 없으면 TTS만. **효과음(SFX)은 없음.**
6. **자막 번인 → 최종 인코딩** — `ass` 필터, libx264 preset fast crf 18 (GPU 옵션 있으나 꺼짐).
- 중간 산출물은 `ultrafast/crf23`(최종 단계에서 재인코딩되므로). 최종 mp4 ≈ 20~25MB(35초).
- 이미지가 없는 씬은 검정 프레임(이제는 이웃 씬 그림 재사용이라 거의 안 나옴).

**BGM 선택** (`Pipeline._resolve_story_bgm`): 대본 `BGM:`에 파일이 하나면 그대로, 여럿/없음이면 `utils/bgm_selector`가 제목·대사·씬 프롬프트의 키워드를 세어 `bgm.moods`에서 고름. 곡은 6개뿐(`happy/lovely/sad/ambient_piano/piano/simple`).

---

## 12. 단계 6 — 썸네일 (`image/thumbnail_generator.py`)
- Pillow. 쇼츠 1080×1920(롱폼 1280×720도 같이 생성되지만 쇼츠는 쇼츠용만 씀).
- 배경: **AI로 그린 씬 이미지 중 첫 장** → 없으면 Pixabay "놀란 반응샷" 검색 → 없으면 첫 씬 이미지. 어둡게 깔고 제목을 큼직하게, 뒤에 반투명 밴드.
- 색: 기본 흰색, 충격 키워드(`_KEYWORD_RE`)만 빨강, 파랑은 한 단어 정도만. 폰트 `NanumGothicBold`.
- 인트로 타이틀 카드(영상 첫 장면에 제목 카드)는 2026-08-19에 꺼둠(`has_intro=False`).

---

## 13. 단계 7 — 업로드와 예약공개

`core/pipeline.py::_run_youtube` → `youtube/youtube_uploader.py`
- 인증: 채널별 `credentials_file`의 refresh token. 헤드리스에서 토큰이 죽으면 브라우저 인증을 못 하므로 즉시 예외(`deleted_client`/`invalid_grant` 안내). 사전 점검(`_preflight_youtube_auth`)으로 **렌더링 시작 전에** 채널을 통째로 건너뛰어 낭비 방지.
- 제목: `{raw_title} #Shorts`(세로 영상일 때). 설명: 제목 + "이 영상은 Auto Video Pipeline으로 자동 생성되었습니다." 태그: `youtube.shorts_tags`. 카테고리 22, 언어 ko.
- 업로드는 **항상 private** → 같은 런에서 `videos.update`로 `publishAt = 20:00 KST`(이미 지났으면 즉시 public). (예전엔 "거절 가능한 대기 창"이 있었으나 GitHub Actions 과금의 실제 원인이라 2026-08-01 제거.)
- 썸네일은 업로드 직후 `thumbnails.set`.
- 완료되면 `video_registry.json`에 `{video_id, channel, category, title, is_shorts, uploaded_at}` 기록, Discord 스레드에 알림.
- 재시도: 500/502/503/504·연결 오류만 재시도(청크 업로드, 10MB 청크).

---

## 14. 배치 실행과 실패 처리

### 14.1 `scripts/daily_generate.py` + `core/batch_runner.py`
1. `queue/pending_scripts/{채널}/*.txt`를 로컬 `input/{채널}/`로 **이동**(복사 아님 — 큐에서 비워져야 커밋 스텝이 "이번에 처리한 것"으로 인식).
2. 채널당 스레드 1개(병렬), 채널 안에서는 **생성시각(ctime) 순으로 한 편씩**.
3. 편마다 `Pipeline.run()` → 끝나면 `archive/{채널}/input_processed|input_failed/`로 이동 + Discord 알림.
4. `Pipeline` 단계는 `JobManager`로 등록(재시도 3회). **TTS와 이미지는 2스레드로 동시**, 나머지(자막→합성→썸네일→업로드)는 순차. 이미지 단계 재시도 간격은 120초.

### 14.2 큐 커밋 방식 (중요)
워크플로우 마지막 스텝이 `git add -A -- queue/pending_scripts`로 **큐의 삭제를 커밋**한다. 즉 "큐에서 사라짐 = 처리 끝"이라는 암묵 규약.
- 2026-11-12 이전: 그림 때문에 실패한 편도 큐에서 사라져 **대본이 영구 소실**(11-08~11 매일 5편).
- 2026-11-12(`7e73253`): `BatchRunner._maybe_requeue` — "한 장도 못 구했습니다"로 실패하면 대본을 큐로 **되돌림**(대본 날짜가 3일 넘으면 포기). 11-11자 5편은 수동 복구해 큐에 있음.

### 14.3 안전망 정리
| 장치 | 위치 | 효과 |
|---|---|---|
| 쇼츠 생성 예산 45분 | `ai_script_generator` | 모델이 멈춰도 워크플로우 90분 제한 전에 이미 만든 대본 커밋 |
| 모델 쿨다운 45분 | 〃 | 죽은 모델에 시간 낭비 방지 |
| 외국어 코드 삭제 | 〃 | TTS가 외국어 낭독하는 사고 방지 |
| 사전 인증 점검 | `daily_generate` | 토큰 죽으면 렌더링 전에 중단 |
| 큐 복귀 | `batch_runner` | 외부 장애로 실패한 대본 보존 |
| NVIDIA 장애 판정 | `nvidia_direct` | 죽은 API를 영상마다 8분씩 두드리지 않음 |
| 이웃 씬 그림 재사용 | `image_generator` | 몇 장만 빠져도 검정 화면 안 나감 |
| 그림 0장이면 실패 | `pipeline` | 검정 영상 업로드 방지 |
| 아카이브 아티팩트 3일 | `assemble_daily.yml` | 업로드만 실패한 영상은 `tools/upload_from_archive.py`로 복구 |
| 안전망 cron 18:00 | Worker | 즉시 트리거가 실패해도 처리 |
| Worker `findActiveRun` | Worker | 중복 실행 방지 |

---

## 15. 분석 루프와 디스코드 제어

### 15.1 성과 분석
- `collect_stats.py` → YouTube Data API(`videos.list statistics`)로 조회수/좋아요/댓글을 registry에 채움.
- `weekly_analysis.py` → YouTube Analytics API(`yt-analytics.readonly`)로 최근 30일 시청 지속률을 **카테고리별** 집계 → `category_performance.json`. 쇼츠는 반복재생 때문에 지속률이 100%를 넘을 수 있어 롱폼과 분리 집계.
- `performance_analysis.summarize_for_prompt`가 이걸 주제 생성 프롬프트에 "잘 되는/안 되는 소재"로 넣도록 설계됨 — 다만 **현재 쇼츠 생성 경로는 이 요약을 쓰지 않는다**(롱폼 주제 선정 경로에서만 사용).
- 사실상 **성과가 대본에 되먹임되지 않는 상태.**

### 15.2 디스코드 `/영상` (Worker, 서명 검증)
`예약 / 목록 / 취소 / 분석 / 주제생성 / 시작 / 상태 / 로그 / 재생성 / cta설정`. Worker가 GitHub Contents API로 `config/*.json`을 직접 읽고 써서 큐·설정을 바꾸고, `workflow_dispatch`로 워크플로우를 건다. 그 외 매일 23:59 채널 청소, 10분마다 Oracle A1 서버 자리 재시도(별개 목적).

### 15.3 Worker 환경변수
`DISCORD_PUBLIC_KEY, DISCORD_APP_ID, DISCORD_BOT_TOKEN, GITHUB_TOKEN(PAT), GITHUB_REPO, IMAGE_GEN_SECRET, NVIDIA_API_KEY, OCI_* ×5`. Worker 코드 변경은 **대시보드에 붙여넣기 수동 배포**이고 `/version`(`WORKER_BUILD` 문자열)으로 확인한다.

---

## 16. 현재 상태 스냅샷 (2026-11-12)

- **영상 생산 0건 (11-08~).** 원인: NVIDIA 이미지 API 전면 장애 + 모든 대체 소스 막힘(§8.5). 대본 생성(`daily.yml`)은 정상.
- 11-09~11-10 대본은 소실, 11-11자 5편(도개 3, 웃짬 2)은 큐에 복구됨. 다음 조립에서 그림이 안 되면 다시 큐로 돌아가며 3일 후 폐기.
- 오늘 바뀐 것: 편집 스타일 자막/무빙/전환, 대본 프롬프트(유도 멘트 금지·밀도 규칙), 국뽕 쇼츠 실사풍 그림, 큐 복귀, 장애 판정.
- 미검증: Worker에 Cloudflare AI 바인딩이 있는지 / Pixabay 403의 정확한 원인 / 새 프롬프트로 만든 대본의 실제 품질(시험 2편만) / 편집 스타일이 실제 Actions에서 도는지(로컬 렌더만 확인).
- 쿼터: 3+2 편성은 여유가 거의 없다.

---

## 17. 구조적 약점 (재설계 근거)

1. **영상의 정체가 "AI 정지 그림 + 합성 음성 슬라이드쇼"** 다. 시청자가 AI 영상임을 한눈에 알아본다. 장면 간 연속성도, 실제 물체/현장도 없다. 무빙과 전환을 넣어도 본질은 같다.
2. **이미지 생성 API 한 곳에 전체가 매달려 있다.** 그 API가 죽으면 생산이 0이 된다(지금처럼). 대체 소스는 전부 막혔거나 키가 필요하다. 이미지 쪽에 "죽어도 영상이 나가는" 층이 없다.
3. **대본 품질이 무료 모델의 기억에 의존한다.** 사실 근거(자료 조회)가 없어서 이름 없는 주어·뜬구름 문장이 나오고, 경제 채널은 숫자·사례를 모델이 지어낼 위험이 구조적으로 있다. 사실 규칙은 "프롬프트로 부탁"하는 수준이다. 검증 단계가 없다.
4. **사람 목소리/연기/리듬이 없다.** Edge-TTS 단일 목소리 + 일정한 속도. 효과음도 없고 BGM은 6곡 고정.
5. **주제 선정이 정적 리스트 + 무작위.** 성과 분석 결과가 쇼츠 생성에 반영되지 않고(§15.1), 트렌드·시의성이 없다. 경제 채널은 "최신 사건을 아는 척하지 마라"로 막아서 결과적으로 시의성이 0이다.
6. **하루 5편 "찍어내기" 구조 + 쿼터 5편 한계.** 편수가 많아도 한 편 한 편의 완성도에 쓸 수 있는 파이프가 없다. 편당 30~40초에 문장 6~12개.
7. **상태가 git 커밋에 얹혀 있다.** 큐·레지스트리·state가 전부 레포 파일이라 동시 푸시 충돌, "삭제 = 완료"라는 암묵 규약, 장애 시 소실 위험이 있다(실제로 소실이 났다).
8. **수동 배포 Worker + 대시보드 설정 의존.** Worker 화풍·크론·바인딩은 코드 저장소만으론 재현·검증이 안 된다.
9. **외부 서비스 의존이 얕고 넓다.** OpenRouter(잔액 0), NVIDIA(LLM·이미지 둘 다), Pixabay(403), Edge-TTS(비공식 엔드포인트), YouTube 쿼터 — 하나가 흔들릴 때마다 사고가 났다.
10. **채널 정체성이 약하다.** 웃짬은 옛 콘텐츠 200개·옛 이름이 남아 있고, 도개는 첫 공개 쇼츠가 막 나간 상태라 시청자 데이터가 거의 없다.

---

## 18. 알아두면 사고를 피하는 것들

- 로컬 `config.json` 수정 후 `tools/push_ci_config.py` 필수(§5).
- Actions 로그의 `***`는 숫자 0(§5).
- `import httpx` 금지, `client._client.build_request(...)` 사용.
- 채널명 비교는 **NFC 정규화** 후에(`_nfc`). 디스코드 Worker(JS)와 파이썬이 같은 한글을 서로 다른 유니코드 형태로 줄 수 있다.
- `.txt`/`.json`을 쓸 땐 UTF-8(BOM 주의). CI에서 `sed -i '1s/^\xEF\xBB\xBF//'`로 제거한다.
- QThread(GUI): 워커의 finished 슬롯에서 스레드 참조를 None으로 만들지 말 것(크래시).
- 새 API 키는 `.env` 쓰는 스텝 3개 워크플로우에도 추가.
- `Pipeline`은 씬의 `.index`를 리스트 위치로 쓰면 안 된다(인트로 카드 index=-1 사고).
- `run_id`(= 대본 파일명 stem)에 `_econ`/`_photo`가 있어야 러너 직접 생성 경로를 탄다. 이 표식을 바꾸면 화풍과 Worker 사용 여부가 같이 바뀐다.
- 로컬에서 ffmpeg 합성/자막 시험은 `scratchpad`의 `t_render.py` 방식(`Pipeline(cfg).run(story, upload_to_youtube=False)`)이 가장 빠르다. 로컬 edge-tts는 7.3.x여야 한다.

---

## 19. GUI와 보조 도구 (참고)

- `main.py` / `gui/main_window.py`(1,628줄) — PySide6 데스크톱 앱. 왼쪽은 Claude.ai 웹뷰로 대본을 쓰고 응답을 채널 대기열 폴더로 가져오는 패널, 오른쪽은 채널별 실행 카드(병렬 슬롯, 배치 모드). **자동 운영(Actions)과는 무관한 수동 도구**이고, `gui/claude_panel.py`는 같은 `script_prompts`를 수동 복사용으로 쓴다.
- `tools/upload_from_archive.py` — 렌더는 됐는데 업로드만 실패한 영상을 아티팩트로 받아 재업로드.
- `tools/reauthorize_youtube.py` — OAuth 토큰 재발급(브라우저 로그인).
- `tools/test_*.py` — 파서/TTS/파이프라인 단위 시험.
- `scripts/oci_a1_*.py`, Worker의 `retryOracleA1` — Oracle Cloud 무료 서버 자리 선점 시도(이 파이프라인의 핵심 경로가 아님).
- 로컬 실행: `run.bat` (cmd의 `chcp 65001` 버그 때문에 줄을 더 늘리면 안 됨).

---

## 20. 파일별 책임 요약표

| 파일 | 한 줄 책임 |
|---|---|
| `scripts/prepare_daily.py` | 채널별 하루치 대본을 만들어 큐에 저장 |
| `core/ai_script_generator.py` | LLM 호출·모델 폴백·후처리·검수·저장 |
| `core/script_prompts.py` | 모든 프롬프트, 소재 시드, 카테고리, 금지 주제 |
| `parser/story_parser.py` | 대본 DSL → `StoryData` |
| `scripts/daily_generate.py` | 큐 → 채널별 병렬 조립·업로드 진입점 |
| `core/batch_runner.py` | 채널 폴더의 대본을 순차 처리, 아카이브, 큐 복귀 |
| `core/pipeline.py` | 한 편의 8단계(파싱·TTS·이미지·자막·합성·썸네일·업로드·정리) |
| `core/job_manager.py` | 단계별 재시도·상태 |
| `tts/tts_engine.py` | Edge-TTS/OpenAI 호출, 어절 타이밍 수집 |
| `tts/tts_builder.py` | 대본 → TTS 요청 → 씬별 오디오 구조 |
| `image/image_generator.py` | 씬 이미지 폴백 사슬 |
| `image/nvidia_direct.py` | 러너에서 FLUX 직접 호출, 장애 판정 |
| `image/ai_image_kickoff.py` | Worker에 그림 요청(레거시) |
| `image/pixabay_client.py` | 스톡 검색 |
| `image/thumbnail_generator.py` | 썸네일 |
| `subtitle/subtitle_builder.py` | SRT + 편집 스타일 ASS |
| `video/scene_timer.py` | 타임라인 계산 |
| `video/video_composer.py` | 6단계 합성 오케스트레이션 |
| `video/ffmpeg_wrapper.py` | ffmpeg 호출(무빙·xfade·믹스·번인) |
| `youtube/youtube_uploader.py` | OAuth·업로드·썸네일 |
| `utils/soft_approval.py` | 예약공개/즉시공개 전환 |
| `core/video_registry.py` | 업로드 이력 기록 |
| `core/analytics_collector.py`, `youtube_analytics.py`, `performance_analysis.py` | 성과 수집·집계 |
| `tools/discord_worker/index.js` | Worker: 크론·디스코드 커맨드·그림 생성·워크플로우 트리거 |
| `tools/push_ci_config.py` | 로컬 config를 CI 시크릿으로 안전하게 동기화 |
