# 开发与发布验证

框架和插件来源见 [ARCHITECTURE](docs/ARCHITECTURE.md)，许可边界见 [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md)。共享文件、任务和网络机制维护在 `qunling/`，QQ／模型适配维护在插件；兼容修复保留可重放补丁，不修改用户正在运行的缓存。

## 离线测试

使用单独的 Python 3.12 环境，在仓库根目录安装普通测试和解析依赖：

```bash
python -m pip install pytest pytest-asyncio requests PyYAML qrcode Pillow aiohttp httpx tzdata -r qunling/requirements.txt
python -m pytest tests --public-only -q
```

这层覆盖部署、初始化、固定来源补丁、文件格式与任务生命周期，不需要安装 LangBot/SDK。它不调用模型、不登录 QQ；RAR/7z 的原生用例需要完整的 7-Zip，可通过 `QUNLING_TEST_7ZIP`／`QQCOMIC_TEST_7Z` 指定可信程序，缺少时会明确跳过。额外 RAR 测试样本用 `QUNLING_TEST_ARCHIVES` 指定，不能拿用户附件替代合成样本。

`--public-only` 根据测试及其测试辅助模块的 Host/SDK 导入依赖自动分组，不使用允许文件清单。未运行的 Host/SDK 测试在固定版本环境或部署镜像中执行；镜像层不使用此过滤，自动收集整个根测试目录。插件测试仍按插件**分别启动 Python**，避免同名 `pkg` 模块混在一起。例如：

```bash
python -m pytest tests -q
python -m pytest custom-plugins/QQTextReader/tests -q
python -m pytest custom-plugins/QQComicTranslator/tests -q
```

CI 的 `public-tests` 自动发现所有普通环境测试；间接加载 SDK、无法从导入依赖判断的模块需声明 `pytestmark = pytest.mark.sdk`。`linux-compose` 先构建实际应用镜像，再用一次性容器收集整个根测试目录及六个插件的离线测试；测试依赖预先下载为 wheels，执行时 `--network none`，不启动业务入口。随后验收容器初始化、插件安装和更新回滚。主分支全部通过后才向 GHCR 发布该提交的两个镜像；首次发布需将包设为公开，非主分支和 PR 不发布。各层不能互相代替，新增 Linux 构建应以该提交的 CI 实际结果为准。

新增解析器行为或调整固定解析依赖时，提升 `qunling/files/cache.py` 的 `VERSION`，避免跨版本复用旧结构。知识库、后台采集、文件缓存、重复召回和能力测试应验证权限/版本/时效边界与调用次数，不能以“返回了成功字段”代替验证。

新增文件格式应实现真实类型检查、结构导航、分页／检索、明确能力与限制，并补合成样本和失败边界；不要只增加扩展名判断。产物遵循固定 schema，通过共享任务与授权资产发送，不能接受任意代码、宿主路径或收件人。共享处理子进程有资源限制，但不是完整 OS 沙箱；不要把它改成通用命令执行入口。

持久任务应覆盖重启、租约失效、重复请求、取消、配额和交付未知等状态。`file_workspace` 只有 `create`／`extract` 接受顶层 `deliver=true`，仅在用户明确要求发送时设置；Host outbox 使用原请求的可信路由，不给中间产物自动登记发送，也不重新尝试 `unconfirmed`。继续执行提示只能来自明确授权工具，不能绕过 24 轮对话预算、重置任务期限或重复付费请求。直接图片 LabelPlus 测试位于漫画插件，已有译文转 TXT 则应走共享文件生成与发送，不增加重复翻译路由。

`scripts/Verify-*.py` 多数用于已有原生开发环境，部分会调用实际模型或访问网络、产生费用；先读文件说明和 `--help`，不要作为所有新用户的安装步骤。脚本从自身位置寻找项目根目录；新增工具也应遵循这一点，不依赖调用者当前工作目录，不把运行数据写入 `scripts/`。

根目录只保留面向用户的 `.cmd` 与 `bot.sh` 入口。维护脚本移位时，要同步 Dockerfile 的复制白名单、CI、测试、systemd 示例和文档；使用 `tests/test_script_paths.py` 与 `tests/test_public_launchers.py` 验证包含空格的路径和非项目工作目录，不启动真实 QQ。

容器更新逻辑放在 `deploy/updater/`，设置网页只使用其认证 API。不要将 Docker socket 挂给业务或向导服务；目标版本必须固定 SHA，失败恢复必须覆盖镜像与五个业务数据卷。用替身测试只证明流程契约，发布前还需要隔离容器中的备份／失败恢复验证，不能拿在线 QQ 数据做破坏性实验。

## 通用对话质量

不为每个聊天例子增加专用路由或关键词回复。先检查模型是否收到正确的提问者、引用关系、图片内容和工具结果，再修改通用理解规则。用反话、普通陈述、背景缺失、新话题和多人归属等相反案例一起验证；自然语言结果需要人工评判，不能用“出现某个词”冒充理解正确。

`config/chat-style.txt` 的示例用于说明判断原则与语气，不是固定答案库。模型需要的背景应通过工具读取，群内约定由有范围的记忆保存；不能把一个群的梗全局硬编码。

## 发布检查

- 确认 `git status --short` 没有私有数据。只提交源码、测试、脱敏示例和文档。
- `.local/`、`langbot/`、`napcat/`、下载产物、数据库、密钥、聊天和 QQ 登录状态不可提交。Docker 构建另用显式白名单。
- 运行 `python scripts/Check-Release.py` 检查候选文件；自动检查不能替代人工审阅，也不重写 Git 历史。
- `.github/workflows/ci.yml` 检查离线用例、实际镜像内的 worker／插件、Linux 全新初始化与更新恢复。首次实际成功前，不宣称已通过该版本的 Linux 容器验收。
- 容器自动检查没有 QQ 账号和 AI Key。发布稳定版前人工验收登录、@触发、多人上下文、多源网页、文件阅读／生成／发送、直接图片与压缩包 LabelPlus。
- 共享资产、任务参数、结果与交付回执都可能包含私有内容；它们留在 LangBot 数据卷中，不进入测试样本、构建上下文或 Release。
- 上游升级必须一起更新锁定来源、摘要、补丁与测试，不依赖 `latest`。
- 遵守各组件许可，提供修改版 Runtime 对应源码与构建信息；根 Apache 许可不覆盖所有上游组件。

不把私有运行镜像、数据卷或项目目录整体打包成 Release。公开发布无需上传本机已经安装的 QQ、NapCat、Python 环境或模型缓存。
