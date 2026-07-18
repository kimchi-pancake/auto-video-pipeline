"""
video/video_composer.py
=======================
씬별 이미지 + TTS 오디오 + BGM → 최종 MP4 합성기.
SceneTimer 기반 정확한 타이밍으로 클립을 구성합니다.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from image.image_generator import ImageResult
from tts.tts_builder import SceneAudio
from video.ffmpeg_wrapper import FFmpegWrapper
from video.scene_timer import SceneTimer
from utils.logger import get_logger

logger = get_logger(__name__)


class VideoComposer:
    """
    사용 예:
        composer = VideoComposer(config["video"], temp_dir)
        out = composer.compose(scene_audios, image_results, ass_path, bgm_files, output)
    """

    def __init__(
        self,
        config: dict,
        temp_dir: str | Path,
        progress_callback: Optional[Callable] = None,
        fonts_dir: Optional[str | Path] = None,
    ):
        self._cfg = config
        self._temp = Path(temp_dir)
        self._temp.mkdir(parents=True, exist_ok=True)
        self._cb = progress_callback
        self._fonts_dir = fonts_dir

        self._ff = FFmpegWrapper(
            ffmpeg=config.get("ffmpeg_path", "ffmpeg"),
            ffprobe=config.get("ffprobe_path", "ffprobe"),
        )

        self._w           = config.get("width",           1920)
        self._h           = config.get("height",          1080)
        self._fps         = config.get("fps",             30)
        self._codec       = config.get("video_codec",     "libx264")
        self._preset      = config.get("preset",          "medium")
        self._crf         = config.get("crf",             18)
        self._v_bitrate   = config.get("video_bitrate",   "8000k")
        self._a_bitrate   = config.get("audio_bitrate",   "192k")
        self._gpu         = config.get("gpu_encoding",    False)
        self._gpu_codec   = config.get("gpu_codec",       "h264_nvenc")
        self._threads     = config.get("threads",         4)
        self._bgm_vol     = config.get("bgm_volume",      0.15)
        self._tts_vol     = config.get("tts_volume",      1.0)
        self._crossfade   = config.get("crossfade_duration", 0.8)
        self._zoom        = config.get("pan_zoom_enabled", True)
        self._zoom_scale  = config.get("pan_zoom_scale",  1.05)
        self._min_dur     = config.get("min_scene_duration", 3.0)
        self._outro_hold  = config.get("outro_hold", 0.5)

        # inter_scene_gap=0: 실제 오디오(concat_audio)/영상(crossfade) 합성
        # 어디에도 씬 사이 무음 간격을 실제로 끼워 넣지 않습니다. 0이 아닌
        # 값을 쓰면 자막 타이밍만 그 간격만큼 밀려서 계산되고 실제 렌더링에는
        # 반영이 안 돼, 씬이 늘어날수록 자막이 실제 음성보다 점점 늦게
        # 나오다가 영상 뒷부분에서 크게 어긋나는 원인이 됩니다.
        self._timer = SceneTimer(
            min_scene_duration=self._min_dur,
            inter_scene_gap=0.0,
        )

    # ─────────────────────────────────────────
    # 공개 API
    # ─────────────────────────────────────────

    def compose(
        self,
        scene_audios: List[SceneAudio],
        image_results: List[ImageResult],
        ass_path: str | Path,
        bgm_files: List[str],
        output_path: str | Path,
        bgm_base_dir: str | Path = "assets/bgm",
    ) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        start = time.time()

        img_map: Dict[int, ImageResult] = {r.scene_index: r for r in image_results}
        timeline = self._timer.compute(scene_audios)

        self._report(0, 6, "씬 오디오 병합 중...")

        # 1) 씬별 TTS 오디오 concat
        scene_voice_paths: List[str] = []
        for sa in scene_audios:
            p = self._concat_scene_audio(sa)
            scene_voice_paths.append(p)

        self._report(1, 6, "씬 클립 생성 중...")

        # 2) 씬별 클립 (이미지 → 영상 + 오디오)
        scene_clip_paths: List[str] = []
        total_scenes = len(scene_audios)
        for idx, (sa, sc_timing) in enumerate(zip(scene_audios, timeline.scenes)):
            img_result = img_map.get(sa.scene_idx)
            voice_path = scene_voice_paths[idx]
            clip = self._make_scene_clip(
                sa, img_result, voice_path, idx, sc_timing.clip_duration,
                is_last=(idx == total_scenes - 1),
            )
            scene_clip_paths.append(clip)
            if self._cb:
                self._cb(idx + 1, total_scenes, f"scene_{idx}")

        self._report(2, 6, "씬 연결(크로스페이드) 중...")

        # 3) 씬 크로스페이드 연결 (무음 영상)
        silent_video = str(self._temp / "silent_video.mp4")
        self._ff.concat_videos(scene_clip_paths, silent_video, crossfade=self._crossfade)

        self._report(3, 6, "전체 TTS 오디오 병합 중...")

        # 4) 전체 TTS 오디오 concat
        full_voice = str(self._temp / "full_voice.mp3")
        valid_voices = [p for p in scene_voice_paths if Path(p).exists()]
        if valid_voices:
            if len(valid_voices) == 1:
                shutil.copy2(valid_voices[0], full_voice)
            else:
                self._ff.concat_audio(valid_voices, full_voice)
        else:
            # 무음 fallback
            self._ff.run([
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-t", str(max(timeline.total_duration, 1)),
                full_voice,
            ])

        self._report(4, 6, "BGM 믹싱 중...")

        # 5) BGM 믹싱
        video_with_audio = str(self._temp / "video_with_audio.mp4")
        bgm_path = self._resolve_bgm(bgm_files, bgm_base_dir)
        total_dur = self._ff.get_duration(silent_video)

        # shortest=False: silent_video는 크로스페이드 패딩 + outro_hold 덕분에
        # 항상 오디오보다 같거나 깁니다. shortest=True(기본값)를 쓰면 둘 중 짧은
        # 쪽(오디오)에 맞춰 영상 끝의 outro_hold 여유분이 통째로 잘려나가
        # 마지막 문장이 끝나자마자 뚝 끊기는 것처럼 보이게 됩니다.
        if bgm_path and total_dur > 0:
            mixed_audio = str(self._temp / "mixed_audio.mp3")
            self._ff.mix_audio(
                voice_path=full_voice,
                bgm_path=bgm_path,
                output=mixed_audio,
                voice_vol=self._tts_vol,
                bgm_vol=self._bgm_vol,
                duration=total_dur,
            )
            self._ff.add_audio_to_video(silent_video, mixed_audio, video_with_audio, shortest=False)
        else:
            if not bgm_path:
                logger.warning("BGM not found; using TTS-only audio.")
            self._ff.add_audio_to_video(silent_video, full_voice, video_with_audio, shortest=False)

        self._report(5, 6, "ASS 자막 하드코딩 중...")

        # 6) 자막 하드코딩 → 최종 출력
        self._ff.burn_subtitles(
            video_path=video_with_audio,
            ass_path=str(ass_path),
            output=str(output_path),
            codec=self._codec,
            preset=self._preset,
            crf=self._crf,
            bitrate=self._v_bitrate,
            audio_bitrate=self._a_bitrate,
            gpu=self._gpu,
            gpu_codec=self._gpu_codec,
            threads=self._threads,
            fonts_dir=str(self._fonts_dir) if self._fonts_dir else None,
        )

        elapsed = time.time() - start
        logger.info("Video composed in %.1fs → %s (%.1f MB)",
                    elapsed, output_path.name,
                    output_path.stat().st_size / 1e6)
        self._report(6, 6, "영상 완성!")
        return output_path

    # ─────────────────────────────────────────
    # 씬 처리
    # ─────────────────────────────────────────

    def _concat_scene_audio(self, sa: SceneAudio) -> str:
        out = str(self._temp / f"voice_{sa.scene_idx:04d}.mp3")
        valid = [
            seg.audio_path
            for seg in sa.segments
            if seg.success and seg.audio_path and Path(seg.audio_path).exists()
        ]
        if not valid:
            # 1초 무음
            self._ff.run([
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-t", "1", out,
            ])
            return out
        if len(valid) == 1:
            shutil.copy2(valid[0], out)
            return out
        self._ff.concat_audio(valid, out)
        return out

    def _make_scene_clip(
        self,
        sa: SceneAudio,
        img_result: Optional[ImageResult],
        voice_path: str,
        clip_index: int,
        clip_duration: float,
        is_last: bool = True,
    ) -> str:
        out = str(self._temp / f"clip_{clip_index:04d}.mp4")

        # 이미지 결정
        if img_result and img_result.success and img_result.image_path:
            img = img_result.image_path
        else:
            img = self._black_image()

        # concat_videos()가 크로스페이드로 이어붙이면 전환 구간(crossfade초) 동안
        # 두 클립이 겹쳐서 전체 길이가 줄어듭니다 (N개 클립이면 총 (N-1)*crossfade
        # 초만큼 짧아짐). 반면 오디오는 겹침 없이 그대로 이어붙이기 때문에,
        # 보정 없이 두면 뒤로 갈수록 영상이 오디오보다 점점 짧아지다가 최종
        # 믹싱 단계에서 오디오 뒷부분이 통째로 잘려나갑니다 (긴 영상일수록 더
        # 심하게 잘림). 마지막 씬을 제외한 모든 클립을 crossfade 만큼 더 길게
        # (마지막 프레임을 늘려서) 만들어두면, 전환 구간이 이 "여분"을 갉아먹을
        # 뿐 실제 씬 내용 길이(clip_duration)는 그대로 유지되어 최종 영상
        # 길이가 오디오 총합과 정확히 맞아떨어집니다.
        #
        # 마지막 씬에는 반대로 여분을 "남겨서" 씁니다 — 마지막 대사가 끝나자마자
        # 영상이 뚝 끊기면 문장이 잘린 것처럼 느껴지므로, outro_hold 만큼 마지막
        # 프레임을 더 들고 있다가 끝나게 합니다(오디오는 그대로, 영상만 살짝 더 김).
        if is_last:
            video_duration = clip_duration + self._outro_hold
        else:
            video_duration = clip_duration + self._crossfade

        # 이미지 → 무음 비디오 클립
        silent_clip = str(self._temp / f"silent_{clip_index:04d}.mp4")
        self._ff.image_to_video(
            image_path=img,
            output=silent_clip,
            duration=video_duration,
            width=self._w,
            height=self._h,
            fps=self._fps,
            zoom_effect=self._zoom,
            zoom_scale=self._zoom_scale,
        )

        # 오디오 합치기
        self._ff.add_audio_to_video(silent_clip, voice_path, out, shortest=False)
        return out

    def _black_image(self) -> str:
        path = str(self._temp / "black_frame.png")
        if not Path(path).exists():
            self._ff.run([
                "-f", "lavfi",
                "-i", f"color=black:s={self._w}x{self._h}",
                "-vframes", "1",
                path,
            ])
        return path

    def _resolve_bgm(
        self, bgm_files: List[str], base_dir: str | Path
    ) -> Optional[str]:
        base = Path(base_dir)
        for name in bgm_files:
            for candidate in [Path(name), base / name]:
                if candidate.exists():
                    return str(candidate)
        return None

    def _report(self, step: int, total: int, msg: str) -> None:
        logger.info("[VideoComposer %d/%d] %s", step, total, msg)
