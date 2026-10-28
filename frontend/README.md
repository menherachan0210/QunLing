# 群灵管理台

React + TypeScript + Vite 的静态管理界面。生产环境直接由现有 Python 管理服务提供 `bootstrap/web`，不运行 Node 服务。旧页暂保留在 `/legacy`，用于兼容既有部署脚本；默认入口为新版。

```text
npm ci
npm run build
npm test
```

构建同时生成运行依赖许可。`bootstrap/web` 随代码提交，CI 重新构建并检查一致性，避免服务器升级时缺少前端工具链或部署过期页面。开发时 `npm run dev` 将 API 代理到本机 5310。

浏览器测试使用 `tests/server.py` 的虚构群聊与记忆，沿用生产 HTTP 认证、Cookie、静态资源和 CSP；不会访问线上服务。Windows 使用已安装的 Chrome，Linux 先运行 `npx playwright install --with-deps chromium`。可用 `CONSOLE_TEST_PYTHON` 指定具备公开测试依赖的 Python。测试结果写入根目录 `.local/console-browser-results`。

管理会话使用 30 天的 HttpOnly、SameSite=Strict Cookie；服务器仅保存令牌哈希，轮换管理口令使旧会话失效。写请求仍检查 Host、Origin、权限与版本。普通配置草稿存入当前标签页的 sessionStorage，API Key 只留在内存；退出登录清除草稿。记忆编辑继续使用群范围和逐条版本校验。

试聊通过 Host 的 `/console/preview` 运行独立、限时、可取消的任务，使用当前配置的判断、聊天、复核模型和界面草稿人格。每个机器人同一时间只允许一轮试聊，重复请求编号不重复执行。默认不读取记忆，可明确选择本群成员进行只读回忆。试聊不进入 QQ 消息队列，不发送消息、不写画像或互动约定，也不执行工具；需要工具的内容会明确提示。浏览器内切换群和页签会保留当前试聊，退出管理会话后清除。

桌面入口运行 `scripts/Open-Console.ps1`。本机用户配置位于 `%LOCALAPPDATA%\QunLing\Console`，包含已固定主机身份的 `ssh_config`、专用密钥、`known_hosts` 与 Windows 当前用户 DPAPI 保护的 `login.dpapi`。专用服务器公钥的本地转发目标限制为 `127.0.0.1:5310`，禁止登录 Shell。入口负责隐藏运行、健康检测、重复打开复用和断线重连；配置与凭据不放入仓库。更换电脑需要重新配置专用密钥与登录口令，不能直接复制 DPAPI 文件。
