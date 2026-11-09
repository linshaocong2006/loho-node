LoHo 完整一键部署 10.2.1

运行：将 loho-node-onekey.sh 上传到空白 VPS 的 /root/ 后执行：
  sudo bash /root/loho-node-onekey.sh

支持范围：Debian 12、13；Ubuntu 22.04、24.04、26.04；x86_64 / aarch64；systemd。
这不是重装系统的工具，也不是已有节点的覆盖升级工具。
默认部署 VLESS + XHTTP(packet-up) + REALITY 主节点（TCP443），以及 XHTTP + TLS 备用节点和面板（TCP5000）。
安装时也可选仅 XHTTP + TLS，或 WebSocket + TLS。两个代理入口共用账号、日志和流量统计。
仅创建 ADMIN 一个代理用户，后续在用户管理网页新增、改名或删除。
节点账号用于代理连接，与网站登录账号独立设置。
包含当前仪表盘、连接历史、用户管理、通知、系统状态、登录设备管理。
支持记住设备30天；记录保留7天。不包含已放弃的 HTTP 内容采集。

安装前：准备域名 A 记录指向服务器；默认 IPv4 入口，若有 AAAA 记录需先正确处理。
使用普通 DNS 解析，不经 CDN 代理；域名每次安装时手动输入。
两个对外端口可在安装时修改。TCP80 用于申请及续期面板证书。
REALITY 目标域名是供握手使用的 HTTPS 站点。可先试 www.apple.com，只填写域名，不带 https://、端口或路径。
目标需从 VPS 通过公网 TCP443 连接，支持 TLS 1.3 / HTTP/2，并与节点域名不同。
安装器不会关闭防火墙、修改 SSH 或重装内核；服务商安全组和主机防火墙须允许 TCP80 和所选 HTTPS、REALITY 端口。
可选证书联系邮箱，空白可跳过；不需要邮箱密码。到期监测不依赖证书机构邮件。
主节点需要支持 VLESS + XHTTP + REALITY 的客户端；v2rayN/v2rayNG 使用兼容版本的 Xray 核心。
尚未确认版本的旧客户端可选 WebSocket 模式。实际手机端连通性由部署后导入验证。

输入：域名、端口[5000]、联系邮箱[可留空]、网站用户名[loho]、密码、部署方式[REALITY + XHTTP TLS 备用]。
选 REALITY 时继续填写节点端口[443]和目标域名。
输入校验失败时会提示并重新询问当前项。两次密码不一致时重新输入并确认密码。REALITY 目标的连通性检查失败时可更换目标，已填写的其他设置会保留。
可选开启系统自带BBR；默认不修改。仅检测到内核支持时应用独立sysctl文件。
安装结束后：
  面板 https://你的域名:5000/node-view/
  sudo cat /root/loho-node-install.txt
两条专属链接保存在上述 root 私有文件内；主节点二维码在登录后的用户管理页面查看。
密码以散列保存，不保留明文。不要公开导入链接、二维码或安装结果文件。

维护命令（安装完成后才可用）：
  sudo loho-node status            检查服务、采集、证书及续期任务
  sudo loho-node check             检查Xray/Nginx配置及本机TLS网页登录保护
  sudo loho-node check --probe     实测各个已部署的代理入口
  sudo loho-node reset-password    修改网站密码，原记住设备会失效
  sudo loho-node renew-test        运行证书续期演练，不签发生产证书
  sudo loho-node links             查看当前用户的导入链接
初始安装结果文件仅是快照，新增/改名/删除后用网页或 links 命令读取当前配置。
安装器本身：--help / --check / --preflight / --extract-source DIR。
--check 只核验内置代码和资源，不能代替在目标VPS实际安装。

维护机制：Certbot定时自动续期，成功后检查并reload Nginx，Xray无需因续期重启。
日志每日或达到20MiB时轮换，最多保留7份；单独每小时检查轮换条件。
轮换使用本机Xray LoggerService重开日志，不通过重启节点切断现有连接。
独立的维护任务定期检查续期、证书过期时间、采集异常与磁盘余量；系统状态页显示结果。
ntfy功能初始关闭；到网页填入服务和主题后才能收到通知。
通知规则支持多选用户，每个用户独立计算冷却与无连接间隔；已有单用户规则自动兼容。
页头保留四个主菜单，圆形账号头像菜单集中系统状态、登录设备与退出登录；状态集中为紧凑一行。
在系统状态页的外观设置中切换深浅主题，浏览器记住选择。
手机客户端入口：/node-view/client.html。手机使用底部导航，iPad 使用侧栏和横屏用户分栏。
iPad 在 Safari 添加到主屏幕即可独立窗口使用；安卓 APK 首次输入服务器地址。
客户端沿用网站登录与通知服务；离线不缓存账号配置或连接记录。
配置和服务状态失败会自动停止本次新装并清理本次创建的应用文件；系统依赖与证书保留。
不提供用户操作的版本回退命令。已有生产节点会被拒绝覆盖。

内核固定 Xray 26.10.21，使用官方 Release，安装前核对内置SHA256。
Nginx/OpenSSL/Certbot采用发行版软件包；不编译旧版OpenSSL、不跳过下载证书校验。
支持系统版本是适配范围；实际全新VPS安装由你执行，不能把本地模拟验证当作每个发行版的实机验证。
安装后仍须从电脑/手机导入链接，确认外网至该服务器的实际可达性。
GitHub 仓库中的 install.sh 下载并校验新装包。旧版 upgrade.sh 属于旧站点维护流程，新装 REALITY 请使用 install.sh。

参考：
  https://github.com/wulabing/xray_docker （REALITY + XHTTP 架构参考）
  https://xtls.github.io/en/config/transports/reality.html
  https://github.com/XTLS/Xray-core/releases/tag/v26.10.21
  https://xtls.github.io/config/transports/splithttp.html
  https://eff-certbot.readthedocs.io/en/stable/using.html
二维码qrcode与图标授权：见 vendor/qrcode-LICENSE.txt 和 IDENTITY_NOTICES.txt。
