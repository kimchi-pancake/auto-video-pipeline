@echo off
chcp 65001 > nul
echo ============================================================
echo   Auto Video Pipeline — Windows 설치 스크립트
echo ============================================================
echo.

:: Python 버전 확인
python --version > nul 2>&1
if errorlevel 1 (
    echo [오류] Python 이 설치되어 있지 않습니다.
    echo       https://www.python.org/downloads/ 에서 Python 3.12 이상을 설치하세요.
    pause
    exit /b 1
)

echo [1/5] Python 버전 확인...
python -c "import sys; v=sys.version_info; exit(0 if v.major==3 and v.minor>=10 else 1)"
if errorlevel 1 (
    echo [오류] Python 3.10 이상이 필요합니다.
    pause
    exit /b 1
)
python --version

echo.
echo [2/5] pip 업그레이드...
python -m pip install --upgrade pip --quiet

echo.
echo [3/5] 패키지 설치 중 (requirements.txt)...
pip install -r requirements.txt
if errorlevel 1 (
    echo [오류] 패키지 설치 실패. 위 오류 메시지를 확인하세요.
    pause
    exit /b 1
)

echo.
echo [4/5] 디렉터리 구조 생성...
if not exist "input"    mkdir input
if not exist "output"   mkdir output
if not exist "temp\tts" mkdir temp\tts
if not exist "temp\images" mkdir temp\images
if not exist "temp\video"  mkdir temp\video
if not exist "logs"     mkdir logs
if not exist "archive"  mkdir archive
if not exist "assets\bgm"    mkdir assets\bgm
if not exist "assets\photos" mkdir assets\photos
if not exist "assets\fonts"  mkdir assets\fonts
if not exist "config"   mkdir config

echo.
echo [5/5] FFmpeg 확인...
ffmpeg -version > nul 2>&1
if errorlevel 1 (
    echo [경고] FFmpeg 가 PATH 에 없습니다.
    echo        https://ffmpeg.org/download.html 에서 다운로드 후 PATH에 추가하세요.
    echo        또는 config\config.json 에서 ffmpeg_path 를 절대 경로로 지정하세요.
) else (
    echo        FFmpeg 확인 완료.
)

echo.
echo ============================================================
echo   설치 완료!
echo.
echo   다음 단계:
echo   1. assets\fonts\ 에 NanumGothic.ttf (또는 원하는 폰트) 복사
echo   2. assets\bgm\   에 배경음악 MP3 파일 복사
echo   3. assets\photos\ 에 썸네일 배경 사진 복사
echo   4. YouTube 사용 시: config\client_secrets.json 배치
echo      (Google Cloud Console → OAuth 2.0 클라이언트 ID 다운로드)
echo   5. python main.py 실행
echo ============================================================
pause
