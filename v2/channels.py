"""
v2/channels.py
===============
채널별 프로필. 인프라는 공유하되 소재·어조·화면·음성은 분리한다.
채널 이름·인증파일은 기존 config.json(youtube.channels[])에서 읽는다 — 인증 정보는 건드리지 않는다.
"""
from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from v2 import ROOT

V2_CONFIG_PATH = ROOT / "config" / "v2.json"

DEFAULT_V2_CONFIG = {
    # review: 비공개 업로드까지만 하고 사람이 승인해야 예약공개. scheduled: 승인 없이 예약공개.
    "publish_mode": "review",
    "posts_per_day": {"도개": 1, "웃짬": 1},
    "publish_hour_kst": 20,
    "publish_minute_kst": 0,
    "contains_synthetic_media": False,
    "min_article_chars": 1500,
    "min_free_photos": 3,
    "exploration_c": 0.35,
    "metrics_ages_hours": [1, 6, 24, 72, 168, 672],
}


def load_v2_config() -> dict:
    cfg = dict(DEFAULT_V2_CONFIG)
    if V2_CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(V2_CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


@dataclass
class ChannelProfile:
    key: str                      # "dogae" | "ujjjam"
    name: str                     # config.json의 채널명 (한글)
    kind: str                     # "kukbong" | "world_econ"
    credentials_file: str = ""
    voice: str = "ko-KR-HyunsuMultilingualNeural"
    voice_rate: str = "+14%"
    voice_pitch: str = "+0Hz"
    categories: List[str] = field(default_factory=list)
    persona: str = ""
    tone_rules: str = ""
    charts_enabled: bool = True
    bgm_choices: List[str] = field(default_factory=list)
    accent_color: str = "#FFE500"
    palette_bg: str = "#0E1420"


DOGAE_CATEGORIES = ["첨단기술", "제조산업", "우주과학", "역사유산", "음식문화", "대중문화", "스포츠", "생활편의", "의료복지", "세계최초"]
UJJJAM_CATEGORIES = ["국제정세", "무역제재", "에너지자원", "금융위기", "물가금리", "환율달러", "국가부채", "산업패권", "공급망", "경제사", "빈부격차"]

_DOGAE_PERSONA = (
    "너는 한국의 기술·산업·역사·문화를 다루는 설명 영상 작가다. 목표는 시청자가 \"이건 몰랐다\"고 느끼게 하는 "
    "구체적인 사실, 비교, 원리를 전달하는 것이다. 찬양이나 구호가 아니라 근거와 디테일로 놀라움을 만든다. "
    "다른 나라·민족을 깎아내리지 않는다."
)
_DOGAE_TONE = (
    "- 객관적 성과, 구체적 수치·연도·이름, 의외의 원리, 문제를 해결한 과정을 쓴다.\n"
    "- '세계 최고', '압도적', '충격' 같은 과장은 근거 자료가 직접 말해줄 때만 쓴다.\n"
    "- 타국 비하·비교우위 서사 금지.\n"
    "- 소재 문서의 '핵심 매력'(어떻게 만들어졌나, 무엇이 특별한가, 어떤 원리인가, 어떤 성과를 냈나)에 집중한다. "
    "남북관계·정치·외교 갈등·소송 분쟁·표절 논란·사건사고 같은 곁가지는 영상의 중심으로 삼지 않는다."
)
_UJJJAM_PERSONA = (
    "너는 세계 경제와 국제 정세를 일반 시청자가 빠르게 이해하도록 풀어 설명하는 작가다. "
    "누가 이익을 얻고 누가 손해를 보는지, 왜 이런 일이 일어났는지, 어떤 조건에서 달라질 수 있는지를 근거로 설명한다. "
    "어조는 차분하고 날카롭다. 특정 인물·국민·정당을 공격하지 않고 구조와 제도를 본다."
)
_UJJJAM_TONE = (
    "- 사실, 해석, 전망을 문장 수준에서 구분한다. 해석에는 '~로 해석됩니다', '~라는 분석이 있습니다'를 쓴다.\n"
    "- 전망은 조건부로만 말한다('~라면 ~할 수 있습니다'). 가격 예측·투자 권유·공포 조장 금지.\n"
    "- 근거 자료에 없는 최신 수치·사건을 아는 척하지 않는다."
)


def default_profiles() -> Dict[str, ChannelProfile]:
    return {
        "dogae": ChannelProfile(
            key="dogae", name="도개", kind="kukbong",
            voice="ko-KR-HyunsuMultilingualNeural", voice_rate="+14%", voice_pitch="+0Hz",
            categories=DOGAE_CATEGORIES, persona=_DOGAE_PERSONA, tone_rules=_DOGAE_TONE,
            bgm_choices=["piano.mp3", "ambient_piano.mp3", "simple.mp3"],
            accent_color="#FFE500", palette_bg="#0E1420",
        ),
        "ujjjam": ChannelProfile(
            key="ujjjam", name="웃짬", kind="world_econ",
            voice="ko-KR-InJoonNeural", voice_rate="+12%", voice_pitch="-3Hz",
            categories=UJJJAM_CATEGORIES, persona=_UJJJAM_PERSONA, tone_rules=_UJJJAM_TONE,
            bgm_choices=["ambient_piano.mp3", "piano.mp3", "simple.mp3"],
            accent_color="#FF6B3D", palette_bg="#0B0F17",
        ),
    }


def load_profiles(config_channels: Optional[list] = None) -> Dict[str, ChannelProfile]:
    """config.json의 채널 목록과 프로필을 연결한다. content_profile == world_econ 이면 웃짬, 아니면 도개."""
    profiles = default_profiles()
    if config_channels is None:
        try:
            cfg = json.loads((ROOT / "config" / "config.json").read_text(encoding="utf-8-sig"))
            config_channels = cfg.get("youtube", {}).get("channels", [])
        except Exception:
            config_channels = []
    for ch in config_channels or []:
        key = "ujjjam" if ch.get("content_profile") == "world_econ" else "dogae"
        p = profiles[key]
        p.name = nfc(ch.get("name", p.name))
        p.credentials_file = ch.get("credentials_file", "")
        if ch.get("paused"):
            p.key = key + ":paused"
    return profiles


def active_profiles(config_channels: Optional[list] = None) -> List[ChannelProfile]:
    return [p for p in load_profiles(config_channels).values() if not p.key.endswith(":paused")]
