import base64
import io
import struct

from PIL import Image, PngImagePlugin
import pytest

from qunling.image_header import Headers, static_image


def encoded(fmt='PNG', *, animated=False, lossless=False, metadata=False):
    output = io.BytesIO()
    with Image.new('RGB', (201, 203), 'red') as first, Image.new('RGB', (201, 203), 'blue') as second:
        options = {'save_all': True, 'append_images': [second], 'duration': 100} if animated else {}
        if fmt == 'WEBP':
            options['lossless'] = lossless
        if metadata:
            info = PngImagePlugin.PngInfo()
            info.add_text('example', 'acTL ANIM ANMF are text, not chunk types. ' + 'x' * 500_000)
            options['pnginfo'] = info
        first.save(output, format=fmt, **options)
    return base64.b64encode(output.getvalue()).decode()


@pytest.mark.parametrize('fmt,animated,lossless', [
    ('PNG', False, False), ('PNG', True, False),
    ('WEBP', False, False), ('WEBP', False, True), ('WEBP', True, False),
    ('GIF', True, False),
])
def test_animation_controls_remain_on_the_decoder_path(fmt, animated, lossless):
    value = encoded(fmt, animated=animated, lossless=lossless)
    for item in (value, f'data:image/{fmt.lower()};base64,' + value):
        assert static_image(item) is (not animated)


def test_large_png_metadata_is_skipped_without_decoding_or_copying_the_body(monkeypatch):
    value = encoded(metadata=True)
    original = base64.b64decode
    sizes = []
    def read(chunk, **kwargs):
        sizes.append(len(chunk))
        return original(chunk, **kwargs)
    monkeypatch.setattr(base64, 'b64decode', read)
    assert static_image(value)
    assert max(sizes) <= 40 and sum(sizes) < 256


@pytest.mark.parametrize('offset', range(12))
def test_base64_header_ranges_respect_all_byte_alignments(offset):
    raw = bytes(range(64))
    assert Headers(base64.b64encode(raw).decode()).read(offset, 9) == raw[offset:offset + 9]


def test_impossible_dimensions_and_truncated_chunks_are_not_forwarded_as_static():
    raw = bytearray(base64.b64decode(encoded()))
    raw[16:24] = struct.pack('>II', 65535, 65535)
    with pytest.raises(ValueError, match='pixel budget'):
        static_image(base64.b64encode(raw).decode())
    raw = bytearray(base64.b64decode(encoded()))
    raw[33:37] = b'\x7f\xff\xff\xff'
    with pytest.raises(ValueError, match='Truncated'):
        static_image(base64.b64encode(raw).decode())


def test_chunk_scan_is_bounded_for_unusual_containers(monkeypatch):
    import qunling.image_header as module
    monkeypatch.setattr(module, 'MAX_CHUNKS', 1)
    assert static_image(encoded(metadata=True)) is False
