# 第三方来源与许可证

本项目原创的独立插件、配置脚本、文档及其他原创内容按维护者选择采用 [Apache License 2.0](LICENSE)。这不改变上游代码、Runtime 衍生部分、外部程序、依赖库或下载内容原有的许可证。`licenses/` 中保留了本次核对的许可原文，来源与 SHA-256 记录在 [SOURCES.json](licenses/SOURCES.json)。

以下版本与来源核对于 2026-10-20；不是对未来上游版本的许可保证。

## 框架与插件

| 组件／本地路径 | 来源与版本 | 许可及本项目的使用 |
| --- | --- | --- |
| LangBot；`patches/` 中的 Host 补丁 | [LangBot v4.11.0](https://github.com/langbot-app/LangBot/tree/v4.11.0) | [Apache-2.0](https://github.com/langbot-app/LangBot/blob/v4.11.0/LICENSE)。框架源码由部署流程获取；本仓库维护兼容补丁。 |
| `langbot-plugin` 的 API、SDK、CLI | [SDK v0.7.11](https://github.com/langbot-app/langbot-plugin-sdk/tree/v0.7.11) | [Apache-2.0](https://github.com/langbot-app/langbot-plugin-sdk/blob/v0.7.11/LICENSE)，但不覆盖下一行的 Runtime 目录。 |
| `langbot_plugin/runtime/` | [Runtime v0.7.11](https://github.com/langbot-app/langbot-plugin-sdk/tree/v0.7.11/src/langbot_plugin/runtime) | [AGPLv3 原文](licenses/LangBot-Runtime-AGPL-3.0.txt)。官方 [README](https://github.com/langbot-app/langbot-plugin-sdk/blob/v0.7.11/README.md) 明确将 Runtime 与 Apache 部分分开。 |
| 官方 `langbot-team/LocalAgent` | [Runner/LocalAgent](https://github.com/langbot-app/langbot-plugins/tree/main/Runner/LocalAgent)，已核对版本 0.2.1 | 来自 [langbot-plugins](https://github.com/langbot-app/langbot-plugins/blob/main/LICENSE) 的 Apache-2.0 插件；安装运行产物不作为本仓库源码上传。 |
| `custom-plugins/AIImagePlugin/` | [langbot-plugins](https://github.com/langbot-app/langbot-plugins/tree/3016b5c763bb3f3c7df942d470df08b8ba643f5d/misc/AIImagePlugin)，基线 0.1.6，提交 `3016b5c763bb3f3c7df942d470df08b8ba643f5d` | 继承 Apache-2.0。来源另存 `upstream-source.json`；本地版本扩展为生成／编辑、参考图选择与受限下载，详见该目录的 `README_LOCAL.md` 和 `NOTICE`。 |
| `custom-plugins/LongTermMemory/` | [KnowledgeEngine/LongTermMemory](https://github.com/langbot-app/langbot-plugins/tree/main/KnowledgeEngine/LongTermMemory)，基线 0.2.7 | 继承仓库 Apache-2.0。该本地副本没有记录精确上游提交，不将版本号冒充提交哈希；兼容修改由源码、版本后缀及补丁记录。 |
| `QQTextReader`、`QQComicTranslator`、`QQGroupContext`、`QQWebSearch` | 本仓库 `custom-plugins/` 下的四个独立插件 | 本项目原创，Apache-2.0；调用第三方 SDK 不改变其各自许可。 |

Apache 原文副本：[Apache-2.0.txt](licenses/Apache-2.0.txt)。六个本地插件目录也各带 `LICENSE`，以便独立打包时保留许可；两个继承插件另带来源与修改告知。保留 `langbot-team` 安装身份是为了兼容现有配置和数据，不代表上游认可或维护这些本地修改。

## Runtime 补丁的许可范围

不能把所有 `sdk-*.patch` 简称为 Apache-2.0 修改。许可随被修改的组件确定：

| 补丁 | 受影响部分 |
| --- | --- |
| `sdk-image-tool-timeout.patch` | `langbot_plugin/runtime/`：沿用 AGPLv3 |
| `sdk-windows-dependency-temp.patch` | `langbot_plugin/runtime/`：沿用 AGPLv3 |
| `sdk-attachment-message-budget.patch` | `langbot_plugin/runtime/`：沿用 AGPLv3 |
| `sdk-dedicated-background.patch` | `api/definition/plugin.py` 为 Apache-2.0 部分；`runtime/` 中的修改沿用 AGPLv3 |

这些是针对上游 0.7.11 的本地修改，不是官方发行版。发布修改后的 Runtime 或派生镜像时，应保留许可证、修改说明，并提供与运行版本相符的对应源码及构建／安装信息；对经网络交互使用修改版的用户，还需落实 AGPLv3 第 13 条的源码获取要求。仅放置一个许可链接不能替代对应源码的实际可得性。

本仓库提供补丁和安装脚本；发布者应同时保留所用上游版本的源码，并在实际发布页／服务说明中提供可用的获取入口。根目录 Apache 许可不会削弱接收者对 Runtime 部分已有的权利，也不在此声明整个部署中的所有程序均采用同一许可证。

## NapCat 与 QQ

QQ 接入使用 [NapCat-Docker 官方项目](https://github.com/NapNeko/NapCat-Docker) 指向的官方镜像。官方部署资料支持 Linux amd64／arm64，并区分 QQ 登录数据、NapCat 配置与插件持久化目录。本项目通过 OneBot 接口连接，不把 NapCat 或 QQ 程序拷入公开源码仓库。

NapCatQQ 使用 [Limited Redistribution License](https://github.com/NapNeko/NapCatQQ/blob/main/LICENSE)，版权标记为 `Copyright © 2024 Mlikiowa`，并非 MIT 或 Apache-2.0；许可包含非商业、再分发及修改代码公开方面的条件。[README](https://github.com/NapNeko/NapCatQQ#license) 另说明混合许可和授权限制。原文副本：[NapCatQQ-LICENSE.txt](licenses/NapCatQQ-LICENSE.txt)。这里不把不同条款作宽泛授权解释；部署与再分发仍须遵守上游要求，必要时向作者取得授权。

本次未在 NapCat-Docker 仓库根目录确认独立的标准许可证，不能仅因仓库公开就给其 Dockerfile／脚本重新标注 Apache。项目的部署配置使用公开接口和镜像参数，不复制其完整构建脚本。QQ 客户端另受其提供方条款约束；官方镜像中的其他依赖也各有许可。

## 7-Zip 与 LabelPlus

漫画压缩包使用 [官方 7-Zip 26.04](https://github.com/ip7z/7zip/releases/tag/26.04)，由 `Setup-ArchiveTools.py` 按平台下载并校验。安装器保留其 `License.txt`；本仓库不分发可执行文件。

7-Zip 涉及 LGPL、部分 BSD 代码和 unRAR 限制，不能整体标成 Apache。保留本项目所用官方包的 [Windows 许可](licenses/7-Zip-windows-x64-License.txt)与 [Linux 许可](licenses/7-Zip-linux-x64-License.txt)。若另行再分发二进制或修改版，应满足对应的许可、声明与源码提供要求。

LabelPlus 作为输出文件格式的兼容目标。格式根据 [LabelFileManager.cs](https://github.com/LabelPlus/LabelPlus/blob/master/LabelPlus/LabelFileManager.cs) 和 [PS-Script 文本解析器](https://github.com/LabelPlus/PS-Script/blob/master/src/text_parser.ts) 核对；本仓库实现自己的 TXT 序列化器，不分发 LabelPlus／Photoshop 程序或其解析器。格式兼容不表示商标授权或项目背书。

## 依赖与发布材料

Python、Node、操作系统镜像、数据库、Python 包、字体及用户下载文件不因本项目的根许可证而被重新许可。本文件列出直接集成的主要组件，不是完整的软件物料清单；构建镜像或分发依赖时需继续保留各包自带许可与版权声明。

公开源代码与 Release 应包括本文件、`NOTICE`、所适用的许可原文，以及对应源码／补丁／构建说明；不应包含实际 API Key、登录数据、用户聊天、运行数据库、个人部署地址或本机环境。测试中的虚构凭据与账号仅用于离线验证。
