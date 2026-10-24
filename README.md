# LoHo Node

在空白 VPS 上一次部署 Xray 节点和 LoHo 监控管理系统。当前安装版本：**9.0.0**。

## 私有仓库安装

此仓库已改为私有。原来的免登录 raw 下载命令不再适用。

先创建细粒度只读 Token：GitHub Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token。

- Token name：`loho-node-install`。
- Resource owner：`linshaocong2006`。
- Expiration：可选30天，安装完成后也可提前撤销。Token到期或撤销不影响已部署的代理和网站。
- Repository access：`Only select repositories`，仅选择 `loho-node`。
- Repository permissions：`Contents: Read-only`。Metadata只读权限由GitHub自动带上；其他权限不用添加。

生成后复制Token。在服务器终端粘贴并运行 [INSTALL_PRIVATE.md](INSTALL_PRIVATE.md) 中的完整命令，再按提示输入Token。

Token输入不回显，下载器不会把它写入凭据文件、命令参数或环境变量。下载完毕后清除当前变量，再校验固定版本的安装器并运行。Token不用提交到仓库，也不用发给任何人。

无需将安装文件上传到服务器。普通用户通过sudo安装，root直接安装。没有curl时先安装curl软件包。

## 安装前准备

- 使用空白服务器：Debian **12 / 13**，或 Ubuntu **22.04 / 24.04 / 26.04**；`x86_64` / `aarch64`，systemd。
- 域名 A 记录指向服务器公网 IPv4，使用普通 DNS 解析。首版为 IPv4 入口；安装前移除该域名的 AAAA 记录。
- 服务商安全组和主机防火墙允许 TCP **80** 与 **5000**。80 用于证书申请和自动续期；5000 是默认节点和网站端口。
- 安装时填写域名、网站用户名和密码；端口默认 5000，可以修改。证书联系邮箱可留空。

已有 Xray、V2Ray、LoHo 或其他 Nginx 站点时，安装器会停止，避免覆盖现有服务。它不会关闭防火墙、修改 SSH 或安装其他内核。

## 默认部署内容

- **VLESS + XHTTP（packet-up）+ TLS + Nginx**；安装时可选 WebSocket。
- 只创建一个代理用户 **ADMIN**。在网站的用户管理页面新增、改名、删除其他用户，查看专属链接和二维码。
- 网站账号和代理用户是两套独立凭据。
- 仪表盘、连接记录、分用户流量、站点图标、活动状态、通知规则、系统状态和登录设备管理。
- 访问记录默认保留 **7 天**；登录支持记住设备 **30 天**。
- ntfy 通知初始关闭，需在网页设置服务地址和主题。
- Certbot 自动续期，成功后检查并 reload Nginx。日志定期轮换，通过 Xray LoggerService 重开日志，不为轮换重启节点。
- BBR 可选，默认不修改，仅使用内核已支持的版本。

本系统记录代理连接的域名、目标、来源及流量等信息，不提供 HTTPS 正文、图片或视频内容采集。

## 安装完成后

打开 `https://你的域名:5000/node-view/`，用安装时设置的网站账号密码登录。

```bash
sudo cat /root/loho-node-install.txt
sudo loho-node status
sudo loho-node check
sudo loho-node check --probe
sudo loho-node links
sudo loho-node reset-password
sudo loho-node renew-test
```

`check --probe` 检查本机完整代理链路；仍需在电脑和手机导入节点，验证外部网络的实际连接。安装结果文件仅是初始快照，后续用户配置以网页或 `loho-node links` 为准。

## 核验与查看源码

只下载并检查内置代码和资源，不进行安装：在已下载本仓库文件的情况下，执行 `bash install.sh --check`，按提示输入只读Token。也可在私有安装命令最后的 `bash` 调用中添加 `--check`。

将安装器下载到本地后，可导出完整运行源码：

```bash
bash loho-node-onekey.sh --extract-source ./loho-source
```

每个版本的安装器和在线入口校验值见 [SHA256SUMS](SHA256SUMS)。当前 Xray 固定为 **26.10.21**，安装时核对官方发布资产的 SHA256；Nginx、OpenSSL、Certbot 使用发行版软件包。

已完成本地实际 Xray / Nginx TLS 链路、分用户统计和日志、身份归属、日志重开、登录保护及安装成功/失败流程验证。支持的系统版本是适配范围，不表示每个发行版都已在独立 VPS 上逐一实测。最终证书签发、软件源和公网可达性由目标服务器安装验证。

## 参考与许可

- 架构参考：[wulabing/V2Ray_ws-tls_bash_onekey](https://github.com/wulabing/V2Ray_ws-tls_bash_onekey)。本安装器不执行其旧版脚本。
- Xray：[XTLS/Xray-core](https://github.com/XTLS/Xray-core)。
- 证书工具：[Certbot](https://eff-certbot.readthedocs.io/en/stable/using.html)。
- 安装器内的二维码模块 qrcode 使用 BSD 许可，图标保留对应许可和品牌说明；导出源码后可查看 `vendor/qrcode-LICENSE.txt` 和 `IDENTITY_NOTICES.txt`。
