import base64
import io

import pytest
from PIL import Image

from qunling.native_gif import inspect_gif
from qunling.vision_input import inspect_generated_gif

pytestmark = [pytest.mark.sdk, pytest.mark.usefixtures('available_file_resources')]


def gif_data(frames=16):
    images = [Image.new('RGB', (32, 32), (i * 12, 90, 140)) for i in range(frames)]
    stream = io.BytesIO()
    images[0].save(stream, format='GIF', save_all=True, append_images=images[1:],
                   duration=[100 + i * 10 for i in range(frames)], loop=0)
    for image in images:
        image.close()
    return 'data:image/gif;base64,' + base64.b64encode(stream.getvalue()).decode()


@pytest.mark.asyncio
async def test_native_sixteen_frames_validate_in_isolated_worker_without_reencoding():
    source = gif_data()
    metadata = await inspect_generated_gif(source)
    assert metadata == inspect_gif(source)
    assert metadata['frame_count'] == 16 and metadata['loop'] == 0
    assert metadata['frame_durations_ms'] == list(range(100, 260, 10))
    assert metadata['duration_ms'] == 2800
    assert metadata['width'] == metadata['height'] == 32


@pytest.mark.asyncio
@pytest.mark.parametrize('value', [gif_data(1), gif_data(17), 'data:image/gif;base64,R0lGODlhAA=='])
async def test_native_decoder_rejects_still_excess_frames_and_corruption(value):
    with pytest.raises(ValueError):
        await inspect_generated_gif(value)
