import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from langbot_plugin.api.entities.builtin.platform.message import Image, MessageChain, Plain, Quote
from langbot.pkg.platform.adapters.aiocqhttp.message_converter import AiocqhttpMessageConverter
from qunling import incoming_media


def test_quoted_pixels_are_not_chat_text_but_stay_on_the_wire():
    pixels = "private-pixels" * 400000
    photo = Image(base64=pixels, url="https://example.test/private?key=private")
    quote = Quote(id="original", origin=MessageChain([Plain(text="改这张"), photo]))
    assert str(quote) == "[Quote: 改这张[Image]]"
    assert repr(quote) == str(quote)
    assert "private" not in str(MessageChain([quote, Plain(text="稍微亮点")]))
    assert quote.model_dump()["origin"][1]["base64"] == pixels


def test_quote_rendering_bounds_text_and_nested_quotes():
    nested = Quote(origin=MessageChain([Plain(text="x" * 1000000)]))
    quote = Quote(origin=MessageChain([nested, Image(base64="private")]))
    assert str(quote) == "[Quote: [Quote][Image]]"
    assert len(str(nested)) < 4200


@pytest.mark.asyncio
@pytest.mark.parametrize("quoted", [False, True])
async def test_group_ingress_keeps_references_without_downloading(monkeypatch, quoted):
    monkeypatch.setenv("QUNLING_DATA_DIR", "/private/test")
    download = AsyncMock(return_value=("pixels", "png"))
    monkeypatch.setattr("langbot.pkg.utils.image.qq_image_url_to_base64", download)
    bot = SimpleNamespace(get_msg=AsyncMock(return_value={"message_id": 11,
        "message": [{"type": "image", "data": {"url": "https://example.test/image.png", "file": "image-id"}}]}))
    segments = ([{"type": "reply", "data": {"id": "11"}}] if quoted else
                [{"type": "image", "data": {"url": "https://example.test/image.png", "file": "image-id"}}])
    chain = await AiocqhttpMessageConverter.target2yiri(segments, bot=bot)
    photo = chain[1].origin[1] if quoted else chain[1]
    assert photo.url == "https://example.test/image.png" and not photo.base64
    download.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("quoted", [False, True])
async def test_admitted_vision_turn_loads_a_copy_and_preserves_edit_reference(monkeypatch, quoted):
    download = AsyncMock(return_value=("pixels", "png"))
    monkeypatch.setattr("langbot.pkg.utils.image.qq_image_url_to_base64", download)
    photo = Image(url="https://example.test/image.png")
    chain = MessageChain([Quote(id="11", origin=MessageChain([photo])) if quoted else photo])
    result = await incoming_media.model_message_chain(chain, True, True)
    rendered = result[0].origin[0] if quoted else result[0]
    assert rendered.base64 == "data:image/png;base64,pixels"
    assert photo.base64 == "" and photo.url == rendered.url
    assert chain.model_dump()[0] != result.model_dump()[0]


@pytest.mark.asyncio
async def test_unselected_and_text_only_turns_do_not_download(monkeypatch):
    download = AsyncMock()
    monkeypatch.setattr("langbot.pkg.utils.image.qq_image_url_to_base64", download)
    chain = MessageChain([Quote(origin=MessageChain([Image(url="https://example.test/image.png")]))])
    assert await incoming_media.model_message_chain(chain, False, True) is chain
    await incoming_media.model_message_chain(chain, True, False)
    download.assert_not_awaited()


@pytest.mark.asyncio
async def test_image_budget_bounds_payload_without_mutating_originals(monkeypatch):
    monkeypatch.setattr(incoming_media, "MAX_MODEL_IMAGE_CHARS", 10)
    images = [Image(base64="12345678"), Image(base64="abcdefgh")]
    result = await incoming_media.model_message_chain(MessageChain(images), True, True)
    assert [item.base64 for item in result] == ["12345678", ""]
    assert [item.base64 for item in images] == ["12345678", "abcdefgh"]


@pytest.mark.asyncio
async def test_download_failure_preserves_reference_and_cancellation_propagates(monkeypatch):
    photo = Image(url="https://example.test/private")
    download = AsyncMock(side_effect=TimeoutError)
    monkeypatch.setattr("langbot.pkg.utils.image.qq_image_url_to_base64", download)
    result = await incoming_media.model_message_chain(MessageChain([photo]), True, True)
    assert result[0].url == photo.url and not result[0].base64
    download.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await incoming_media.model_message_chain(MessageChain([photo]), True, True)
