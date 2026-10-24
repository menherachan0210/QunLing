"""Bounded, declarative GIF rendering; recipes are data, never executable code."""
from __future__ import annotations

from bisect import bisect_right
import io
import json
import math
from pathlib import Path
import re

from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageOps

from qunling.assets import AssetError

MAX_FRAMES = 120
MAX_OUTPUT_BYTES = 6 * 1024 * 1024
_REF = re.compile(r"asset_[0-9a-f]{32}")
_PROPS = {"x", "y", "scale", "rotation", "opacity"}


def _fail(message):
    raise AssetError("invalid_parameters", message)


def _keys(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        _fail("动画参数含不支持的字段或缺少必填项。")


def _number(value, low, high, *, integer=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not low <= value <= high
            or (integer and type(value) is not int)):
        _fail(f"动画数值应在 {low} 至 {high} 范围内。")
    return value


def _color(value):
    if value == "transparent":
        return (0, 0, 0, 0)
    if not isinstance(value, str) or len(value) > 32:
        _fail("颜色应为颜色名、#RRGGBB、#RRGGBBAA 或 transparent。")
    try:
        return ImageColor.getcolor(value, "RGBA")
    except (ValueError, TypeError):
        _fail("动画颜色无法识别。")


def _ref(value):
    if not isinstance(value, str) or not _REF.fullmatch(value):
        _fail("图片必须使用本轮文件工具提供的 asset_ref。")


def validate_recipe(value):
    _keys(value, {"width", "height", "duration_ms", "fps", "loop", "background", "frames", "layers", "source_speed"})
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        _fail("动画方案必须是有限 JSON 数据。")
    if len(encoded.encode()) > 7000:
        _fail("动画方案超过 7000 字节，请减少图层或关键帧。")
    recipe = json.loads(encoded)
    for key in ("width", "height"):
        recipe[key] = _number(recipe.get(key, 384), 32, 512, integer=True)
    recipe["fps"] = _number(recipe.get("fps", 12), 1, 24, integer=True)
    recipe["loop"] = _number(recipe.get("loop", 0), 0, 100, integer=True)
    recipe["source_speed"] = _number(recipe.get("source_speed", 1), 0.125, 8)
    recipe["background"] = recipe.get("background", "transparent")
    _color(recipe["background"])
    frames = recipe.setdefault("frames", [])
    layers = recipe.setdefault("layers", [])
    if not isinstance(frames, list) or len(frames) > 12 or not isinstance(layers, list) or len(layers) > 16:
        _fail("最多使用 12 张顺序图片、16 个图层。")
    if not frames and not layers:
        _fail("至少需要顺序图片或绘图图层。")
    for frame in frames:
        _keys(frame, {"asset_ref", "duration_ms"}, {"asset_ref"})
        _ref(frame["asset_ref"])
        frame["duration_ms"] = _number(frame.get("duration_ms", 500), 50, 10000, integer=True)
        frame["duration_ms"] = round(frame["duration_ms"] / 10) * 10
    recipe["duration_ms"] = _number(recipe.get("duration_ms", sum(f["duration_ms"] for f in frames) or 2000),
                                     100, 10000, integer=True)
    if math.ceil(recipe["duration_ms"] * recipe["fps"] / 1000) > MAX_FRAMES:
        _fail("动画超过 120 帧，请降低 fps 或缩短 duration_ms。")
    for layer in layers:
        _keys(layer, {"type", "asset_ref", "text", "color", "width", "height", "font_size",
                      "x", "y", "scale", "rotation", "opacity", "effect", "amplitude", "cycles", "keyframes"}, {"type"})
        if layer["type"] not in {"image", "text", "ellipse", "rectangle", "line"}:
            _fail("图层类型支持 image、text、ellipse、rectangle、line。")
        if layer["type"] == "image":
            _ref(layer.get("asset_ref"))
        elif "asset_ref" in layer:
            _fail("只有 image 图层使用 asset_ref。")
        if layer["type"] == "text":
            if not isinstance(layer.get("text"), str) or not 1 <= len(layer["text"]) <= 120:
                _fail("文字图层需要 1 至 120 字的 text。")
        for key, default in (("width", recipe["width"]), ("height", recipe["height"]), ("font_size", 36)):
            _number(layer.get(key, default), 1 if key != "font_size" else 8, 512 if key != "font_size" else 120, integer=True)
        _color(layer.get("color", "#111111"))
        if layer.get("effect", "none") not in {"none", "shake", "bounce", "pulse", "spin"}:
            _fail("动效支持 none、shake、bounce、pulse、spin，也可用 keyframes 自定义轨迹。")
        _number(layer.get("amplitude", 12), 0, 200)
        _number(layer.get("cycles", 2), 0.1, 12)
        points = layer.get("keyframes", [])
        if not isinstance(points, list) or len(points) > 20:
            _fail("每个图层最多 20 个关键帧。")
        previous = -1
        for point in [layer, *points]:
            if point is not layer:
                _keys(point, _PROPS | {"t"}, {"t"})
                t = _number(point["t"], 0, 1)
                if t <= previous:
                    _fail("关键帧 t 必须从小到大，范围为 0 至 1。")
                previous = t
            for key in _PROPS.intersection(point):
                lo, hi = {"x": (-1024, 1536), "y": (-1024, 1536), "scale": (0.1, 3),
                          "rotation": (-36000, 36000), "opacity": (0, 1)}[key]
                _number(point[key], lo, hi)
    if len(source_refs(recipe)) > 12:
        _fail("一次最多使用 12 个不同图片素材。")
    if len(json.dumps(recipe, ensure_ascii=False).encode()) > 7600:
        _fail("动画方案过长，请减少图层。")
    return recipe


def source_refs(recipe):
    return list(dict.fromkeys(item["asset_ref"] for item in recipe.get("frames", []) + recipe.get("layers", [])
                              if "asset_ref" in item))


def revise_recipe(original, changes, speed=1):
    _number(speed, 0.25, 4)
    _keys(changes, {"width", "height", "duration_ms", "fps", "loop", "background", "frames", "layers", "source_speed"})
    recipe = {**original, **changes}
    if speed != 1:
        recipe = json.loads(json.dumps(recipe))
        recipe["duration_ms"] = round(recipe["duration_ms"] / speed)
        recipe["source_speed"] = recipe.get("source_speed", 1) * speed
        if speed < 1 and "fps" not in changes:
            recipe["fps"] = max(1, round(recipe["fps"] * speed))
        for frame in recipe.get("frames", []):
            frame["duration_ms"] = round(frame["duration_ms"] / speed)
    return validate_recipe(recipe)


def _font(size, text):
    candidates = (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    for name in candidates:
        if Path(name).is_file() and (all(ord(c) < 256 for c in text) or "DejaVu" not in name):
            return ImageFont.truetype(name, size)
    if any(ord(c) >= 256 for c in text):
        raise AssetError("font_unavailable", "服务器缺少中文字库，暂时无法渲染中文字幕。")
    return ImageFont.load_default(size=size)


class _Source:
    def __init__(self, raw):
        self.image = Image.open(io.BytesIO(raw), formats=["PNG", "JPEG", "GIF", "WEBP", "BMP"])
        if self.image.width * self.image.height > 12_000_000:
            self.image.close()
            _fail("单张素材超过 1200 万像素，请先缩小。")
        self.ends = []
        elapsed = 0
        try:
            for i in range(241):
                try:
                    self.image.seek(i)
                except EOFError:
                    break
                if i == 240 or (i + 1) * self.image.width * self.image.height > 80_000_000:
                    _fail("素材动画解码量过大，请先缩短或缩小。")
                elapsed += max(20, int(self.image.info.get("duration", 100)))
                self.ends.append(elapsed)
        except BaseException:
            self.image.close()
            raise

    def frame(self, time_ms, size):
        index = min(bisect_right(self.ends, time_ms % self.ends[-1]), len(self.ends) - 1)
        self.image.seek(index)
        frame = ImageOps.exif_transpose(self.image).convert("RGBA")
        frame.thumbnail(size, Image.Resampling.LANCZOS)
        return frame


def _state(layer, position, width, height):
    values = {"x": layer.get("x", width / 2), "y": layer.get("y", height / 2),
              "scale": layer.get("scale", 1), "rotation": layer.get("rotation", 0), "opacity": layer.get("opacity", 1)}
    for prop, default in list(values.items()):
        points = [(p["t"], p[prop]) for p in layer.get("keyframes", []) if prop in p]
        if points:
            if points[0][0] > 0:
                points.insert(0, (0, default))
            points.append((1.000001, points[-1][1]))
            for (a, va), (b, vb) in zip(points, points[1:]):
                if a <= position <= b:
                    values[prop] = va + (vb - va) * (position - a) / (b - a)
                    break
    angle = position * math.tau * layer.get("cycles", 2)
    amplitude = layer.get("amplitude", 12)
    effect = layer.get("effect")
    if effect == "shake":
        values["x"] += math.sin(angle) * amplitude
    elif effect == "bounce":
        values["y"] -= abs(math.sin(angle)) * amplitude
    elif effect == "pulse":
        values["scale"] *= 1 + min(amplitude / 100, 0.8) * math.sin(angle)
    elif effect == "spin":
        values["rotation"] += position * 360 * layer.get("cycles", 2)
    return values


def _layer_image(layer, sources, time_ms, canvas):
    size = (layer.get("width", canvas[0]), layer.get("height", canvas[1]))
    if layer["type"] == "image":
        return sources[layer["asset_ref"]].frame(time_ms, size)
    if layer["type"] == "text":
        font = _font(layer.get("font_size", 36), layer["text"])
        lines = []
        for paragraph in layer["text"].split("\n"):
            line = ""
            for character in paragraph:
                if line and font.getlength(line + character) > max(1, size[0] - 6):
                    lines.append(line)
                    line = ""
                line += character
            lines.append(line)
        text = "\n".join(lines)
        bounds = ImageDraw.Draw(Image.new("L", (1, 1))).multiline_textbbox((0, 0), text, font=font, stroke_width=1)
        size = (max(1, bounds[2] - bounds[0] + 6), max(1, bounds[3] - bounds[1] + 6))
        if size[0] > 1024 or size[1] > 512:
            _fail("字幕排版超过画布预算，请减小字号、缩短文字或加宽文字图层。")
        image = Image.new("RGBA", size)
        ImageDraw.Draw(image).multiline_text((3 - bounds[0], 3 - bounds[1]), text,
                                            font=font, fill=_color(layer.get("color", "#111111")), align="center")
        return image
    image = Image.new("RGBA", size)
    draw = ImageDraw.Draw(image)
    color = _color(layer.get("color", "#111111"))
    box = (0, 0, size[0] - 1, size[1] - 1)
    if layer["type"] == "ellipse":
        draw.ellipse(box, fill=color)
    elif layer["type"] == "rectangle":
        draw.rectangle(box, fill=color)
    else:
        draw.line(box, fill=color, width=max(1, min(size) // 8))
    return image


def inspect_animation(raw):
    with Image.open(io.BytesIO(raw), formats=["GIF"]) as image:
        if image.width > 512 or image.height > 512:
            _fail("生成动画尺寸超过限制。")
        durations = []
        for index in range(MAX_FRAMES + 1):
            try:
                image.seek(index)
            except EOFError:
                break
            if index == MAX_FRAMES:
                _fail("生成动画帧数超过限制。")
            image.load()
            durations.append(int(image.info.get("duration", 0)))
        return {"format": "gif", "width": image.width, "height": image.height,
                "frame_count": len(durations), "duration_ms": sum(durations),
                "frame_durations_ms": durations, "loop": image.info.get("loop"),
                "animated": len(durations) > 1, "size": len(raw)}


def render(recipe, inputs, target):
    recipe = validate_recipe(recipe)
    sources, encoded = {}, []
    try:
        input_pixels = 0
        for ref in source_refs(recipe):
            sources[ref] = _Source(inputs[ref])
            input_pixels += sources[ref].image.width * sources[ref].image.height
            if input_pixels > 24_000_000:
                _fail("动画素材合计超过 2400 万像素，请先缩小。")
        size = (recipe["width"], recipe["height"])
        count = math.ceil(recipe["duration_ms"] * recipe["fps"] / 1000)
        # GIF stores centiseconds; distribute rounding across frame boundaries.
        boundaries = [round(i * recipe["duration_ms"] / count / 10) * 10 for i in range(count + 1)]
        frame_ends, elapsed = [], 0
        for frame in recipe["frames"]:
            elapsed += frame["duration_ms"]
            frame_ends.append(elapsed)
        if frame_ends:
            transitions = {end + cycle for cycle in range(0, recipe["duration_ms"], frame_ends[-1]) for end in frame_ends
                           if end + cycle < boundaries[-1]}
            boundaries = sorted(set(boundaries) | transitions)
            count = len(boundaries) - 1
            if count > MAX_FRAMES:
                _fail("图片切换与动效合计超过 120 帧，请降低帧率或缩短时长。")
        for i in range(count):
            time_ms = boundaries[i]
            canvas = Image.new("RGBA", size, _color(recipe["background"]))
            if frame_ends:
                tick = time_ms % frame_ends[-1]
                index = min(bisect_right(frame_ends, tick), len(frame_ends) - 1)
                spec = recipe["frames"][index]
                with sources[spec["asset_ref"]].frame((tick - (frame_ends[index - 1] if index else 0)) * recipe["source_speed"], size) as frame:
                    canvas.alpha_composite(frame, ((size[0] - frame.width) // 2, (size[1] - frame.height) // 2))
            for layer in recipe["layers"]:
                state = _state(layer, time_ms / max(boundaries[-2], 1), *size)
                sprite = _layer_image(layer, sources, time_ms * recipe["source_speed"], size)
                try:
                    if state["scale"] != 1:
                        resized = sprite.resize((max(1, round(sprite.width * state["scale"])),
                                                 max(1, round(sprite.height * state["scale"]))), Image.Resampling.BICUBIC)
                        sprite.close()
                        sprite = resized
                    if state["rotation"]:
                        rotated = sprite.rotate(state["rotation"], Image.Resampling.BICUBIC, expand=True)
                        sprite.close()
                        sprite = rotated
                    if state["opacity"] < 1:
                        sprite.putalpha(sprite.getchannel("A").point(lambda a: round(a * state["opacity"])))
                    canvas.alpha_composite(sprite, (round(state["x"] - sprite.width / 2), round(state["y"] - sprite.height / 2)))
                finally:
                    sprite.close()
            with canvas.convert("RGB") as rgb:
                quantized = rgb.quantize(colors=255, method=Image.Quantize.FASTOCTREE)
            mask = canvas.getchannel("A").point(lambda a: 255 if a < 128 else 0)
            quantized.paste(255, mask=mask)
            quantized.info["transparency"] = 255
            encoded.append(quantized)
            mask.close()
            canvas.close()
        encoded[0].save(target, format="GIF", save_all=True, append_images=encoded[1:],
                        duration=[b - a for a, b in zip(boundaries, boundaries[1:])],
                        loop=recipe["loop"], disposal=2, transparency=255, optimize=False)
        if Path(target).stat().st_size > MAX_OUTPUT_BYTES:
            raise AssetError("output_limit", "GIF 超过 6 MiB，请降低尺寸、帧率或时长。")
        info = inspect_animation(Path(target).read_bytes())
        if not info["animated"]:
            info["warning"] = "成品只有一个不同画面；若需要动画，请增加图片或动效。"
        return info
    except (KeyError, ValueError, OSError, Image.DecompressionBombError):
        raise AssetError("invalid_image", "动画素材无法解码或不属于可用图片。") from None
    finally:
        for source in sources.values():
            source.image.close()
        for frame in encoded:
            frame.close()
