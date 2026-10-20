# Windows 本机完整部署教程

本教程让机器人运行在自己的 Windows 电脑上，采用 Docker Desktop 的 Linux amd64 容器。新用户无需另装 Git、Python、QQ 客户端或 7-Zip，也无需手工配置插件和 WebSocket。

如果机器人已经部署在 Linux 服务器上，而你只是使用 Windows 访问管理页，请按 [README 的服务器管理步骤](../README.md#服务器管理页获取带令牌的链接)建立 SSH 隧道。在本机再启动一套机器人属于另一份部署，可能与隧道占用相同端口。

## 1. 准备 WSL 2 和 Docker Desktop

使用 Docker Desktop 支持的 Windows x86-64 系统，并为镜像、模型、数据和更新备份留出空间。本项目建议至少 8 GiB 内存、10 GiB 可用磁盘，持续使用和保留备份会增加磁盘占用。Windows ARM、Windows 容器模式不在本项目当前支持范围内；Windows 系统版本与硬件虚拟化要求见 [Docker 官方安装说明](https://docs.docker.com/desktop/setup/install/windows-install/)。

尚未安装 WSL 时，右键开始菜单，以管理员身份打开 PowerShell 或终端，执行：

```powershell
wsl --install --no-distribution
```

按提示重启电脑。已有 WSL 但 Docker 提示版本过旧时，执行 `wsl --update`，然后按提示完成更新。可用 `wsl --version` 查看版本。这些命令和选项见 [微软 WSL 文档](https://learn.microsoft.com/en-us/windows/wsl/basic-commands)。

从 [Docker 官网](https://docs.docker.com/desktop/setup/install/windows-install/)下载 Windows **x86_64** 安装包，按向导安装并启动 Docker Desktop。使用 WSL 2 引擎；有对应设置时在 Settings → General 中启用 **Use WSL 2 based engine**。部分版本默认启用，不显示这个选项。Docker 支持直接从 Windows 终端运行命令，无需单独安装 Ubuntu 发行版，见 [Docker WSL 2 说明](https://docs.docker.com/desktop/features/wsl/)。

等待 Docker 引擎运行，在普通 CMD 或 PowerShell 中执行：

```text
docker compose version
docker info --format "{{.OSType}}/{{.Architecture}}"
```

预期分别得到 Docker Compose 的版本号，以及 `linux/x86_64` 或 `linux/amd64`。如果第二条显示 Windows 引擎，在 Docker Desktop 菜单切换到 **Linux containers**；如果连不上引擎，先解决 Docker 启动问题，再继续。项目日常启停一般不需要管理员终端。

## 2. 下载并启动项目

1. [下载项目 ZIP](https://github.com/menherachan0210/QunLing/archive/refs/heads/main.zip)，完整解压到固定目录，例如 `D:\Apps\QunLing`。
2. 打开包含 `Start-Bot.cmd`、`Open-Bot.cmd`、`Stop-Bot.cmd`、`scripts` 和 `deploy` 的目录。ZIP 解压后可能还有一层 `QunLing-main`，以实际文件所在位置为准。
3. 确认 Docker Desktop 正在运行，双击 `Start-Bot.cmd`。
4. 首次启动会下载依赖、构建镜像、启动容器。等命令执行完成，不要在构建途中直接关闭窗口。

启动成功后命令窗口可以自动关闭，容器在后台继续运行；浏览器会自动打开设置页。网页首次出现时，插件可能还在安装，等待页面提示插件就绪后再保存 AI 配置。

若启动失败，窗口会暂停并显示错误。保留上方的具体错误，按本文第 6 节排查。修复网络或环境后再次运行同一份 `Start-Bot.cmd` 即可复用构建缓存；失败也可能留下已启动的部分服务，不要直接删除数据卷。

## 3. 打开带令牌的设置页

首次启动会自动打开专属链接，以后双击 `Open-Bot.cmd` 即可再次打开。**`Open-Bot.cmd` 只负责打开页面，不负责启动机器人**；如果服务已经停止，先运行 `Start-Bot.cmd`。

需要手动获取链接时，可以在资源管理器中进入项目根目录，在地址栏输入 `cmd` 并按回车，然后执行：

```text
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\Public-Bot.ps1" -Action url
```

这条命令也可以在已进入项目根目录的 PowerShell 中运行。如果希望它同时打开浏览器，在末尾加 `-OpenBrowser`。

输出地址的格式如下，其中令牌由本次部署自动生成：

```text
http://127.0.0.1:5310/#token=YOUR_SETUP_TOKEN
```

复制**完整链接**，包含 `#token=` 及后面的字符串，在本机浏览器打开。本机部署无需 SSH 隧道。

如果页面显示“打开专属设置链接／设置令牌”，重新双击 `Open-Bot.cmd`，或获取完整链接再次打开。也可以只把 `#token=` 后面的字符串填入“设置令牌”，点击“连接”；令牌框不填整条 URL、AI API Key 或 Windows 密码。

网页读取链接后会从地址栏移除令牌，当前标签页用浏览器会话保存认证状态。换浏览器、新建无痕窗口或再次打开普通地址时可能要求重新认证，使用原专属链接即可。获取链接不会更换令牌或重置数据。请私下保存完整链接，不要公开分享。

## 4. 填写 AI 服务并登录 QQ

等待插件安装完成，然后在设置页填写：

| 字段 | 填写内容 |
| --- | --- |
| API 地址 | 自己的 AI 服务提供的兼容接口地址，以服务文档为准 |
| API Key | 该 AI 服务的调用密钥，不是设置页管理令牌 |
| 聊天模型 | 服务实际提供的模型名；聊天与工具需要相应模型能力 |
| 图片生成／编辑模型 | 服务实际提供的图片模型名 |
| 额外模型选项 | 历史图片、漫画或联网需要不同模型时展开填写 |

点击“保存并启用全部功能”，等待配置完成，再用机器人 QQ 扫描页面二维码并在手机上确认。二维码过期时点击“刷新二维码”。页面显示“机器人已连接”，才表示 QQ 和 LangBot 已连通。

机器人 QQ 加入目标群后，群内通过 **@机器人** 或 **`/ai 问题`** 触发回复；私聊直接发送。可以先私聊发一句简单问题验证自己的 AI 接口，再测试识图、生图或联网。保存配置本身不会调用模型或验证额度，模型能力要求见 [README](../README.md#ai-服务需要支持什么)。

已有配置时无需重复填写。API Key 留空会保留已保存值；修改模型或密钥前先结束正在进行的长任务。

## 5. 日常启停、更新和数据

| 需要做什么 | 操作 |
| --- | --- |
| 启动机器人 | 先启动 Docker Desktop，等待引擎就绪，再双击 `Start-Bot.cmd` |
| 打开设置页 | 双击 `Open-Bot.cmd` |
| 停止并保留数据 | 双击 `Stop-Bot.cmd` |
| 检查、安装新版本 | 设置页“版本更新”中检查 GitHub，再点击“更新到此版本” |

成功启动后可以关闭命令窗口和浏览器，**Docker Desktop 需要保持运行**。退出 Docker Desktop、电脑休眠或关机都会影响本机机器人在线。电脑重启后先启动 Docker Desktop，再用原目录的 `Start-Bot.cmd` 恢复；启动脚本不会代为安装或启动 Docker Desktop。

网页更新会备份数据、构建新版本并切换服务，失败时尝试恢复。更新期间保持 Docker 和电脑运行，不要停止服务；停止入口会拒绝正在进行的更新。QQ 如果要求重新验证，仍需在设置页扫码。网页更新后正常双击 `Start-Bot.cmd` 会恢复已启用的版本；`rebuild` 用于明确应用本地源码修改，不是日常检查更新的入口。

配置、聊天、记忆和 QQ 数据位于五个 Docker 命名卷，更新记录和备份位于第六个 `update-data` 卷。`Stop-Bot.cmd` 会停止并删除本项目服务容器，保留这些卷、镜像和源码。复制项目源码文件夹并不能备份这些私有数据；备份和迁移见 [运维说明](OPERATIONS.md#备份与恢复)。删除数据卷、Docker 清理数据／恢复出厂或 `docker compose down -v` 会破坏数据或备份，不要用来解决普通启动失败。

需要查看状态、日志时，在项目根目录的 CMD 或 PowerShell 中分别执行：

```text
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\Public-Bot.ps1" -Action status
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\Public-Bot.ps1" -Action logs
```

日志命令显示近期 app、wizard、NapCat 日志。不要把含令牌、密钥或群聊内容的日志直接贴到公开 Issue。

## 6. 常见问题

| 现象 | 处理方法 |
| --- | --- |
| 找不到 `docker` | 确认 Docker Desktop 已安装，安装后重新打开 CMD／PowerShell，再检查环境 |
| `Cannot connect`、Docker 引擎未运行 | 启动 Docker Desktop，等待引擎就绪；仍失败时先处理 Docker 显示的 WSL／虚拟化错误 |
| WSL 缺失、版本过旧或虚拟化错误 | 按第 1 节安装／更新 WSL；按 Docker 和电脑厂商说明启用硬件虚拟化，按提示重启 |
| 提示需要 Linux amd64 容器 | 用 `docker info` 核对引擎，切换到 Linux 容器；Windows ARM 不在当前支持范围 |
| 找不到 `scripts` 或脚本文件 | 完整解压 ZIP，进入真正包含三个 `.cmd` 的目录，不要只单独复制启动文件 |
| PowerShell 提示禁止运行脚本 | 优先双击 `.cmd`，或使用本文带 `-NoProfile -ExecutionPolicy Bypass` 的命令；只对本次进程生效，无需全局更改执行策略。组织策略限制需由管理员处理 |
| 第一次下载／构建失败 | 检查终端首个具体错误及到 GitHub、Docker Hub、PyPI、模型站点的网络连接；修复后再次启动，保留缓存和数据 |
| 启动后窗口关闭了 | 执行成功后自动关闭正常，用 `Open-Bot.cmd` 或 `status` 检查；失败时入口通常会暂停并显示错误 |
| 浏览器没有自动打开 | 服务启动后双击 `Open-Bot.cmd`，或用第 3 节命令复制完整链接 |
| 页面连接被拒绝 | 先确认 Docker 和项目服务已启动；`Open-Bot.cmd` 取到链接不代表网页服务正在运行 |
| 页面要求“设置令牌” | 用专属完整链接打开，或仅填写 `#token=` 后面的字符串；不用 AI Key 解锁 |
| 5300／5310／6099 端口被占用 | 检查旧版机器人、其他容器或已有 SSH 隧道；确认来源后使用对应停止入口，避免同时运行两套 |
| 一直安装插件或没有二维码 | 先看页面阶段与 `logs` 输出；首次依赖安装需要时间，失败按具体错误处理 |
| 重启后 QQ 未连接 | 保持 Docker 运行，在设置页检查状态，必要时重新扫码并确认登录 |
| 更新中停止被拒绝 | 等待更新完成或恢复结果，不要绕过入口强行删除容器 |

本教程针对公开容器部署。已有旧原生 `langbot/.venv` 与 `napcat/` 环境的目录仍走兼容入口，相关维护方法见 [已有本机环境](OPERATIONS.md#已有本机环境)，不会自动迁移到 Docker。

Windows 启动脚本已做回归验证，完整容器构建、更新和失败恢复在 Linux Docker 环境实测；Windows Docker Desktop 的完整部署尚未实测。
