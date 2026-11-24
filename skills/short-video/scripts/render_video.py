import json
import math
import shutil
import sys
from pathlib import Path

SHARED = Path(__file__).resolve().parents[2] / "shared" / "scripts"
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

from media_common import (  # noqa: E402
    MediaError,
    concat_audio,
    mux_audio,
    probe_duration,
    render_slideshow,
)
from tts_common import (  # noqa: E402
    DEFAULT_VOICE,
    TTSError,
    merge_srt,
    normalize_voice,
    scene_srt,
    shift_srt,
    synthesize,
)

W, H = 1080, 1920
MAX_SCENES = 8
MAX_SCENE_CHARS = 120
MAX_TITLE_CHARS = 40
ROOT = Path(__file__).resolve().parents[3]
OUTPUTS_ROOT = (ROOT / "outputs").resolve()
FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]
PLATFORM_LABELS = {
    "douyin": "抖音",
    "channels": "视频号",
    "xiaohongshu": "小红书",
    "bilibili": "B 站",
    "kuaishou": "快手",
    "wechat": "公众号",
}
THEMES = {
    "dark": {
        "bg": (11, 16, 32),
        "bg2": (38, 46, 84),
        "fg": (238, 242, 255),
        "muted": (148, 163, 184),
        "accent": (129, 140, 248),
    },
    "light": {
        "bg": (248, 250, 252),
        "bg2": (219, 228, 240),
        "fg": (15, 23, 42),
        "muted": (71, 85, 105),
        "accent": (79, 70, 229),
    },
    "warm": {
        "bg": (42, 24, 16),
        "bg2": (104, 52, 28),
        "fg": (255, 237, 213),
        "muted": (253, 186, 116),
        "accent": (249, 115, 22),
    },
}


def normalize(text: object) -> str:
    return " ".join(str(text or "").split())


def load_font(size: int):
    from PIL import ImageFont

    for candidate in FONT_CANDIDATES:
        path = Path(candidate)
        if path.exists():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    print("警告：未找到中文字体，使用默认字体", file=sys.stderr)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def wrap_text(draw, text: str, font, max_width: int, max_lines: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for char in text:
        candidate = current + char
        if current and draw.textlength(candidate, font=font) > max_width:
            lines.append(current)
            current = char
            if len(lines) == max_lines:
                break
        else:
            current = candidate
    if len(lines) < max_lines and current:
        lines.append(current)
    consumed = len("".join(lines))
    if lines and consumed < len(text):
        last = lines[-1]
        while last and draw.textlength(f"{last}…", font=font) > max_width:
            last = last[:-1]
        lines[-1] = f"{last}…"
    return lines or [""]


def gradient(image, start, end) -> None:
    from PIL import ImageDraw

    draw = ImageDraw.Draw(image)
    for y in range(H):
        ratio = y / max(H - 1, 1)
        color = tuple(
            int(start[index] + (end[index] - start[index]) * ratio)
            for index in range(3)
        )
        draw.line([(0, y), (W, y)], fill=color)


def build_card(
    path: Path,
    *,
    title: str,
    body: str,
    eyebrow: str,
    index: int,
    total: int,
    theme: dict,
    platform_label: str,
) -> None:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (W, H), theme["bg"])
    gradient(image, theme["bg"], theme["bg2"])
    draw = ImageDraw.Draw(image)

    # 左侧强调条与顶部标签
    draw.rounded_rectangle([72, 96, 88, 260], radius=8, fill=theme["accent"])
    badge_font = load_font(30)
    badge = f"{platform_label} · AI 短视频"
    badge_width = draw.textlength(badge, font=badge_font)
    draw.rounded_rectangle(
        [116, 96, 116 + badge_width + 48, 152], radius=28, fill=theme["accent"]
    )
    draw.text((140, 108), badge, font=badge_font, fill=theme["bg"])
    draw.text(
        (W - 116 - draw.textlength(eyebrow, font=badge_font), 108),
        eyebrow,
        font=badge_font,
        fill=theme["muted"],
    )

    y = 300
    if title:
        title_font = load_font(84)
        for line in wrap_text(draw, title, title_font, W - 232, 3):
            draw.text((116, y), line, font=title_font, fill=theme["fg"])
            y += 112
        y += 48

    body_size = 58 if title else 66
    if y > H - 520:
        body_size = 52
    body_font = load_font(body_size)
    for line in wrap_text(draw, body, body_font, W - 232, 6):
        draw.text((116, y), line, font=body_font, fill=theme["fg"])
        y += int(body_size * 1.45)

    # 底部分镜进度
    segment_width = (W - 232 - (total - 1) * 12) / total
    for position in range(total):
        left = 116 + position * (segment_width + 12)
        right = left + segment_width
        color = theme["accent"] if position == index else theme["muted"]
        draw.rounded_rectangle([left, H - 210, right, H - 198], radius=6, fill=color)

    footer_font = load_font(30)
    draw.text(
        (116, H - 156),
        "AI 辅助创作 · 请人工复核",
        font=footer_font,
        fill=theme["muted"],
    )
    image.save(path)


