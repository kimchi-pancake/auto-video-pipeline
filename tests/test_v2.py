"""
v2 단위 테스트 (네트워크·LLM 없이 도는 것만). 실행: python -m pytest tests -q
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from v2 import research, script as sm
from v2.channels import default_profiles
from v2.jobstore import JobStore, LeaseError, TransitionError
from v2.publish import next_slot
from v2.sources.wiki import license_ok
from v2.textnorm import normalize_narration
from v2.topics import similarity

PACK = {
    "sources": [{"id": "S1", "title": "t", "url": "u", "published": "2026-01-01", "fetched_at": "2026-11-18", "lang": "ko",
                 "text": "측우기는 1441년 세종 시대에 장영실이 만든 강우량 측정 기구이다. 받침대 위에 쇠로 만든 원통형 그릇을 올려 두었다. "
                         "당시 측우기의 지름은 14센티미터였다."}],
    "photos": [{"id": "P1", "caption": "측우기", "license": "CC BY 4.0", "page": "p", "artist": "a"}],
}
HOOK = {"first_line": "1441년에 비를 숫자로 쟀습니다", "screen_text": "비를 재다", "type": "surprising_fact"}


def _scene(nar, visual, claims=None):
    return {"narration": nar, "visual": visual,
            "claims": claims or [{"text": nar, "kind": "fact", "source": "S1", "quote": "측우기는 1441년 세종 시대에 장영실이 만든 강우량 측정 기구이다"}]}


def _good_script():
    return {"title": "측우기가 비를 재던 방법", "category": "역사유산", "scenes": [
        _scene("1441년에 비를 숫자로 쟀습니다. 세종 시대의 측우기입니다.", {"type": "photo", "id": "P1"}),
        _scene("장영실이 만든 강우량 측정 기구입니다.", {"type": "stat", "big": "1441년", "small": "측우기가 만들어진 해"}),
        _scene("쇠로 만든 원통형 그릇을 받침대 위에 올렸습니다.", {"type": "steps", "title": "구조", "items": ["받침대", "원통 그릇", "눈금자"]}),
        _scene("지름은 14센티미터였습니다.", {"type": "text", "headline": "지름 14센티미터"},
               [{"text": "당시 측우기의 지름은 14센티미터였다", "kind": "fact", "source": "S1", "quote": "당시 측우기의 지름은 14센티미터였다"}]),
        _scene("그래서 비의 양을 비교할 수 있었습니다.", {"type": "photo", "id": "P1"}),
    ]}


def test_good_script_passes():
    prof = default_profiles()["dogae"]
    s = _good_script()
    s["scenes"][4]["claims"] = [{"text": "비교", "kind": "interpretation", "source": "S1", "quote": ""}]
    s["scenes"][4]["narration"] = "그래서 비의 양을 비교할 수 있었다고 해석됩니다."
    sm.validate(s, PACK, prof, HOOK)


def test_ungrounded_number_blocked():
    prof = default_profiles()["dogae"]
    s = _good_script()
    s["scenes"][1]["narration"] = "장영실이 1450개를 만들었습니다."
    with pytest.raises(ValueError, match="숫자"):
        sm.validate(s, PACK, prof, HOOK)


def test_fake_quote_blocked_and_code_evidence_accepts_real_claim():
    prof = default_profiles()["dogae"]
    s = _good_script()
    s["scenes"][2]["claims"] = [{"text": "우주선을 발사했다", "kind": "fact", "source": "S1", "quote": "측우기는 우주선을 발사했다고 한다 매우 길다 길다"}]
    with pytest.raises(ValueError, match="뒷받침"):
        sm.validate(s, PACK, prof, HOOK)
    s2 = _good_script()
    s2["scenes"][1]["claims"] = [{"text": "1441년 장영실이 강우량 측정 기구를 만들었다", "kind": "fact", "source": "S1", "quote": "잘못된 인용입니다 잘못"}]
    s2["scenes"][4]["claims"] = [{"text": "x", "kind": "interpretation", "source": "S1", "quote": ""}]
    s2["scenes"][4]["narration"] = "그래서 비의 양을 비교할 수 있었다고 해석됩니다."
    sm.validate(s2, PACK, prof, HOOK)
    assert s2["scenes"][1]["claims"][0]["quote_found_by"] == "code"


def test_filler_and_latin_blocked():
    prof = default_profiles()["dogae"]
    s = _good_script()
    s["scenes"][0]["narration"] = "1441년에 비를 숫자로 쟀습니다. 구독과 좋아요 부탁드립니다."
    with pytest.raises(ValueError, match="유도"):
        sm.validate(s, PACK, prof, HOOK)
    s = _good_script()
    s["scenes"][3]["narration"] = "지름은 14cm였습니다."
    with pytest.raises(ValueError, match="알파벳"):
        sm.validate(s, PACK, prof, HOOK)


def test_unknown_photo_and_repeated_visual_blocked():
    prof = default_profiles()["dogae"]
    s = _good_script()
    s["scenes"][0]["visual"] = {"type": "photo", "id": "P9"}
    with pytest.raises(ValueError, match="사진"):
        sm.validate(s, PACK, prof, HOOK)


def test_normalize_units():
    assert normalize_narration("높이 46cm와 3.5t, 12km") == "높이 46센티미터와 3.5톤, 12킬로미터"
    assert "국내총생산" in normalize_narration("GDP")


def test_quote_support_and_numbers():
    assert research.quote_supported("측우기는 1441년 세종 시대에 장영실이 만든", PACK["sources"][0]["text"])
    assert not research.quote_supported("전혀 없는 문장입니다 정말로 없습니다", PACK["sources"][0]["text"])
    allowed = research.source_numbers(PACK)
    assert research.ungrounded_numbers("1441년 14센티미터", allowed) == []
    assert research.ungrounded_numbers("9999명", allowed) == ["9999"]


def test_license_filter():
    for ok in ("CC BY 4.0", "CC BY 2.0", "Public domain", "CC0", "KOGL Type 1"):
        assert license_ok(ok), ok
    for bad in ("CC BY-SA 4.0", "CC BY-SA 3.0", "CC BY-NC 4.0", "CC BY-ND 2.0", "Fair use", "", "All rights reserved"):
        assert not license_ok(bad), bad


def test_similarity():
    assert similarity("측우기가 비를 재던 방법", "측우기가 비를 재던 방법") == 1.0
    assert similarity("측우기가 비를 재던 방법", "누리호 발사 성공") < 0.2


def test_jobstore_state_machine_and_lease(tmp_path):
    st = JobStore(tmp_path)
    job = st.create("도개", {"title": "t", "category": "c"}, day="20261118")
    assert job["status"] == "NEW"
    st.transition(job, "RESEARCHING", "x")
    st.claim(job, owner="A")
    with pytest.raises(LeaseError):
        st.claim(st.get(job["id"]), owner="B")
    st.release(job)
    # 서비스 장애 → RETRY_WAIT (데이터 보존), 품질 실패 → NEEDS_REVIEW
    j = st.fail(st.get(job["id"]), "script", "service_transient", "timeout")
    assert j["status"] == "RETRY_WAIT" and j["retry_after"]
    j2 = st.create("도개", {"title": "t2"}, day="20261118")
    j2 = st.fail(j2, "script", "content_quality", "근거 없음")
    assert j2["status"] == "NEEDS_REVIEW"
    assert (tmp_path / f"{j2['id']}.json").exists()  # 삭제되지 않음
    # 반복 장애는 FAILED
    j3 = st.create("도개", {"title": "t3"}, day="20261118")
    for _ in range(4):
        j3 = st.fail(j3, "upload", "service_transient", "5xx")
    assert j3["status"] == "FAILED"
    with pytest.raises(TransitionError):
        st.transition(j3, "NEW")


def test_publish_slot_never_immediate():
    from v2.publish import KST
    late = KST.localize(datetime(2026, 11, 18, 21, 30))
    slot = next_slot(late)
    assert slot > late and slot.day == 19 and slot.hour == 20
    early = KST.localize(datetime(2026, 11, 18, 10, 0))
    assert next_slot(early).day == 18


def test_channel_profiles_separate():
    p = default_profiles()
    assert p["dogae"].kind != p["ujjjam"].kind
    assert p["dogae"].voice == p["ujjjam"].voice  # 2026-11-19 사용자가 현수 목소리로 통일
    assert set(p["dogae"].categories).isdisjoint(p["ujjjam"].categories)
