# Linux 服务器迁移

机器人业务代码是 Python。Windows 的 `.ps1/.cmd` 是本机启动入口；Linux 使用项目根目录的 `scripts/Start-LangBot.sh` 或此目录的 systemd 示例。漫画解压使用 Linux 原生 7zz，不依赖 Windows 的 7z.exe、QQ 安装或盘符。

当前实机验证环境为 Windows。本目录提供 Linux 部署入口、原生解压工具安装和跨平台补丁工具；尚未在目标服务器执行完整部署验收。首次建议选择 Linux x86_64、Python 3.12，与现有测试 Python 版本一致。ARM64 的官方 7zz 安装也受支持，但整套 LangBot/NapCat 依赖还需在实际服务器验证。

## 重建运行环境

服务器需要 Python 3.12、Git、unzip、curl 和可用的 C/C++ 运行环境，具体系统包按发行版安装。不要从 Windows 复制 `.venv`、`.tools`、NapCat/QQ 可执行文件或插件的 Windows 依赖缓存。

项目 Git 仓库不包含 `langbot/` 和运行数据。先在项目根目录下载当前匹配的官方 LangBot 4.11.0 完整发布包；不能直接切到未知最新版后盲目套补丁：

```sh
curl -fL -o /tmp/langbot-v4.11.0-all.zip https://github.com/langbot-app/LangBot/releases/download/v4.11.0/langbot-v4.11.0-all.zip
printf '%s  %s\n' c14de9152fa95d514ce76a2f63782042912f6f07550ccf890a48e127af171878 /tmp/langbot-v4.11.0-all.zip | sha256sum -c -
mkdir langbot
unzip /tmp/langbot-v4.11.0-all.zip -d langbot
python3.12 -m venv langbot/.venv
langbot/.venv/bin/python -m pip install uv
langbot/.venv/bin/uv sync --project langbot --frozen
```

在服务停止时运行 `scripts/Apply-RuntimePatches.py`：默认检查，`--apply` 才应用。它通过目标 Python 的 `sysconfig` 寻找 SDK，不把 `.venv/Lib/site-packages` 的 Windows 路径硬套到 Linux；已应用的补丁跳过，不匹配时停止。

```sh
python3.12 scripts/Apply-RuntimePatches.py --python langbot/.venv/bin/python
python3.12 scripts/Apply-RuntimePatches.py --python langbot/.venv/bin/python --apply
python3.12 scripts/Setup-ArchiveTools.py
```

补丁清单包含 `onebot-group-context.patch` 和 `group-message-isolation.patch`：前者提供受限的群聊前文读取，后者修复跨发送者串话，让群消息逐条独立排队并检查自己的 @／命令触发条件。群内新消息不会再并入另一位成员正在执行的请求；私聊原有的聚合和追加输入行为不变。应用 Host 补丁后需要重启 LangBot。

`group-history-speakers.patch` 补齐普通会话历史的说话人标签：即使模型没有调用群聊前文工具，也能区分不同成员已经与机器人说过的话。标签取自保存的发送者元数据，仅加入本轮读取的副本，不重写或清空历史；无身份记录不能归给当前提问者。该补丁使用同一份 Python 代码，适用于 Windows/Linux；重启后运行 `scripts/Configure-ChatStyle.py` 同步话题切换和成员归属规则。

