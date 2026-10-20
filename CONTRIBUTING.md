# 开发与发布验证

框架和插件来源见 [ARCHITECTURE](docs/ARCHITECTURE.md)，许可边界见 [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md)。新功能优先放独立插件；兼容修复保留可重放补丁，不修改用户正在运行的缓存。

## 离线测试

使用 Python 3.12 环境，安装 `pytest requests pyyaml qrcode pillow` 后，在仓库根目录执行：

```bash
python -m pytest tests -q
```

这是部署、初始化、补丁与传输契约测试，不调用模型、不登录 QQ。插件测试需要插件声明的依赖；在安装了固定 SDK 的开发环境中单独执行对应 `custom-plugins/<名称>/tests`，避免多个插件的同名模块混在同一解释器中。

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
- `.github/workflows/ci.yml` 检查离线用例与 Linux 容器的全新初始化。首次实际成功前，不宣称已通过 Linux 容器验收。
- 容器自动检查没有 QQ 账号和 AI Key。发布稳定版前人工验收登录、@触发、多人上下文、图片、TXT、漫画和附件收发。
- 上游升级必须一起更新锁定来源、摘要、补丁与测试，不依赖 `latest`。
- 遵守各组件许可，提供修改版 Runtime 对应源码与构建信息；根 Apache 许可不覆盖所有上游组件。

不把私有运行镜像、数据卷或项目目录整体打包成 Release。公开发布无需上传本机已经安装的 QQ、NapCat、Python 环境或模型缓存。
