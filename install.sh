#!/usr/bin/env bash
set -euo pipefail
umask 077
case "${1:-}" in
    --help|-h)
        echo 'LoHo Node v9.1.2 安装器'
        echo '默认执行；--check 进行检查。'
        exit 0 ;;
esac
loho_payload_url='https://raw.githubusercontent.com/linshaocong2006/loho-node/fd5cb3f17812e17bc3b136cfaf69ff6e0f605906/loho-node-onekey.sh'
loho_payload_sha256='f4b3cbddfcb1950d71f80e476271bccd2eaedd43f701e209346f202ec438ab03'
if ! command -v sha256sum >/dev/null 2>&1; then
    echo '缺少 sha256sum，请先安装 coreutils。' >&2
    exit 1
fi

loho_download_dir=$(mktemp -d -t loho-download.XXXXXXXX)
trap 'rm -rf -- "$loho_download_dir"' EXIT
loho_installer="$loho_download_dir/loho-node-onekey.sh"
echo '正在下载 LoHo Node v9.1.2 安装器……'
if command -v curl >/dev/null 2>&1; then
    curl -q --fail --show-error --silent --location --retry 3 --connect-timeout 15 --max-time 120 \
        --proto '=https' --proto-redir '=https' --tlsv1.2 \
        --output "$loho_installer" "$loho_payload_url"
elif command -v wget >/dev/null 2>&1; then
    wget --https-only --secure-protocol=TLSv1_2 --timeout=30 --tries=3 \
        -q -O "$loho_installer" "$loho_payload_url"
else
    echo '缺少下载工具，请先安装 curl 或 wget。' >&2
    exit 1
fi
if ! printf '%s  %s\n' "$loho_payload_sha256" "$loho_installer" | sha256sum --check --status; then
    echo 'SHA256 校验失败，已停止，没有执行。' >&2
    exit 1
fi
echo '校验通过。'
case "${1:-}" in
    --check|--extract-source)
        bash "$loho_installer" "$@" ;;
    *)
        if [[ "$EUID" -eq 0 ]]; then
            bash "$loho_installer" "$@"
        elif command -v sudo >/dev/null 2>&1; then
            sudo bash "$loho_installer" "$@"
        else
            echo '请切换到 root 用户后再执行。' >&2
            exit 1
        fi ;;
esac
