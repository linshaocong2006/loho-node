#!/usr/bin/env bash
# LoHo full fresh-server installer 10.1.6. Run with bash, not sh.
set -euo pipefail
umask 077
case "${1:-}" in
  --help|-h)
    echo 'LoHo 一键部署 10.1.6：sudo bash loho-node-onekey.sh'
    echo '支持 Debian 12/13、Ubuntu 22.04/24.04/26.04；x86_64/aarch64；systemd 空白服务器。'
    echo '默认端口 5000；只创建 ADMIN；域名和密码在安装时输入。'
    echo '--check 只检查内置资源；--preflight 检查本机；--extract-source DIR 导出完整源码。'
    exit 0;;
esac
if ! command -v python3 >/dev/null 2>&1; then
    if [[ -n "${1:-}" ]]; then echo '此检查模式需要 Python 3.10+；未安装软件。' >&2; exit 1; fi
    if [[ "$EUID" -ne 0 ]]; then echo '请用 sudo bash 运行。' >&2; exit 1; fi
    source /etc/os-release
    case "${ID:-}:${VERSION_ID:-}" in
      debian:12|debian:13|ubuntu:22.04|ubuntu:24.04|ubuntu:26.04) ;;
      *) echo '支持 Debian 12/13、Ubuntu 22.04/24.04/26.04；未修改系统。' >&2; exit 1;;
    esac
    apt-get -o DPkg::Lock::Timeout=120 update
    DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=120 install -y --no-install-recommends python3
fi
loho_work=$(mktemp -d -t loho-onekey.XXXXXXXX)
trap 'rm -rf -- "$loho_work"' EXIT
cat > "$loho_work/bundle.b64" <<'LOHO_BUNDLE_END'
@@PAYLOAD@@
LOHO_BUNDLE_END
python3 - "$loho_work" "$@" <<'LOHO_LOADER_END'
import base64,gzip,hashlib,json,pathlib,subprocess,sys
if sys.version_info<(3,10):raise SystemExit('需要 Python 3.10+；请使用受支持的系统镜像。')
work=pathlib.Path(sys.argv[1])
raw=base64.b64decode((work/'bundle.b64').read_bytes())
if hashlib.sha256(raw).hexdigest()!='@@HASH@@':raise SystemExit('内置资源损坏，请重新下载。')
files=json.loads(gzip.decompress(raw));source=files.pop('_installer')
core=work/'installer.py'
source+='\nPAYLOAD_FILES='+repr(files)+'\nif __name__=="__main__":\n    try: main(PAYLOAD_FILES)\n    except KeyboardInterrupt: raise SystemExit("安装已中止。")\n    except Exception as error: raise SystemExit("未完成："+str(error))\n'
core.write_text(source,encoding='utf-8')
args=[sys.executable,str(core)]+sys.argv[2:]
if not sys.argv[2:]:
    # The loader's stdin is this heredoc; interactive input must use the real TTY.
    try:
        terminal=open('/dev/tty','r')
    except OSError:
        raise SystemExit('没有交互终端；请在SSH终端中执行 sudo bash /root/loho-node-onekey.sh。')
    with terminal:status=subprocess.call(args,stdin=terminal)
else:status=subprocess.call(args)
raise SystemExit(status)
LOHO_LOADER_END
