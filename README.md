# 本地 QQ AI 机器人

本目录安装了 NapCat 4.18.38 和 LangBot 4.11.0。NapCat 负责登录 QQ、收发群聊和私聊消息；LangBot 负责调用你的 sub2api、对话上下文、看图和长期记忆。

## 日常使用

- 双击 `Start-Bot.cmd` 启动两个服务，后台运行，启动成功后窗口会关闭。
- 双击 `Open-Bot.cmd` 打开管理页，NapCat 的本地管理密码会自动带入。
- 双击 `Stop-Bot.cmd` 停止本目录管理的进程。
- LangBot 管理页：<http://127.0.0.1:5300/>；初始账号保存在 `.local/langbot-admin.json`。
- NapCat 管理页：<http://127.0.0.1:6099/webui/>；QQ 登录过期时在这里重新扫码。

当前没有设置开机自动启动。关闭管理网页不会停止机器人；电脑关机或双击停止脚本后，机器人离线。

## 当前配置

| 项目 | 设置 |
| --- | --- |
| AI 地址 | `https://ai.qlht.uk/v1`，OpenAI Chat Completions 兼容接口 |
| 默认模型 | `gpt-6.1-sol` |
| 可切换模型 | `gpt-6-astra` |
| 生图与改图模型 | `gpt-image-2.5-sunburst`，同一个 sub2api 服务 |
| 群聊触发 | @ 机器人，或消息以 `/ai` 开头 |
| 私聊 | 直接回复 |
| 图片 | 使用模型的视觉能力，可以随问题一起发送图片 |
| TXT 阅读 | QQTextReader 读取当前、引用或同发送者近期的 TXT 正文，再交给模型分析 |
| 对话上下文 | LocalAgent 保留当前会话上下文 |
| 长期记忆 | LongTermMemory，使用本地 Chroma 保存和检索 |
| 记忆隔离 | 按群聊或私聊会话隔离；同一群内共享该群记忆 |
| 自动后台提取记忆 | 关闭；通过对话中的记忆工具保存、修改和删除 |
| 系统执行能力 | Box 关闭，模型没有本机终端和文件操作工具 |

群聊示例：`@机器人 这张图里有什么？`、`/ai 帮我整理一下这个想法`。

生图与改图按自然语言意图调用工具，没有固定口令。例如 `@机器人 生成一张咖啡店海报`，或附图后说 `@机器人 把背景换成海边`。也可以继续说 `把刚才那张改成漫画风`。仅讨论或描述图片时，不会因为出现“图片”二字就直接生成。

改图优先使用当前附图或引用的图片，再使用同一用户、同一会话最近收到或生成的图片。近期图片仅作内存缓存，30 分钟过期，重启后清空；同时受条目数和容量限制。找不到原图或多图目标不清楚时，需要补发或明确引用。当前一次处理一张图。

绘图插件配置在“插件 → AI 绘图生成器”，支持修改图片模型、API 地址、密钥和质量。密钥由已配置的 sub2api 提供方复制到插件配置；以后更换密钥时，聊天模型与绘图插件需分别更新。图片接口最多等待 300 秒，工具调用链为图片任务预留 420 秒，QQ AI 流水线单轮上限为 600 秒。请求不自动重试；遇到超时，上游可能仍在处理，先查看服务端记录再决定是否重试。

记忆示例：`@机器人 请记住，我希望回答尽量简短`。之后可以要求回忆、更正或忘记这条信息。群成员的信息放在同一个群的记忆空间里，不属于逐成员私密存储。

TXT 示例：先上传 `.txt`，再发送 `@机器人 阅读我刚才发的 TXT，检查错别字和半角标点`；也可以引用文件后提出问题。插件优先读取当前或引用的文件，再读取同一发送者、同一会话 30 分钟内的最近文件。安装前上传、重启后或过期的文件需要重新上传或引用。上传通知本身只缓存文件信息，不调用模型或主动回复。多份文件目标不清楚时会要求明确。

