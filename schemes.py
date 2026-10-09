"""Рисует текстовые схемы (из блоков <pre> конспекта) в PNG-картинки."""
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent
FONT_CANDIDATES = [
    HERE / "fonts" / "DejaVuSansMono.ttf",  # лежит рядом с ботом, работает везде
    Path("C:/Windows/Fonts/consola.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
    Path("/System/Library/Fonts/Menlo.ttc"),
]

BG, BORDER, ACCENT, FG = "#F7F7F5", "#D5D5D0", "#4F7CAC", "#1F2933"


def _font(size: int) -> ImageFont.FreeTypeFont:
    for p in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(str(p), size)
        except OSError:
            continue
    raise RuntimeError("Не найден моноширинный шрифт с кириллицей (fonts/DejaVuSansMono.ttf)")


def render(text: str, max_width: int = 1200) -> bytes:
    lines = text.expandtabs(4).rstrip().split("\n")
    pad, bar = 36, 12

    size = 30
    while True:  # уменьшаем шрифт, пока схема не влезет по ширине
        font = _font(size)
        width = max(int(font.getlength(line)) for line in lines)
        if width + 2 * pad + bar <= max_width or size <= 16:
            break
        size -= 2

    asc, desc = font.getmetrics()
    line_h = asc + desc  # без межстрочного зазора, чтобы │ и ├ соединялись
    w = max(width + 2 * pad + bar, 420)
    h = line_h * len(lines) + 2 * pad

    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=20, fill=BG, outline=BORDER, width=2)
    d.rounded_rectangle([0, 0, bar + 8, h - 1], radius=20, fill=ACCENT)
    d.rectangle([bar, 0, bar + 10, h - 1], fill=BG)  # скругление только слева
    for i, line in enumerate(lines):
        d.text((pad + bar, pad + i * line_h), line, font=font, fill=FG)

    out = BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()
