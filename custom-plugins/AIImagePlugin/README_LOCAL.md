# 本地图片生成和编辑扩展

基于官方 AIImagePlugin 0.1.6（`upstream-source.json` 记录精确版本）定制。该目录不包含密钥，也未向插件市场发布。

## 使用

- 私聊中用自然语言表达生成新图或修改图片的意图；不要求固定句式。
- 群聊仍需遵守现有触发规则，例如先 @ 机器人或使用 `/ai`。
- 新图由 `generate_image` 工具生成；修改图由 `edit_image` 工具编辑。
- 修改图优先使用本条消息或引用消息中的单张图片；否则使用同一机器人、会话和发送者最近的图（用户上传或本插件已发出的结果）。
- 本轮有多张图且目标不明确时，需要用户引用或重新发送目标图。暂不支持多图合成。
- 引用内容无法读取时不会回退旧图；请重新引用或补发。若本条消息直接附有唯一图片，则优先使用这张直接附图。
- 缓存只覆盖进入本流水线的消息；群里未触发过机器人的图片请直接引用。
- 图片直接回复原会话，模型只获得操作状态；不以 Markdown 图片链接代替回图。
- 保留 `!draw <描述>` 命令，其触发仍受现有流水线规则约束。

## 配置

| 字段 | 本地默认值 |
| --- | --- |
| `api_base_url` | `https://ai.qlht.uk/v1` |
| `openai_api_key` | 留空；在插件设置中填写，使用 secret 输入框 |
| `model_name` | `gpt-image-2.5-sunburst`，可自由填写 |
| `image_size` | `1024x1024` |
| `quality` | `auto` |
| `request_timeout_seconds` | `300`，允许 30–300 秒 |

生成使用 `/images/generations`；编辑使用 `/images/edits` JSON `images:[{image_url:dataURI}]`。每个请求只生成一张，优先读取 `b64_json`，也兼容服务返回 HTTP(S) 图片 URL。

## 生命周期与限制

- API 无自动重试；生成/编辑工具对同一用户消息共用一次调用标记，防止模型重复调用导致重复计费。超时不会声称上游已取消，服务可能仍在生成或计费。
- 单张图片上限 10 MiB，为 Base64 编码和 Runtime 16 MiB 消息封装预留空间。下载参考图单独设置超时、总字节限制，不向图片 URL 附加 API 密钥。
- 短期图像缓存按可信安装、工作区、机器人、会话和发送者隔离，TTL 30 分钟，最多 32 个 scope、总 64 MiB。每个 scope 只保留最新图，进程重启或过期后请补发/引用图片。
- Query 变量只含 opaque 引用和状态，图片内容不进工具参数、模型工具结果或长期记忆。
- 不在插件代码、异常文字或日志中保存密钥或完整上游错误响应。不要开启 Host 原始 MessageChain 的 DEBUG 日志。
- 这里只支持单张图的文字指令修改，不包含画笔蒙版或多图合成 UI。

## 本地验证与构建

使用部署自带的虚拟环境运行：

```powershell
& '..\..\langbot\.venv\Scripts\python.exe' -m pytest tests -q
& '..\..\langbot\.venv\Scripts\python.exe' -m langbot_plugin.cli build -o dist
```

测试使用 mock，不调用图片 API、不向 QQ 发送消息。构建的 `.lbpkg` 可通过 LangBot 插件页的“本地安装”导入，再填写插件配置并给流水线授权两个工具。
