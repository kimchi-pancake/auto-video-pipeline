"""
core/job_manager.py
===================
작업(Job) 큐 관리자.
실패한 작업의 자동 재시도, 작업 상태 추적, 멀티스레드 실행을 담당합니다.
파이프라인의 각 단계를 독립적인 Job으로 등록하여 실행합니다.
"""

from __future__ import annotations

import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from utils.logger import get_logger

logger = get_logger(__name__)


class JobStatus(Enum):
    PENDING = auto()
    RUNNING = auto()
    SUCCESS = auto()
    FAILED = auto()
    RETRYING = auto()
    CANCELLED = auto()


@dataclass
class Job:
    """실행 가능한 작업 단위."""
    job_id: str
    name: str
    fn: Callable
    args: tuple = field(default_factory=tuple)
    kwargs: dict = field(default_factory=dict)
    max_retries: int = 3
    retry_delay: float = 2.0
    # 런타임 상태
    status: JobStatus = JobStatus.PENDING
    result: Any = None
    error: Optional[str] = None
    attempt: int = 0
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    @property
    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.finished_at or time.time()
        return end - self.started_at

    @property
    def success(self) -> bool:
        return self.status == JobStatus.SUCCESS


@dataclass
class JobReport:
    """전체 작업 실행 결과 보고서."""
    jobs: List[Job]

    @property
    def total(self) -> int:
        return len(self.jobs)

    @property
    def succeeded(self) -> int:
        return sum(1 for j in self.jobs if j.success)

    @property
    def failed(self) -> int:
        return sum(1 for j in self.jobs if j.status == JobStatus.FAILED)

    @property
    def all_success(self) -> bool:
        return self.failed == 0

    def summary(self) -> str:
        lines = [f"Jobs: {self.succeeded}/{self.total} succeeded"]
        for j in self.jobs:
            icon = "OK" if j.success else "FAIL"
            lines.append(
                f"  [{icon}] [{j.name}] status={j.status.name} "
                f"attempts={j.attempt} elapsed={j.elapsed:.1f}s"
                + (f" error={j.error}" if j.error else "")
            )
        return "\n".join(lines)


class JobManager:
    """
    작업 큐 관리자.

    사용 예:
        manager = JobManager(max_workers=2)
        manager.add("TTS", tts_fn, args=(story,))
        manager.add("Image", img_fn, args=(story,), max_retries=3)
        report = manager.run_sequential()   # 또는 run_parallel()
    """

    def __init__(
        self,
        max_workers: int = 2,
        on_job_start: Optional[Callable[[Job], None]] = None,
        on_job_done: Optional[Callable[[Job], None]] = None,
    ):
        self._max_workers = max_workers
        self._jobs: List[Job] = []
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._on_start = on_job_start
        self._on_done = on_job_done

    # ─────────────────────────────────────────
    # 작업 등록
    # ─────────────────────────────────────────

    def add(
        self,
        name: str,
        fn: Callable,
        args: tuple = (),
        kwargs: Optional[dict] = None,
        max_retries: int = 3,
        retry_delay: float = 2.0,
    ) -> str:
        """작업을 큐에 추가하고 job_id를 반환합니다."""
        job = Job(
            job_id=str(uuid4()),
            name=name,
            fn=fn,
            args=args,
            kwargs=kwargs or {},
            max_retries=max_retries,
            retry_delay=retry_delay,
        )
        with self._lock:
            self._jobs.append(job)
        logger.debug("Job added: [%s] %s", job.job_id[:8], name)
        return job.job_id

    def clear(self) -> None:
        with self._lock:
            self._jobs.clear()
        self._cancel_event.clear()

    def cancel(self) -> None:
        self._cancel_event.set()

    # ─────────────────────────────────────────
    # 실행
    # ─────────────────────────────────────────

    def run_sequential(self) -> JobReport:
        """등록된 작업을 순서대로 실행합니다."""
        self._cancel_event.clear()
        for job in self._jobs:
            if self._cancel_event.is_set():
                job.status = JobStatus.CANCELLED
                continue
            self._execute(job)
        report = JobReport(list(self._jobs))
        logger.info("Sequential run done:\n%s", report.summary())
        return report

    def run_parallel(self) -> JobReport:
        """등록된 작업을 병렬로 실행합니다."""
        self._cancel_event.clear()
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures: Dict[Future, Job] = {}
            for job in self._jobs:
                if self._cancel_event.is_set():
                    job.status = JobStatus.CANCELLED
                    continue
                future = pool.submit(self._execute, job)
                futures[future] = job

            for future in futures:
                try:
                    future.result()
                except Exception as e:
                    logger.error("Unexpected future error: %s", e)

        report = JobReport(list(self._jobs))
        logger.info("Parallel run done:\n%s", report.summary())
        return report

    def retry_failed(self) -> JobReport:
        """실패한 작업만 재시도합니다."""
        failed = [j for j in self._jobs if j.status == JobStatus.FAILED]
        if not failed:
            logger.info("No failed jobs to retry.")
            return JobReport(list(self._jobs))

        logger.info("Retrying %d failed jobs...", len(failed))
        self._cancel_event.clear()
        for job in failed:
            job.attempt = 0
            job.status = JobStatus.PENDING
            job.error = None
            self._execute(job)

        report = JobReport(list(self._jobs))
        logger.info("Retry run done:\n%s", report.summary())
        return report

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._lock:
            for j in self._jobs:
                if j.job_id == job_id:
                    return j
        return None

    def get_report(self) -> JobReport:
        return JobReport(list(self._jobs))

    # ─────────────────────────────────────────
    # 내부 실행 로직
    # ─────────────────────────────────────────

    def _execute(self, job: Job) -> None:
        """재시도 포함 단일 작업 실행."""
        job.started_at = time.time()

        if self._on_start:
            try:
                self._on_start(job)
            except Exception:
                pass

        for attempt in range(1, job.max_retries + 1):
            if self._cancel_event.is_set():
                job.status = JobStatus.CANCELLED
                break

            job.attempt = attempt
            job.status = JobStatus.RUNNING

            try:
                logger.info(
                    "Running job [%s] attempt %d/%d",
                    job.name, attempt, job.max_retries,
                )
                result = job.fn(*job.args, **job.kwargs)
                job.result = result
                job.status = JobStatus.SUCCESS
                job.error = None
                logger.info("Job [%s] succeeded (%.1fs)", job.name, job.elapsed)
                break

            except Exception as e:
                job.error = f"{type(e).__name__}: {e}"
                tb = traceback.format_exc()
                logger.warning(
                    "Job [%s] failed attempt %d/%d: %s",
                    job.name, attempt, job.max_retries, job.error,
                )
                logger.debug("Traceback:\n%s", tb)

                if attempt < job.max_retries:
                    job.status = JobStatus.RETRYING
                    sleep = job.retry_delay * attempt
                    logger.info("Waiting %.1fs before retry...", sleep)
                    time.sleep(sleep)
                else:
                    job.status = JobStatus.FAILED
                    logger.error(
                        "Job [%s] permanently failed after %d attempts.",
                        job.name, job.max_retries,
                    )

        job.finished_at = time.time()

        if self._on_done:
            try:
                self._on_done(job)
            except Exception:
                pass
