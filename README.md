# Auto Video Pipeline

`input/` 폴더에 넣어둔 `story.txt` 대본으로 **TTS 음성 → 이미지 생성 → 자막 → 영상 합성 → 썸네일 → YouTube 업로드**까지 완전 자동화하는 프로그램입니다.

## 요구 사항

- Python 3.10+ (3.14에서 테스트됨)
- FFmpeg / FFprobe (PATH 또는 `config/config.json`에 경로 지정)
- Google Cloud 프로젝트 + YouTube Data API v3 (업로드 기능 쓸 경우)

## 설치

```bash
git clone <repo>
cd auto_video_pipeline
install.bat        # 또는: pip install -r requirements.txt
```

## 실행

더블클릭: **`run.bat`**

> ⚠️ Windows cmd.exe의 `chcp 65001` 버그로 실행 자체가 깨지던 문제, 콘솔 유니코드(✅/⚠️) 출력 시 크래시하던 문제 모두 수정되어 있습니다. `run.bat`은 최소 구조(`chcp` → `python main.py` → 에러 시 `pause`)만 유지하세요 — 중간에 줄을 더 추가하면 같은 cmd.exe 버그가 재발할 수 있습니다.

## GUI 사용법

### 기본 흐름
1. `input/story.txt`(또는 아무 이름의 `.txt`)를 준비 (형식은 아래 참고)
2. 해상도 프리셋 선택
3. "▶ 시작"

### 채널별 대본 폴더
- `config/config.json`의 `youtube.channels`에 등록된 각 채널은 자기만의 대본 폴더(`input_dir`)를 가집니다 (예: `input/1번채널`, `input/2번채널`, `input/3번채널`). 설정 화면에서 `➕ 채널 추가`로 채널을 만들면 `input/<채널이름>/` 폴더가 자동으로 생성됩니다.
- 채널을 지정하지 않은 대본은 최상위 `input/` 폴더("기본 채널")에 넣으면 됩니다.
- 폴더는 서로 완전히 분리되어 있어서, 한 채널 폴더의 대본이 다른 채널 슬롯에 잘못 배분되지 않습니다.

### 동시 실행 슬롯 (왼쪽 패널)
- 여러 영상을 **병렬로** 동시에 만들 수 있습니다.
- 슬롯에서 **채널을 선택**하면, 그 아래 대기 개수 표시가 해당 채널의 대본 폴더 기준으로 바뀝니다.
- 슬롯의 경로 입력칸을 **비워두고 "▶ 시작"**을 누르면 선택한 채널의 대본 폴더에서 아직 다른 슬롯이 안 쓰는 파일을 자동으로 하나 가져와 처리하고, 업로드도 그 채널 계정으로 진행됩니다. 끝나면 `archive/<채널이름>/input_processed`(성공) 또는 `archive/<채널이름>/input_failed`(실패)로 자동 이동합니다 (기본 채널은 `archive/input_processed`).
- 특정 파일을 지정하고 싶으면 📂로 직접 선택 (이 경우 자동 이동 안 함).
- `➕ 슬롯 추가` 버튼으로 슬롯 개수를 원하는 만큼 늘릴 수 있고, 슬롯의 `✕`로 제거할 수 있습니다 (실행 중인 슬롯은 제거 불가).
- 해상도 / YouTube 업로드 여부·공개범위·예약일수는 오른쪽 패널의 현재 값을 슬롯 시작 시점에 공유해서 씁니다. 슬롯마다 다른 해상도를 쓰고 싶다면, 앞 슬롯이 "영상 합성" 단계에 들어가기 전까지는 설정을 바꾸지 말고 순서대로 시작하세요.

### 배치 모드 (오른쪽 패널)
- "입력 파일" 그룹에서 **채널을 선택**하면 그 채널의 대본 폴더가 대상이 됩니다.
- 선택한 채널 폴더에 대본을 여러 개 쌓아두고, 수량을 지정하면 오래된 것부터 순차적으로 처리하며, 업로드도 그 채널 계정으로 진행됩니다.
- 순서는 **파일 생성 시각** 기준이라, 나중에 대본을 열어서 수정해도 큐 순서가 바뀌지 않습니다.
- 슬롯(병렬)과 배치(순차)는 동시에 쓸 수 있습니다 — 상황에 맞게 섞어 쓰세요.

### 다크/라이트 모드
- 우측 상단 🌙/☀️ 버튼으로 전환. 선택한 테마는 `config/ui_prefs.json`에 저장되어 다음 실행 시에도 유지됩니다.

## story.txt 형식

빈 양식은 [`input/story_template.txt`](input/story_template.txt), 완성 예시는 [`input/story_example.txt`](input/story_example.txt) 참고.

```
CAST:
나레이터: 나레이터
남자1: 홍길동
여자1: 이순신

BGM:
background_music.mp3

PHOTO:
forest_morning.jpg

THUMBNAIL_LONG:
홍길동의 하루

THUMBNAIL_SHORTS:
홍길동의 하루 #Shorts

[SCENE:a beautiful misty forest in the morning, 1920x1080]
나레이터: 어느 맑은 아침이었습니다.&숲 속에서 새소리가 들려왔죠.
남자1: 오늘도 좋은 하루가 될 것 같아!&기분이 정말 상쾌하군.
```

