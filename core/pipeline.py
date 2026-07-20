"""
core/pipeline.py
================
전체 파이프라인 오케스트레이터.
story.txt → TTS → 이미지 → 자막 → 영상 → 썸네일 → YouTube 업로드.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from image.image_generator import ImageGenerator, ImageResult
from image.thumbnail_generator import ThumbnailGenerator
from parser.story_parser import StoryParser, StoryData
from subtitle.subtitle_builder import SubtitleBuilder
from tts.tts_builder import TTSBuilder, SceneAudio
from utils.config_manager import ConfigManager
from utils.file_utils import archive_output, cleanup_old_files, cleanup_temp_dir, sanitize_filename
from utils.logger import get_logger
from core.job_manager import JobManager, JobStatus
from video.ffmpeg_wrapper import FFmpegWrapper
from video.video_composer import VideoComposer
from youtube.youtube_uploader import YouTubeUploader

logger = get_logger(__name__)


# ─────────────────────────────────────────────
# 진행 이벤트 / 결과
# ─────────────────────────────────────────────

STAGES = [
    "파싱",           # 0
    "TTS 생성",       # 1
    "이미지 생성",    # 2
    "자막 생성",      # 3
    "영상 합성",      # 4
    "썸네일 생성",    # 5
    "YouTube 업로드", # 6
    "정리",           # 7
]


@dataclass
class PipelineProgress:
    stage: str
    stage_index: int
    stage_total: int
    step: int
    step_total: int
    message: str

    @property
    def overall_pct(self) -> float:
        stage_pct = self.stage_index / max(self.stage_total, 1)
        inner_pct = (self.step / max(self.step_total, 1)) / self.stage_total
        return min((stage_pct + inner_pct) * 100, 100.0)


@dataclass
class PipelineResult:
    success: bool
    video_path: Optional[str] = None
    shorts_video_path: Optional[str] = None
    thumbnail_long_path: Optional[str] = None
    thumbnail_shorts_path: Optional[str] = None
    youtube_video_id: Optional[str] = None
    youtube_error: Optional[str] = None
    youtube_shorts_video_id: Optional[str] = None
    youtube_shorts_error: Optional[str] = None
    elapsed: float = 0.0
    error: Optional[str] = None
    stage_times: dict = field(default_factory=dict)
    category: Optional[str] = None
    title: Optional[str] = None


# ─────────────────────────────────────────────
# 파이프라인
# ─────────────────────────────────────────────

class _StopRequested(Exception):
    pass


class Pipeline:
    def __init__(self, config: ConfigManager):
        self._cfg = config
        self._progress_cb: Optional[Callable[[PipelineProgress], None]] = None
        self._stop_requested = False

        root = Path(__file__).parent.parent
        self._temp_dir    = root / self._cfg.get("paths.temp_dir",    "temp")
        self._output_dir  = root / self._cfg.get("paths.output_dir",  "output")
        self._archive_dir = root / self._cfg.get("paths.archive_dir", "archive")
        self._assets_dir  = root / self._cfg.get("paths.assets_dir",  "assets")
        self._bgm_dir     = root / self._cfg.get("paths.bgm_dir",     "assets/bgm")
        self._photo_dir   = root / self._cfg.get("paths.photo_dir",   "assets/photos")

        for d in [self._temp_dir, self._output_dir, self._archive_dir]:
            d.mkdir(parents=True, exist_ok=True)

    def set_progress_callback(self, cb: Callable[[PipelineProgress], None]) -> None:
        self._progress_cb = cb

    def request_stop(self) -> None:
        self._stop_requested = True
        logger.info("Stop requested.")

    # ─────────────────────────────────────────
    # 메인 실행
    # ─────────────────────────────────────────

    def run(
        self,
        story_path: str | Path,
        upload_to_youtube: bool = True,
        youtube_privacy: str = "private",
        schedule_days_ahead: int = 0,
        schedule_today: bool = False,
        youtube_credentials_file: Optional[str] = None,
        also_make_shorts: bool = False,
    ) -> PipelineResult:
        self._stop_requested = False
        start_all = time.time()
        stage_times: dict = {}
        temp: Optional[Path] = None

        try:
            # ── 0. 파싱 (재시도 불필요) ──────
            t = time.time()
            self._emit(0, 0, 1, "story.txt 파싱 중...")
            story = self._parse(story_path)
            stage_times["parsing"] = time.time() - t
            self._check_stop()

            # story.txt에 RESOLUTION: 이 있으면 GUI에서 고른 해상도보다 우선합니다.
            # (롱폼/쇼츠 대본을 한 프롬프트로 같이 받아서 큐에 넣었을 때, 매번
            # 해상도 드롭다운을 수동으로 안 바꿔도 각자 맞는 해상도로 만들어짐)
            if story.resolution:
                sw, sh = story.resolution
                self._cfg.set("video.width", sw)
                self._cfg.set("video.height", sh)
                self._cfg.set("image.default_width", sw)
                self._cfg.set("image.default_height", sh)
                logger.info("story.txt RESOLUTION 지정값 사용: %dx%d", sw, sh)

            # 본편 해상도가 이미 세로(쇼츠 모양)면 "쇼츠도 함께"를 또 적용해도
            # 내용이 완전히 같은 세로 영상을 하나 더 만드는 것뿐이라 의미가
            # 없습니다. (롱폼+쇼츠 콤보 프롬프트로 받은 _shorts.txt 를, "쇼츠도
            # 함께" 체크박스가 켜진 채로 자동반복 큐에서 처리하면 본편 자체도
            # 세로로 나오는데 거기에 중복 쇼츠까지 또 생겨서 "쇼츠만 두 개
            # 만들어지는" 원인이 됐습니다.)
            main_w = self._cfg.get("video.width")
            main_h = self._cfg.get("video.height")
            main_is_shorts = main_h > main_w
            if also_make_shorts and main_is_shorts:
                logger.info(
                    "본편이 이미 세로 해상도(%dx%d)라 쇼츠 중복 생성을 건너뜁니다.",
                    main_w, main_h,
                )
                also_make_shorts = False

            # 런 디렉터리 생성
            safe_title = sanitize_filename(story.raw_title or "video", 50)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            run_name = f"{ts}_{safe_title}"

            run_out = self._output_dir / run_name
            run_out.mkdir(parents=True, exist_ok=True)

            temp = self._temp_dir / run_name
            for sub in ("tts", "images", "video"):
                (temp / sub).mkdir(parents=True, exist_ok=True)

            # ── JobManager: 단계 1~5 등록 ────
            max_retries = self._cfg.get("automation.max_retry_count", 3)
            jm = JobManager(
                max_workers=1,   # 순차 실행 (각 단계가 다음 단계 결과에 의존)
                on_job_start=lambda j: logger.info("▶ Job 시작: %s", j.name),
                on_job_done=lambda j: logger.info(
                    "[%s] Job 완료: %s (%.1fs)",
                    "OK" if j.success else "FAIL", j.name, j.elapsed,
                ),
            )

            # 각 단계를 클로저로 감싸서 등록
            # 결과를 단계 간에 전달하기 위해 dict 사용
            ctx: dict = {}

            def stage_tts():
                self._emit(1, 0, len(story.scenes), "TTS 음성 생성 중...")
                t0 = time.time()
                ctx["scene_audios"] = self._run_tts(story, temp / "tts")
                self._fix_durations(ctx["scene_audios"])
                stage_times["tts"] = time.time() - t0

            def stage_images():
                self._emit(2, 0, len(story.scenes), "이미지 생성 중...")
                t0 = time.time()
                ctx["image_results"] = self._run_images(story, temp / "images")
                stage_times["images"] = time.time() - t0

            def stage_subtitles():
                self._emit(3, 0, 1, "SRT / ASS 자막 생성 중...")
                t0 = time.time()
                ctx["srt_path"], ctx["ass_path"] = self._run_subtitles(
                    ctx["scene_audios"], temp / "video"
                )
                stage_times["subtitles"] = time.time() - t0

            def stage_video():
                self._emit(4, 0, 6, "영상 합성 중...")
                t0 = time.time()
                video_path = run_out / f"{safe_title}.mp4"
                ctx["video_path"] = self._run_video(
                    ctx["scene_audios"], ctx["image_results"],
                    ctx["ass_path"], self._resolve_story_bgm(story),
                    video_path, temp / "video",
                )
                stage_times["video"] = time.time() - t0

            def stage_shorts_video():
                self._emit(4, 0, 6, "쇼츠 영상 합성 중...")
                t0 = time.time()
                shorts_path = run_out / f"{safe_title}_shorts.mp4"
                # 이미 만든 TTS/이미지를 그대로 재사용하고 세로 해상도로만 다시
                # 합성합니다 (TTS·이미지 생성을 다시 돌리지 않아 빠름). 단, 씬을
                # 통째로 다 재사용하면 롱폼 대본 그대로 세로로만 바뀐 8분짜리
                # "쇼츠"가 나와버려서(진짜 쇼츠가 아님), 앞에서부터 누적 길이가
                # shorts_max_duration을 넘기기 전까지만 잘라서 씁니다 — 롱폼의
                # 도입부(훅)만 세로 쇼츠로 재활용하는 셈입니다.
                max_dur = self._cfg.get("video.shorts_max_duration", 60)
                shorts_audios, shorts_images = self._select_shorts_scenes(
                    ctx["scene_audios"], ctx["image_results"], max_dur
                )
                shorts_cfg = self._cfg.section("video")
                shorts_cfg["width"] = self._cfg.get("video.shorts_width", 1080)
                shorts_cfg["height"] = self._cfg.get("video.shorts_height", 1920)
                ctx["shorts_video_path"] = self._run_video(
                    shorts_audios, shorts_images,
                    ctx["ass_path"], self._resolve_story_bgm(story),
                    shorts_path, temp / "video_shorts",
                    video_cfg_override=shorts_cfg,
                )
                stage_times["shorts_video"] = time.time() - t0

            def stage_thumbnails():
                self._emit(5, 0, 2, "썸네일 생성 중...")
                t0 = time.time()
                ctx["thumb_long"], ctx["thumb_shorts"] = self._run_thumbnails(
                    story, run_out, ctx.get("image_results")
                )
                stage_times["thumbnails"] = time.time() - t0
                # 자막 파일 복사
                shutil.copy2(ctx["srt_path"], run_out / "subtitles.srt")
                shutil.copy2(ctx["ass_path"], run_out / "subtitles.ass")

            jm.add("TTS 생성",    stage_tts,        max_retries=max_retries, retry_delay=3.0)
            jm.add("이미지 생성", stage_images,     max_retries=max_retries, retry_delay=5.0)
            jm.add("자막 생성",   stage_subtitles,  max_retries=max_retries, retry_delay=2.0)
            jm.add("영상 합성",   stage_video,      max_retries=max_retries, retry_delay=5.0)
            if also_make_shorts:
                jm.add("쇼츠 영상 합성", stage_shorts_video, max_retries=max_retries, retry_delay=5.0)
            jm.add("썸네일 생성", stage_thumbnails, max_retries=2,           retry_delay=2.0)

            # 순차 실행 (중지 요청 반영)
            for job in jm._jobs:
                if self._stop_requested:
                    raise _StopRequested()
                jm._execute(job)
                if job.status == JobStatus.FAILED:
                    raise RuntimeError(f"단계 [{job.name}] 실패: {job.error}")

            # ── 6. YouTube 업로드 (실패해도 완성된 영상 자체는 성공으로 처리) ──
            youtube_id = None
            youtube_error = None
            youtube_shorts_id = None
            youtube_shorts_error = None
            if upload_to_youtube:
                t = time.time()
                self._emit(6, 0, 1, "YouTube 업로드 중...")
                # 본편 자체가 세로(쇼츠 모양)면 가로 썸네일(thumb_long)을 붙이면
                # 실제 영상과 썸네일 비율이 안 맞고, "#Shorts" 처리도 안 됩니다.
                main_thumb = ctx.get("thumb_shorts") if main_is_shorts else ctx.get("thumb_long")
                try:
                    youtube_id = self._run_youtube(
                        story=story,
                        video_path=str(ctx["video_path"]),
                        thumbnail_path=str(main_thumb) if main_thumb else None,
                        privacy=youtube_privacy,
                        schedule_days_ahead=schedule_days_ahead,
                        schedule_today=schedule_today,
                        credentials_file=youtube_credentials_file,
                        is_shorts=main_is_shorts,
                    )
                except Exception as e:
                    logger.exception("YouTube upload failed (video was produced successfully): %s", e)
                    youtube_error = str(e)
                stage_times["youtube"] = time.time() - t

                if ctx.get("shorts_video_path"):
                    t = time.time()
                    self._emit(6, 0, 1, "YouTube 쇼츠 업로드 중...")
                    try:
                        youtube_shorts_id = self._run_youtube(
                            story=story,
                            video_path=str(ctx["shorts_video_path"]),
                            thumbnail_path=str(ctx["thumb_shorts"]) if ctx.get("thumb_shorts") else None,
                            privacy=youtube_privacy,
                            schedule_days_ahead=schedule_days_ahead,
                            schedule_today=schedule_today,
                            credentials_file=youtube_credentials_file,
                            is_shorts=True,
                        )
                    except Exception as e:
                        logger.exception("YouTube shorts upload failed (shorts video was produced successfully): %s", e)
                        youtube_shorts_error = str(e)
                    stage_times["youtube_shorts"] = time.time() - t

            # ── 7. 정리 ──────────────────────
            self._emit(7, 0, 1, "임시 파일 정리 중...")
            if self._cfg.get("automation.auto_cleanup_temp", True):
                cleanup_temp_dir(temp)
            if self._cfg.get("automation.auto_archive", True):
                archive_output(run_out, self._archive_dir, prefix=safe_title)
                cleanup_days = self._cfg.get("automation.cleanup_days", 7)
                if cleanup_days and cleanup_days > 0:
                    try:
                        removed = cleanup_old_files(self._archive_dir, days=cleanup_days)
                        if removed:
                            logger.info("Archive cleanup: removed %d old file(s)", removed)
                    except Exception:
                        logger.warning("Archive cleanup failed", exc_info=True)

            elapsed = time.time() - start_all
            logger.info("Pipeline finished in %.1fs", elapsed)

            return PipelineResult(
                success=True,
                video_path=str(ctx["video_path"]),
                shorts_video_path=str(ctx["shorts_video_path"]) if ctx.get("shorts_video_path") else None,
                thumbnail_long_path=str(ctx["thumb_long"]) if ctx.get("thumb_long") else None,
                thumbnail_shorts_path=str(ctx["thumb_shorts"]) if ctx.get("thumb_shorts") else None,
                youtube_video_id=youtube_id,
                youtube_error=youtube_error,
                youtube_shorts_video_id=youtube_shorts_id,
                youtube_shorts_error=youtube_shorts_error,
                elapsed=elapsed,
                stage_times=stage_times,
                category=story.category,
                title=story.raw_title,
            )

        except _StopRequested:
            logger.info("Pipeline stopped by user.")
            self._cleanup_failed_run(temp)
            return PipelineResult(success=False, error="사용자가 중지했습니다.")
        except Exception as e:
            logger.exception("Pipeline failed: %s", e)
            self._cleanup_failed_run(temp)
            return PipelineResult(success=False, error=str(e))

    def _cleanup_failed_run(self, temp: Optional[Path]) -> None:
        """실패/중지된 실행의 임시 디렉터리를 정리합니다 (실패해도 무시)."""
        if temp is None or not self._cfg.get("automation.auto_cleanup_temp", True):
            return
        try:
            cleanup_temp_dir(temp)
        except Exception:
            logger.warning("Failed-run temp cleanup failed for %s", temp, exc_info=True)

    # ─────────────────────────────────────────
    # 단계별 구현
    # ─────────────────────────────────────────

    def _parse(self, story_path: str | Path) -> StoryData:
        return StoryParser(story_path).parse()

    def _run_tts(self, story: StoryData, temp: Path) -> List[SceneAudio]:
        def cb(done, total, rid):
            self._emit(1, done, total, f"TTS: {rid}")

        builder = TTSBuilder(
            tts_config=self._cfg.section("tts"),
            temp_dir=temp,
            progress_callback=cb,
        )
        return builder.build(story)

    def _run_images(self, story: StoryData, temp: Path) -> List[ImageResult]:
        def cb(done, total, idx):
            self._emit(2, done, total, f"이미지 {done}/{total}")

        gen = ImageGenerator(
            config=self._cfg.section("image"),
            temp_dir=temp,
            progress_callback=cb,
        )
        return gen.generate_all(
            scenes=story.scenes,
            photo_files=story.photo_files if story.photo_files else None,
            photo_base_dir=self._photo_dir,
        )

    def _run_subtitles(
        self, scene_audios: List[SceneAudio], temp: Path
    ):
        builder = SubtitleBuilder(
            config=self._cfg.section("subtitle"),
            output_dir=temp,
            min_scene_duration=self._cfg.get("video.min_scene_duration", 3.0),
        )
        return builder.build(scene_audios)

    def _resolve_story_bgm(self, story: StoryData) -> List[str]:
        """BGM: 에 파일을 딱 하나만 적었으면 그대로 쓰고, 여러 개(후보 목록)를 적었거나
        아예 비어 있으면 제목/대사 분위기를 분석해 그 중(또는 config 전체 목록 중)
        가장 잘 맞는 걸 자동으로 고릅니다."""
        if len(story.bgm_files) == 1:
            return story.bgm_files
        if not self._cfg.get("bgm.auto_select", True):
            return story.bgm_files

        from utils.bgm_selector import select_bgm
        moods = self._cfg.get("bgm.moods", {}) or {}
        default_bgm = self._cfg.get("bgm.default", "")
        chosen = select_bgm(story, moods, default_bgm, candidates=story.bgm_files or None)
        if chosen:
            logger.info("BGM 자동 선택 (분위기 분석): %s", chosen)
            return [chosen]
        return []

    def _select_shorts_scenes(
        self,
        scene_audios: List[SceneAudio],
        image_results: List[ImageResult],
        max_duration: float,
    ) -> tuple[List[SceneAudio], List[ImageResult]]:
        """롱폼 씬을 재사용해 쇼츠를 만들 때, 앞에서부터 누적 낭독 시간이
        max_duration을 넘기기 전까지의 씬만 골라 반환합니다 (최소 1개는
        보장). ass_path(자막)는 그대로 재사용해도 됩니다 — 잘려나간 뒷부분
        타임스탬프의 자막 큐는 영상 자체가 그 지점 전에 끝나버리므로 자연히
        표시되지 않습니다."""
        img_map = {r.scene_index: r for r in image_results}
        selected_audios: List[SceneAudio] = []
        selected_images: List[ImageResult] = []
        cumulative = 0.0
        for sa in scene_audios:
            if selected_audios and cumulative >= max_duration:
                break
            selected_audios.append(sa)
            if sa.scene_idx in img_map:
                selected_images.append(img_map[sa.scene_idx])
            cumulative += sa.total_duration
        return selected_audios, selected_images

    def _run_video(
        self,
        scene_audios: List[SceneAudio],
        image_results: List[ImageResult],
        ass_path: Path,
        bgm_files: List[str],
        video_path: Path,
        temp: Path,
        video_cfg_override: Optional[dict] = None,
    ) -> Path:
        def cb(done, total, name):
            self._emit(4, done, total, f"씬 {done}/{total}")

        composer = VideoComposer(
            config=video_cfg_override or self._cfg.section("video"),
            temp_dir=temp,
            progress_callback=cb,
            fonts_dir=self._assets_dir / "fonts",
        )
        return composer.compose(
            scene_audios=scene_audios,
            image_results=image_results,
            ass_path=ass_path,
            bgm_files=bgm_files,
            output_path=video_path,
            bgm_base_dir=self._bgm_dir,
        )

    def _run_thumbnails(
        self, story: StoryData, out_dir: Path, image_results: Optional[List[ImageResult]] = None
    ):
        gen = ThumbnailGenerator(
            config=self._cfg.section("thumbnail"),
            assets_dir=self._assets_dir,
        )
        # 첫 번째로 실제 다운로드된 장면 이미지를 썸네일 배경으로 재활용합니다
        # (영상 도입부와 톤이 맞아서 검정 배경보다 시선을 더 끕니다).
        bg_image_path = None
        if image_results:
            for r in sorted(image_results, key=lambda r: r.scene_index):
                if r.image_path:
                    bg_image_path = r.image_path
                    break

        long_path = None
        shorts_path = None
        if story.thumbnail_long:
            self._emit(5, 0, 2, "썸네일(Long 1280×720) 생성 중...")
            long_path = gen.generate_long(story.thumbnail_long, out_dir, bg_image_path)
        if story.thumbnail_shorts:
            self._emit(5, 1, 2, "썸네일(Shorts 1080×1920) 생성 중...")
            shorts_path = gen.generate_shorts(story.thumbnail_shorts, out_dir, bg_image_path)
        return long_path, shorts_path

    def _run_youtube(
        self,
        story: StoryData,
        video_path: str,
        thumbnail_path: Optional[str],
        privacy: str,
        schedule_days_ahead: int,
        schedule_today: bool = False,
        credentials_file: Optional[str] = None,
        is_shorts: bool = False,
    ) -> Optional[str]:
        yt_cfg = self._cfg.section("youtube")
        if credentials_file:
            yt_cfg["credentials_file"] = credentials_file
        uploader = YouTubeUploader(yt_cfg)
        if not uploader.authenticate():
            logger.error("YouTube authentication failed. Skipping upload.")
            return None

        scheduled_at = None
        if schedule_today:
            # config의 schedule_hour:schedule_minute(오늘)에 유튜브가 자체적으로
            # 공개 전환하도록 예약합니다 — 생성이 그 시각보다 일찍 끝나든
            # 늦게 끝나든, 실제 공개는 항상 그 정각에 맞춰집니다. 이미 그
            # 시각이 지났으면(생성이 예상보다 오래 걸린 경우) 다음날 같은
            # 시각으로 밀립니다(즉시 공개 대신 안전하게 하루 미룸).
            scheduled_at = uploader.make_schedule_datetime(days_ahead=0)
            logger.info("Scheduled publish at (today): %s", scheduled_at.isoformat())
        elif schedule_days_ahead > 0:
            scheduled_at = uploader.make_schedule_datetime(days_ahead=schedule_days_ahead)
            logger.info("Scheduled publish at: %s", scheduled_at.isoformat())

        base_title = story.raw_title or "자동 생성 영상"
        if is_shorts:
            # 유튜브는 세로 영상 + "#Shorts" 태그/제목으로 쇼츠 여부를 판별합니다.
            title = f"{base_title} #Shorts"
            tags = list(self._cfg.get("youtube.shorts_tags", [])) or list(self._cfg.get("youtube.default_tags", []))
        else:
            title = base_title
            tags = list(self._cfg.get("youtube.default_tags", []))

        description = "\n".join([
            base_title,
            "",
            "이 영상은 Auto Video Pipeline으로 자동 생성되었습니다.",
        ])

        return uploader.upload(
            video_path=video_path,
            title=title,
            description=description,
            tags=tags,
            privacy=privacy,
            thumbnail_path=thumbnail_path,
            scheduled_at=scheduled_at,
        )

    def _fix_durations(self, scene_audios: List[SceneAudio]) -> None:
        """FFprobe 로 실제 오디오 길이를 측정하여 세그먼트 duration 을 보정합니다."""
        ff = FFmpegWrapper(
            ffmpeg=self._cfg.get("video.ffmpeg_path", "ffmpeg"),
            ffprobe=self._cfg.get("video.ffprobe_path", "ffprobe"),
        )
        for sa in scene_audios:
            for seg in sa.segments:
                if seg.success and seg.audio_path and Path(seg.audio_path).exists():
                    real = ff.get_audio_duration(seg.audio_path)
                    if real > 0:
                        seg.duration = real

    # ─────────────────────────────────────────
    # 헬퍼
    # ─────────────────────────────────────────

    def _emit(self, stage_idx: int, step: int, step_total: int, msg: str) -> None:
        if self._progress_cb:
            p = PipelineProgress(
                stage=STAGES[stage_idx],
                stage_index=stage_idx,
                stage_total=len(STAGES),
                step=step,
                step_total=max(step_total, 1),
                message=msg,
            )
            try:
                self._progress_cb(p)
            except Exception:
                pass
        logger.info("[Stage %d/%d] %s — %s", stage_idx + 1, len(STAGES), STAGES[stage_idx], msg)

    def _check_stop(self) -> None:
        if self._stop_requested:
            raise _StopRequested()