TXT 支持 UTF-8（含 BOM）、带 BOM 的 UTF-16 和 GB18030，保留原文标点；下载上限 128 KiB、正文上限 16,000 字符，序列化结果另有长度限制。超限会明确提示拆分，不会悄悄截掉后半段仍声称校对完成。正文只作为待分析资料，不执行附件中的指令。当前不包含 PDF、Word、压缩包或 Markdown 附件读取。

在 LangBot 的“流水线 → QQ AI”中修改模型、角色提示词和群聊触发规则；在“模型”中修改 API Key。默认没有自动切换备用模型。图片和用户提问会交给你配置的 AI 服务处理。

## 文件与后续修改

- `langbot/`：包含可直接修改的 LangBot 源码及 `.venv` Python 环境。
- `langbot/data/`：配置、数据库、插件和记忆数据。迁移或备份前先停止服务。
- `napcat/data/`：隔离的 QQ 登录数据、NapCat 配置和日志。
- `.local/`：本地管理账号、连接凭据、进程记录、LangBot 日志与验收记录。请勿提交到公共仓库。
- `patches/onebot-group-trigger.patch`：修复 OneBot 默认消息路由绕过群聊触发规则。
- `patches/chroma-delete-count.patch`：修复 Chroma 删除成功却返回 0 导致的记忆健康检查误报。
- `patches/onebot-image-vision.patch`：修复 QQ 图片只有下载链接、没有传入像素的问题，并避免向模型提交空图片。
- `patches/sdk-windows-dependency-temp.patch`：修复 Windows 插件安装时 pip 临时目录过深导致的依赖安装失败。仅 Windows 安装过程使用短临时目录，仍将依赖保存在原有隔离目录，不修改系统长路径设置；补丁针对 `langbot/.venv/Lib/site-packages/langbot_plugin/runtime/plugin/worker_launcher.py`，并包含 5 项回归测试。
- `patches/image-tool-host-timeout.patch`、`patches/sdk-image-tool-timeout.patch`：将绘图与改图的 Host、插件 SDK 等待预算对齐为 420 秒，避免图片请求仍在处理时被中间层的 180 秒限制截断。Host 和最终插件执行按实际绘图插件身份限定，其他插件工具仍使用原有 180 秒预算。QQ AI 流水线总执行预算为 600 秒，绘图插件请求预算为 300 秒，仍不自动重试。
- `custom-plugins/LongTermMemory/`：长期记忆插件的本地修订源码，版本 `0.2.7+local.2`。修复知识库访问范围与记忆删除假成功，按会话和记录双重过滤删除并复读确认。使用本地安装来源，保留同一个插件身份与记忆数据。完整补丁为 `patches/longterm-memory-compatibility.patch`，重装方法见该源码目录内的说明。
- `custom-plugins/AIImagePlugin/`：基于官方绘图插件的本地扩展，版本 `0.1.7-local.3`，增加自然语言生成/编辑工具、图片上下文、短期缓存及 base64 回图。每轮向模型同步经过核验的图片可用状态，支持接着修改上一张图。修改和打包方式见该目录的 `README_LOCAL.md`；完整补丁为 `patches/ai-image-generation-editing.patch`。
- `custom-plugins/QQTextReader/`：TXT 阅读插件源码，版本 `0.1.0`，含编码识别、受限下载、上下文隔离缓存与群文件上传事件处理器。插件补丁为 `patches/qq-text-reader.patch`，OneBot 只读取址接口补丁为 `patches/onebot-text-attachment.patch`。
- `Verify-Bot.py`：使用本地网页调试通道测试图片、保存记忆、清除短期上下文后回忆并删除测试记忆。会消耗少量模型额度，不发送 QQ 消息。
- `Verify-ImageAPI.py`：单次测试图片生成；加 `--edit` 可编辑上一次测试图，会调用上游图片接口。
- `Verify-PictureTools.py`：通过网页调试验证自然语言生成及连续改图，会调用上游图片接口，不发送 QQ 消息。
- `Verify-TextAttachments.py`：使用合成 TXT 附件验证先上传后阅读及中文编码；会调用聊天模型，不发送 QQ 消息。
- `Configure-Bot.py`：首次配置脚本，重新执行会恢复本次默认模型、提示词和触发规则；日常启动不调用它。
- `Configure-Image.py`：安装绘图插件后，从现有 sub2api 提供方复制密钥并配置图片模型，补充自然语言调用工具的提示词，保留流水线其他设置。以后更换提供方密钥时，可运行此脚本同步到绘图插件。
- `Configure-TextReader.py`：安装 TXT 插件后创建或复用群文件捕获处理器，追加到当前 QQ Bot，保留原有事件路由，并补充读取正文的提示词；无需额外密钥。

