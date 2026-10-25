import base64
import io

import pytest

pytestmark = pytest.mark.sdk
from PIL import Image

from qunling.vision_input import animation_data_url, model_image_content


def animated_url():
    frames = [Image.new("RGB", (32, 32), color) for color in ("red", "blue", "green")]
    output = io.BytesIO()
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], duration=[100, 200, 300])
    return "data:image/gif;base64," + base64.b64encode(output.getvalue()).decode()


def test_animation_model_input_contains_chronological_frames_not_raw_gif():
    original = animated_url()
    result, animated = animation_data_url(original)
    assert animated and result.startswith("data:image/png;base64,")
    with Image.open(io.BytesIO(base64.b64decode(result.split(",")[1]))) as sheet:
        colors = sheet.convert("RGB").getdata()
        assert (255, 0, 0) in colors and (0, 0, 255) in colors and (0, 128, 0) in colors
        assert sheet.width == 900
    assert original == animated_url()


@pytest.mark.asyncio
async def test_bad_animation_is_a_placeholder_not_a_failed_chat_turn():
    value = "data:image/gif;base64," + base64.b64encode(b"GIF89a corrupt").decode()
    result = await model_image_content(value)
    assert result.type == "text" and "读取失败" in result.text


@pytest.mark.asyncio
async def test_host_input_accepts_both_plain_and_data_uri_gif():
    original = animated_url()
    for value in [original, original.split(",")[1]]:
        result = await model_image_content(value)
        assert result.type == "image_base64" and result.image_base64.startswith("data:image/png;base64,")
