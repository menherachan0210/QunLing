# Modified by qq-ai-bot contributors on 2026-10-20; local changes, see NOTICE.
from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass
from urllib.parse import urlsplit
from types import SimpleNamespace

from langbot_plugin.api.definition.plugin import BasePlugin

DEFAULT_BASE_URL = "https://api.openai.com/v1"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_PROMPT_CHARS = 8000
QUALITY_VALUES = {"auto", "low", "medium", "high", "xhigh", "max"}
CONTENT_REFUSAL_CODES = frozenset(
    {
        "content_filter",
        "content_filtered",
        "content_policy_violation",
        "content_policy_rejected",
        "image_generation_content_policy_violation",
        "moderation_blocked",
        "safety_violation",
        "safety_rejection",
    }
)
REFERENCE_TOO_LARGE = "参考图片超过 10 MiB，未提交图片编辑请求；请上传更小的图片。"
REFERENCE_INVALID = "参考图片数据无效或格式不支持，未提交图片编辑请求；请重新上传 PNG、JPEG、GIF 或 WebP 图片。"
PROVIDER_TIMEOUT = (
    "图片生成服务请求超时，已停止等待且未自动重试；上游可能仍在生成或计费。"
)


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


def _base64_data_url(payload: str, *, reference: bool = False) -> str:
    invalid_message = (
        REFERENCE_INVALID if reference else "生成服务返回了无效的图片数据。"
    )
    size_message = (
        REFERENCE_TOO_LARGE if reference else "生成图片超过 10 MiB，无法发送。"
    )
    if not isinstance(payload, str):
        raise ImageGenerationError(invalid_message)
    if len(payload) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
        raise ImageGenerationError(size_message)
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise ImageGenerationError(invalid_message) from None
    if len(raw) > MAX_IMAGE_BYTES:
        raise ImageGenerationError(size_message)
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif raw.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif raw.startswith((b"GIF87a", b"GIF89a")):
        mime = "image/gif"
    elif raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        raise ImageGenerationError(
            REFERENCE_INVALID if reference else "生成服务返回了不支持的图片格式。"
        )
    return f"data:{mime};base64,{payload}"


def _image_result(response) -> GeneratedImage:
    if not response.data:
        raise ImageGenerationError("生成服务没有返回图片，请检查服务状态。")
    item = response.data[0]
    payload = getattr(item, "b64_json", None)
    if payload:
        return GeneratedImage(base64=_base64_data_url(payload))
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


def _content_refused(exc: Exception) -> bool:
    """Only trust exact machine-readable codes, never free-form error text."""
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return False
    error = body.get("error", body)
    if not isinstance(error, dict):
        return False
    return any(
        isinstance(value := error.get(field), str)
        and len(value) <= 80
        and value.strip().lower() in CONTENT_REFUSAL_CODES
        for field in ("code", "type")
    )


def _provider_error(exc: Exception) -> ImageGenerationError:
    from openai import APIConnectionError, APIStatusError, APITimeoutError

    # No exception text, response bodies, headers, URLs, or prompts reach the result.
    if isinstance(exc, (APITimeoutError, TimeoutError)):
        return ImageGenerationError(PROVIDER_TIMEOUT)
    if isinstance(exc, APIConnectionError):
        return ImageGenerationError(
            "图片生成服务连接失败，未自动重试；请检查服务网络。上游是否收到请求尚未确认。"
        )
    if isinstance(exc, APIStatusError):
        status = exc.status_code
        if status == 401:
            message = "图片生成服务认证失败（HTTP 401）；请检查图片插件的 API Key。"
        elif status == 429:
            message = "图片生成服务限流或额度不足（HTTP 429）；请检查服务配额。"
        elif isinstance(status, int) and 500 <= status <= 599:
            message = f"图片生成服务暂时异常（HTTP {status}）；请检查上游服务。"
        elif status in {400, 403, 422} and _content_refused(exc):
            message = f"图片生成服务明确拒绝了本次内容（HTTP {status}，内容审核拒绝）。"
        elif status == 403:
            message = "图片生成服务拒绝访问（HTTP 403）；请检查账号或模型权限。"
        elif status == 404:
            message = (
                "图片生成服务接口或模型不存在（HTTP 404）；请检查接口地址及模型名。"
            )
        elif status == 413:
            message = "图片生成服务拒绝了过大的请求（HTTP 413）；请缩小参考图片。"
        elif status in {400, 422}:
            message = (
                f"图片生成服务不接受本次请求（HTTP {status}）；"
                "请检查模型、图片格式及接口参数兼容性。"
            )
        elif isinstance(status, int) and 100 <= status <= 599:
            message = f"图片生成服务返回 HTTP {status}；请检查上游服务与插件配置。"
        else:
            message = "图片生成服务返回异常状态；请检查上游服务与插件配置。"
        return ImageGenerationError(message + " 未自动重试。")
    return ImageGenerationError(
        "图片生成请求失败，未自动重试；请检查生成服务与插件配置。"
    )