| 항목 | 설명 |
|---|---|
| `CAST:` | 대사에 쓸 화자 키 → 실제 표시 이름 매핑 |
| `BGM:` | `assets/bgm/` 안의 파일명 |
| `PHOTO:` | `assets/photos/` 안의 파일명. **PHOTO/Pixabay 우선순위**에서 1순위로 쓰임 (필수 아님) |
| `THUMBNAIL_LONG` / `THUMBNAIL_SHORTS` | 썸네일에 넣을 제목 텍스트 한 줄. 검정 배경 + 컬러 텍스트로 자동 생성됨 (이미지 불필요) |
| `[SCENE:설명, 해상도]` | 장면 이미지를 찾을 때 쓰일 설명(영어 권장 — Pixabay 검색어로 변환됨) + 해상도. `NEGATIVE: ...`, `SEED: 123` 은 더 이상 쓰이지 않지만 무시되므로 남겨둬도 무방 |
| `화자: 대사&대사` | `&`로 나누면 그 지점에서 자막이 여러 줄(세그먼트)로 쪼개짐 (TTS 호흡/타이밍 구분용) |

여러 편을 미리 써서 `input/` (또는 채널별 폴더 `input/<채널이름>/`)에 쌓아두면 슬롯 자동 배분이나 배치 모드로 한 번에 처리할 수 있습니다.

## 이미지 생성 우선순위

1. **PHOTO** (`story.txt`의 `PHOTO:`에 지정한 사진, 순환 사용)
2. **Pixabay** (무료 스톡 이미지 자동 검색·다운로드)
3. 둘 다 실패하면 검정 배경

## 여러 YouTube 채널에 업로드

한 구글 계정으로 브랜드 채널을 여러 개 운영 중이라면, 슬롯마다 다른 채널을 지정해 병렬로 업로드할 수 있습니다.

1. GUI **⚙ 설정 → YouTube 탭 → 채널 관리 → ➕ 채널 추가** 에서 이름만 입력해 채널을 등록
2. 각 슬롯의 "채널:" 드롭다운에서 원하는 채널 선택
3. 그 슬롯을 처음 실행할 때 구글 로그인 창이 뜨는데, 이때 해당 채널이 있는 계정/브랜드 계정으로 로그인하면 이후로는 자동으로 그 채널에 업로드됩니다.

채널별 로그인 토큰은 `config/channels/<채널이름>_credentials.json`에 개별 저장됩니다. "기본 채널"을 선택하면 기존 `youtube.credentials_file` 설정을 그대로 씁니다.

## 설정

`config/config.json`에서 모든 설정을 변경할 수 있습니다 (최초 실행 시 `default_config.json`을 복사해 생성됨). GUI **⚙ 설정** 버튼으로도 편집 가능합니다.

주요 설정:

| 항목 | 키 | 기본값 |
|---|---|---|
| Pixabay API 키 | `image.pixabay_api_key` | (미리 설정됨) |
| GPU 인코딩 | `video.gpu_encoding` | `false` |
| BGM 볼륨 | `video.bgm_volume` | `0.15` |
| 자막 폰트 | `subtitle.font_name` | `Malgun Gothic` |
| 썸네일 폰트 | `thumbnail.font_name` | `NanumGothicBold` (없으면 맑은 고딕으로 자동 대체) |
| YouTube 채널 목록 | `youtube.channels` | `[]` |
| 오래된 archive 자동 정리 기준(일) | `automation.cleanup_days` | `7` |

## YouTube 인증 (최초 1회)

1. Google Cloud Console → APIs & Services → Credentials
2. OAuth 2.0 클라이언트 ID 생성 (데스크톱 앱)
3. `config/client_secrets.json` 으로 저장
4. 첫 업로드 시 브라우저 인증 창이 열림 → 로그인/승인 → 이후 자동 갱신
5. 여러 채널을 쓸 경우 채널별로 최초 1회씩 로그인 필요 (위 "여러 YouTube 채널에 업로드" 참고)

## 폰트 / 인코딩 관련 참고

- 한글 자막·썸네일 텍스트 렌더링은 Windows 기본 폰트(맑은 고딕)를 `assets/fonts/`에 번들해서 폰트가 없어도 깨지지 않도록 되어 있습니다.
- 콘솔·ffprobe 관련 인코딩(cp949) 문제는 모두 UTF-8로 고정되어 있습니다. 파일/폴더 경로에 한글이 섞여 있어도 정상 동작합니다.

## 프로젝트 구조

```
auto_video_pipeline/
├── main.py
├── run.bat                   # 실행 진입점
├── install.bat
├── requirements.txt
├── config/
│   ├── default_config.json
│   ├── config.json           # 사용자 설정 (자동 생성)
│   ├── ui_prefs.json          # 다크/라이트 테마 저장
│   ├── recent.json            # 최근 작업 목록
│   └── channels/              # 채널별 YouTube credentials
├── core/
│   ├── pipeline.py            # 전체 오케스트레이터
│   ├── batch_runner.py        # 배치(순차) 실행기
│   └── job_manager.py
├── parser/                    # story.txt 파서
├── tts/                       # Edge-TTS 음성 합성
├── image/                     # PHOTO/Pixabay 이미지, 썸네일
├── subtitle/                  # SRT / ASS 자막
├── video/                     # FFmpeg 영상 합성
├── youtube/                   # YouTube API 업로드
├── gui/                       # PySide6 GUI (슬롯, 테마, 설정 등)
├── utils/                     # 공통 유틸리티
├── assets/
│   ├── bgm/
│   ├── photos/
│   └── fonts/                 # 자막/썸네일용 폰트 (맑은 고딕 번들)
├── input/                     # story.txt 입력 폴더 (기본 채널)
│   ├── 1번채널/                # 채널별 대본 폴더 (config.json youtube.channels 기준)
│   ├── 2번채널/
│   └── 3번채널/
├── temp/                      # 임시 파일 (자동 정리)
├── output/                    # 최종 출력 (영상 + 썸네일 + 자막)
├── archive/                   # 완료된 output 아카이브 + 처리된 input (채널별 하위 폴더)
└── logs/                      # 로그
```

## 라이선스

MIT
