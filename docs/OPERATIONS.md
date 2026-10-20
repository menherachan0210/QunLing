# 启动、更新与恢复

全新部署按照[首页](../README.md)进行。服务器当前支持 Linux amd64，Windows 使用 Docker Desktop 的 Linux 容器。根目录 `bot.sh` 与 `scripts/Public-Bot.ps1` 操作同一份 Compose，Windows 的三个 `.cmd` 是后者的快捷入口。

## 设置入口

`bash bot.sh url` 或 `powershell -File scripts/Public-Bot.ps1 url -OpenBrowser` 显示专属设置链接。Windows 也可直接双击 `Open-Bot.cmd`。链接中的令牌相当于此部署的管理凭据，请勿贴到公开截图、Issue 或日志中。

默认只开放服务器自身的回环地址。基础设置只需转发 5310；需要高级管理页时：

```bash
ssh -N -L 5310:127.0.0.1:5310 -L 5300:127.0.0.1:5300 -L 6099:127.0.0.1:6099 用户@服务器
```

配置页可显示自动生成的 LangBot 管理账号。大多数用户只需使用配置页；需要修改提示词、触发规则和扩展工具时再进入管理后台。不要把管理端口直接暴露到公网。

重新保存 AI 配置会同步聊天、视觉、漫画、生图和搜索使用的模型或密钥；留空密钥保留已保存值。更换配置会重载相关插件，正在执行的漫画任务应先完成或取消，再修改配置。普通启动使用相同配置时不会反复重载。

## 在网页更新容器部署

在设置页打开“版本更新”，先检查版本，确认来源仓库与目标提交，再点击更新。默认来源是 `menherachan0210/QunLing` 的 `main` 分支；检查得到的完整提交 SHA 会固定为本次目标，执行时不会悄悄换成后来推送的提交。下载 ZIP 的环境也支持此流程，不要求宿主机安装 Git。

更新由独立 `updater` 服务发起，并由独立任务执行，因此更新器自身被替换不会终止这次任务。它准备目标源码并构建镜像，在替换运行服务前停止业务服务，对五个业务卷做一致性备份，校验备份 SHA-256 后才允许切换，再启动新版本并检查健康状态。页面可能短暂断开；恢复后重新打开原设置链接查看结果。插件安装以持久化包摘要为准，同一包恢复中时只等待，不重复安装。

备份放在独立 `update-data` 命名卷，该卷不备份自身。所有历史任务及其备份都保留，不自动删除。更新失败会恢复此次备份和旧镜像；若恢复过程也失败，则进入 `recovery_required`，禁止新更新并保留现场，不能把“发起恢复”理解为“恢复完成”。此时不要反复点击更新或删除卷，先保留现有卷与备份，再按页面阶段信息排查。自动恢复无法替代部署外的私有备份。

更新操作只管理 Docker 中的镜像和数据，不改电脑／服务器上的源码目录，不自动上传密钥或聊天。自定义分支、未提交改动及 ZIP 中的本地修改不会被合并到官方版本；维护自己的功能时应使用自己审阅的来源，或走下面的手动构建流程。版本检查只看已经发布到远端的提交，本机修改完成但尚未推送时不会作为可更新版本。原生 Windows/Python 部署没有网页一键升级支持。

`updater` 是唯一挂载 Docker socket 的服务。这个权限允许它管理宿主机容器和数据；app、wizard、NapCat 及插件不直接获得 socket。向导通过内部认证接口请求固定的版本检查和更新操作，不提供任意 shell 命令入口。只从你信任的仓库更新。

更新前请先完成或取消漫画等长任务，确认磁盘能容纳构建缓存和五个卷的备份。更新中不能 `down`；入口会阻止这类操作，也不要绕过入口手动停止任务容器。备份含 API Key、QQ 登录、聊天和记忆；不要上传 GitHub，不要随便删除 `update-data`。备份保留和磁盘清理应在确认当前版本正常且另有私有备份后处理，失败现场不自动清理。

需要人工恢复时，先排除磁盘、Docker 或下载故障，确认页面给出的任务 ID。页面无法打开、但 updater 仍运行时，可只读查看状态文件中的阶段与任务 ID（不输出凭据）：

```bash
docker compose -f deploy/compose/compose.yaml exec -T updater python -c "import json, os; from pathlib import Path; p=Path(os.environ.get('QQBOT_UPDATE_DIR', '/var/lib/qqbot-updater'))/'status.json'; d=json.loads(p.read_text()); print(json.dumps({k:d.get(k) for k in ('phase','job_id','message')}, ensure_ascii=False))"
```

