"""Turn one generated storyboard into reusable, scoped frames and a GIF job."""
from __future__ import annotations

import asyncio
import io

from qunling.assets import AssetError, AssetStore, data_root
from qunling.integration import task_hint
from qunling.jobs import JobService
from qunling.scope import Scope


def frame_plan(params):
    if "gif_frames" not in params:
        if "frame_duration_ms" in params:
            raise AssetError("invalid_parameters", "frame_duration_ms 需要同时提供 gif_frames。")
        return None
    frames = params["gif_frames"]
    duration = params.get("frame_duration_ms", 350)
    if (not isinstance(frames, list) or not 2 <= len(frames) <= 6
            or any(not isinstance(frame, str) or not frame.strip() or len(frame) > 300 for frame in frames)
            or type(duration) is not int or not 100 <= duration <= 1500):
        raise AssetError("invalid_parameters", "gif_frames 需要 2 至 6 段动作描述，每段最多 300 字；每帧停留 100 至 1500 毫秒。")
    return {"frames": [frame.strip() for frame in frames], "frame_duration_ms": duration,
            "grid": 2 if len(frames) <= 4 else 3}


def storyboard_prompt(prompt, plan):
    if plan is None:
        return prompt.strip()
    grid = plan["grid"]
    text = (
        f"Create ONE animation sprite sheet in an exact {grid} by {grid} equal-cell grid. "
        "No gutters, borders, labels, captions or panel numbers. Each cell is a full animation frame. "
        "Keep the same character identity, proportions, palette, camera, scale and background across cells. "
        "Change only the specified pose/expression. Keep the subject inside its own cell. "
        "Frames are read LEFT TO RIGHT, then TOP TO BOTTOM. Leave unused final cells blank. "
        "Make the final used pose transition naturally back to the first for a looping GIF.\n"
        f"Shared subject and style: {prompt.strip()}\n"
        + "\n".join(f"Frame {i + 1}: {frame}" for i, frame in enumerate(plan["frames"]))
    )
    if len(text) > 8000:
        raise AssetError("invalid_parameters", "分镜说明过长，请缩短共同描述后继续；尚未提交生图请求。")
    return text


def save_frames(store, scope, context, raw, plan):
    """Save the original first, so a later decode/storage failure never loses it."""
    from PIL import Image

    saved, frame_refs = [], []
    result = {"saved_refs": saved, "frame_refs": frame_refs}
    try:
        if not isinstance(raw, bytes) or not raw or len(raw) > 10 * 1024 * 1024:
            raise AssetError("size_limit", "分镜图片超过处理上限。")
        extension = "png" if raw.startswith(b"\x89PNG") else "jpg" if raw.startswith(b"\xff\xd8") else "webp"

        def put(name, content, metadata):
            item = store.put(scope, name, content, origin="generated", metadata=metadata)
            saved.append(item["asset_ref"])
            store.grant(scope, context["request_key"], item["asset_ref"])
            return item["asset_ref"]

        result["storyboard_ref"] = put("AI分镜." + extension, raw, {"ai_frame_plan": plan})
        with Image.open(io.BytesIO(raw)) as source:
            grid = plan["grid"]
            if (source.format not in {"PNG", "JPEG", "WEBP"} or getattr(source, "is_animated", False)
                    or source.width * source.height > 12_000_000
                    or min(source.size) < 32 * grid):
                raise AssetError("invalid_image", "分镜需要尺寸适当的静态 PNG、JPEG 或 WebP 图片。")
            source.load()
            for index in range(len(plan["frames"])):
                row, column = divmod(index, grid)
                box = (column * source.width // grid, row * source.height // grid,
                       (column + 1) * source.width // grid, (row + 1) * source.height // grid)
                with source.crop(box).convert("RGBA") as cell:
                    cell.thumbnail((512, 512), Image.Resampling.LANCZOS)
                    if min(cell.size) < 32:
                        raise AssetError("invalid_image", "分镜比例过窄，无法制作可读的动画。")
                    out = io.BytesIO()
                    cell.save(out, format="PNG")
                    frame_refs.append(put(f"AI动画帧-{index + 1}.png", out.getvalue(), {
                        "storyboard_ref": result["storyboard_ref"], "frame_index": index + 1,
                    }))
                    width, height = cell.size
        scale = min(384 / width, 384 / height, 1)
        result["recipe"] = {"width": max(32, round(width * scale)), "height": max(32, round(height * scale)),
            "fps": 1, "loop": 0, "frames": [
                {"asset_ref": ref, "duration_ms": plan["frame_duration_ms"]} for ref in frame_refs]}
        return result
    except Exception as exc:
        return {**result, "status": "partial", "error_code": getattr(exc, "code", "invalid_image"),
                "error": "AI 分镜已返回，但拆帧未完成；saved_refs 是已保留的素材，不要再次调用生图。"}


async def assemble(context, raw, plan):
    store, scope = AssetStore.from_env(), Scope(**context["scope"])
    assets = await asyncio.to_thread(save_frames, store, scope, context, raw, plan)
    if "recipe" not in assets:
        return assets
    jobs = JobService(data_root())
    task_id = None
    try:
        submitted = jobs.submit(scope, "create", {
            "format": "gif", "filename": "AI表情动画.gif", "payload": assets["recipe"],
        }, context["request_key"])
        task_id = submitted.get("task_id", submitted.get("id"))
        if plan.get("deliver", True):
            jobs.attach_delivery(scope, task_id, context["request_key"], context.get("host_route"))
        value = await jobs.wait(scope, task_id, seconds=15,
                                before=context["created"], request_key=context["request_key"])
        for ref in value.get("result_refs", []):
            store.grant(scope, context["request_key"], ref)
    except Exception:
        return {**assets, "status": "partial", "task_id": task_id,
                "error": "分镜和各帧已保留，动画处理或交付尚未确认；不要重新生图。",
                "next_step": ("先用 file_task 查询 task_id，确认已有任务和交付状态，不要重复提交。"
                              if task_id else "可使用 gif_animation 和返回的 recipe 完成合成，无需重新生图。")}
    value = {**assets, **value, "next_step": (
        "使用 file_task 查看任务；queued/started 表示等待交付，sent 表示已发送，不能重复发送。"
        "后续使用 gif_animation 调整速度、顺序或字幕，复用原图。分镜提示不代表实际画面已通过视觉检查。")}
    if value.get("status") in {"pending", "running", "waiting"}:
        value["_qunling_task"] = task_hint(status=value["status"], task_id=task_id)
    if not plan.get("deliver", True):
        value["next_step"] = "这是中间动画，尚未发送；用 file_task 等待完成，再把 result_refs 交给其他工具加工或打包。不要重复生图。"
        value["_qunling_task"] = task_hint(status="pending", task_id=task_id)
    return value
