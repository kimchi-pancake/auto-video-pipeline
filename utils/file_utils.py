"""
utils/file_utils.py
===================
파일/디렉터리 관련 유틸리티 함수 모음.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Generator, List, Optional

from utils.logger import get_logger

logger = get_logger(__name__)


# ─────────────────────────────────────────────
# 디렉터리 관리
# ─────────────────────────────────────────────

def ensure_dirs(*paths: str | Path) -> None:
    """존재하지 않는 디렉터리를 모두 생성합니다."""
    for p in paths:
        Path(p).mkdir(parents=True, exist_ok=True)


def get_project_root() -> Path:
    """프로젝트 루트 경로를 반환합니다."""
    return Path(__file__).parent.parent


def resolve_path(rel_or_abs: str | Path) -> Path:
    """상대 경로라면 프로젝트 루트 기준으로 절대 경로로 변환합니다."""
    p = Path(rel_or_abs)
    if p.is_absolute():
        return p
    return get_project_root() / p


# ─────────────────────────────────────────────
# 파일 복사 / 이동 / 삭제
# ─────────────────────────────────────────────

def safe_copy(src: str | Path, dst: str | Path, overwrite: bool = True) -> Path:
    """
    파일을 안전하게 복사합니다.
    dst가 디렉터리면 파일 이름 유지. overwrite=False 시 덮어쓰기 방지.
    """
    src, dst = Path(src), Path(dst)
    if dst.is_dir():
        dst = dst / src.name
    if dst.exists() and not overwrite:
        logger.debug("Skip copy (already exists): %s", dst)
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    logger.debug("Copied: %s → %s", src, dst)
    return dst


def safe_move(src: str | Path, dst: str | Path) -> Path:
    """파일을 안전하게 이동합니다."""
    src, dst = Path(src), Path(dst)
    if dst.is_dir():
        dst = dst / src.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    logger.debug("Moved: %s → %s", src, dst)
    return dst


def safe_delete(path: str | Path, missing_ok: bool = True) -> bool:
    """파일 또는 디렉터리를 안전하게 삭제합니다."""
    p = Path(path)
    try:
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
        elif not missing_ok:
            raise FileNotFoundError(p)
        return True
    except Exception as e:
        logger.error("Delete failed %s: %s", path, e)
        return False


# ─────────────────────────────────────────────
# 파일 정보
# ─────────────────────────────────────────────

def file_md5(path: str | Path, chunk_size: int = 65536) -> str:
    """파일의 MD5 해시를 반환합니다."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def file_size_mb(path: str | Path) -> float:
    """파일 크기를 MB 단위로 반환합니다."""
    return Path(path).stat().st_size / (1024 * 1024)


# ─────────────────────────────────────────────
# 임시 파일 / 아카이브 정리
# ─────────────────────────────────────────────

def cleanup_temp_dir(temp_dir: str | Path, keep_extensions: Optional[List[str]] = None) -> int:
    """
    temp 디렉터리를 비웁니다.
    keep_extensions 가 지정되면 해당 확장자 파일은 유지합니다.
    반환값: 삭제된 파일 수
    """
    temp_dir = Path(temp_dir)
    if not temp_dir.exists():
        return 0

    count = 0
    for item in temp_dir.rglob("*"):
        if not item.is_file():
            continue
        if keep_extensions and item.suffix.lower() in keep_extensions:
            continue
        try:
            item.unlink()
            count += 1
        except Exception as e:
            logger.warning("Could not delete temp file %s: %s", item, e)

    logger.info("Cleaned up %d temp files from %s", count, temp_dir)
    return count


def cleanup_old_files(directory: str | Path, days: int = 7) -> int:
    """
    지정된 일 수보다 오래된 파일을 삭제합니다.
    반환값: 삭제된 파일 수
    """
    directory = Path(directory)
    if not directory.exists():
        return 0

    cutoff = time.time() - days * 86400
    count = 0
    for item in directory.rglob("*"):
        if item.is_file() and item.stat().st_mtime < cutoff:
            try:
                item.unlink()
                count += 1
            except Exception as e:
                logger.warning("Could not delete old file %s: %s", item, e)

    logger.info("Removed %d files older than %d days from %s", count, days, directory)
    return count


def archive_output(
    src_dir: str | Path,
    archive_dir: str | Path,
    prefix: str = "",
) -> Path:
    """
    output 폴더를 타임스탬프 디렉터리로 아카이브합니다.
    반환값: 아카이브된 디렉터리 경로
    """
    src_dir = Path(src_dir)
    archive_dir = Path(archive_dir)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder_name = f"{prefix}_{timestamp}" if prefix else timestamp
    dst = archive_dir / folder_name
    dst.mkdir(parents=True, exist_ok=True)

    for item in src_dir.iterdir():
        if item.is_file():
            safe_copy(item, dst / item.name)

    logger.info("Archived output → %s", dst)
    return dst


# ─────────────────────────────────────────────
# 유니크 파일명 생성
# ─────────────────────────────────────────────

def unique_path(path: str | Path) -> Path:
    """이미 존재하는 파일이면 (1), (2) ... 를 붙여 유니크하게 만듭니다."""
    p = Path(path)
    if not p.exists():
        return p
    stem = p.stem
    suffix = p.suffix
    parent = p.parent
    counter = 1
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def sanitize_filename(name: str, max_length: int = 200) -> str:
    """
    윈도우/리눅스에서 사용할 수 없는 문자를 제거하여 안전한 파일명을 만듭니다.

    작은따옴표(')와 스마트 따옴표(‘’“”)도 제거합니다 — 파일시스템 자체에는
    합법인 문자라 예전엔 안 걸렀는데, 이 이름이 그대로 run 디렉터리명이 되고
    그 경로가 ffmpeg concat 리스트 파일(`file '경로'` 형식)에 그대로 들어가서,
    제목에 따옴표가 섞이면(2026-08-02: "척추 전문의들이 경고하는 '그 앉기'...")
    concat 파싱이 그 따옴표에서 끊겨 렌더링이 통째로 실패하는 사고가 있었습니다.
    """
    # 사용 불가 문자 + ffmpeg concat 리스트 파일을 깨뜨리는 따옴표류 제거
    illegal = r'\/:*?"<>|' + "'‘’“”"
    for ch in illegal:
        name = name.replace(ch, "_")
    name = name.strip(". ")
    return name[:max_length] or "untitled"


# ─────────────────────────────────────────────
# 파일 탐색
# ─────────────────────────────────────────────

def iter_files(
    directory: str | Path,
    extensions: Optional[List[str]] = None,
    recursive: bool = False,
) -> Generator[Path, None, None]:
    """
    디렉터리 내 파일을 순회합니다.
    extensions: ['.mp3', '.wav'] 처럼 소문자 확장자 리스트
    """
    directory = Path(directory)
    if not directory.exists():
        return

    pattern = "**/*" if recursive else "*"
    for item in directory.glob(pattern):
        if not item.is_file():
            continue
        if extensions is None or item.suffix.lower() in extensions:
            yield item
