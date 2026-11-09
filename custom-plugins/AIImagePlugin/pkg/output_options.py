"""Native image output options; never synthesize animation with sprite sheets."""
from qunling.assets import AssetError

GIF_MODEL = "gpt-image-2.6-sunburst"
GIF_SIZES = {"auto", "256x256", "384x384", "512x512", "768x768", "1024x1024"}


def output_options(params):
    if type(params.get("deliver", True)) is not bool:
        raise AssetError("invalid_parameters", "deliver 必须为 true 或 false。")
    fmt = params.get("output_format", "png")
    if fmt not in ("png", "gif"):
        raise AssetError("invalid_parameters", "output_format 只能为 png 或 gif。")
    options = {"output_format": "gif"} if fmt == "gif" else {}
    for key, minimum, maximum in (("frame_count", 2, 16), ("frame_duration_ms", 20, 2000)):
        if key not in params:
            continue
        value = params[key]
        if fmt != "gif" or type(value) is not int or not minimum <= value <= maximum:
            raise AssetError("invalid_parameters", f"{key} 仅用于 GIF，需为 {minimum}–{maximum} 之间的整数。")
        options[key] = value
    return options


def request_options(config, options=None):
    options = output_options(options or {})
    result = {}
    complexity = config.get("complexity", "auto")
    if complexity not in ("auto", "low", "medium", "high"):
        raise AssetError("invalid_parameters", "图片复杂度配置无效。")
    if complexity != "auto":
        result["complexity"] = complexity
    if not options:
        return result
    defaults = {"output_format": "gif", "frame_count": config.get("gif_frame_count", 16),
                "frame_duration_ms": config.get("gif_frame_duration_ms", 200)}
    result.update(output_options({**defaults, **options}))
    size = config.get("gif_size", "auto")
    if not isinstance(size, str) or size not in GIF_SIZES:
        raise AssetError("invalid_parameters", "GIF 尺寸配置无效。")
    model = config.get("gif_model_name", GIF_MODEL)
    if not isinstance(model, str) or not model.strip():
        raise AssetError("invalid_parameters", "请先配置支持原生 GIF 的图片模型。")
    result.update(size=size, model=model.strip())
    return result
