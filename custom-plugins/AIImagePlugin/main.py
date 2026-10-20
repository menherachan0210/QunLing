from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass
from urllib.parse import urlsplit
from types import SimpleNamespace

from langbot_plugin.api.definition.plugin import BasePlugin

DEFAULT_BASE_URL = "https://ai.qlht.uk/v1"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_PROMPT_CHARS = 8000
QUALITY_VALUES = {"auto", "low", "medium", "high", "xhigh", "max"}


class ImageGenerationError(Exception):
    """An actionable message safe to show to the user or model."""


@dataclass(frozen=True)
class GeneratedImage:
    base64: str | None = None
    url: str | None = None


def validate_prompt(prompt: str) -> str:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ImageGenerationError("请提供要生成的图片描述。")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ImageGenerationError("图片描述过长，请限制在 8000 字以内。")
    return prompt.strip()


def _timeout(config: dict) -> float:
    try:
        timeout = float(config.get("request_timeout_seconds", 300))
    except (TypeError, ValueError):
        raise ImageGenerationError(
            "图片生成超时配置无效，请设置为 30–300 秒。"
        ) from None
    if not 30 <= timeout <= 300:
        raise ImageGenerationError("图片生成超时配置无效，请设置为 30–300 秒。")
    return timeout


def _image_result(response) -> GeneratedImage:
    if not response.data:
        raise ImageGenerationError("生成服务没有返回图片，请检查服务状态。")
    item = response.data[0]
    payload = getattr(item, "b64_json", None)
    if payload:
        if (
            not isinstance(payload, str)
            or len(payload) > ((MAX_IMAGE_BYTES + 2) // 3) * 4
        ):
            raise ImageGenerationError("生成图片超过 10 MiB，无法发送。")
        try:
            raw = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError):
            raise ImageGenerationError("生成服务返回了无效的图片数据。") from None
        if len(raw) > MAX_IMAGE_BYTES:
            raise ImageGenerationError("生成图片超过 10 MiB，无法发送。")
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif raw.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif raw.startswith((b"GIF87a", b"GIF89a")):
            mime = "image/gif"
        elif raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
            mime = "image/webp"
        else:
            raise ImageGenerationError("生成服务返回了不支持的图片格式。")
        return GeneratedImage(base64=f"data:{mime};base64,{payload}")
    image_url = getattr(item, "url", None)
    if isinstance(image_url, str):
        try:
            parsed = urlsplit(image_url)
            valid = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.netloc)
                and not parsed.username
            )
        except ValueError:
            valid = False
        if valid:
            return GeneratedImage(url=image_url)
    raise ImageGenerationError("生成服务没有返回可用的图片数据或下载链接。")


async def image_data_url(image: GeneratedImage) -> str:
    """Normalize one trusted message image without sending API credentials to its URL."""
    if image.base64:
        payload = image.base64
        if payload.startswith("data:"):
            prefix, separator, payload = payload.partition(",")
            if not separator or ";base64" not in prefix:
                raise ImageGenerationError("参考图片数据格式无效，请重新上传。")
        return _image_result(
            SimpleNamespace(data=[SimpleNamespace(b64_json=payload)])
        ).base64
    if not image.url:
        raise ImageGenerationError("请先发送或引用要修改的图片。")
    _image_result(SimpleNamespace(data=[SimpleNamespace(url=image.url)]))
    import httpx

    async with httpx.AsyncClient(
        timeout=20.0, follow_redirects=True, trust_env=False
    ) as client:
        async with client.stream("GET", image.url) as response:
            response.raise_for_status()
            if int(response.headers.get("content-length", 0)) > MAX_IMAGE_BYTES:
                raise ImageGenerationError("参考图片超过 10 MiB，请上传更小的图片。")
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_IMAGE_BYTES:
                    raise ImageGenerationError(
                        "参考图片超过 10 MiB，请上传更小的图片。"
                    )
    payload = base64.b64encode(raw).decode("ascii")
    return _image_result(
        SimpleNamespace(data=[SimpleNamespace(b64_json=payload)])
    ).base64


class AIImagePlugin(BasePlugin):
    """Read all credentials and settings per invocation; never cache clients."""

    async def on_installation_revoked(self, binding) -> None:
        from pkg.image_context import revoke_installation

        revoke_installation(binding)

    def create_client(self):
        config = self.get_config()
        api_key = str(config.get("openai_api_key", "") or "")
        if not api_key:
            return None
        from openai import AsyncOpenAI

        return AsyncOpenAI(
            api_key=api_key,
            base_url=str(
                config.get("api_base_url", DEFAULT_BASE_URL) or DEFAULT_BASE_URL
            ),
            timeout=_timeout(config),
            max_retries=0,
        )

    async def generate_image(self, prompt: str) -> GeneratedImage:
        return await self._request_image(prompt)

    async def edit_image(self, prompt: str, image: GeneratedImage) -> GeneratedImage:
        return await self._request_image(prompt, image=image)

    async def _request_image(
        self, prompt: str, image: GeneratedImage | None = None
    ) -> GeneratedImage:
        prompt = validate_prompt(prompt)
        config = self.get_config()
        timeout = _timeout(config)
        quality = config.get("quality", "auto")
        if quality not in QUALITY_VALUES:
            raise ImageGenerationError("图片质量配置无效，请检查插件设置。")
        model = str(config.get("model_name", "gpt-image-2.5-sunburst") or "").strip()
        if not model:
            raise ImageGenerationError("请先配置图片生成模型。")
        client = None
        try:
            client = self.create_client()
            if client is None:
                raise ImageGenerationError(
                    "未配置图片生成 API Key，请先在插件设置中配置。"
                )

            async def request():
                body = {
                    "model": model,
                    "prompt": prompt,
                    "size": config.get("image_size", "1024x1024"),
                    "quality": quality,
                    "n": 1,
                    "response_format": "b64_json",
                }
                if image is None:
                    return await client.images.generate(**body)
                from openai.types import ImagesResponse

                body["images"] = [{"image_url": await image_data_url(image)}]
                return await client.post(
                    "/images/edits", cast_to=ImagesResponse, body=body
                )

            response = await asyncio.wait_for(request(), timeout=timeout)
            return _image_result(response)
        except ImageGenerationError:
            raise
        except TimeoutError:
            raise ImageGenerationError(
                "图片生成超时，已停止等待且未自动重试；上游可能仍在生成或计费。"
            ) from None
        except Exception as exc:
            # Never expose provider response bodies, URLs, API keys, or image bytes.
            if type(exc).__name__ == "APITimeoutError":
                raise ImageGenerationError(
                    "图片生成超时，已停止等待且未自动重试；上游可能仍在生成或计费。"
                ) from None
            raise ImageGenerationError(
                "图片生成请求失败，未自动重试；请检查生成服务与插件配置。"
            ) from None
        finally:
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass
