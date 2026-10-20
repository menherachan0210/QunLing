# 启动、更新与恢复

全新部署按照[首页](../README.md)进行。服务器当前支持 Linux amd64，Windows 使用 Docker Desktop 的 Linux 容器。根目录 `bot.sh` 与 `Public-Bot.ps1` 操作同一份 Compose。

## 设置入口

`bash bot.sh url` 或 `.\Public-Bot.ps1 url -OpenBrowser` 显示专属设置链接。链接中的令牌相当于此部署的管理凭据，请勿贴到公开截图、Issue 或日志中。

默认只开放服务器自身的回环地址。基础设置只需转发 5310；需要高级管理页时：

```bash
ssh -N -L 5310:127.0.0.1:5310 -L 5300:127.0.0.1:5300 -L 6099:127.0.0.1:6099 用户@服务器
```

配置页可显示自动生成的 LangBot 管理账号。大多数用户只需使用配置页；需要修改提示词、触发规则和扩展工具时再进入管理后台。不要把管理端口直接暴露到公网。

重新保存 AI 配置会同步聊天、视觉、漫画、生图和搜索使用的模型或密钥；留空密钥保留已保存值。更换配置会重载相关插件，正在执行的漫画任务应先完成或取消，再修改配置。普通启动使用相同配置时不会反复重载。

## 更新

先停止并备份数据，再更新仓库源码，执行 `bash bot.sh up`。构建步骤校验上游来源、应用兼容补丁、重新打包插件；配置向导会安装有变化的包，并保留原模型关联、记忆库和数据。

初次构建和安装插件都需要联网。缺少依赖或插件初始化失败时，向导不会假装完成；查看 `bash bot.sh logs`，恢复网络后重新启动服务。不要在新版源码上直接复用宿主机虚拟环境。

## 备份与恢复

数据位于 `qq-ai-bot` Compose 项目的五个命名卷。必须把 `langbot-data`、`setup-state`、`napcat-qq`、`napcat-config`、`napcat-plugins` 一起备份，尤其不能丢失 `setup-state` 后把旧 LangBot 数据当作全新环境初始化。

1. 使用 `bash bot.sh down` 停止服务；不带 `-v`，数据保留。
2. 按 [Docker 官方卷备份方法](https://docs.docker.com/engine/storage/volumes/#back-up-restore-or-migrate-data-volumes)备份五个卷；可用 `docker volume ls --filter label=com.docker.compose.project=qq-ai-bot` 查看实际名称。
3. 备份是私有资料，含 API Key、QQ 登录状态、会话与记忆；加密保存，不上传 GitHub。
4. 恢复到同版本源码和空卷，保留卷名与 Compose 项目名，再运行启动命令。QQ 可能要求重新确认登录。

本项目暂不提供“一键清空数据”。不要把 `down -v`、删除卷或删除数据库用作排障步骤。

## 已有本机环境

原本已经安装 `langbot/.venv` 和 `napcat/` 的目录继续使用 `Start-Bot.cmd` 等旧入口；它不会自动迁移或重置当前机器人。配置风格可用原 `Configure-ChatStyle.py` 应用。

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
