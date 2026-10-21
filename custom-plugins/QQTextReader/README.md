# QQTextReader

本地 LangBot 插件 `local/QQTextReader`，版本 `0.2.1`。提供共享文件工作区和旧 TXT 快捷读取入口。

## 共享文件工作区

通过 `file_workspace` 导入附件、分页读取、搜索、提取压缩包成员或生成 TXT、CSV、DOCX、XLSX、PPTX、ZIP 等文件；`file_task` 查询后台任务，`send_attachment` 交付真实文件。使用标准部署时自动安装解析依赖并启动后台任务，无需另配服务。

ZIP／RAR／7z 可以传入用户提供的 `arguments.password`，供 inspect/read/search/extract 使用。密码不进入持久任务文件，受密码保护的操作在当前调用中完成；缺少或错误密码返回明确提示，补正后可重试。普通任务仍可后台执行并通过 outbox 自动发送。

完整能力与限制见 [功能说明](../../docs/FEATURES.md)。以下说明针对兼容保留的 `read_text_attachment({})` 快捷入口。

## TXT 快捷入口使用

在绑定了本插件的流水线中上传 TXT，再请求机器人阅读、校对、翻译或总结。
QQ群继续使用现有 `@机器人` 或 `/ai` 入口；上传附件本身不会触发本插件回复。
工具选择当前直接附件，其次有效引用附件，再选择同机器人、同会话、同发送者近期 TXT。
本条消息有多个候选文件、引用无法确认、文件不支持或缓存过期时，请单独补发或重新引用。
当前直接附带一个文件时优先使用它，不使用同时出现的引用作为替代目标。

只支持 TXT；不会执行附件中的脚本、命令或 `SKILL.md`。正文作为工具资料返回，
不写入系统提示。系统提示只声明附件是否可读，要求模型按用户请求读取并检查 `complete`。
插件不主动发送 QQ 消息，不修改文件，不调用 AI，不回放安装前的消息。
安装、升级或服务重启后缓存为空，需要重新上传或引用。

## 安装和配置

1. 通过 LangBot 本地插件上传安装 `dist/local-QQTextReader-0.1.0.lbpkg`。
2. 将插件绑定到使用的流水线。插件配置为空 `{}`，不需要任何凭据。
3. 确认模型可用工具中包含 `read_text_attachment`。
4. 可额外创建事件处理器：`kind=event_processor`，
   `component_ref=plugin:local/QQTextReader/group_upload`，必须显式提供 `parameters: {}`。
   在机器人的 `plugin_processors` 中追加该处理器 `{processor_uuid: ..., enabled: true}`。
   保留原 `event_bindings`。此处理器仅捕获 OneBot `notice.group_upload` 元数据，
   不调用模型或平台 API，不发送回复。NapCat 普通文件消息也能被消息监听器捕获。
5. 当文件没有现成 URL 时，Host 的 OneBot 平台动作需支持以下两个只读接口：

   - `get_group_file_url`，参数为可信缓存中的 `group_id` 与原始 `file_id`。
   - `get_private_file_url`，参数仅为可信缓存中的原始 `file_id`。

   插件调用 `call_platform_api(bot_uuid, "call_platform_api", {"action": ..., "params": ...})`，
   期望返回 `{"url": "https://..."}`。文件 ID 不应解码为裸 UUID。

## 完整性与边界

- 临时缓存按安装、Workspace、机器人、会话、发送者隔离；30 分钟 TTL，最多 64 条、
  总量 8 MiB。共享运行还隔离安装版本和 Workspace 执行代数。
  相同文件 ID 的重复事件不延长 TTL，已捕获的查询引用不会因其他文件到达而立即失效。
- 通常仅缓存文件元数据；也支持当前/引用 `File.base64` 作为本地调试与其他平台附件输入，
  支持裸 base64、`data:...;base64,`、`base64://`。内联数据同样有大小及缓存总量限制。
- 文件最多 128 KiB，正文最多 16,000 字符。读取结果 JSON 序列化后最多 19,000 字符，
  低于当前 Host 的 20,000 字符工具结果限制。任何超限均明确失败，不截断后声称完整。
- 支持 UTF-8、UTF-8 BOM、带 BOM 的 UTF-16、GB18030。全部严格解码，保留换行及标点；
  拒绝明显二进制、非法编码和文本中的非空白控制字符。不确定编码时请另存为 UTF-8。
- 已知附件大小必须与读到的实际字节数相符；空文本也会如实返回。
- 工具没有参数，模型不能指定 URL、路径、群号、文件 ID 或发送者。
- 只允许公共 HTTP(S) 地址及 80/443 端口。DNS 结果先校验公网地址，再连接校验过的同一地址，
  防止 DNS 重绑定；每次重定向重新校验，最多 3 次。拒绝内网、环回、链路本地、非标准数字 IP、
  URL 用户名密码、本地路径和文件协议。不会读取 `File.path`。
- 不携带 API 密钥、Cookie 或环境代理。请求 `Accept-Encoding: identity`、禁用自动解压，
  拒绝压缩响应；声明大小和实际流式字节均检查。整个工具调用包括 Host 地址解析最多 30 秒。
- 不记录正文、base64、签名 URL、QQ 标识或上游异常内容；模型只得到文件名、编码、正文和
  `complete=true`，失败为 `complete=false` 和固定说明。

文件正文在用户请求读取之后会进入当前模型上下文及 Host 原有工具记录；插件不会额外持久化正文。

## 离线验证与重建

使用当前 LangBot 虚拟环境（已实测 Python 3.12、langbot-plugin 0.7.11、aiohttp 3.14.1）：

```powershell
Set-Location .\custom-plugins\QQTextReader
& '..\..\langbot\.venv\Scripts\python.exe' -m pytest tests -q
& '..\..\langbot\.venv\Scripts\python.exe' -m ruff check .
& '..\..\langbot\.venv\Scripts\python.exe' -m langbot_plugin.cli build -o dist
```

测试只使用合成附件和模拟 Host/HTTP，不发送 QQ 消息、不下载用户文件、不调用模型。
