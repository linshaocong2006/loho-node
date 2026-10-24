#!/usr/bin/env bash
# LoHo Node v9.0.0 online bootstrap. The payload is pinned to a commit and SHA256.
set -euo pipefail
umask 077

loho_payload_url='https://raw.githubusercontent.com/linshaocong2006/loho-node/7cc6cf5ce69c39614f2ce9310866b2ecfb137142/loho-node-onekey.sh'
loho_payload_sha256='caa69440c96f1c5c9e399fe711c0176d6fa29288d667935d345d71510b5ec234'

if ! command -v sha256sum >/dev/null 2>&1; then
    echo '缺少 sha256sum，请先安装 coreutils。' >&2
    exit 1
fi
loho_download_dir=$(mktemp -d -t loho-download.XXXXXXXX)
trap 'rm -rf -- "$loho_download_dir"' EXIT
loho_installer="$loho_download_dir/loho-node-onekey.sh"

echo '正在下载 LoHo Node v9.0.0 安装器……'
if command -v curl >/dev/null 2>&1; then
    curl --fail --show-error --silent --location --retry 3 --connect-timeout 15 --max-time 120 \
        --proto '=https' --proto-redir '=https' --tlsv1.2 \
        --output "$loho_installer" "$loho_payload_url"
elif command -v wget >/dev/null 2>&1; then
    wget --https-only --secure-protocol=TLSv1_2 --timeout=30 --tries=3 \
        -q -O "$loho_installer" "$loho_payload_url"
else
    echo '缺少下载工具，请先运行：sudo apt-get update && sudo apt-get install -y curl' >&2
    exit 1
fi

if ! printf '%s  %s\n' "$loho_payload_sha256" "$loho_installer" | sha256sum --check --status; then
    echo '安装器 SHA256 校验失败，已停止，没有执行安装。' >&2
    exit 1
fi
echo '安装器校验通过。'

case "${1:-}" in
    --check|--help|-h|--extract-source)
        bash "$loho_installer" "$@" ;;
    *)
        if [[ "$EUID" -eq 0 ]]; then
            bash "$loho_installer" "$@"
        elif command -v sudo >/dev/null 2>&1; then
            sudo bash "$loho_installer" "$@"
        else
            echo '请切换到 root 用户后再执行；当前系统没有 sudo。' >&2
            exit 1
        fi ;;
esac
