#!/usr/bin/env bash
# RadeonPilot installer (Arch / Fedora / Ubuntu・Debian 系)
#
#   ./install.sh [--user NAME] [--no-deps] [--yes]
#
# 行うこと:
#   - 依存パッケージの確認とインストール
#   - /opt/radeonpilot への配置と venv の作成
#   - systemd サービス radeonpilot-daemon の登録・有効化
#   - radeonpilot グループの作成とユーザーの追加
#   - /usr/share/applications/radeonpilot.desktop とアイコンの配置
#   - /usr/local/bin/radeonpilot の配置
set -euo pipefail

PREFIX=/opt/radeonpilot
BIN=/usr/local/bin/radeonpilot
UNIT=/etc/systemd/system/radeonpilot-daemon.service
DESKTOP=/usr/share/applications/radeonpilot.desktop
ICON=/usr/share/icons/hicolor/scalable/apps/radeonpilot.svg
CONFIG_DIR=/etc/radeonpilot
GROUP=radeonpilot
SERVICE=radeonpilot-daemon

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m警告:\033[0m %s\n' "$*" >&2; }
die()   { printf '\033[1;31mエラー:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

ORIG_ARGS=("$@")
TARGET_USER=""
INSTALL_DEPS=1
ASSUME_YES=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --user) [[ $# -ge 2 ]] || usage 1; TARGET_USER="$2"; shift 2 ;;
        --user=*) TARGET_USER="${1#*=}"; shift ;;
        --no-deps) INSTALL_DEPS=0; shift ;;
        -y|--yes) ASSUME_YES=1; shift ;;
        -h|--help) usage 0 ;;
        *) warn "不明なオプション: $1"; usage 1 ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    info "root 権限が必要です。sudo で再実行します"
    exec sudo -- bash "$0" "${ORIG_ARGS[@]}"
fi

[[ -f "$SRC/radeonpilot/__init__.py" ]] || die "$SRC にソースがありません。リポジトリのルートで実行してください"

if [[ -z "$TARGET_USER" && -n "${SUDO_USER:-}" && "${SUDO_USER}" != root ]]; then
    TARGET_USER="$SUDO_USER"
fi
if [[ -z "$TARGET_USER" && $ASSUME_YES -eq 0 && -t 0 ]]; then
    read -rp "radeonpilot グループに追加するユーザー名（空欄でスキップ）: " TARGET_USER
fi
if [[ -n "$TARGET_USER" ]] && ! id "$TARGET_USER" >/dev/null 2>&1; then
    die "ユーザー $TARGET_USER は存在しません"
fi

# ------------------------------------------------------------------ deps
detect_distro() {
    local ids=""
    if [[ -r /etc/os-release ]]; then
        # shellcheck disable=SC1091
        ids="$(. /etc/os-release; echo "${ID:-} ${ID_LIKE:-}")"
    fi
    case " $ids " in
        *" arch "*|*" manjaro "*|*" endeavouros "*|*" cachyos "*) echo arch ;;
        *" fedora "*|*" rhel "*|*" centos "*|*" nobara "*|*" bazzite "*) echo fedora ;;
        *" debian "*|*" ubuntu "*|*" linuxmint "*|*" pop "*) echo debian ;;
        *) echo unknown ;;
    esac
}

DISTRO="$(detect_distro)"
info "ディストリビューション: $DISTRO"

if [[ $INSTALL_DEPS -eq 1 ]]; then
    info "依存パッケージをインストールします"
    case "$DISTRO" in
        arch)
            pacman -S --needed --noconfirm python libxkbcommon-x11 xcb-util-cursor libglvnd fontconfig hwdata ;;
        fedora)
            dnf install -y python3 libxkbcommon-x11 xcb-util-cursor mesa-libEGL fontconfig hwdata ;;
        debian)
            apt-get update
            DEBIAN_FRONTEND=noninteractive apt-get install -y \
                python3 python3-venv python3-pip libxcb-cursor0 libxkbcommon-x11-0 libegl1 libfontconfig1 pciutils ;;
        *)
            warn "未対応のディストリビューションです。Python 3.11+ (venv) と Qt6 の実行ライブラリ"
            warn "(libEGL, libxkbcommon-x11, xcb-util-cursor, fontconfig) を手動で入れてください" ;;
    esac
fi

command -v python3 >/dev/null || die "python3 が見つかりません"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
    || die "Python 3.11 以上が必要です（現在: $(python3 -V 2>&1)）"
python3 -c 'import venv, ensurepip' 2>/dev/null \
    || die "python3 の venv/ensurepip がありません（Debian/Ubuntu: python3-venv）"

