#!/usr/bin/env bash
# Private-repository downloader; see INSTALL_PRIVATE.md for the copy-paste command.
set +x
set -euo pipefail
umask 077

loho_payload_url='https://api.github.com/repos/linshaocong2006/loho-node/contents/loho-node-onekey.sh?ref=7cc6cf5ce69c39614f2ce9310866b2ecfb137142'
loho_payload_sha256='caa69440c96f1c5c9e399fe711c0176d6fa29288d667935d345d71510b5ec234'

if ! command -v sha256sum >/dev/null 2>&1; then
    echo '缺少 sha256sum，请先安装 coreutils。' >&2
    exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
    echo '缺少curl。root用户请先运行：apt-get update && apt-get install -y curl' >&2
    echo '普通用户请在上面两个apt-get命令前分别添加sudo。' >&2
    exit 1
fi

unset loho_private_token
IFS= read -rsp 'GitHub只读Token（粘贴后回车，输入不显示）：' loho_private_token </dev/tty
printf '\n'
if [[ ! "$loho_private_token" =~ ^[A-Za-z0-9_]+$ ]]; then
    unset loho_private_token
    echo 'Token格式不正确，请复制完整的GitHub Token。' >&2
    exit 1
fi
loho_download_dir=$(mktemp -d -t loho-private.XXXXXXXX)
trap 'unset loho_private_token; rm -rf -- "$loho_download_dir"' EXIT
loho_installer="$loho_download_dir/loho-node-onekey.sh"

echo '正在从私有仓库下载 LoHo Node v9.0.0 安装器……'
# Credentials travel only over stdin. -q first disables user curlrc options.
if ! printf 'header = "Authorization: Bearer %s"\nheader = "Accept: application/vnd.github.raw+json"\n' "$loho_private_token" |
    curl -q --config - --fail --show-error --silent --retry 2 --connect-timeout 15 --max-time 120 \
        --proto '=https' --tlsv1.2 --output "$loho_installer" "$loho_payload_url"; then
    unset loho_private_token
    echo '下载失败，请检查Token有效期、是否选中loho-node仓库，以及Contents是否设为Read-only。' >&2
    exit 1
fi
unset loho_private_token

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
