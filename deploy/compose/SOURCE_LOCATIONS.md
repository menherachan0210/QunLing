# 镜像内的源码、修改与许可证位置

以下路径均位于本地构建的 `qq-ai-bot-local:4.11.0` 镜像内。本镜像不是上游 LangBot 或 Runtime 的官方发行版；构建流程会应用本仓库维护的兼容补丁。

| 内容 | 镜像内位置 |
| --- | --- |
| 本项目根许可及范围声明 | `/opt/qq-ai-bot/LICENSE`、`NOTICE`、`THIRD_PARTY_NOTICES.md` |
| 保留的第三方许可原文、来源与摘要 | `/opt/qq-ai-bot/licenses/` |
| LangBot 4.11.0 源码及已应用的 Host 修改 | `/opt/qq-ai-bot/langbot/src/langbot/` |
| LangBot 上游许可 | `/opt/qq-ai-bot/langbot/LICENSE` |
| 未修改的完整 SDK/Runtime 0.7.11 上游源码、构建文件与测试 | `/opt/qq-ai-bot/upstream/langbot-plugin-sdk/` |
| SDK/API/CLI 上游许可 | `/opt/qq-ai-bot/upstream/langbot-plugin-sdk/LICENSE` |
| Runtime 的 AGPLv3 许可 | `/opt/qq-ai-bot/upstream/langbot-plugin-sdk/src/langbot_plugin/runtime/LICENSE` |
| 当前运行的已修改 SDK/Runtime Python 源码 | `/opt/qq-ai-bot/langbot/.venv/lib/python3.12/site-packages/langbot_plugin/` |
| 上游基线、下载来源与 SHA-256 | `/opt/qq-ai-bot/deploy/compose/sources.lock.json` |
| 本地修改差异及安装器 | `/opt/qq-ai-bot/patches/`、`/opt/qq-ai-bot/scripts/Apply-RuntimePatches.py` |
| 镜像构建与启动说明 | `/opt/qq-ai-bot/deploy/compose/` |
| 插件源码、各自许可和修改告知 | `/opt/qq-ai-bot/custom-plugins/` |
| 7-Zip 程序及其随附许可 | `/opt/qq-ai-bot/.local/tools/7zip/linux-x64/` |

SDK/Runtime 上游基线是 `v0.7.11` 对应提交 `e10d8385b2843a25e30219f9153f3227c8a0009f`，构建时重新下载完整源码并校验固定摘要。Linux 构建应用的 Runtime 修改位于 `sdk-image-tool-timeout.patch`、`sdk-dedicated-background.patch`、`sdk-attachment-message-budget.patch`、`sdk-tool-provenance.patch` 和 `sdk-qunling-worker-storage.patch`。最后一项确保直接启动的插件进程与宿主、后台任务使用同一个附件数据目录，只传递已配置的绝对 `QUNLING_DATA_DIR` 和查找原生解压器所需的 `PATH`，不继承其他密钥或开放共享沙箱的宿主目录。Windows 专用补丁在 Linux 上跳过。

`host-managed-runtime-transport.patch` 修改 LangBot 4.11.0 的 `src/langbot/pkg/plugin/connector.py`，允许显式使用同容器 stdio Runtime，并保留外部 Runtime 参数优先级与默认行为；该 Host 修改沿用 LangBot 的 Apache-2.0 许可，与 SDK/Runtime 目录的修改分开。

`host-persisted-plugin-installations.patch` 为 LangBot 增加 `GET /api/v1/plugins/_/installations`。此接口要求当前工作区的资源读取权限，仅返回六个持久化安装元数据字段，不依赖 Runtime 或暴露插件配置。向导据此区分“尚未安装”和“正在恢复”，避免重启时重复安装相同包。该 Host 修改沿用 Apache-2.0 许可。

`host-tool-password-redaction.patch` 修改工具监控参数的序列化，递归遮蔽 `password` 字段，并处理以 JSON 字符串提供的嵌套参数。只处理写入 `monitoring_tool_calls` 的副本，不改变实际工具调用参数或用户聊天文本；这不代表群消息、模型上下文或聊天历史中的密码会被删除。该 Host 修改沿用 Apache-2.0 许可。

`host-proactive-chat.patch` 为可信 OneBot 消息入口、显式提问结束和成功发送增加可选钩子，并提供要求当前工作区资源管理权限的按机器人自由对话设置及群列表接口。独立判断调度、短期上下文、预算与配置实现位于 `/opt/qq-ai-bot/qunling/proactive/`；不修改原来的 `@` 触发授权，不开放工具调用，也不在安装时开启任何群。该 Host 修改沿用 Apache-2.0 许可。

`host-proactive-context.patch` 在 PromptPreProcessing 插件执行后，为正常群提问附加可信运行状态及进入系统时固定的有界前文快照；关闭授权后不再附加缓存文字。此补丁独立于原自由对话补丁，支持已经部署原版的环境增量应用。该 Host 修改沿用 Apache-2.0 许可。安装器目前包含 20 个补丁，其中 Linux 应用 19 个。

该固定上游源码中的 `CLAUDE.md` 原为指向 `AGENTS.md` 的文档链接。为兼容安全解包和不同文件系统，镜像将这个已知链接物化为目标内容的普通文件，内容及来源归属仍为上游；不创建文件系统符号链接，也不允许其他未列出的链接。

根目录 Apache-2.0 **不覆盖或替换** Runtime 的 AGPLv3、NapCat、QQ、7-Zip 和其他依赖原有的许可。NapCat/QQ 位于独立的官方 NapCat 容器，不复制进本项目应用镜像。Python 依赖包的许可仍保留在其安装目录及 `.dist-info` 元数据中。

默认流程仅在部署者机器上构建，CI 也只构建并加载镜像，不推送镜像。若发布派生镜像或提供修改版 Runtime 的网络服务，应根据 `THIRD_PARTY_NOTICES.md` 和相应许可向接收者、服务用户提供实际可获取的对应源码及构建说明；仅在容器里保留文件，不等于已经向这些用户提供了访问入口。