async def image_data_url(image: GeneratedImage) -> str:
    """Normalize one trusted message image without sending API credentials to its URL."""
    if image.base64:
        payload = image.base64
        if not isinstance(payload, str):
            raise ImageGenerationError(REFERENCE_INVALID)
        if payload.startswith("data:"):
            prefix, separator, payload = payload.partition(",")
            if not separator or ";base64" not in prefix:
                raise ImageGenerationError(REFERENCE_INVALID)
        return _base64_data_url(payload, reference=True)
    if not image.url:
        raise ImageGenerationError("请先发送或引用要修改的图片。")
    try:
        _image_result(SimpleNamespace(data=[SimpleNamespace(url=image.url)]))
    except ImageGenerationError:
        raise ImageGenerationError(
            "参考图片链接无效，未提交图片编辑请求；请重新上传或引用图片。"
        ) from None
    from pkg.remote_image import RemoteImageError, fetch_reference_image

    try:
        raw = await fetch_reference_image(image.url)
    except RemoteImageError as exc:
        if exc.code == "http_status":
            message = f"参考图片下载失败（HTTP {exc.status}），未提交图片编辑请求且未自动重试；请重新上传或引用图片。"
        else:
            message = {
                "timeout": "参考图片下载超时，未提交图片编辑请求且未自动重试；请重新上传或引用图片。",
                "network": "参考图片下载网络失败，未提交图片编辑请求且未自动重试；请检查图片网络或重新上传图片。",
                "invalid_url": "参考图片链接无效或不是公共网络地址，未提交图片编辑请求；请重新上传或引用图片。",
                "invalid_response": "参考图片下载响应无效，未提交图片编辑请求；请重新上传或引用图片。",
                "too_large": REFERENCE_TOO_LARGE,
                "invalid_body": REFERENCE_INVALID,
            }.get(
                exc.code,
                "参考图片读取失败，未提交图片编辑请求且未自动重试；请重新上传或引用图片。",
            )
        raise ImageGenerationError(message) from None
    except ImageGenerationError:
        raise
    except Exception:
        raise ImageGenerationError(
            "参考图片读取失败，未提交图片编辑请求且未自动重试；请重新上传或引用图片。"
        ) from None
    payload = base64.b64encode(raw).decode("ascii")
    return _base64_data_url(payload, reference=True)


class AIImagePlugin(BasePlugin):
    """Read all credentials and settings per invocation; never cache clients."""

    async def on_installation_revoked(self, binding) -> None:
        from pkg.image_context import _binding_owner, revoke_installation
        from pkg.edit_references import STORE

        revoke_installation(binding)
        STORE.revoke(_binding_owner(binding))

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

    async def edit_image(
        self, prompt: str, image: GeneratedImage | list[GeneratedImage]
    ) -> GeneratedImage:
        return await self._request_image(prompt, image=image)

    async def _request_image(
        self, prompt: str, image: GeneratedImage | list[GeneratedImage] | None = None
    ) -> GeneratedImage:
        prompt = validate_prompt(prompt)
        references = None
        if image is not None:
            references = image if isinstance(image, list) else [image]
            if not 1 <= len(references) <= 4 or any(
                not isinstance(item, GeneratedImage) for item in references
            ):
                raise ImageGenerationError(
                    "一次编辑请选择 1–4 张有效参考图片；未提交生成请求。"
                )
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
                if references is None:
                    return await client.images.generate(**body)
                from openai.types import ImagesResponse

                # Fetch every source before the first paid POST. Never silently
                # degrade a two-image request to whichever source was readable.
                try:
                    async with asyncio.timeout(25):
                        data_urls = await asyncio.gather(
                            *(image_data_url(reference) for reference in references),
                            return_exceptions=True,
                        )
                except TimeoutError:
                    raise ImageGenerationError(
                        "参考图片读取超时，尚未提交图片编辑请求；没有使用剩余图片替代。"
                    ) from None
                for value in data_urls:
                    if isinstance(value, ImageGenerationError):
                        raise value
                    if isinstance(value, BaseException):
                        raise ImageGenerationError(
                            "所选参考图片中有图片读取失败，尚未提交图片编辑请求；没有使用剩余图片替代。"
                        )
                if sum(len(value) for value in data_urls) > 40 * 1024 * 1024:
                    raise ImageGenerationError(
                        "多图参考总大小超过限制，未提交图片编辑请求；请缩小图片。"
                    )
                from qunling.vision_input import animation_data_url
                try:
                    from qunling.resource_gate import finish_thread
                    normalized = [await finish_thread(animation_data_url, value) for value in data_urls]
                except (ValueError, OSError):
                    raise ImageGenerationError("参考动图无法安全读取关键帧，尚未提交编辑请求。") from None
                data_urls = [value for value, _ in normalized]
                if any(animated for _, animated in normalized):
                    body["prompt"] += ("\n部分参考图是带 Frame/时间标记的原动图关键帧采样。"
                                       "按时间顺序参考动作、主体和构图；时间标签不是原图内容，不画进成品。"
                                       "若要求输出多帧，遵循输出分镜布局，不能把参考采样拼图直接当成最终一帧。")
                body["images"] = [{"image_url": value} for value in data_urls]
                return await client.post(
                    "/images/edits", cast_to=ImagesResponse, body=body
                )

            from qunling.resource_gate import ResourceBusy, resource_slot
            try:
                async with resource_slot("image"):
                    response = await asyncio.wait_for(request(), timeout=timeout)
            except ResourceBusy:
                raise ImageGenerationError("图片任务排队超时，尚未提交生成请求；稍后再试。") from None
            return _image_result(response)
        except ImageGenerationError:
            raise
        except Exception as exc:
            raise _provider_error(exc) from None
        finally:
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass
