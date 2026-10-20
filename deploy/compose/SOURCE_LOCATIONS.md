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
| 本地修改差异及安装器 | `/opt/qq-ai-bot/patches/`、`/opt/qq-ai-bot/Apply-RuntimePatches.py` |
| 镜像构建与启动说明 | `/opt/qq-ai-bot/deploy/compose/` |
| 插件源码、各自许可和修改告知 | `/opt/qq-ai-bot/custom-plugins/` |
| 7-Zip 程序及其随附许可 | `/opt/qq-ai-bot/.local/tools/7zip/linux-x64/` |

SDK/Runtime 上游基线是 `v0.7.11` 对应提交 `e10d8385b2843a25e30219f9153f3227c8a0009f`，构建时重新下载完整源码并校验固定摘要。Linux 构建应用的 Runtime 修改位于 `sdk-image-tool-timeout.patch`、`sdk-dedicated-background.patch` 和 `sdk-attachment-message-budget.patch`。Windows 专用补丁在 Linux 上跳过。

根目录 Apache-2.0 **不覆盖或替换** Runtime 的 AGPLv3、NapCat、QQ、7-Zip 和其他依赖原有的许可。NapCat/QQ 位于独立的官方 NapCat 容器，不复制进本项目应用镜像。Python 依赖包的许可仍保留在其安装目录及 `.dist-info` 元数据中。

默认流程仅在部署者机器上构建，CI 也只构建并加载镜像，不推送镜像。若发布派生镜像或提供修改版 Runtime 的网络服务，应根据 `THIRD_PARTY_NOTICES.md` 和相应许可向接收者、服务用户提供实际可获取的对应源码及构建说明；仅在容器里保留文件，不等于已经向这些用户提供了访问入口。
