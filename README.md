# RadeonPilot

AMD RDNA3 / RDNA4 GPU 向けの Linux 用 GUI 管理ツール（PySide6 / Qt6）。

> ⚠️ 開発中です。フェーズごとに機能を追加しています。

## 予定機能

- **監視**: コア/メモリクロック、消費電力、負荷、VRAM、ファン回転数、温度（edge / junction / mem）、直近60秒グラフ、複数GPUタブ
- **制御**（特権デーモン経由）: 電力上限、クロック上下限、パフォーマンスレベル、ファンカーブ（`gpu_od/fan_ctrl`）、デフォルトに戻す、プロファイル保存と起動時自動適用
- **GPU切り替えランチャー**: `DRI_PRIME` / `MESA_VK_DEVICE_SELECT` を設定してアプリを起動、Steam起動オプション文字列のコピー、`.desktop` 生成
- **インストーラー**: Arch / Fedora / Ubuntu 系対応の `install.sh` / `uninstall.sh`

## 構成

- GUI（一般ユーザー権限）: sysfs を直接読み取って監視
- デーモン（root, systemd）: `/run/radeonpilot.sock`（グループ `radeonpilot` のみアクセス可）で JSON メッセージを受け、検証後に sysfs へ書き込み

## ライセンス

MIT
