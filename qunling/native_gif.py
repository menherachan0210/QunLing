"""Validate provider GIFs in the disposable decoder, without re-encoding them."""
import base64
import io


def inspect_gif(value):
    from PIL import Image
    from qunling.vision_input import MAX_BYTES, MAX_ANIMATION_PIXELS

    payload = value.partition(",")[2] if value.startswith("data:") else value
    raw = base64.b64decode(payload, validate=True)
    if len(raw) > MAX_BYTES:
        raise ValueError("GIF exceeds input budget")
    with Image.open(io.BytesIO(raw), formats=["GIF"]) as image:
        if image.width * image.height > MAX_ANIMATION_PIXELS:
            raise ValueError("GIF exceeds decoder budget")
        durations = []
        loop = image.info.get("loop")
        for index in range(17):
            try:
                image.seek(index)
            except EOFError:
                break
            if index == 16:
                raise ValueError("Native GIF exceeds 16 frames")
            image.load()
            durations.append(int(image.info.get("duration", 0)))
        if len(durations) < 2:
            raise ValueError("Provider returned a still instead of an animation")
        return {"format": "gif", "animated": True, "width": image.width, "height": image.height,
                "frame_count": len(durations), "frame_durations_ms": durations,
                "duration_ms": sum(durations), "loop": loop}
