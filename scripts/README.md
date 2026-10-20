# 维护与验证工具

新用户从项目根目录的 `Start-Bot.cmd`（Windows）或 `bot.sh`（Linux）启动，不需要逐个运行这里的工具。脚本搬迁后仍使用项目根目录的 `config/`、`.local/`、`langbot/` 和 `napcat/`，没有移动或重置已有运行数据。

| 文件 | 用途 |
| --- | --- |
| `Public-Bot.ps1` | Windows 的 Docker Compose 管理；支持 `up/down/status/logs/url/rebuild` |
| `Start/Open/Stop-Bot.ps1` | 根目录 `.cmd` 的实现；检测已有原生环境并保留其启动方式 |
| `Start/Stop-LangBot.ps1`、`Start-LangBot.sh` | 维护已有 Windows/Linux 原生 LangBot 环境 |
| `Apply-RuntimePatches.py` | 检查固定上游版本的补丁；显式 `--apply` 才写入 |
| `Setup-ArchiveTools.py` | 下载、校验并安装当前平台的 7-Zip |
| `Check-Release.py` | 只读检查待公开文件，报告路径而不输出密钥 |
| `Configure-*.py` | 为已有原生环境配置插件；不是容器初始化入口 |
| `Verify-*.py` | 合成集成验证；部分会联网和调用收费模型，使用前先读说明 |
| `local_api.py`、`chat_style.py` | 脚本共用的本地管理 API 和对话风格读取 |
| `download-release.py` | 按指定大小和 SHA-256 分段下载公开构建产物 |

以下命令均在项目根目录运行：

```powershell
.\scripts\Public-Bot.ps1 -Action status
python scripts/Check-Release.py
python scripts/Apply-RuntimePatches.py --python langbot/.venv/Scripts/python.exe
```

Linux 原生环境将最后一项的目标 Python 改为 `langbot/.venv/bin/python`。容器用户由镜像构建应用补丁，日常不需要手动运行这些脚本。脚本也可以从其他工作目录用绝对路径调用；下载工具显式指定的输出参数仍相对调用者当前目录。