def resolve_output_dir(raw: str) -> Path:
    candidate = Path(raw)
    target = (
        candidate.resolve()
        if candidate.is_absolute()
        else (ROOT / candidate).resolve()
    )
    try:
        target.relative_to(OUTPUTS_ROOT)
    except ValueError:
        raise SystemExit("output_dir 必须位于 outputs 目录下") from None
    return target


def number(value: object, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def clean_scenes(raw: object, default_seconds: float) -> list[dict]:
    if not isinstance(raw, list):
        raise SystemExit("分镜列表缺失")
    scenes: list[dict] = []
    for item in raw[:MAX_SCENES]:
        if not isinstance(item, dict):
            continue
        text = normalize(item.get("text"))
        if not text:
            continue
        seconds = min(max(number(item.get("seconds"), default_seconds), 1.0), 20.0)
        scenes.append({"text": text[:MAX_SCENE_CHARS], "seconds": seconds})
    if not scenes:
        raise SystemExit("分镜中没有可用文本")
    return scenes


def clean_hashtags(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    tags: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        tag = normalize(item)
        if not tag:
            continue
        if not tag.startswith("#"):
            tag = f"#{tag}"
        if tag not in tags:
            tags.append(tag[:30])
        if len(tags) == 5:
            break
    return tags


def base_durations(scenes: list[dict], target: int) -> list[float]:
    raw_total = sum(scene["seconds"] for scene in scenes)
    scale = target / raw_total if raw_total > 0 else 1.0
    return [
        min(max(scene["seconds"] * scale, 1.2), 15.0) for scene in scenes
    ]


def synthesize_scenes(
    scenes: list[dict], audio_dir: Path, voice_name: str
) -> tuple[list[float], list[str], Path | None, dict]:
    segments: list[tuple[Path, str]] = []
    durations: list[float] = []
    subtitles: list[str] = []
    offset = 0.0
    try:
        for index, scene in enumerate(scenes):
            result = synthesize(
                scene["text"],
                audio_dir / f"scene_{index:02d}.mp3",
                voice=voice_name,
                receive_timeout=60,
            )
            segments.append((result.path, result.srt))
            durations.append(result.duration + 0.35)
            if result.srt:
                subtitles.append(shift_srt(result.srt, offset))
            offset += result.duration
        voiceover = audio_dir.parent / "voiceover.mp3"
        concat_audio([path for path, _ in segments], voiceover)
        return (
            durations,
            subtitles,
            voiceover,
            {"status": "ok", "voice": voice_name, "segments": len(segments)},
        )
    except (TTSError, MediaError) as exc:
        for path, _ in segments:
            path.unlink(missing_ok=True)
        (audio_dir.parent / "voiceover.mp3").unlink(missing_ok=True)
        return [], [], None, {
            "status": "failed",
            "reason": str(exc)[:200],
        }


def main() -> None:
    payload = json.load(sys.stdin)
    inputs = payload.get("inputs", {})
    output = payload.get("output") or {}
    if not isinstance(inputs, dict) or not isinstance(output, dict):
        raise SystemExit("payload 结构不正确")

    raw_dir = normalize(inputs.get("output_dir"))
    if not raw_dir:
        raise SystemExit("output_dir 不能为空")
    out_dir = resolve_output_dir(raw_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    topic = normalize(inputs.get("topic"))[:200]
    title = normalize(output.get("title")) or topic
    title = title[:MAX_TITLE_CHARS]
    if not title:
        raise SystemExit("标题不能为空")
    narration = normalize(output.get("narration")) or " ".join(
        normalize(scene.get("text"))
        for scene in (output.get("scenes") or [])
        if isinstance(scene, dict)
    )
    hashtags = clean_hashtags(output.get("hashtags"))
    platform = normalize(inputs.get("platform")) or "douyin"
    platform_label = PLATFORM_LABELS.get(platform, platform[:12])
    theme_name = normalize(inputs.get("theme")) or "dark"
    theme = THEMES.get(theme_name, THEMES["dark"])
    target_seconds = int(min(max(number(inputs.get("duration_seconds"), 30), 8), 180))
    voice_enabled = inputs.get("voice", True) is not False
    voice_name = normalize_voice(inputs.get("voice_name"))

    scenes = clean_scenes(output.get("scenes"), max(target_seconds / 4, 2.0))
    durations = base_durations(scenes, target_seconds)
    audio: dict = {"status": "disabled"}
    audio_path: Path | None = None
    srt_parts: list[str] = []

    if voice_enabled:
        audio_dir = out_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        audio_durations, srt_parts, audio_path, audio = synthesize_scenes(
            scenes, audio_dir, voice_name
        )
        if audio_path is not None:
            durations = audio_durations
            # 视频比音频略长，-shortest 截断时最后一帧不会被提前吃掉
            durations[-1] += 0.5
        else:
            shutil.rmtree(audio_dir, ignore_errors=True)

    subtitles_path = out_dir / "subtitles.srt"
    srt = merge_srt(srt_parts) if srt_parts else scene_srt(
        [scene["text"] for scene in scenes], durations
    )
    subtitles_path.write_text(srt, encoding="utf-8")

    cards_dir = out_dir / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)
    cards: list[Path] = []
    for index, scene in enumerate(scenes):
        card = cards_dir / f"card_{index:02d}.png"
        build_card(
            card,
            title=title if index == 0 else "",
            body=scene["text"],
            eyebrow=f"{index + 1:02d}/{len(scenes):02d}",
            index=index,
            total=len(scenes),
            theme=theme,
            platform_label=platform_label,
        )
        cards.append(card)
    cover_path = out_dir / "cover.png"
    shutil.copyfile(cards[0], cover_path)

    video_path = out_dir / "video.mp4"
    try:
        silent_path = out_dir / "video_silent.mp4"
        render_slideshow(cards, durations, silent_path, timeout=240)
        if audio_path is not None:
            mux_audio(silent_path, audio_path, video_path, timeout=180)
            silent_path.unlink(missing_ok=True)
        else:
            silent_path.replace(video_path)
    except MediaError as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        shutil.rmtree(cards_dir, ignore_errors=True)
        if voice_enabled:
            shutil.rmtree(out_dir / "audio", ignore_errors=True)

    script_path = out_dir / "script.txt"
    script_text = "\n".join(
        part for part in (title, narration, " ".join(hashtags)) if part
    )
    script_path.write_text(script_text + "\n", encoding="utf-8")

    try:
        duration = probe_duration(video_path)
    except MediaError as exc:
        raise SystemExit(str(exc)) from exc

    files = [
        {"name": "video.mp4", "path": str(video_path), "kind": "video"},
        {"name": "cover.png", "path": str(cover_path), "kind": "image"},
        {"name": "subtitles.srt", "path": str(subtitles_path), "kind": "subtitles"},
        {"name": "script.txt", "path": str(script_path), "kind": "text"},
    ]
    if audio_path is not None:
        files.append(
            {"name": "voiceover.mp3", "path": str(audio_path), "kind": "audio"}
        )

    print(
        json.dumps(
            {
                "kind": "video",
                "title": title,
                "narration": narration,
                "hashtags": hashtags,
                "scenes": [
                    {"text": scene["text"], "seconds": round(seconds, 3)}
                    for scene, seconds in zip(scenes, durations, strict=True)
                ],
                "duration_seconds": round(duration, 3),
                "width": W,
                "height": H,
                "audio": audio,
                "files": files,
                "preview": "video.mp4",
                "cover": "cover.png",
                "subtitles": "subtitles.srt",
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