# ------------------------------------------------------------------ files
if command -v systemctl >/dev/null && systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
    info "稼働中のデーモンを停止します（更新）"
    systemctl stop "$SERVICE"
fi

info "$PREFIX に配置します"
install -d -m 0755 "$PREFIX"
rm -rf "${PREFIX:?}/radeonpilot"
cp -r "$SRC/radeonpilot" "$PREFIX/radeonpilot"
find "$PREFIX/radeonpilot" -name __pycache__ -type d -prune -exec rm -rf {} +
install -m 0644 "$SRC/requirements.txt" "$SRC/LICENSE" "$SRC/README.md" "$PREFIX/"
install -m 0755 "$SRC/uninstall.sh" "$PREFIX/uninstall.sh"

if [[ -x "$PREFIX/venv/bin/python" ]] && "$PREFIX/venv/bin/python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    info "既存の venv を再利用します"
else
    info "venv を作成します"
    rm -rf "${PREFIX:?}/venv"
    python3 -m venv "$PREFIX/venv"
fi
info "Python パッケージ (PySide6) をインストールします"
"$PREFIX/venv/bin/python" -m pip install --quiet --upgrade pip
"$PREFIX/venv/bin/python" -m pip install --quiet -r "$PREFIX/requirements.txt"
SITE="$("$PREFIX/venv/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
echo "$PREFIX" > "$SITE/radeonpilot.pth"
"$PREFIX/venv/bin/python" -m compileall -q "$PREFIX/radeonpilot" >/dev/null
# The daemon runs this code as root: nobody else may be able to modify it.
chown -R root:root "$PREFIX"
chmod -R go-w "$PREFIX"
"$PREFIX/venv/bin/python" -P -c 'import radeonpilot.daemon.server, PySide6.QtWidgets' \
    || die "インストールした環境の読み込みに失敗しました"

info "$BIN を配置します"
install -d -m 0755 "$(dirname "$BIN")"
cat > "$BIN" <<LAUNCHER
#!/bin/sh
exec $PREFIX/venv/bin/python -P -m radeonpilot "\$@"
LAUNCHER
chmod 0755 "$BIN"

info "デスクトップエントリとアイコンを配置します"
install -D -m 0644 "$SRC/radeonpilot/data/radeonpilot.svg" "$ICON"
install -D -m 0644 "$SRC/packaging/radeonpilot.desktop" "$DESKTOP"
if command -v gtk-update-icon-cache >/dev/null; then gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true; fi
if command -v update-desktop-database >/dev/null; then update-desktop-database -q /usr/share/applications || true; fi

# ------------------------------------------------------------------ group
if ! getent group "$GROUP" >/dev/null; then
    info "グループ $GROUP を作成します"
    groupadd --system "$GROUP"
fi
if [[ -n "$TARGET_USER" ]]; then
    info "ユーザー $TARGET_USER を $GROUP グループに追加します"
    usermod -aG "$GROUP" "$TARGET_USER"
else
    warn "グループに追加するユーザーが指定されていません（後で: sudo usermod -aG $GROUP ユーザー名）"
fi

install -d -m 0755 "$CONFIG_DIR"

# ------------------------------------------------------------------ systemd
install -m 0644 "$SRC/packaging/radeonpilot-daemon.service" "$UNIT"
if [[ -d /run/systemd/system ]]; then
    info "systemd サービス $SERVICE を有効化・起動します"
    systemctl daemon-reload
    systemctl enable "$SERVICE" >/dev/null
    systemctl restart "$SERVICE"
    sleep 1
    systemctl is-active --quiet "$SERVICE" || warn "サービスが起動していません: journalctl -u $SERVICE を確認してください"
else
    warn "systemd が動作していないため、サービスの有効化をスキップしました（$UNIT は配置済み）"
fi

# ------------------------------------------------------------------ summary
echo
info "インストールが完了しました"
echo "  起動: アプリメニューの「RadeonPilot」または端末で radeonpilot"
if [[ -n "$TARGET_USER" ]]; then
    echo "  * グループ追加を反映するため、$TARGET_USER は一度ログアウトして再ログインしてください。"
fi
MASK_FILE=/sys/module/amdgpu/parameters/ppfeaturemask
if [[ -r $MASK_FILE ]]; then
    mask="$(cat "$MASK_FILE")"
    if (( (mask & 0x4000) == 0 )); then
        echo "  * OverDrive が無効です (ppfeaturemask=$mask)。クロック/電圧/ファンカーブを変更するには"
        echo "    カーネルパラメータ amdgpu.ppfeaturemask=0xffffffff を追加して再起動してください（README 参照）。"
    fi
else
    echo "  * amdgpu モジュールが読み込まれていません。"
fi
