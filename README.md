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
| 群聊前文 | QQGroupContext 按需读取同群不同成员的近期文字和图片，接续正在讨论的话题 |
| TXT 阅读 | QQTextReader 读取当前、引用或同发送者近期的 TXT 正文，再交给模型分析 |
| 漫画压缩包翻译 | ZIP/RAR/7z（含 CBZ/CBR），后台逐页翻译并回传 LabelPlus TXT |
| 对话上下文 | LocalAgent 保留当前会话上下文 |
| 长期记忆 | LongTermMemory，使用本地 Chroma 保存和检索 |
| 记忆隔离 | 按群聊或私聊会话隔离；同一群内共享该群记忆 |
| 自动后台提取记忆 | 关闭；通过对话中的记忆工具保存、修改和删除 |
| 系统执行能力 | Box 关闭，模型没有本机终端和文件操作工具 |

群聊示例：`@机器人 这张图里有什么？`、`/ai 帮我整理一下这个想法`。

话风由 `config/chat-style.txt` 单独维护：闲聊自然接话，减少加粗、提纲、总结和客服式追问；解释先讲重点，翻译与校对仍保留完整内容。修改后运行 `langbot/.venv/Scripts/python.exe Configure-ChatStyle.py` 即可应用，重复运行不叠加提示；它只更新自己的语气段，保留模型、记忆、工具与触发规则。服务器改用 `.venv/bin/python`。首次配置 `Configure-Bot.py` 也会加载同一份话风文件。提示词调整参考 [OpenAI Prompting 指南](https://developers.openai.com/api/docs/guides/prompting)，实际效果用当前 sub2api 模型验证。

遇到质疑时先核对依据：有理由就坚持并解释，证据有效就修正具体错误，普通纠错不反复道歉。事实、推测和个人偏好分别处理，不因多人附和就改口，也不为表现主见而刻意反驳；评价作品时沿用明确的选择标准，有新事实或需求变化时可以调整判断。

群聊前文支持自由表达。例如群友先讨论几部漫画或分别发图，之后其他成员可以问 `@机器人 他们说的几部漫画，你觉得哪部适合我？`、`@机器人 上面两张介绍都说了什么？`，不用固定口令或把问题限定为图片识别。模型按需要先读取文字、说话人、回复关系和图片编号，再理解所选图片；会区分本轮提问者与其他群友的发言。

前文范围是**本轮问句之前、最近 60 分钟内的最多 100 条群消息**，取决于 NapCat 实际可提供的记录；文本还有长度限制，缺失或截断时不能当作完整历史。一次最多分析 4 张历史图片，每轮最多一次视觉分析，不自动重试。历史读取只面向当前群，不读取其他群或私聊，也不替代长期记忆。合并转发、语音和文件正文不在此历史解析范围内，TXT 和漫画压缩包继续使用各自工具。

群聊前文插件 `0.1.1` 也覆盖提问者自己先前没有 @ 的发言。“我这样做对吗”“那怎么办”等省略背景的追问会先按需读取前文；轻微错字只在读取后语境明确时理解，背景不足再澄清。独立问题与普通群消息不会因此自动读取历史。

### 联网、搜图与附件上传

`QQWebSearch` 提供自然语言联网查询、读取公开网页、查找现成图片并回传。搜索复用现有 sub2api 的 Responses `web_search`；查询结果附来源，搜索网页不等于已读取完整正文。可以说“联网查查这个消息”“读一下这个链接”“找一张帝企鹅照片发来”。若问题依赖“刚才他们说的”，先解析群聊前文再搜索。

`0.2.0` 增加通用附件流程：找到公开下载来源 → 列出具体文件 → 下载 → 作为 QQ 附件上传到当前会话。支持“找这个字体传上来”“把这份 PDF/源码/压缩包发过来”等自然表达，没有文件扩展名白名单。单文件最多 20 MiB，每轮最多两个不同附件；只转发文件，不执行、安装或解压。GitHub 支持查看仓库目录并直接下载指定文件，无需下载整个仓库。大附件需安装器应用 `sdk-attachment-message-budget.patch` 后重启服务，将 SDK 消息帧上限调整为 32 MiB。

搜图会读取来源页的实际图片，并用已有视觉模型核对内容；每轮最多回传两张并附来源。不能获取或确认图片时给出限制与来源，不自动生成替代图片。此版本未接独立图片搜索引擎，受公开页面的可读取性影响，不保证任意角色或构图都有结果。代码兼容 Windows/Linux，无需桌面浏览器。

安装本地插件包后运行 `Configure-WebSearch.py`，从已有提供方同步地址和密钥。更换提供方密钥后需再运行以同步插件配置。缓存隔离、请求限额和构建方式见 [联网插件说明](custom-plugins/QQWebSearch/README.md)。

仍然只有 @ 机器人或 `/ai` 命令才会回复；普通群消息不会主动触发历史读取或模型分析。群消息现在逐条独立排队、各自检查触发条件：机器人正在回复某位成员时，其他成员的后续发言不会被并入该轮提问。私聊原有的消息聚合和追加输入行为保持不变。

生图与改图按自然语言意图调用工具，没有固定口令。例如 `@机器人 生成一张咖啡店海报`，或附图后说 `@机器人 把背景换成海边`。也可以继续说 `把刚才那张改成漫画风`。仅讨论或描述图片时，不会因为出现“图片”二字就直接生成。

改图支持一次选取 1–4 张参考图，生成一张结果。可以同条消息附两张图，或分开发图后说“把上面两张合成一张”；群聊也可明确选用其他群友此前发的图片，无需先手动拼图。当前、引用、本人近期图片以及本轮提问之前的同群图片由工具列出，模型只能选择本轮展示的编号；目标不明确时才询问。无编号的旧单图调用仍按当前／引用／同人近期图处理。

近期图片仅作内存缓存，30 分钟过期，重启后清空；每轮参考编号 15 分钟过期，不跨成员、群或请求复用。群历史最多查看提问前 60 分钟、100 条消息。支持作为群文件发送的常见图片格式，读取后校验实际内容；任何选中参考图失败时都不拿剩余图片代替，也不提交生成请求。识图和改图的编号由各自工具管理，模型不接触原图下载地址。

编辑参考图支持 PNG、JPEG、GIF、WebP，单张最多 10 MiB。群聊识图另支持 BMP；BMP 如需编辑，应先转为上述编辑格式。纯 fake-IP 网络通过固定 DoH 取得并核验真实公网地址，仍拒绝内网或混合私网解析结果。

绘图插件配置在“插件 → AI 绘图生成器”，支持修改图片模型、API 地址、密钥和质量。密钥由已配置的 sub2api 提供方复制到插件配置；以后更换密钥时，聊天模型与绘图插件需分别更新。图片接口最多等待 300 秒，工具调用链为图片任务预留 420 秒，QQ AI 流水线单轮上限为 600 秒。请求不自动重试；遇到超时，上游可能仍在处理，先查看服务端记录再决定是否重试。

记忆示例：`@机器人 请记住，我希望回答尽量简短`。之后可以要求回忆、更正或忘记这条信息。群成员的信息放在同一个群的记忆空间里，不属于逐成员私密存储。

TXT 示例：先上传 `.txt`，再发送 `@机器人 阅读我刚才发的 TXT，检查错别字和半角标点`；也可以引用文件后提出问题。插件优先读取当前或引用的文件，再读取同一发送者、同一会话 30 分钟内的最近文件。安装前上传、重启后或过期的文件需要重新上传或引用。上传通知本身只缓存文件信息，不调用模型或主动回复。多份文件目标不清楚时会要求明确。

TXT 支持 UTF-8（含 BOM）、带 BOM 的 UTF-16 和 GB18030，保留原文标点；下载上限 128 KiB、正文上限 16,000 字符，序列化结果另有长度限制。超限会明确提示拆分，不会悄悄截掉后半段仍声称校对完成。正文只作为待分析资料，不执行附件中的指令。TXT 阅读器不处理 PDF、Word、压缩包或 Markdown；漫画压缩包使用下面的独立功能。

漫画翻译示例：先上传 ZIP、RAR 或 7z，再说 `@机器人 把压缩包里的漫画翻译成中文，给我 LabelPlus TXT`。也可以引用压缩包后提出请求，无固定口令。后台使用 `gpt-6.1-sol` 逐页识字、翻译和标注位置，完成后向原会话发回真实 TXT 附件。支持“查询翻译进度”“继续未完成的页”“取消翻译”“重发翻译结果”“删除翻译缓存”。任务属于当前发送者与当前会话，不会取用其他人的任务。

压缩包最多 40 MiB、100 张图片；支持常规 ZIP、RAR4/RAR5 和 7z（含固实压缩），图片为 PNG/JPEG/WEBP/BMP，CBZ/CBR 作为 ZIP/RAR 别名。保留原图片文件名，按自然顺序输出，包含框内/框外标记；LabelPlus TXT 是翻译初稿，需要核对译文、漏字和位置，不会自动嵌字到漫画图。加密、分卷、自解压 EXE、链接和重名图片会拒绝。未完成页会明确标记，不能把部分结果当全文完成。每插件最多同时两项任务，单页模型最长 150 秒，后台最长两小时；完成页逐页保存，服务重启后需明确要求继续。结果保留七天，成功后删除压缩包缓存，支持主动删除。发送超时不自动重试，可明确要求重发。

Windows 与 Linux 共用业务代码。RAR/7z 使用原生 7-Zip CLI，分别限制解压子进程内存和执行时间，不把压缩包成员直接解压到文件系统。`Setup-ArchiveTools.py` 安装已校验的官方 Windows/Linux 工具；`Configure-ComicTranslator.py` 写入当前系统的原生路径。Linux 启动、依赖重建、补丁和数据迁移见 [服务器部署说明](deploy/linux/README.md)。

在 LangBot 的“流水线 → QQ AI”中修改模型、角色提示词和群聊触发规则；在“模型”中修改 API Key。默认没有自动切换备用模型。图片、用户提问，以及按需选取的群聊前文会交给你配置的 AI 服务处理。

## 安装或更新群聊上下文

新环境或重新安装时，先停止 LangBot，再在项目根目录检查、应用运行补丁并打包插件。下面是 Windows PowerShell 命令；Linux 命令见 [服务器部署说明](deploy/linux/README.md)。

```powershell
.\langbot\.venv\Scripts\python.exe Apply-RuntimePatches.py --python .\langbot\.venv\Scripts\python.exe
.\langbot\.venv\Scripts\python.exe Apply-RuntimePatches.py --python .\langbot\.venv\Scripts\python.exe --apply
Push-Location custom-plugins\QQGroupContext
..\..\langbot\.venv\Scripts\python.exe -m langbot_plugin.cli build -o dist
Pop-Location
```

启动 LangBot，在“插件”中本地安装 `custom-plugins/QQGroupContext/dist/` 生成的 `.lbpkg`，确认插件已启用。如果 QQ AI 流水线手动选择了插件，也要把“QQ 群聊前文与多图理解”加入允许列表。然后从项目根目录运行：

```powershell
.\langbot\.venv\Scripts\python.exe Configure-GroupContext.py
```

该脚本在尚未指定模型时选择已有的 `gpt-6.1-sol`，保留已有模型选择和群聊触发规则，不另存一份 API Key。`onebot-group-context.patch` 提供受限的群历史读取接口；`group-message-isolation.patch` 修复群聊跨发送者串话；`group-history-speakers.patch` 给普通会话历史补齐可信说话人标签。这些补丁由 `Apply-RuntimePatches.py` 管理，修改 Host 源码后需要重启 LangBot。

群聊历史中，多条 `user` 消息并不代表同一个人。说话人补丁在读取历史的副本时加入已保存的成员身份，保留图片等原内容，不重写或清空数据库；缺少身份时明确标为未知。`Configure-ChatStyle.py` 同步成员归属与话题切换规则：新话题不硬接旧梗，明确回指时仍可继续讨论。模型仍可能判断失误，身份标签用于减少把别人的经历说成“你的”这类错误。

## 文件与后续修改

- `langbot/`：包含可直接修改的 LangBot 源码及 `.venv` Python 环境。
- `langbot/data/`：配置、数据库、插件和记忆数据。迁移或备份前先停止服务。
- `napcat/data/`：隔离的 QQ 登录数据、NapCat 配置和日志。
- `.local/`：本地管理账号、连接凭据、进程记录、LangBot 日志与验收记录。请勿提交到公共仓库。
- `patches/onebot-group-trigger.patch`：修复 OneBot 默认消息路由绕过群聊触发规则。
- `patches/onebot-group-context.patch`：增加受限的只读群历史接口，按提问消息位置和时间筛选当前群的前文。
- `patches/group-message-isolation.patch`：禁止群消息在触发检查前聚合或并入正在执行的其他请求，修复跨发送者串话；群消息独立排队，私聊行为不变。
- `patches/group-history-speakers.patch`：在已授权的群会话历史读取中保留发送者身份，修复普通历史丢失成员归属；适用于已有带身份元数据的记录，私聊不变。
- `patches/chroma-delete-count.patch`：修复 Chroma 删除成功却返回 0 导致的记忆健康检查误报。
- `patches/onebot-image-vision.patch`：修复 QQ 图片只有下载链接、没有传入像素的问题，并避免向模型提交空图片。
- `patches/sdk-windows-dependency-temp.patch`：修复 Windows 插件安装时 pip 临时目录过深导致的依赖安装失败。仅 Windows 安装过程使用短临时目录，仍将依赖保存在原有隔离目录，不修改系统长路径设置；补丁针对 `langbot/.venv/Lib/site-packages/langbot_plugin/runtime/plugin/worker_launcher.py`，并包含 5 项回归测试。
- `patches/image-tool-host-timeout.patch`、`patches/sdk-image-tool-timeout.patch`：将绘图与改图的 Host、插件 SDK 等待预算对齐为 420 秒，避免图片请求仍在处理时被中间层的 180 秒限制截断。Host 和最终插件执行按实际绘图插件身份限定，其他插件工具仍使用原有 180 秒预算。QQ AI 流水线总执行预算为 600 秒，绘图插件请求预算为 300 秒，仍不自动重试。
- `custom-plugins/LongTermMemory/`：长期记忆插件的本地修订源码，版本 `0.2.7+local.2`。修复知识库访问范围与记忆删除假成功，按会话和记录双重过滤删除并复读确认。使用本地安装来源，保留同一个插件身份与记忆数据。完整补丁为 `patches/longterm-memory-compatibility.patch`，重装方法见该源码目录内的说明。
- `custom-plugins/AIImagePlugin/`：基于官方绘图插件的本地扩展，版本 `0.1.7-local.5`，增加自然语言生成/编辑工具、图片上下文、短期缓存及 base64 回图。支持本轮 1–4 张有序参考图、同群他人图片及接着修改上一张图。失败提示区分原图读取和上游 API 的认证、限流、参数、服务等错误，不回显密钥、地址或完整错误正文。修改和打包方式见该目录的 `README_LOCAL.md`；完整补丁为 `patches/ai-image-generation-editing.patch`。
- `custom-plugins/QQTextReader/`：TXT 阅读插件源码，版本 `0.1.0`，含编码识别、受限下载、上下文隔离缓存与群文件上传事件处理器。插件补丁为 `patches/qq-text-reader.patch`，OneBot 只读取址接口补丁为 `patches/onebot-text-attachment.patch`。
- `custom-plugins/QQComicTranslator/`：漫画压缩包翻译插件源码，版本 `0.2.0`，含附件隔离、安全解压、模型识字翻译、LabelPlus 导出、后台任务与断点恢复。使用已有 LangBot 模型配置，不复制 API Key。
- `custom-plugins/QQGroupContext/`：多人群聊前文与图片理解插件源码，版本 `0.1.2`。按自然问题读取同群前文，再按需分析最多 4 张图；支持代理纯 fake-IP 的安全 DNS 回退，返回读取失败阶段及对应编号，避免误称已调用视觉模型。
- `custom-plugins/QQWebSearch/`：联网资料、公开网页和现成图片检索插件源码，版本 `0.2.0`。新增通用文件查找、下载和 QQ 附件上传，按当前查询隔离回传。
- `Configure-WebSearch.py`：从已有 sub2api 提供方配置联网插件，保留聊天模型与触发规则。
- `Verify-WebSearch.py`：以公开网页执行真实联网与模型联调；QQ 回传用本地替身捕获，不向真实聊天发消息。会消耗模型额度，可用 `--case search/page/images/greeting/font_attachment/source_attachment` 选择单项。
- `patches/sdk-dedicated-background.patch`：SDK 0.7.11 本地后台任务扩展，仅允许已安装的独立插件创建受安装权限、数量和时限约束的任务；关闭、断连或超时时撤销权限并取消。后台不能沿用过期聊天 Query 或 Runner 权限。重建 `.venv` 后需与其他 SDK 补丁一样先检查再恢复。
- `Verify-Bot.py`：使用本地网页调试通道测试图片、保存记忆、清除短期上下文后回忆并删除测试记忆。会消耗少量模型额度，不发送 QQ 消息。
- `Verify-GroupConversation.py`：用虚构成员验证新话题切换、明确回指、不同成员偏好和未知历史归属，复用 Host 身份投影与已安装 LocalAgent 的历史解码。默认调用已配置的 `gpt-6.1-sol`，每场景一次、无重试；`--check-only` 不调用 API，不读真实群记录或向 QQ 发消息。
- `Verify-ImageAPI.py`：单次测试图片生成；加 `--edit` 可编辑上一次测试图，会调用上游图片接口。
- `Verify-PictureTools.py`：通过网页调试验证自然语言生成及连续改图，会调用上游图片接口，不发送 QQ 消息。
- `Verify-TextAttachments.py`：使用合成 TXT 附件验证先上传后阅读及中文编码；会调用聊天模型，不发送 QQ 消息。
- `Verify-ComicTranslation.py`：用两页合成日文／英文漫画验证自然语言调用、后台翻译、页序及实际 TXT 附件；默认 ZIP，`--format 7z` 验证固实 7z；会调用聊天模型，不发送 QQ 消息。
- `Verify-GroupContext.py`：用虚构三人讨论和两张合成图片验证自然选择工具、提问者偏好、多图理解及问候不读历史；默认调用真实模型，群历史和图片下载使用合成替身，不发送 QQ 消息。`--check-only` 只检查合成图片与工具定义，不调用模型。
- `Verify-MultiImage.py`：用真实聊天模型验证同条两图、分开发图和他人群图的编辑工具选择及参考顺序；图片生成和 QQ 发送均为替身，不读取真实群聊，每场景最多 4 次聊天模型请求。
- `Configure-Bot.py`：首次配置脚本，重新执行会恢复本次默认模型、提示词和触发规则；日常启动不调用它。
- `Configure-Image.py`：安装绘图插件后，从现有 sub2api 提供方复制密钥并配置图片模型，补充自然语言调用工具的提示词，保留流水线其他设置。以后更换提供方密钥时，可运行此脚本同步到绘图插件。
- `Configure-TextReader.py`：安装 TXT 插件后创建或复用群文件捕获处理器，追加到当前 QQ Bot，保留原有事件路由，并补充读取正文的提示词；无需额外密钥。
- `Configure-ComicTranslator.py`：安装漫画插件后选择已有视觉模型与本机解压组件，创建或复用压缩包捕获处理器，并补充自然语言工具提示；保留现有群触发与其他路由。
- `Configure-GroupContext.py`：安装群聊上下文插件后选择已有视觉模型，保留原有聊天模型和 @／命令触发条件；不复制提供方密钥。
- `Setup-ArchiveTools.py`：安装项目私有的官方 7-Zip 26.04，支持 Windows/Linux 的 x64/ARM64，不改变全局文件关联。
- `Apply-RuntimePatches.py`：按目标 Python 实际路径检查或应用 Host/SDK 补丁，适配 Windows/Linux，默认只检查，显式 `--apply` 才修改。
- `Start-LangBot.sh`、`deploy/linux/`：Linux 前台入口、systemd 示例和服务器迁移说明；NapCat 使用其官方 Linux 部署方式。

启动依赖本机 `F:\work\Environment\python312\python.exe`，NapCat 使用目录内的 Node 和 QQ 运行文件。Linux 服务器迁移时应重新建立 Python 环境并安装 Linux 版 NapCat，不能直接复制 Windows 运行环境使用。

修改 LangBot Python 源码后重启 LangBot 生效。升级 LangBot 或更换记忆插件前保存本地修改与数据，检查上游是否已包含这些修复；不要直接重复应用补丁。本地修订插件需按其目录内说明重新打包安装，直接修改源码目录不会改变运行中的插件。

重新创建 `.venv`、升级 `langbot-plugin` 或重新同步依赖会覆盖 Windows 安装器修复。当前补丁基于 SDK `0.7.11`；恢复前停止 LangBot，在 `langbot/` 目录先运行 `git apply --check ../patches/sdk-windows-dependency-temp.patch`，检查通过后再用 `git apply ../patches/sdk-windows-dependency-temp.patch` 应用并重启。若回归测试文件已经保留，可给这两条命令加 `--exclude=tests/*`，仅恢复 SDK 源码。新版本若不适用，应核对上游实现后调整补丁。

绘图超时链的 `sdk-image-tool-timeout.patch` 同样会被 SDK 升级或环境重建覆盖，需按上述检查与恢复方法处理；Host 的 `image-tool-host-timeout.patch` 则随 LangBot 源码保存。两者与绘图插件、流水线超时配置需要一同保留，单独延长上游请求超时仍会被中间层提前中断。

## 验收与端口

两个模型接口、默认模型图片识别、记忆向量模型和 QQ 连接均单独检查。QQ 真实收图经转换后调用模型成功，且用户已确认实际 QQ 识图正常。最后一次调试对话验收结果保存在 `.local/verification.json`；数据库删除的独立复核也必须通过，不能只依赖模型的文字回复。

绘图插件 `0.1.7-local.3` 已通过本地聊天链路实测：自然语言生成白底紫色方块，随后不重传图片，直接要求改为橙色。两轮各返回一张有效 PNG，记录确认分别只调用一次 `generate_image` 和 `edit_image`，人工查看颜色修改正确；本次分别耗时约 25 秒、42 秒。结果保存在 `.local/picture-tools-result.json`。绘图插件 93 项离线测试、安装器与多层超时回归测试通过。这些自动验收没有向 QQ 发送消息。

`0.1.7-local.4` 增加分阶段错误提示，143 项插件测试通过，仍禁止自动重试。2026-10-20 的单次真实 API 合成改图将红色方块改为蓝色，约 13 秒返回有效 PNG；记录为 `.local/edit-service-probe.json`。该测试未使用真实群图或向 QQ 发消息。用户随后在 QQ 重试原引用图，新版工具明确返回上游 HTTP 400 内容审核拒绝；该次失败发生在生成服务阶段，原图只读下载检查正常。此前仅有通用报错的那次无法追溯原始错误码；新版诊断不会绕过上游审核或自动重试。

`0.1.7-local.5` 与 QQGroupContext `0.1.2` 的多图改图、代理取图和群文件图片修复通过 504 项离线测试（编辑插件 234、群上下文 169、Host 80、安装器 21）。用户报告的普通图和群文件图均已用两种下载流程只读取得，未保存原图或再次交给模型。一次真实图片 API 将两张合成参考图组合成有效结果，已人工核对；另有 4 组真实聊天模型测试验证同条两图、分开发图、他人群图，以及旧单图拒绝后的继续操作，编辑服务和 QQ 回复使用替身。验收记录为 `.local/multi-image-provider-verification.json`、`.local/group-media-read-verification.json`、`.local/group-media-edit-read-verification.json` 和 `.local/multi-image-routing*-verification.json`。这些检查不替代真实 QQ 的最终图片回传验收。

话风调整以虚构闲聊、作品选择、认真解释及缺失文件四类输入调用 `gpt-6.1-sol` 检查，记录为 `.local/chat-style-verification.json` 和 `.local/chat-style-explanation-verification.json`；后者使用最后修订的语气段。合成回复未发送到 QQ，风格是否合意仍以实际群聊体验为准。

回应质疑的规则另用六类多轮合成对话、共七次 `gpt-6.1-sol` 调用检查：错误纠正与连续施压、有效纠正、主观偏好、缺少证据、需求变化、已有证据推翻推荐。结果能保留有依据的结论，也能纠正误读并改变推荐；记录为 `.local/chat-judgment-verification.json` 和 `.local/chat-judgment-evidence_changes_conclusion-verification.json`。该检查不向 QQ 发消息，样例通过不代表所有真实群聊都能判断正确。

联网插件 313 项离线测试通过，覆盖真实搜索记录与引用、受限下载、fake-IP DNS、压缩内容、图片核对、会话隔离与防重复发送。真实 `gpt-6.1-sol` 联调已完成带来源搜索、公开网页读取、照片检索及本地模拟回传；问候不会触发联网。结果分别在 `.local/web-search-verification-search.json`、`-page.json`、`-images.json`；初始 `-all.json` 记录包含兼容修复前的失败，不代表最终结果。帝企鹅照片由公开来源取得，经过视觉检查，模拟发送同时包含图片和来源；未向真实 QQ 发送测试图。

通用附件版 `0.2.0` 的联网插件共 520 项离线测试通过，安装器和 SDK 传输另有 32 项通过；20 MiB 合成附件已穿过真实 stdio/WS 编解码，超 32 MiB 消息仍被拒绝。真实模型与网络测试已完成作者仓库中的 12.3 MiB H1 字体、Python 公开源码文件的下载和发送工具调用，发送以本地替身捕获原始字节及 SHA-256，未向真实 QQ 上传。结果在 `.local/web-search-verification-font_attachment.json`、`.local/web-search-verification-source_attachment.json`；真实 QQ 附件回传仍需用户试发确认。

群聊前文插件 `0.1.1` 的 111 项离线测试通过。虚构“爬山安排—等天气再决定—我这样做对面”的多轮模型测试先调用前文工具，再结合语境回答，记录为 `.local/group-followup-verification.json`；没有把真实群聊再次用于模型测试。

TXT 插件 120 项离线测试及 OneBot 取址相关 134 项回归测试通过。本地聊天实测验证了先上传再请求读取、当前附件读取、UTF-8 BOM/GB18030 解码，以及校对合成错别字和半角标点，结果保存在 `.local/text-attachments-result.json`。截图所指真实 TXT 也经过受限下载与解码验证，11,367 字节、9,295 字符、UTF-8，未打印正文或把真实文件交给模型；该检查只保存元数据至 `.local/txt-real-download-check.json`。

漫画插件与跨平台补丁工具的 247 项回归测试通过，包含真实 RAR4/RAR5、固实 7z 解码及进程资源限制。合成两页日文／英文 ZIP 和固实 7z 均完成真实模型翻译，生成 2 页、4 个文字标记的中文 TXT；LabelPlus 官方 PS 解析器读取成功，无结构错误。最近的 7z 结果记录为 `.local/comic-translation-7z-result.json`，生成文件为 `.local/comic-generated-LabelPlus.txt`。本地网页调试的虚拟机器人没有 QQ 收件人，因此后台自动发送记为未确认，再通过新消息的结果工具验证附件回传；QQ 群聊／私聊发送链完成离线验证，真实 QQ 回传需用户上传文件后验收。没有替用户处理截图中的原漫画包，也尚未在 Linux 服务器执行完整验收。

群聊上下文插件 105 项测试通过，覆盖请求隔离、事件链、图片下载与处理。真实模型的合成测试通过多人讨论、多图比较和问候不读历史三项场景，记录为 `.local/group-context-verification.json`；其中的 `platform_history_mocked` 和 `qq_messages_sent_by_test` 区分模拟历史与真实群聊验收。另已通过 NapCat 只读接口验证从提问向前读取历史，元数据记录为 `.local/group-context-native-check.json`，没有保存真实群聊正文或把它交给模型。

2026-10-20 已重启服务、安装并配置 QQGroupContext；用户在真实 QQ 群复测“@ 后其他人发消息”的场景，确认串话恢复正常。多人文字／历史图片的完整 QQ 回答可继续用“他们说的哪个更适合我”或“上面两张说了什么”验收。合成模型测试不替代这部分实际交互，也不代表已经在 Linux 服务器完成部署验证。

同日修复普通历史投影丢失发送者身份的问题，避免把不同群友的购买、要求和偏好归给当前人；同时补充换话题时不强接旧梗的规则。Host 与历史权限等相关测试 73 项、补丁安装器测试 21 项通过。`Verify-GroupConversation.py` 的 6 组合成真实模型对话通过，覆盖同人/不同人换话题、明确回指、未知身份和不同偏好；结果保存在 `.local/group-conversation-verification.json`，没有读取真实群记录或向 QQ 发消息。本地服务已重启，7 个插件初始化正常，QQ 已连接；真实群聊效果仍需实际交互观察。

WebUI 5300 和 6099 只监听本机；插件运行时使用 5400/5401，当前 SDK 默认监听所有接口并使用认证。迁移服务器时不要将插件运行时端口开放到公网。

LangBot 日志：`.local/langbot.stdout.log`、`.local/langbot.stderr.log`。
NapCat 日志：`napcat/data/logs/`。