`scripts/Setup-ArchiveTools.py` 从 [7-Zip 官方发布](https://github.com/ip7z/7zip/releases/tag/26.04) 下载对应架构的 7zz，校验固定 SHA-256，安装到 `.local/tools/7zip/linux-x64/` 或 `linux-arm64/`。不修改全局 PATH，也不执行 Windows 安装器。

## 迁移配置和记忆

先停止原机 LangBot，完整备份 `langbot/data/`，再迁移数据库、Chroma、存储和插件制品。保留一份原始备份；启动时不要同时让两台机器使用同一份可写数据库。

不要迁移 `langbot/data/plugin-runtime/environments/`，这是平台相关依赖缓存。服务器上让插件运行时重新安装依赖；若已有插件状态未自动重建，按源目录重新打包并在管理页安装相同插件身份，保留数据库和插件存储。

管理账号、模型密钥、OneBot Token 等都在运行数据中，通过你自己的安全传输方式迁移，不放进 Git。配置脚本还使用 `.local/setup-state.json` 和 `.local/langbot-admin.json`，这些文件可随私有配置迁移。不要迁移旧的 PID 文件。`scripts/Configure-Bot.py` 是首次配置脚本，迁移已有数据时不要运行它覆盖现有设置。

```sh
sh scripts/Start-LangBot.sh
```

服务启动后，漫画插件重新运行 `langbot/.venv/bin/python scripts/Configure-ComicTranslator.py`，使其选择当前服务器的 7zz 路径，保留已有模型 UUID 和群触发设置。插件源码可用下面的跨平台命令打包，再通过管理页安装对应 `.lbpkg`：

```sh
cd custom-plugins/QQComicTranslator
../../langbot/.venv/bin/python -m langbot_plugin.cli build -o dist
```

若需后台常驻，把 `langbot.service.example` 中的 `User/Group/WorkingDirectory/ExecStart` 改为实际账号和项目路径，再由服务器管理员安装到 systemd。该服务只管理 LangBot；停止服务会一并停止插件和解压子进程。

示例中的 `qqbot` 账号和同名用户组需要先创建，也可以改为已有的专用服务账号。以最终服务账号执行安装和数据迁移；如果由管理员复制文件，启动前应校正项目目录所有权，确保该账号可读源码和模型配置、可写 `langbot/data/`、`.local/` 及插件依赖缓存。只修改服务的 `User` 不会自动修正文件权限。

## 群聊前文与多图理解

`QQGroupContext` 支持同群不同成员的文字和图片讨论，不要求固定话术。例如群友先聊几部漫画，之后可以问“他们说的几部漫画，你觉得哪个适合我”；也可以问“上面两张介绍都说了什么”。仍需 @ 机器人或使用已配置的命令，普通消息不会主动触发历史读取或模型分析。

工具先读取**本轮问句之前最近 60 分钟内的最多 100 条消息**，由模型结合说话人、提问者标记和回复关系理解话题；涉及图片时，一次最多分析 4 张，每轮最多一次视觉调用。只能读取当前群，实际范围取决于 NapCat 保存的记录；这是按需读取近期前文，不是永久保存全部群聊。超过范围或图片不可读取时，需要引用或补充资料。

迁移已有插件时保留配置；新装或更新则从项目根目录打包，使用服务器本地 Python：

```sh
(
  cd custom-plugins/QQGroupContext
  ../../langbot/.venv/bin/python -m langbot_plugin.cli build -o dist
)
```

确保前述运行补丁已应用并重启 LangBot。在管理页安装 `custom-plugins/QQGroupContext/dist/` 中的 `.lbpkg` 并启用插件；如果 QQ AI 流水线只允许指定插件，将“QQ 群聊前文与多图理解”加入允许列表。然后在项目根目录运行：

```sh
langbot/.venv/bin/python scripts/Configure-GroupContext.py
```

配置脚本在未指定时使用已有的 `gpt-6.1-sol` 模型，保留现有模型和群触发规则，无需再填写密钥。它通过本机管理接口读取 `.local/setup-state.json` 与 `.local/langbot-admin.json`，因此应在 LangBot 启动、私有配置迁移完成后执行。群历史读取沿用已有 OneBot 连接，不需要额外开启 NapCat HTTP 接口。

聊天语气保存在 `config/chat-style.txt`。迁移后的已有配置会保留；需要同步文件中的风格时，在项目根目录运行 `langbot/.venv/bin/python scripts/Configure-ChatStyle.py`，只更新语气段，不覆盖模型、工具或触发规则。

联网插件 `custom-plugins/QQWebSearch` 与 Windows 使用同一份代码，不依赖浏览器或桌面环境。迁移插件配置和现有提供方后，在项目根目录运行 `langbot/.venv/bin/python scripts/Configure-WebSearch.py` 同步 API 地址、密钥和视觉模型。它需要上游支持 Responses `web_search`，并允许服务器访问公开网页；不可访问的来源会明确报错，不以生成图片替代搜图。

使用 `QQWebSearch 0.2.0` 或后续版本可下载公开网络文件并作为 QQ 附件上传。文件类型不设扩展名白名单，单个最多 20 MiB，每轮最多两个不同附件。文件只在插件内存中临时保存，通过当前查询的 `File(base64=...)` 回传给 NapCat；不依赖 Windows 路径、共享磁盘或桌面软件，不自动执行和解压。GitHub 目录查询走公开 Contents API，可能受匿名访问限额影响；普通网页支持静态下载链接。更新插件包后需用 `scripts/Apply-RuntimePatches.py --apply` 应用 `sdk-attachment-message-budget.patch` 并重启全部服务，使 SDK 单条消息限制从 16 MiB 调整为 32 MiB，足以承载 20 MiB 附件的 base64 与消息包装；仍保留严格上限。无需新增独立密钥。

联网结果仅保存在本轮内存缓存，不随数据目录迁移；最多保留 32 轮、64 MiB，15 分钟过期。图片解码上限 1200 万像素、进程内最多两个线程，图像回传绑定当前查询。群聊前文插件使用 `0.1.2` 或后续版本，覆盖自然追问、群文件图片和代理 fake-IP 取图。

多图编辑需安装 `AIImagePlugin 0.1.7-local.5` 或后续版本，重新打包时包含其 `aiohttp` 依赖。同条或分开的 1–4 张参考图可进入一次编辑请求，群内其他人的图片通过本轮可信锚点查询选取。需要应用新版 `onebot-group-context.patch` 并重启 Host，才能识别以群文件发送的图片；补丁安装器会拒绝把旧的局部修改当成最新版，升级已有安装时应先保存数据并核对源码差异。Windows/Linux 使用相同 Python 逻辑；纯 fake-IP 回退只在系统 DNS 全部返回 198.18.0.0/15 时使用固定 DoH，正常公网 DNS 无需回退。

## NapCat 和验收

NapCat 在服务器上按 [官方文档](https://napneko.github.io/) 安装 Linux 版本或官方容器，重新扫码登录。不要运行本机 Windows `napcat/` 目录的启动脚本。为新环境配置同一个 LangBot Bot 的 OneBot 反向 WebSocket 地址及 Token；同主机原生安装可以用回环地址，容器需改成容器实际可达的 LangBot 地址。

确认默认聊天、群聊仅 @／命令触发、识图、生图/改图、TXT 阅读、记忆以及 ZIP/RAR/7z 翻译都正常。漫画任务重启后不会自动重复调用模型，原发送者可要求“继续未完成的页”。已经完成页的 checkpoint 可以保留，迁移后先检查状态。

群聊上下文按下面的实际交互检查：

1. 两位成员先讨论两个方案或作品，第三位成员 @ 机器人追问“你觉得哪个适合我”，确认回答结合前文并区分各人的意见。
2. 两位成员各发一张图片，随后 @ 机器人询问“上面两张分别说了什么”，确认读取实际图片内容。
3. 机器人回复期间，另一位成员发送未 @ 的普通消息，确认它不会取代或混入当前问题；之后单独 @ 的新问题应独立排队处理。
4. 单独 @ 机器人说“你好”，确认无需读取群历史；私聊继续正常使用。

这些是目标环境的验收步骤，尚不代表已完成真实 QQ 回复验证或 Linux 部署验证。

管理端和插件端口无需直接公开到互联网。可以用 SSH 隧道访问 5300 和 NapCat 管理页；5400/5401 只用于插件运行时。

离线回归可先运行 `langbot/.venv/bin/python -m pytest custom-plugins/QQComicTranslator/tests -q`。模型集成验收脚本会消耗模型额度；合成日文图片测试需要 CJK 字体，可安装发行版的 Noto CJK 字体包或设置 `QQBOT_TEST_FONT` 为字体路径。

群聊上下文可先做不调用模型的检查：

```sh
langbot/.venv/bin/python -m pytest custom-plugins/QQGroupContext/tests -q
langbot/.venv/bin/python scripts/Verify-GroupContext.py --check-only
```

需要合成模型联调时，运行 `langbot/.venv/bin/python scripts/Verify-GroupContext.py`。它会消耗模型额度，以虚构三人讨论、合成图片及纯问候验证自然工具调用；群历史和图片下载使用替身，不发送 QQ 消息。结果写入 `.local/group-context-verification.json`，不能替代以上实际交互检查。
