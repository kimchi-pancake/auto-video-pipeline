"""
v2/render.py
=============
SCRIPT_READY 상태의 job을 영상으로 만든다(기존 TTS·자막·합성·썸네일 모듈 재사용).
이 단계는 결정적이다: 같은 script/assets 로 다시 돌리면 같은 영상이 나오므로 중간에 죽어도 처음부터 안전하게 재실행한다.

ASSETS_READY(장면별 미디어 확보) → RENDERING(합성) → QUALITY_CHECK 로 넘어가는 건 호출자(run.py)가 상태를 기록한다.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Dict, List

from image.image_generator import ImageResult
from image.thumbnail_generator import ThumbnailGenerator
from parser.story_parser import DialogueLine, Scene, StoryData, StoryParser, ThumbnailInfo
from subtitle.subtitle_builder import SubtitleBuilder
from tts.tts_builder import TTSBuilder
from utils.config_manager import get_config
from utils.logger import get_logger
from v2 import ROOT
from v2.channels import ChannelProfile
from v2.visuals import resolve as vresolve
from video.ffmpeg_wrapper import FFmpegWrapper
from video.video_composer import VideoComposer

logger = get_logger(__name__)

BGM_BY_MOOD = {"calm": 0, "tense": 1, "bright": 2}


def _segments(narration: str) -> List[str]:
    # 문장 하나 = TTS 한 번. 문장을 중간에서 토막 내서 읽히면 억양이 끊기고 어구가 잘려 들린다.
    # (자막은 TTS 어절 타이밍을 받아 subtitle/phrasing.py 가 의미 단위로 따로 끊는다.)
    return [p.strip() for p in re.split(r"(?<=[.?!])\s+", narration.strip()) if p.strip()]


def build_story(job: Dict) -> StoryData:
    script = job["script"]
    scenes = []
    for i, sc in enumerate(script["scenes"]):
        text = sc["narration"].strip()
        d = DialogueLine(speaker="나레이터", display_name="나레이터", text=text, segments=_segments(text), line_index=0)
        scenes.append(Scene(index=i, prompt="", resolution="1080x1920", dialogues=[d]))
    title = script.get("title") or job["topic"]["title"]
    ti = ThumbnailInfo(image_path="", title_text=job["hook"]["chosen"].get("screen_text") or title)
    return StoryData(cast={"나레이터": "나레이터"}, bgm_files=[], photo_files=[], scenes=scenes, thumbnail_long=None,
                     thumbnail_shorts=ti, raw_title=title, raw_path="", resolution=(1080, 1920),
                     category=script.get("category"), run_id=job["id"])


def resolve_assets(job: Dict, profile: ChannelProfile, work: Path) -> List[Dict]:
    out = []
    for i, sc in enumerate(job["script"]["scenes"]):
        out.append(vresolve.resolve(i, sc, job["pack"], profile, work))
    return out


def _fix_durations(ff: FFmpegWrapper, scene_audios) -> None:
    for sa in scene_audios:
        for seg in sa.segments:
            if seg.success and seg.audio_path and Path(seg.audio_path).exists():
                real = ff.get_audio_duration(seg.audio_path)
                if real > 0:
                    seg.duration = real


def render(job: Dict, profile: ChannelProfile, work: Path, assets: List[Dict]) -> Dict:
    cfg = get_config()
    work = Path(work)
    (work / "tts").mkdir(parents=True, exist_ok=True)
    (work / "video").mkdir(parents=True, exist_ok=True)
    out_dir = work / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    story = build_story(job)

    tts_cfg = dict(cfg.section("tts"))
    voices = dict(tts_cfg.get("voices", {}))
    voices["나레이터"] = {"voice": profile.voice, "rate": profile.voice_rate, "pitch": profile.voice_pitch, "volume": "+0%"}
    tts_cfg["voices"] = voices
    tts_cfg["default_voice"] = profile.voice
    # CI(데이터센터 IP)에서는 Edge-TTS가 동시 요청을 거절("No audio was received")하므로 천천히·여러 번
    tts_cfg.update({"max_workers": 2, "retry_count": 6, "retry_delay": 3})
    scene_audios = TTSBuilder(tts_cfg, work / "tts").build(story)
    ff = FFmpegWrapper(ffmpeg=cfg.get("video.ffmpeg_path", "ffmpeg"), ffprobe=cfg.get("video.ffprobe_path", "ffprobe"))
    _fix_durations(ff, scene_audios)
    failed = [s for sa in scene_audios for s in sa.segments if not s.success]
    if failed:
        raise RuntimeError(f"TTS 실패 세그먼트 {len(failed)}개")

    sub_cfg = dict(cfg.section("subtitle"))
    srt, ass = SubtitleBuilder(sub_cfg, work / "video", min_scene_duration=0.8, video_width=1080, video_height=1920).build(
        scene_audios, title=job["hook"]["chosen"].get("screen_text") or story.raw_title)

    results: List[ImageResult] = []
    for i, a in enumerate(assets):
        results.append(ImageResult(scene_index=i, image_path=a["path"], prompt="", success=True,
                                   is_video=(a["kind"] == "anim"), source="anim" if a["kind"] == "anim" else "photo"))

    vcfg = dict(cfg.section("video"))
    vcfg.update({"width": 1080, "height": 1920, "min_scene_duration": 0.8, "dynamic_edit": True, "duck_bgm": True,
                 "loudnorm": True, "sfx": True, "bgm_volume": 0.30, "dynamic_crossfade": 0.28, "pan_zoom_scale": 1.10})
    mood = job["script"].get("bgm_mood", "calm")
    choices = profile.bgm_choices or ["piano.mp3"]
    bgm = choices[min(BGM_BY_MOOD.get(mood, 0), len(choices) - 1)]
    video_path = out_dir / "video.mp4"
    VideoComposer(vcfg, work / "video", fonts_dir=ROOT / "assets" / "fonts").compose(
        scene_audios=scene_audios, image_results=results, ass_path=ass, bgm_files=[bgm],
        output_path=video_path, bgm_base_dir=ROOT / "assets" / "bgm")

    bg = next((a["path"] for a in assets if a["kind"] == "image"), None)
    thumb = None
    try:
        thumb = ThumbnailGenerator(cfg.section("thumbnail"), ROOT / "assets").generate_shorts(story.thumbnail_shorts, out_dir, bg)
    except Exception as e:  # noqa: BLE001
        logger.warning("썸네일 생성 실패(업로드는 가능): %s", e)
    shutil.copy2(ass, out_dir / "subtitles.ass")
    return {"video": str(video_path), "thumbnail": str(thumb) if thumb else None, "ass": str(out_dir / "subtitles.ass"),
            "bgm": bgm, "voice": profile.voice}
