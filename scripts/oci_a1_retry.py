"""
scripts/oci_a1_retry.py
========================
Oracle Cloud Always Free의 Ampere A1.Flex는 인기가 많아서 "Out of capacity"로
바로 생성이 안 되는 경우가 흔합니다. 이 스크립트는 한 번 실행될 때마다 인스턴스
생성을 "딱 한 번" 시도하고, 용량 부족이면 조용히 실패하고 끝납니다.
Windows 작업 스케줄러가 이 스크립트를 몇 분 간격으로 반복 실행하면서 자리가
날 때까지 계속 두드리는 방식입니다 (커뮤니티에서 흔히 쓰는 "재시도 봇" 패턴).

성공하면 Windows 알림을 띄우고, 스케줄러 작업을 스스로 비활성화합니다
(계속 시도할 필요가 없어지므로).

수동 실행: python scripts/oci_a1_retry.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import oci

from utils.notify import notify, notify_discord, notify_phone

# GUI 모니터(scripts/oci_a1_monitor_gui.py)가 읽는 상태 파일.
STATE_PATH = ROOT / "config" / "oci_a1_state.json"

# 디스코드에 현황을 올리는 최소 간격 — 매 시도(10분)마다 올리면 스팸이라
# 이만큼 지났을 때만 올립니다. 성공했을 때는 이 간격과 무관하게 바로 올립니다.
DISCORD_POST_INTERVAL = timedelta(minutes=30)

_DEFAULT_STATE = {
    "attempts": 0,
    "first_attempt": None,
    "last_attempt": None,
    "last_discord_post": None,
    "status": "waiting",
    "instance_id": None,
    "last_error": None,
}


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return dict(_DEFAULT_STATE)
    try:
        state = dict(_DEFAULT_STATE)
        state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        return state
    except Exception:
        return dict(_DEFAULT_STATE)


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _format_elapsed(start: datetime, end: datetime) -> str:
    delta = end - start
    days, rem = divmod(int(delta.total_seconds()), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}일")
    if hours:
        parts.append(f"{hours}시간")
    parts.append(f"{minutes}분")
    return " ".join(parts)


def _maybe_post_discord_status(state: dict) -> None:
    """30분 간격으로만 디스코드에 누적 현황을 올립니다 (스팸 방지)."""
    now = datetime.now()
    last_post = state.get("last_discord_post")
    if last_post:
        elapsed_since_post = now - datetime.fromisoformat(last_post)
        if elapsed_since_post < DISCORD_POST_INTERVAL:
            return

    first = datetime.fromisoformat(state["first_attempt"])
    elapsed_str = _format_elapsed(first, now)
    notify_discord(f"⏳ {elapsed_str} 전부터 지금까지 총 {state['attempts']}회 시도 중..")
    state["last_discord_post"] = now.isoformat(timespec="seconds")

# 앞서 API로 조회해둔 값들
COMPARTMENT_ID = "ocid1.tenancy.oc1..aaaaaaaaq52bb2bzldawr4jh4hlntyvliswcbkpuduxyi3oanaaerly262bq"
AVAILABILITY_DOMAIN = "lYdr:AP-OSAKA-1-AD-1"
SUBNET_ID = "ocid1.subnet.oc1.ap-osaka-1.aaaaaaaagpflof3hvkkrzhsnozrbz3fbr3ffy3qnaen3ggidluxxrux3ovoa"
IMAGE_ID = "ocid1.image.oc1.ap-osaka-1.aaaaaaaacxblapqsiodvwbiyhnuzv4edk5uw23boofijdlit2xtgz3f3h6eq"  # Oracle Linux 9.7 aarch64
SSH_PUBLIC_KEY_PATH = Path.home() / ".ssh" / "oci_a1_key.pub"
INSTANCE_NAME = "auto-video-pipeline-a1"

# 무료 한도 안에서 여유있게 — 필요하면 나중에 늘려도 됨 (최대 4 OCPU/24GB 무료)
OCPUS = 2.0
MEMORY_GBS = 12.0

_TASK_NAME = "OciA1RetryBot"


def try_launch() -> str | None:
    """인스턴스 생성을 한 번 시도합니다. 성공하면 인스턴스 OCID, 용량 부족 등
    실패 시 None을 반환합니다 (다른 종류의 에러는 그대로 예외를 던짐)."""
    config = oci.config.from_file()
    compute = oci.core.ComputeClient(config)

    ssh_key = SSH_PUBLIC_KEY_PATH.read_text(encoding="utf-8").strip()

    details = oci.core.models.LaunchInstanceDetails(
        compartment_id=COMPARTMENT_ID,
        availability_domain=AVAILABILITY_DOMAIN,
        shape="VM.Standard.A1.Flex",
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=OCPUS, memory_in_gbs=MEMORY_GBS,
        ),
        display_name=INSTANCE_NAME,
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=SUBNET_ID,
            assign_public_ip=True,
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=IMAGE_ID,
        ),
        metadata={"ssh_authorized_keys": ssh_key},
    )

    try:
        response = compute.launch_instance(details)
        return response.data.id
    except oci.exceptions.ServiceError as e:
        capacity_errors = ("LimitExceeded", "InternalError", "OutOfCapacity", "TooManyRequests")
        if e.code in capacity_errors or "capacity" in (e.message or "").lower():
            return None
        raise


def disable_scheduled_task() -> None:
    try:
        subprocess.run(
            ["schtasks", "/Change", "/TN", _TASK_NAME, "/DISABLE"],
            capture_output=True, timeout=30,
        )
    except Exception:
        pass


def main() -> int:
    state = _load_state()
    now = datetime.now()
    if state["first_attempt"] is None:
        state["first_attempt"] = now.isoformat(timespec="seconds")
    state["attempts"] = state.get("attempts", 0) + 1
    state["last_attempt"] = now.isoformat(timespec="seconds")

    print("Ampere A1 인스턴스 생성 시도 중...")
    try:
        instance_id = try_launch()
    except Exception as e:
        state["status"] = "error"
        state["last_error"] = str(e)
        _maybe_post_discord_status(state)
        _save_state(state)
        print(f"예상치 못한 오류: {e}")
        return 1

    if instance_id is None:
        state["status"] = "waiting"
        state["last_error"] = None
        _maybe_post_discord_status(state)
        _save_state(state)
        print("아직 자리 없음 (Out of capacity). 다음 스케줄에 재시도.")
        return 0

    state["status"] = "succeeded"
    state["instance_id"] = instance_id
    state["last_error"] = None
    _save_state(state)

    print(f"성공! instance_id={instance_id}")
    elapsed_str = _format_elapsed(datetime.fromisoformat(state["first_attempt"]), now)
    notify_discord(f"🎉 잡았다! {elapsed_str} 만에, 총 {state['attempts']}회 시도 끝에 A1 서버 확보 성공!")
    notify(
        "Ampere A1 서버 확보!",
        "무료 A1 인스턴스가 생성됐습니다. Claude에게 '오라클 A1 됐음'이라고 알려주세요.",
        duration_ms=15000,
    )
    notify_phone(
        "🎉 Ampere A1 서버 확보!",
        "무료 A1 인스턴스가 생성됐습니다. 노트북 열어서 Claude에게 '오라클 A1 됐음'이라고 알려주세요.",
    )
    disable_scheduled_task()
    return 0


if __name__ == "__main__":
    sys.exit(main())
