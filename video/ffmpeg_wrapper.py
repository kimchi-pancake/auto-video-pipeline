"""
video/ffmpeg_wrapper.py
=======================
FFmpeg / FFprobe 를 subprocess 로 호출하는 저수준 래퍼.
MoviePy 는 사용하지 않습니다.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import List, Optional

from utils.logger import get_logger

logger = get_logger(__name__)


class FFmpegError(RuntimeError):
    pass


class FFmpegWrapper:
    def __init__(self, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe"):
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe

    # ── 저수준 실행 ─────────────────────────────────────

    def run(self, args: List[str], capture: bool = False) -> subprocess.CompletedProcess:
        cmd = [self._ffmpeg, "-y", "-loglevel", "error"] + args
        logger.debug("FFmpeg: %s", " ".join(cmd))
        # capture_output은 모든 호출부가 항상 stderr를 캡처하도록 True로 고정합니다.
        # -loglevel error 라서 표준출력에 실제 미디어 데이터가 안 섞이니 캡처해도
        # 안전합니다. 예전에는 이 인자가 그대로 전달돼서 대부분의 호출이 stderr를
        # 안 받아왔고, 그 결과 실패해도 진짜 원인 없이 "(no stderr)"로만 로깅되는
        # 문제가 실제로 있었습니다(2026-07-25 웃짬 채널 영상 2개가 rc=254로 3번
        # 다 실패했는데 원인을 하나도 못 봄). `capture` 인자는 호환을 위해 남겨
        # 두되 내부적으로는 강제로 True를 씁니다.
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            err = result.stderr or "(no stderr)"
            raise FFmpegError(f"FFmpeg failed (rc={result.returncode}): {err[:500]}")
        return result

    def probe(self, path: str | Path) -> dict:
        cmd = [
            self._ffprobe, "-v", "quiet",
            "-print_format", "json",
            "-show_streams", "-show_format",
            str(path),
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        if result.returncode != 0:
            raise FFmpegError(f"ffprobe failed: {result.stderr[:300]}")
        return json.loads(result.stdout)

    # ── 정보 조회 ───────────────────────────────────────

    def get_duration(self, path: str | Path) -> float:
        try:
            info = self.probe(path)
            return float(info["format"]["duration"])
        except Exception as e:
            logger.warning("get_duration failed for %s: %s", path, e)
            return 0.0

    def get_audio_duration(self, path: str | Path) -> float:
        return self.get_duration(path)

    def get_video_info(self, path: str | Path) -> dict:
        try:
            info = self.probe(path)
            for s in info.get("streams", []):
                if s.get("codec_type") == "video":
                    return {
                        "width": s.get("width", 0),
                        "height": s.get("height", 0),
                        "fps": self._parse_fps(s.get("r_frame_rate", "30/1")),
                        "duration": float(info["format"].get("duration", 0)),
                    }
        except Exception as e:
            logger.warning("get_video_info failed: %s", e)
        return {}

    @staticmethod
    def _parse_fps(fps_str: str) -> float:
        try:
            num, den = fps_str.split("/")
            return float(num) / float(den)
        except Exception:
            return 30.0

    # ── 오디오 조작 ─────────────────────────────────────

    def concat_audio(self, inputs: List[str], output: str) -> None:
        """여러 오디오 파일을 순서대로 이어붙입니다."""
        list_file = Path(output).with_suffix(".txt")
        lines = "\n".join(f"file '{Path(p).as_posix()}'" for p in inputs)
        list_file.write_text(lines, encoding="utf-8")
        self.run([
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c", "copy", output,
        ])
        list_file.unlink(missing_ok=True)

    def mix_audio(
        self,
        voice_path: str,
        bgm_path: str,
        output: str,
        voice_vol: float = 1.0,
        bgm_vol: float = 0.15,
        duration: Optional[float] = None,
    ) -> None:
        """TTS 보이스와 BGM을 믹싱합니다."""
        filter_chain = (
            f"[0:a]volume={voice_vol}[v];"
            f"[1:a]volume={bgm_vol},aloop=loop=-1:size=2e+09[b];"
            f"[v][b]amix=inputs=2:duration=first[out]"
        )
        args = [
            "-i", voice_path,
            "-i", bgm_path,
            "-filter_complex", filter_chain,
            "-map", "[out]",
            "-ac", "2", "-ar", "44100",
        ]
        if duration:
            args += ["-t", str(duration)]
        args.append(output)
        self.run(args)

    def loop_audio(self, input_path: str, output: str, duration: float) -> None:
        """오디오를 지정 길이만큼 루프합니다."""
        self.run([
            "-stream_loop", "-1",
            "-i", input_path,
            "-t", str(duration),
            "-c", "copy", output,
        ])

    # ── 이미지/비디오 조작 ──────────────────────────────

    def image_to_video(
        self,
        image_path: str,
        output: str,
        duration: float,
        width: int,
        height: int,
        fps: int = 30,
        zoom_effect: bool = True,
        zoom_scale: float = 1.05,
    ) -> None:
        """정지 이미지를 지정 길이의 영상으로 변환합니다. pan&zoom 옵션 포함."""
        if zoom_effect:
            # Ken Burns 효과 (천천히 줌인).
            # scale 뒤에 force_original_aspect_ratio=increase + crop 을 거치는 이유:
            # 소스 이미지 비율과 목표(width x height) 비율이 다르면(예: 가로 사진을
            # 세로 쇼츠에 재사용) 그냥 scale만 하면 이미지가 찌그러집니다.
            # "꽉 채우게 확대한 뒤 가운데를 잘라내는" 방식으로 항상 비율을 유지합니다.
            frames = int(duration * fps)
            target_w = int(width * zoom_scale)
            target_h = int(height * zoom_scale)
            vf = (
                f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
                f"crop={target_w}:{target_h},"
                f"zoompan=z='min(zoom+0.0003,{zoom_scale})':d={frames}:"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                f"s={width}x{height}:fps={fps}"
            )
        else:
            vf = f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            vf += f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"

        # preset=ultrafast/crf=23: 이 출력은 concat_videos()가 크로스페이드로
        # 다시 읽어서 재인코딩하는 중간산출물이라(아무도 직접 안 봄), 최종
        # 화질(burn_subtitles의 preset/crf, config 기준)과 무관하게 최대한
        # 빨리 뽑는 게 이득입니다. 실측(2026-07-28): 8초 클립 기준 preset=fast
        # 대비 약 44% 빠름. 롱폼은 씬마다(20개 이상) 이 인코딩이 반복되므로
        # GitHub Actions 러너 시간(=과금) 절감 효과가 씬 수만큼 누적됩니다.
        self.run([
            "-loop", "1",
            "-i", image_path,
            "-vf", vf,
            "-t", str(duration),
            "-r", str(fps),
            "-pix_fmt", "yuv420p",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "23",
            output,
        ])

    def concat_videos(self, inputs: List[str], output: str, crossfade: float = 0.8) -> None:
        """여러 영상을 크로스페이드로 연결합니다."""
        if not inputs:
            raise FFmpegError("concat_videos: 이어붙일 입력 영상이 하나도 없습니다 (씬이 0개인 대본일 수 있습니다).")
        if len(inputs) == 1:
            import shutil
            shutil.copy2(inputs[0], output)
            return

        # 크로스페이드 필터 체인 구성
        n = len(inputs)
        input_args = []
        for p in inputs:
            input_args += ["-i", p]

        durations = [self.get_duration(p) for p in inputs]

        # xfade 체인. offset은 "지금까지 이어붙인 결과 스트림"의 누적 길이를
        # 기준으로 계산해야 합니다 (직전 개별 클립 하나의 길이가 아니라) —
        # 그렇지 않으면 씬이 늘어날수록 offset이 실제 위치보다 훨씬 작게
        # 계산되어 뒤쪽 씬들이 잘려나가거나 서로 겹쳐버립니다.
        filter_parts = []
        last_v = "[0:v]"
        cumulative = durations[0]
        for i in range(1, n):
            offset = max(cumulative - crossfade, 0)
            out_v = f"[v{i}]" if i < n - 1 else "[vout]"
            filter_parts.append(
                f"{last_v}[{i}:v]xfade=transition=fade:duration={crossfade}:offset={offset:.3f}{out_v}"
            )
            last_v = out_v
            cumulative = offset + durations[i]

        filter_chain = ";".join(filter_parts)
        # image_to_video()와 같은 이유로 preset=ultrafast/crf=23 — 이 출력
        # (silent_video.mp4)도 BGM 믹싱 후 burn_subtitles()에서 최종 화질로
        # 다시 인코딩되는 중간산출물입니다.
        self.run(input_args + [
            "-filter_complex", filter_chain,
            "-map", "[vout]",
            "-pix_fmt", "yuv420p",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "23",
            output,
        ])

    def burn_subtitles(
        self,
        video_path: str,
        ass_path: str,
        output: str,
        codec: str = "libx264",
        preset: str = "medium",
        crf: int = 18,
        bitrate: str = "8000k",
        audio_bitrate: str = "192k",
        gpu: bool = False,
        gpu_codec: str = "h264_nvenc",
        threads: int = 4,
        fonts_dir: Optional[str] = None,
    ) -> None:
        """ASS 자막을 영상에 하드코딩합니다."""
        # Windows 경로의 드라이브 콜론을 이스케이프하고 필터 옵션 값은 따옴표로 감쌈
        ass_escaped = str(ass_path).replace("\\", "/").replace(":", "\\:")
        vf = f"ass='{ass_escaped}'"
        if fonts_dir:
            fonts_dir_escaped = str(fonts_dir).replace("\\", "/").replace(":", "\\:")
            vf += f":fontsdir='{fonts_dir_escaped}'"

        video_codec = gpu_codec if gpu else codec
        encode_args = (
            ["-c:v", video_codec, "-preset", "p4", f"-b:v", bitrate]
            if gpu
            else ["-c:v", codec, "-preset", preset, "-crf", str(crf)]
        )

        self.run([
            "-i", video_path,
            "-vf", vf,
        ] + encode_args + [
            "-c:a", "aac",
            "-b:a", audio_bitrate,
            "-threads", str(threads),
            output,
        ])

    def add_audio_to_video(
        self,
        video_path: str,
        audio_path: str,
        output: str,
        shortest: bool = True,
    ) -> None:
        """영상에 오디오 트랙을 추가합니다."""
        args = [
            "-i", video_path,
            "-i", audio_path,
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-map", "0:v:0",
            "-map", "1:a:0",
        ]
        if shortest:
            args.append("-shortest")
        args.append(output)
        self.run(args)

    def trim_video(self, input_path: str, output: str, start: float, duration: float) -> None:
        self.run([
            "-ss", str(start),
            "-i", input_path,
            "-t", str(duration),
            "-c", "copy",
            output,
        ])
