# 固定 HTTPS 管理入口

管理台是服务器上的网页。部署一次 Nginx 和证书后，通过固定 HTTPS 网址访问；电脑无需安装程序，也无需保持 SSH 隧道。没有域名时可使用公网 IP 证书。首次在服务器终端执行：

```bash
sudo docker exec qq-ai-bot-wizard-1 python -m bootstrap.server --url
```

打开输出的专属链接后，口令会从地址栏移除，浏览器保留登录 30 天。可以收藏不带口令的固定网址；主动退出或 Cookie 过期后重新使用专属链接。完整链接与管理口令不要公开分享。

## 一次性部署

保留容器 `127.0.0.1:5310` 的端口映射，由服务器 Nginx 在 443 提供 HTTPS。5300、6099 和更新器端口不需要对公网开放。

1. 安装支持 IP 证书的 Certbot 5.4 或更新版本。建议放入独立虚拟环境 `/opt/qunling-certbot`，避免影响服务器已有证书工具。准备 `/var/lib/qunling-acme/.well-known/acme-challenge/`，由 Nginx 的 HTTP 80 站点直接提供文件；首次签发前可先仅加载示例配置的 HTTP 段，并将普通请求返回 404。
2. 开放 HTTP 80 的证书验证路径和 HTTPS 443。先通过外网读取一个临时验证文件，再签发证书。下例中的 `203.0.113.8` 要替换为真实公网 IP；可先在独立配置目录使用 `--staging` 验证，然后申请生产证书。

```bash
sudo /opt/qunling-certbot/bin/certbot certonly --non-interactive \
  --agree-tos --register-unsafely-without-email \
  --webroot --webroot-path /var/lib/qunling-acme \
  --preferred-profile shortlived --ip-address 203.0.113.8 \
  --cert-name qunling-console \
  --config-dir /etc/qunling-tls --work-dir /var/lib/qunling-tls \
  --logs-dir /var/log/qunling-tls
```

3. 将 [nginx.conf.example](nginx.conf.example) 的 IP 替换后加入 Nginx，先 `nginx -t` 再重载。IP 访问通常不发送 SNI，需要该 TCP 443 站点作为默认 TLS 站点；核对现有 `default_server`，域名站点继续按各自名称匹配。
4. 记录 HTTPS 地址和实际代理来源 IP。Docker 默认网络下，宿主机的代理请求通常从 Docker 网关进入容器；用 `docker inspect qq-ai-bot-wizard-1` 的 `NetworkSettings.Networks` 确认网关，不要直接照抄示例。

```bash
sudo docker exec qq-ai-bot-wizard-1 python -m bootstrap.access \
  --origin https://203.0.113.8 --proxy 172.19.0.1
sudo docker restart qq-ai-bot-wizard-1
```

地址及可信代理保存在 `setup-state/public-access.json`，随既有数据卷持久化。HTTP API 不提供修改此配置的接口。只有配置内的代理可通过 `X-Real-IP` 提供客户端 IP；Nginx 必须覆盖客户端自带的该请求头。公网写请求严格匹配 HTTPS Origin，登录 Cookie 带 Secure、HttpOnly 和 SameSite=Strict。

## 自动续期

IP 证书有效期短，必须自动续期。依据 [Let's Encrypt 的 IP 证书说明](https://letsencrypt.org/2026/03/11/shorter-certs-certbot/)，使用 shortlived 配置并设置部署钩子，成功续期后执行实际 Nginx 二进制的 `-t` 和 `-s reload`。宝塔的路径通常为 `/www/server/nginx/sbin/nginx`，不要假定是 `/usr/sbin/nginx`。

把仅 root 可写的重载脚本放到 `/etc/qunling-tls/renewal-hooks/deploy/`。安装本目录的 service、timer 到 `/etc/systemd/system/`，执行：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now qunling-certificate.timer
sudo /opt/qunling-certbot/bin/certbot renew --dry-run --run-deploy-hooks \
  --config-dir /etc/qunling-tls --work-dir /var/lib/qunling-tls \
  --logs-dir /var/log/qunling-tls
```

验收应包括外网证书校验、未登录访问被拒绝、HTTPS 登录及保存操作、Cookie 属性、关闭本地隧道后仍可访问，以及续期演练和定时器状态。现有其他站点的域名路由与证书也应核对。