启动依赖本机 `F:\work\Environment\python312\python.exe`，NapCat 使用目录内的 Node 和 QQ 运行文件。Linux 服务器迁移时应重新建立 Python 环境并安装 Linux 版 NapCat，不能直接复制 Windows 运行环境使用。

修改 LangBot Python 源码后重启 LangBot 生效。升级 LangBot 或更换记忆插件前保存本地修改与数据，检查上游是否已包含这些修复；不要直接重复应用补丁。本地修订插件需按其目录内说明重新打包安装，直接修改源码目录不会改变运行中的插件。

重新创建 `.venv`、升级 `langbot-plugin` 或重新同步依赖会覆盖 Windows 安装器修复。当前补丁基于 SDK `0.7.11`；恢复前停止 LangBot，在 `langbot/` 目录先运行 `git apply --check ../patches/sdk-windows-dependency-temp.patch`，检查通过后再用 `git apply ../patches/sdk-windows-dependency-temp.patch` 应用并重启。若回归测试文件已经保留，可给这两条命令加 `--exclude=tests/*`，仅恢复 SDK 源码。新版本若不适用，应核对上游实现后调整补丁。

绘图超时链的 `sdk-image-tool-timeout.patch` 同样会被 SDK 升级或环境重建覆盖，需按上述检查与恢复方法处理；Host 的 `image-tool-host-timeout.patch` 则随 LangBot 源码保存。两者与绘图插件、流水线超时配置需要一同保留，单独延长上游请求超时仍会被中间层提前中断。

## 验收与端口

两个模型接口、默认模型图片识别、记忆向量模型和 QQ 连接均单独检查。QQ 真实收图经转换后调用模型成功，且用户已确认实际 QQ 识图正常。最后一次调试对话验收结果保存在 `.local/verification.json`；数据库删除的独立复核也必须通过，不能只依赖模型的文字回复。

绘图插件 `0.1.7-local.3` 已通过本地聊天链路实测：自然语言生成白底紫色方块，随后不重传图片，直接要求改为橙色。两轮各返回一张有效 PNG，记录确认分别只调用一次 `generate_image` 和 `edit_image`，人工查看颜色修改正确；本次分别耗时约 25 秒、42 秒。结果保存在 `.local/picture-tools-result.json`。绘图插件 93 项离线测试、安装器与多层超时回归测试通过。这些自动验收没有向 QQ 发送消息。

TXT 插件 120 项离线测试及 OneBot 取址相关 134 项回归测试通过。本地聊天实测验证了先上传再请求读取、当前附件读取、UTF-8 BOM/GB18030 解码，以及校对合成错别字和半角标点，结果保存在 `.local/text-attachments-result.json`。截图所指真实 TXT 也经过受限下载与解码验证，11,367 字节、9,295 字符、UTF-8，未打印正文或把真实文件交给模型；该检查只保存元数据至 `.local/txt-real-download-check.json`。

WebUI 5300 和 6099 只监听本机；插件运行时使用 5400/5401，当前 SDK 默认监听所有接口并使用认证。迁移服务器时不要将插件运行时端口开放到公网。

LangBot 日志：`.local/langbot.stdout.log`、`.local/langbot.stderr.log`。
NapCat 日志：`napcat/data/logs/`。
