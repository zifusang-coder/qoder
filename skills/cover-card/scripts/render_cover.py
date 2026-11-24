import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

THEMES = {
    "dark": ((15, 23, 42), (226, 232, 240), (99, 102, 241)),
    "light": ((248, 250, 252), (15, 23, 42), (79, 70, 229)),
    "warm": ((41, 24, 16), (255, 237, 213), (249, 115, 22)),
}
FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]
W, H = 1080, 1440
ROOT = Path(__file__).resolve().parents[3]
OUTPUTS_ROOT = (ROOT / "outputs").resolve()


def load_font(size: int):
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    print("警告：未找到中文字体，使用默认字体", file=sys.stderr)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def wrap(text: str, per_line: int) -> list[str]:
    return [text[i : i + per_line] for i in range(0, len(text), per_line)] or [""]


def normalize(text: str) -> str:
    return " ".join(text.split())


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


def main() -> None:
    payload = json.load(sys.stdin)
    inputs = payload.get("inputs", {})
    output = payload.get("output") or {}
    title = normalize(str(inputs.get("title", "")))
    subtitle = normalize(str(inputs.get("subtitle") or output.get("subtitle") or ""))
    theme_name = str(inputs.get("theme", "dark"))
    if not title:
        raise SystemExit("title 不能为空")
    if len(title) > 32:
        title = title[:31] + "…"
    if len(subtitle) > 18:
        subtitle = subtitle[:17] + "…"
    if theme_name not in THEMES:
        raise SystemExit(f"非法 theme：{theme_name}")
    raw_dir = str(inputs.get("output_dir") or "").strip()
    if not raw_dir:
        raise SystemExit("output_dir 不能为空")

    bg, fg, accent = THEMES[theme_name]
    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)
    draw.rectangle([80, 120, 96, H - 160], fill=accent)

    title_font = load_font(96)
    y = 420
    for line in wrap(title, 8)[:4]:
        draw.text((150, y), line, font=title_font, fill=fg)
        y += 128

    if subtitle:
        draw.text((150, y + 40), subtitle, font=load_font(48), fill=accent)
    draw.text((150, H - 140), "AI 辅助创作 · 请人工复核", font=load_font(32), fill=fg)

    out_dir = resolve_output_dir(raw_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "cover.png"
    img.save(path)
    print(
        json.dumps(
            {"path": str(path), "width": W, "height": H, "theme": theme_name},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