然后从项目目录运行：

```bash
docker compose -f deploy/compose/compose.yaml run --rm --no-deps updater worker <job_id>
```

`<job_id>` 替换为该次失败任务的 ID，不要随意创建新任务。恢复命令继续处理已有任务及备份；执行完成后核对页面状态、实际镜像版本、QQ 连接和记忆数据，仍为 `recovery_required` 时保留现场进一步排查。

## 手动构建与源码开发

普通 `bash bot.sh up` / `Start-Bot.cmd` 会优先恢复已经启用的部署快照；不存在快照时才做首次构建，因此从旧下载目录启动不会自动降级。要应用本地改动，先完成私有备份，再在该目录运行 `bash bot.sh rebuild`；Windows 使用 `powershell -File scripts/Public-Bot.ps1 rebuild`。此操作明确按本地源码构建，若本地目录仍是旧代码就会引入旧版本，操作前确认所用版本。

初次构建和安装插件都需要联网。缺少依赖或插件初始化失败时，向导不会假装完成；查看 `bash bot.sh logs`，恢复网络后重新启动服务。不要在新版源码上直接复用宿主机虚拟环境。

## 备份与恢复

业务数据位于 `qq-ai-bot` Compose 项目的五个命名卷。必须把 `langbot-data`、`setup-state`、`napcat-qq`、`napcat-config`、`napcat-plugins` 一起备份，尤其不能丢失 `setup-state` 后把旧 LangBot 数据当作全新环境初始化。第六个 `update-data` 卷保存更新工作状态和自动备份，也应作为私有资料管理；它不是新的业务数据库。

1. 使用 `bash bot.sh down` 停止服务；不带 `-v`，数据保留。
2. 按 [Docker 官方卷备份方法](https://docs.docker.com/engine/storage/volumes/#back-up-restore-or-migrate-data-volumes)备份五个卷；可用 `docker volume ls --filter label=com.docker.compose.project=qq-ai-bot` 查看实际名称。
3. 备份是私有资料，含 API Key、QQ 登录状态、会话与记忆；加密保存，不上传 GitHub。
4. 恢复到同版本源码和空卷，保留卷名与 Compose 项目名，再运行启动命令。QQ 可能要求重新确认登录。

本项目暂不提供“一键清空数据”。不要把 `down -v`、删除卷或删除数据库用作排障步骤。

## 已有本机环境

原本已经安装 `langbot/.venv` 和 `napcat/` 的目录继续使用 `Start-Bot.cmd` 等旧入口；它不会自动迁移或重置当前机器人。维护脚本已集中到 `scripts/`，但所有原生数据路径保持不变。例如在项目根目录用 `langbot/.venv/Scripts/python.exe scripts/Configure-ChatStyle.py` 应用风格；Linux 的目标解释器是 `langbot/.venv/bin/python`。脚本清单见 [scripts/README](../scripts/README.md)。

公开容器部署使用独立数据卷，不自动导入当前电脑的密钥或聊天。迁移时先停止旧服务，避免占用同一端口或同一个 QQ 同时登录；按照上面的备份策略迁移数据。跨系统不复制虚拟环境，QQ 登录状态也可能需要重新建立。原生环境到容器的数据转换尚未作为自动迁移路径验收，请先保留完整备份。

## 排障

| 现象 | 检查 |
| --- | --- |
| 构建下载失败 | GitHub、PyPI、Docker Hub 和模型下载站点是否可达；保留摘要校验 |
| 提示端口占用 | 是否还有旧版 LangBot/NapCat，或另一套部署 |
| 一直安装插件 | 查看 app、wizard 日志；首次依赖安装较慢，失败后恢复网络再重启 |
| 二维码未出现或过期 | 确认 napcat 容器运行；在设置页刷新，手机确认新设备登录 |
| 已登录但未连接 | 配置是否已保存，LangBot 与 NapCat 是否健康；查看向导状态 |
| QQ 已连通但 AI 报错 | 检查地址、Key、模型名和服务端接口；保存配置没有验证额度或模型能力 |
| 能聊天不能改图／搜索 | 服务是否真的提供 Images edits / Responses web_search，模型是否可用 |
| 更新后记忆似乎消失 | 先核对 Compose 项目名与卷名，不要重新建空知识库 |

提交 Issue 时提供系统、部署方式、版本、复现步骤和脱敏错误。不要发送设置链接、API Key、`secrets.json`、数据库、QQ 配置卷或完整群聊日志。
