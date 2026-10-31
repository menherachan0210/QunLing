"""Small chronological contact sheets for bounded animated-image inspection."""
from __future__ import annotations

from bisect import bisect_right
import math

from PIL import Image, ImageDraw


def contact_sheet(image):
    """Sample decoded frames, preserving timing labels and partial-read evidence."""
    size = image.size
    budget = max(1, min(240, 80_000_000 // max(1, size[0] * size[1])))
    ends, total, complete = [], 0, False
    for index in range(budget):
        try:
            image.seek(index)
        except EOFError:
            complete = True
            break
        total += max(20, min(60000, int(image.info.get("duration", 100) or 100)))
        ends.append(total)
    if not ends:
        raise ValueError("No image frames")
    samples = []
    for i in range(min(6, len(ends))):
        tick = max(0, (total - 1) * i / max(1, min(6, len(ends)) - 1))
        index = min(bisect_right(ends, tick), len(ends) - 1)
        if index not in samples:
            samples.append(index)
    columns = min(3, len(samples))
    tile_w, tile_h = 300, 260
    sheet = Image.new("RGB", (columns * tile_w, math.ceil(len(samples) / columns) * tile_h + 42), "white")
    draw = ImageDraw.Draw(sheet)
    label = "Sampled animation frames; not full playback"
    draw.text((5, 4), label, fill="black")
    status = f"{len(ends)} frames / {total / 1000:.2f}s" if complete else f"PARTIAL: first {len(ends)} frames / {total / 1000:.2f}s"
    draw.text((5, 20), status, fill="black")
    for offset, index in enumerate(samples):
        image.seek(index)
        # Resize before adding an alpha channel. A small compressed GIF may
        # have a huge canvas; converting it first allocates another full frame.
        ratio = min(1, (tile_w - 12) / image.width, (tile_h - 28) / image.height)
        target = (max(1, int(image.width * ratio)), max(1, int(image.height * ratio)))
        with image.resize(target, Image.Resampling.LANCZOS, reducing_gap=3) as small, small.convert("RGBA") as frame:
            x = (offset % columns) * tile_w
            y = (offset // columns) * tile_h + 42
            sheet.paste(frame, (x + (tile_w - frame.width) // 2, y + 22), frame)
            tick = ends[index - 1] if index else 0
            draw.text((x + 5, y + 3), f"Frame {index + 1} @ {tick / 1000:.2f}s", fill="black")
    return sheet
