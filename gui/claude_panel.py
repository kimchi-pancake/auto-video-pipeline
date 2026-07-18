"""
gui/claude_panel.py
====================
Claude(claude.ai) 웹앱을 내장해서 대본을 작성하고, 클립보드에 복사해둔 마지막
응답을 채널의 대본 대기 폴더에 story.txt 로 바로 저장하는 패널.

채널 선택은 이 패널이 아니라 메인 창 상단 바에서 하나로 통일해서 관리합니다
(get_active_channel 콜백으로 전달받음) — '가져오기'를 누르면 그 채널의
대기열에 저장됩니다.

로그인 세션은 config/webengine_profile/ 에 영구 저장되므로, 최초 1회
Anthropic 계정으로 로그인해두면 앱을 껐다 켜도 로그인이 유지됩니다.
(Google 계정이 아니라 별도의 Anthropic 계정입니다.)

'가져오기'는 DOM을 직접 긁지 않고, 사용자가 Claude 응답을 직접 드래그해서
Ctrl+C로 복사한 클립보드 내용을 그대로 저장합니다. claude.ai가 자체 "복사"
버튼을 응답마다 제공하므로 그걸 눌러도 됩니다. DOM 스크래핑 방식은 Claude의
실제 화면 구조를 예측할 수 없어(로그인이 필요해 직접 확인 불가) 셀렉터가
계속 안 맞는 문제가 있어 클립보드 방식으로 바꿨습니다 — 사이트 구조가 바뀌어도
절대 깨지지 않습니다.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, QThread, QUrl, Signal
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QVBoxLayout, QWidget,
)
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage, QWebEnginePermission
from PySide6.QtWebEngineWidgets import QWebEngineView

from core.ai_script_generator import ScriptGenerationError, generate_daily_batch, save_story
from core.script_prompts import (
    COMBO_SCRIPT_PROMPT,
    LONG_SCRIPT_PROMPT,
    SHORTS_SCRIPT_PROMPT,
    SPLIT_DELIMITER,
)


class _MainPage(QWebEnginePage):
    """
    '구글로 계속하기' 로그인은 window.open()으로 뜨는 팝업 창에서 진행되는데,
    QWebEngineView는 기본적으로 팝업을 그냥 무시해버립니다 (버튼을 눌러도
    아무 반응이 없는 것처럼 보이는 원인). createWindow()를 오버라이드해서
    별도의 QWebEngineView 창으로 띄워줘야 팝업 로그인이 동작합니다.
    """

    def __init__(self, profile: QWebEngineProfile, popups: list, parent=None):
        super().__init__(profile, parent)
        self._popups = popups

    def createWindow(self, _window_type):
        popup_view = QWebEngineView()
        popup_view.setWindowTitle("로그인")
        popup_view.resize(480, 640)
        popup_page = QWebEnginePage(self.profile(), popup_view)
        popup_view.setPage(popup_page)
        popup_view.destroyed.connect(lambda: self._popups.remove(popup_view) if popup_view in self._popups else None)
        popup_view.show()
        self._popups.append(popup_view)
        return popup_page

CLAUDE_URL = "https://claude.ai/new"

# 프롬프트 템플릿(LONG_SCRIPT_PROMPT/SHORTS_SCRIPT_PROMPT/COMBO_SCRIPT_PROMPT)과
# SPLIT_DELIMITER는 core/script_prompts.py로 옮겼습니다 — API 자동 생성 경로
# (core/ai_script_generator.py)와 이 수동 복사 경로가 같은 프롬프트를 씁니다.

_PROFILE_DIR = Path(__file__).parent.parent / "config" / "webengine_profile"


class _ScriptGenWorker(QObject):
    """하루치 대본 일괄 생성(기본 롱폼 1개+쇼츠 2개)을 백그라운드 스레드에서 돌리는
    워커. QThread 수명주기는 ChannelCardWidget과 동일한 규칙을 따릅니다 — 스레드
    참조는 절대 이 워커의 finished 계열 시그널에서 null 처리하지 않고, QThread
    자신의 finished 시그널에서만 처리합니다."""

    succeeded = Signal(list)
    failed = Signal(str)
    progress = Signal(int, int)

    def __init__(self, input_dir: Path):
        super().__init__()
        self._input_dir = input_dir

    def run(self) -> None:
        try:
            saved = generate_daily_batch(self._input_dir, progress_cb=self.progress.emit)
            self.succeeded.emit(saved)
        except ScriptGenerationError as e:
            self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(f"예상치 못한 오류: {e}")


class ClaudePanel(QWidget):
    """
    좁은 사이드 패널로 쓰이는 대본 작성 패널.
    Claude 응답을 드래그 선택 후 Ctrl+C(또는 응답의 '복사' 버튼)로 복사해두면,
    '가져오기'를 눌렀을 때 그 클립보드 내용을 상단 바에서 선택된 채널의
    대기열에 새 story.txt 로 저장합니다.
    """

    def __init__(
        self,
        get_active_channel: Callable[[], dict],
        resolve_input_dir: Callable[[str], Path],
        on_saved: Optional[Callable[[str], None]] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._get_active_channel = get_active_channel  # () -> {"name","input_dir",...}
        self._resolve_input_dir = resolve_input_dir     # (input_dir) -> Path
        self._on_saved = on_saved                       # (channel_name) -> None
        self._popups: list = []                         # 로그인 팝업 창 참조 보관 (GC 방지)
        self._gen_thread: Optional[QThread] = None
        self._gen_worker: Optional[_ScriptGenWorker] = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        header_row = QHBoxLayout()
        title = QLabel("AI 대본 작성")
        title.setObjectName("sectionTitle")
        header_row.addWidget(title)
        header_row.addStretch()

        self._gen_btn = QPushButton("오늘 대본 생성 (롱1+숏2)")
        self._gen_btn.setObjectName("btn_start")
        self._gen_btn.setToolTip(
            "Claude API(Haiku)로 롱폼 1개 + 쇼츠 2개를 한 번에 자동 생성해서\n"
            "상단에서 선택한 채널의 대기열에 바로 저장합니다.\n"
            "(.env 파일에 ANTHROPIC_API_KEY 설정 필요)"
        )
        self._gen_btn.clicked.connect(self._generate_with_api)
        header_row.addWidget(self._gen_btn)

        new_chat_btn = QPushButton("새 대화 시작")
        new_chat_btn.setObjectName("ghostBtn")
        new_chat_btn.setToolTip("새 Claude 대화 시작")
        new_chat_btn.clicked.connect(self._new_chat)
        header_row.addWidget(new_chat_btn)

        import_btn = QPushButton("마지막 응답 가져오기")
        import_btn.setObjectName("outlineAccentBtn")
        import_btn.setToolTip(
            "Claude 응답을 드래그로 선택해 Ctrl+C(또는 응답의 '복사' 버튼)로 복사한 뒤 누르면,\n"
            "클립보드 내용을 상단에서 선택한 채널의 대기열에 저장합니다."
        )
        import_btn.clicked.connect(self._import_script)
        header_row.addWidget(import_btn)
        layout.addLayout(header_row)

        browser_frame = QWidget()
        browser_frame.setObjectName("browserFrame")
        browser_layout = QVBoxLayout(browser_frame)
        browser_layout.setContentsMargins(1, 1, 1, 1)
        # 주의: QGraphicsDropShadowEffect는 절대 달지 않습니다 — QWebEngineView는
        # 자체 GPU 프로세스(Chromium)가 합성하는데, 부모에 QGraphicsEffect를
        # 걸면 Qt가 매 리페인트마다 그 서브트리 전체를 오프스크린 픽스맵으로
        # 다시 그려서 블러를 입혀야 해서 클릭 반응이 몇 초씩 느려집니다.

        _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self._profile = QWebEngineProfile("claude_profile", self)
        self._profile.setPersistentStoragePath(str(_PROFILE_DIR))
        self._profile.setCachePath(str(_PROFILE_DIR / "cache"))
        # User-Agent는 일부러 손대지 않습니다 — 실제 번들된 Chromium 버전과 다른
        # UA 문자열을 억지로 넣으면 UA와 Client Hints(Sec-CH-UA)가 서로 안 맞아
        # 오히려 "자동화 브라우저"로 더 잘 걸립니다. (navigator.webdriver 노출은
        # main.py의 QTWEBENGINE_CHROMIUM_FLAGS 로 별도 처리합니다.)
        self._profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
        )

        self._view = QWebEngineView()
        page = _MainPage(self._profile, self._popups, self._view)
        page.permissionRequested.connect(self._on_permission_requested)
        self._view.setPage(page)
        self._view.load(QUrl(CLAUDE_URL))
        browser_layout.addWidget(self._view)
        layout.addWidget(browser_frame, 1)

        template_label = QLabel("프롬프트 템플릿 복사")
        template_label.setObjectName("templateLabel")
        layout.addWidget(template_label)

        template_row = QHBoxLayout()
        long_template_btn = QPushButton("긴 대본용 복사")
        long_template_btn.setObjectName("templateBtn")
        long_template_btn.setToolTip(
            "장면 수·분량을 못박은 긴 대본용 프롬프트를 클립보드에 복사합니다.\n"
            "Claude 채팅창에 Ctrl+V로 붙여넣고 '주제:' 부분만 채워서 보내세요."
        )
        long_template_btn.clicked.connect(self._copy_long_prompt)
        template_row.addWidget(long_template_btn)

        shorts_template_btn = QPushButton("쇼츠용 복사")
        shorts_template_btn.setObjectName("templateBtn")
        shorts_template_btn.setToolTip(
            "5~8장면, 30~50초 분량으로 짧게 못박은 쇼츠 전용 프롬프트를 클립보드에 복사합니다.\n"
            "Claude 채팅창에 Ctrl+V로 붙여넣고 '주제:' 부분만 채워서 보내세요."
        )
        shorts_template_btn.clicked.connect(self._copy_shorts_prompt)
        template_row.addWidget(shorts_template_btn)

        combo_template_btn = QPushButton("롱폼+쇼츠 통합용 복사")
        combo_template_btn.setObjectName("templateBtn")
        combo_template_btn.setToolTip(
            "롱폼·쇼츠 대본을 한 번의 Claude 응답으로 같이 받는 프롬프트를 복사합니다.\n"
            "'가져오기'를 누르면 두 대본이 자동으로 나뉘어 각각 story.txt로 저장되고,\n"
            "각자 RESOLUTION이 박혀 있어서 처리될 때 해상도도 알아서 맞춰집니다."
        )
        combo_template_btn.clicked.connect(self._copy_combo_prompt)
        template_row.addWidget(combo_template_btn)
        layout.addLayout(template_row)

    @staticmethod
    def _on_permission_requested(permission: QWebEnginePermission) -> None:
        # Claude 응답의 '복사' 버튼은 navigator.clipboard.writeText()를 쓰는데,
        # QtWebEngine은 기본적으로 클립보드 권한 요청을 그냥 무시(거부)해서
        # 버튼을 눌러도 아무 일도 안 일어나는 원인이 됩니다. 클립보드 권한만
        # 자동 허용하고, 카메라/마이크/위치 등 다른 권한은 그대로 거부합니다.
        if permission.permissionType() == QWebEnginePermission.PermissionType.ClipboardReadWrite:
            permission.grant()
        else:
            permission.deny()

    def _new_chat(self) -> None:
        self._view.load(QUrl(CLAUDE_URL))

    def _copy_long_prompt(self) -> None:
        QApplication.clipboard().setText(LONG_SCRIPT_PROMPT)
        QMessageBox.information(
            self, "복사됨",
            "긴 대본용 프롬프트를 클립보드에 복사했습니다.\n"
            "Claude 채팅창을 클릭하고 Ctrl+V로 붙여넣은 뒤, '주제:' 부분만 채워서 보내세요."
        )

    def _copy_shorts_prompt(self) -> None:
        QApplication.clipboard().setText(SHORTS_SCRIPT_PROMPT)
        QMessageBox.information(
            self, "복사됨",
            "쇼츠용 프롬프트를 클립보드에 복사했습니다.\n"
            "Claude 채팅창을 클릭하고 Ctrl+V로 붙여넣은 뒤, '주제:' 부분만 채워서 보내세요."
        )

    def _copy_combo_prompt(self) -> None:
        QApplication.clipboard().setText(COMBO_SCRIPT_PROMPT)
        QMessageBox.information(
            self, "복사됨",
            "롱폼+쇼츠 한번에 프롬프트를 클립보드에 복사했습니다.\n"
            "Claude 채팅창을 클릭하고 Ctrl+V로 붙여넣은 뒤, '주제:' 부분만 채워서 보내세요.\n"
            "응답 하나에 두 대본이 같이 오면 '가져오기'가 자동으로 나눠서 저장합니다."
        )

    def _import_script(self) -> None:
        text = (QApplication.clipboard().text() or "").strip()
        self._on_scraped(text)

    def _on_scraped(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            QMessageBox.warning(
                self, "가져오기 실패",
                "클립보드가 비어 있습니다.\n"
                "Claude 응답을 드래그로 선택해 Ctrl+C로 복사(또는 응답의 '복사' 버튼)한 뒤\n"
                "다시 '가져오기'를 눌러주세요."
            )
            return

        chan = self._get_active_channel() or {"name": "", "input_dir": ""}
        input_dir = self._resolve_input_dir(chan.get("input_dir", ""))
        input_dir.mkdir(parents=True, exist_ok=True)
        chan_label = chan.get("name") or "기본 채널"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        # 롱폼+쇼츠 한번에 프롬프트로 받은 응답이면 구분선 기준으로 둘로 쪼갭니다.
        if SPLIT_DELIMITER in text:
            long_part, _, shorts_part = text.partition(SPLIT_DELIMITER)
            long_part = long_part.strip()
            shorts_part = shorts_part.strip()
            saved_names = []
            if long_part:
                saved_names.append(save_story(input_dir, f"claude_{ts}_long.txt", long_part).name)
            if shorts_part:
                saved_names.append(save_story(input_dir, f"claude_{ts}_shorts.txt", shorts_part).name)
            if not saved_names:
                QMessageBox.warning(
                    self, "가져오기 실패",
                    "구분선은 찾았지만 양쪽에 내용이 없습니다. Claude 응답을 확인해주세요."
                )
                return
            QMessageBox.information(
                self, "가져오기 완료",
                f"'{chan_label}' 대기열에 {len(saved_names)}개 저장했습니다:\n" + "\n".join(saved_names)
            )
        else:
            dest = save_story(input_dir, f"claude_{ts}.txt", text)
            QMessageBox.information(
                self, "가져오기 완료",
                f"'{chan_label}' 대기열에 저장했습니다:\n{dest.name}"
            )

        if self._on_saved:
            self._on_saved(chan.get("name", ""))

    def _generate_with_api(self) -> None:
        if self._gen_thread is not None:
            QMessageBox.information(self, "생성 중", "이미 대본을 생성하고 있습니다. 잠시만 기다려주세요.")
            return

        chan = self._get_active_channel() or {"name": "", "input_dir": ""}
        input_dir = self._resolve_input_dir(chan.get("input_dir", ""))

        self._gen_btn.setEnabled(False)
        self._gen_btn.setText("생성 중... (0/2)")

        self._gen_worker = _ScriptGenWorker(input_dir)
        self._gen_thread = QThread(self)
        self._gen_worker.moveToThread(self._gen_thread)
        self._gen_thread.started.connect(self._gen_worker.run)
        self._gen_worker.progress.connect(self._on_gen_progress)
        self._gen_worker.succeeded.connect(self._on_gen_succeeded)
        self._gen_worker.failed.connect(self._on_gen_failed)
        self._gen_worker.succeeded.connect(self._gen_thread.quit)
        self._gen_worker.failed.connect(self._gen_thread.quit)
        self._gen_thread.finished.connect(self._on_gen_thread_finished)
        self._gen_thread.start()

    def _on_gen_progress(self, done: int, total: int) -> None:
        self._gen_btn.setText(f"생성 중... ({done}/{total})")

    def _on_gen_succeeded(self, saved: list[Path]) -> None:
        chan = self._get_active_channel() or {"name": ""}
        chan_label = chan.get("name") or "기본 채널"
        names = "\n".join(p.name for p in saved)
        QMessageBox.information(
            self, "생성 완료",
            f"'{chan_label}' 대기열에 {len(saved)}개 저장했습니다:\n{names}"
        )
        if self._on_saved:
            self._on_saved(chan.get("name", ""))

    def _on_gen_failed(self, message: str) -> None:
        QMessageBox.warning(self, "생성 실패", message)

    def _on_gen_thread_finished(self) -> None:
        # QThread 수명주기 규칙: 스레드/워커 참조는 QThread 자신의 finished
        # 시그널에서만 null 처리합니다 (worker의 succeeded/failed에서 하면
        # 아직 돌고 있는 스레드를 파괴할 수 있어 위험합니다).
        self._gen_thread = None
        self._gen_worker = None
        self._gen_btn.setEnabled(True)
        self._gen_btn.setText("오늘 대본 생성 (롱1+숏2)")
