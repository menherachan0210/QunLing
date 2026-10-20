# Docker Compose 简易部署

主路径是 Linux amd64 服务器、Docker Engine 与 Compose v2。首次构建需要下载 Python 依赖、LangBot 完整发布包、官方 NapCat 镜像和本地记忆模型；建议预留 8 GiB 内存及至少 10 GiB 磁盘。

在项目根目录执行：

```bash
docker compose -f deploy/compose/compose.yaml up -d --build
docker compose -f deploy/compose/compose.yaml run --rm --no-deps wizard access
```

第二条命令显示带本机随机口令的专属设置链接。打开完整链接，按引导填写自己的 AI 服务并登录 QQ。管理账号、模型、流水线、插件和反向 WebSocket 由引导自动配置，不需要手填插件 UUID 或修改 JSON 文件。

远程服务器通过 SSH 转发访问本机端口：

```bash
ssh -N \
  -L 5310:127.0.0.1:5310 \
  -L 5300:127.0.0.1:5300 \
  -L 6099:127.0.0.1:6099 用户@服务器
```

引导页为 5310，LangBot 管理页为 5300，NapCat 管理页为 6099。三个端口均只绑定服务器回环地址。插件 Runtime 由 Host 自动启动，5400/5401 不映射到宿主机。不要为此部署挂载 Docker socket 或开启容器特权模式。

## 构建内容

镜像从固定 SHA-256 的 LangBot 4.11.0 完整发布包开始，包含已编译前端。依赖依据上游 `uv.lock` 安装，SDK 固定为 0.7.11；构建会调用项目补丁安装器，核对全部 12 项清单，在 Linux 应用其中 11 项并跳过 Windows 专用补丁。不匹配时直接停止构建。

镜像准备以下七个插件包，供引导导入：

| 插件 | 功能 |
| --- | --- |
| LocalAgent | 对话、工具调用和知识检索 |
| LongTermMemory | 长期记忆 |
| AIImagePlugin | 生图与多图编辑 |
| QQTextReader | TXT 等文本附件阅读 |
| QQComicTranslator | ZIP/RAR/7z 漫画翻译与 LabelPlus 文本 |
| QQGroupContext | 群聊历史与历史图片理解 |
| QQWebSearch | 搜索、网页阅读、搜图及公开附件下载 |

包位于 `/opt/qq-ai-bot/packages`，`packages.json` 记录每个包的身份、版本、文件名及 SHA-256。LocalAgent 从固定公开提交构建；其他六个插件来自本仓库源码。7-Zip 从官方固定版本下载并校验，路径为 `/opt/qq-ai-bot/.local/tools/7zip/linux-x64/7zz`。Chroma 的本地记忆模型在构建时预取并使用其固定 SHA-256 校验。

构建上下文使用 `Dockerfile.dockerignore` 白名单，Dockerfile 只显式复制公开源码。本机 `langbot/`、`.local/`、`napcat/`、下载目录、数据库、插件运行数据与密钥不会进入镜像。镜像中的 `.local/tools/7zip` 是构建时从官方发布重新生成的工具目录。

镜像固定信息见 [sources.lock.json](sources.lock.json)。NapCat 使用其[官方容器仓库](https://github.com/NapNeko/NapCat-Docker)推荐的 `mlikiowa/napcat-docker`，锁定 `v4.18.37` 的 Linux amd64 镜像摘要；首次生成 WebUI 配置，登录后由引导通过其 API 配置反向 WebSocket。

镜像保留根目录与第三方许可告知、完整 SDK/Runtime 0.7.11 上游源码、当前已修改的运行源码，以及对应补丁和构建脚本。具体位置见 [SOURCE_LOCATIONS.md](SOURCE_LOCATIONS.md)。根 Apache 许可不会覆盖 Runtime 的 AGPLv3 或其他组件许可；默认流程及 CI 都不推送镜像。

## 数据与重启

Compose 使用五个命名卷：

| 卷 | 保存内容 |
| --- | --- |
| `langbot-data` | LangBot 配置、数据库、记忆、插件安装与漫画任务状态 |
| `setup-state` | 引导进度、管理凭据和随机访问口令 |
| `napcat-qq` | QQ 登录数据 |
| `napcat-config` | NapCat WebUI 与 OneBot 配置 |
| `napcat-plugins` | NapCat 插件数据 |

初始化服务只准备缺失状态，不重置已有配置。应用启动不会重新套补丁或重新构建插件；这些操作在镜像构建时完成。普通重启：

```bash
docker compose -f deploy/compose/compose.yaml restart
```

更新仓库源码后，重新执行 `up -d --build`，然后使用引导核对插件版本与连接状态。备份应在服务停止后保存上述全部命名卷。`docker compose down` 保留数据；`down -v` 会删除数据，不用于日常更新。

## 检查与排障

```bash
docker compose -f deploy/compose/compose.yaml ps
docker compose -f deploy/compose/compose.yaml logs --tail=100 app wizard napcat
```

构建遇到下载或校验失败时，先检查服务器对 GitHub、PyPI、Docker Hub 和 Chroma 模型下载站点的访问。不要删除哈希检查或改用未经核对的同名下载包。AI 服务需要实际支持所选择的聊天、识图、图片生成/编辑和联网接口；部署会准备功能，不能替上游增加它没有的能力。

此目录的脚本与 Compose 约束有离线测试。GitHub Actions 的 `Linux image and fresh initialization` 作业会真实构建镜像，在全新命名卷上安装七个插件，再通过设置页接口提交纯虚构的模型名和密钥，AI 地址固定为不提供服务的 `http://127.0.0.1:1/v1`。检查覆盖提供方、聊天与视觉模型、本地记忆库、流水线、QQ Bot、两个文件事件处理器和插件参数关联；重启后比较凭据摘要、插件摘要与资源 UUID，确认不重复创建。NapCat 管理接口需可访问并保持未登录。检查不调用模型、不登录 QQ，也不发送消息。

当前开发主机未安装 Docker。新增 CI 作业需要在 GitHub 上实际运行，只有作业通过才代表容器初始化检查已通过；部署后仍需完成首次引导并检查 QQ 连通性。
