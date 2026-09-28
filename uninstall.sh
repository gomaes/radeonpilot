#!/usr/bin/env bash
# RadeonPilot uninstaller: install.sh で行ったことをすべて元に戻します。
#
#   ./uninstall.sh [--yes] [--keep-config] [--purge-user-data]
#
#   --yes              確認なしで実行（GPU設定はデフォルトに戻し、設定ファイルも削除）
#   --keep-config      /etc/radeonpilot（保存済みプロファイル）を残す
#   --purge-user-data  実行ユーザーの ~/.config/radeonpilot と生成した .desktop も削除
set -euo pipefail

PREFIX=/opt/radeonpilot
BIN=/usr/local/bin/radeonpilot
UNIT=/etc/systemd/system/radeonpilot-daemon.service
DESKTOP=/usr/share/applications/radeonpilot.desktop
ICON=/usr/share/icons/hicolor/scalable/apps/radeonpilot.svg
CONFIG_DIR=/etc/radeonpilot
SOCKET=/run/radeonpilot.sock
GROUP=radeonpilot
SERVICE=radeonpilot-daemon

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m警告:\033[0m %s\n' "$*" >&2; }

ORIG_ARGS=("$@")
ASSUME_YES=0
KEEP_CONFIG=0
PURGE_USER=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        -y|--yes) ASSUME_YES=1 ;;
        --keep-config) KEEP_CONFIG=1 ;;
        --purge-user-data) PURGE_USER=1 ;;
        -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) warn "不明なオプション: $1"; exit 1 ;;
    esac
    shift
done

if [[ $EUID -ne 0 ]]; then
    info "root 権限が必要です。sudo で再実行します"
    exec sudo -- bash "$0" "${ORIG_ARGS[@]}"
fi

ask() {  # ask "question" default(y/n)
    local answer
    if [[ $ASSUME_YES -eq 1 || ! -t 0 ]]; then
        [[ $2 == y ]]
        return
    fi
    read -rp "$1 [$([[ $2 == y ]] && echo Y/n || echo y/N)] " answer
    answer="${answer:-$2}"
    [[ $answer == [yY]* ]]
}

have_systemd=0
[[ -d /run/systemd/system ]] && command -v systemctl >/dev/null && have_systemd=1

if [[ $have_systemd -eq 1 ]] && systemctl list-unit-files "$SERVICE.service" >/dev/null 2>&1; then
    info "サービス $SERVICE を停止・無効化します"
    systemctl disable --now "$SERVICE" 2>/dev/null || true
fi

if [[ -x "$PREFIX/venv/bin/python" ]] && ask "GPU の設定（電力上限・クロック・ファンなど）をドライバのデフォルトに戻しますか？" y; then
    info "全GPUの設定をデフォルトに戻します"
    "$PREFIX/venv/bin/python" -P -m radeonpilot.daemon --reset-all \
        || warn "一部のリセットに失敗しました。再起動すればドライバのデフォルトに戻ります"
fi

info "ファイルを削除します"
rm -f "$UNIT"
[[ $have_systemd -eq 1 ]] && { systemctl daemon-reload; systemctl reset-failed "$SERVICE" 2>/dev/null || true; }
rm -f "$SOCKET" "$BIN" "$DESKTOP" "$ICON"
if [[ "$PREFIX" == /opt/radeonpilot && -d "$PREFIX" ]]; then
    rm -rf "$PREFIX"
fi
if command -v gtk-update-icon-cache >/dev/null; then gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true; fi
if command -v update-desktop-database >/dev/null; then update-desktop-database -q /usr/share/applications || true; fi

if [[ -d "$CONFIG_DIR" ]]; then
    if [[ $KEEP_CONFIG -eq 0 ]] && ask "保存済みプロファイル ($CONFIG_DIR) を削除しますか？" y; then
        rm -rf "$CONFIG_DIR"
    else
        info "$CONFIG_DIR を残しました"
    fi
fi

if getent group "$GROUP" >/dev/null; then
    info "グループ $GROUP を削除します"
    groupdel "$GROUP" || warn "グループ $GROUP を削除できませんでした"
fi

user="${SUDO_USER:-}"
if [[ -n "$user" && "$user" != root ]]; then
    home="$(getent passwd "$user" | cut -d: -f6)"
    user_files=()
    [[ -d "$home/.config/radeonpilot" ]] && user_files+=("$home/.config/radeonpilot")
    for f in "$home"/.local/share/applications/radeonpilot-*.desktop; do
        [[ -e "$f" ]] && user_files+=("$f")
    done
    if [[ ${#user_files[@]} -gt 0 ]]; then
        if [[ $PURGE_USER -eq 1 ]] || { [[ $ASSUME_YES -eq 0 ]] && ask "$user のランチャー登録と生成した .desktop (${#user_files[@]} 件) も削除しますか？" n; }; then
            # Not running update-desktop-database here: as root it would leave a root-owned cache in $home.
            rm -rf "${user_files[@]}"
        else
            info "ユーザーデータを残しました: ${user_files[*]}"
        fi
    fi
fi

info "アンインストールが完了しました"
