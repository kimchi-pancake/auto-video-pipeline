"""
v2 — 조회수 중심 재설계 파이프라인.

설계 원칙
- 모든 영상은 고유 job_id와 명시적 상태(NEW → … → PUBLISHED)를 가진다.
- 상태는 data/jobs/{job_id}.json 에 저장하며 코드는 절대 삭제하지 않는다(장애 시 소실 방지).
- 장애(서비스 문제)와 콘텐츠 품질 실패를 구분한다. 서비스 장애는 RETRY_WAIT, 품질 실패는 NEEDS_REVIEW.
- 성과 수치는 공식 API가 주는 값만 기록하고, 없는 값을 추정해 채우지 않는다.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
