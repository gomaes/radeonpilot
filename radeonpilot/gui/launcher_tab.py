"""GPU launcher page: registered apps, Steam launch options, .desktop files."""

from __future__ import annotations

import shlex
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import launcher
from ..launcher import AppEntry, AppStore
from ..sysfs import GpuInfo


def _gpu_combo(gpus: list[GpuInfo]) -> QComboBox:
    combo = QComboBox()
    for gpu in gpus:
        combo.addItem(f"{gpu.card}: {gpu.name}  [{gpu.pci_address}]", gpu.pci_address)
    return combo


class AppDialog(QDialog):
    def __init__(self, gpus: list[GpuInfo], app: AppEntry | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("アプリの編集" if app else "アプリの追加")
        self.gpus = {g.pci_address: g for g in gpus}
        self._app = app
        form = QFormLayout(self)
        self.name = QLineEdit(app.name if app else "")
        self.command = QLineEdit(app.command if app else "")
        self.command.setPlaceholderText("例: vkcube  /  /opt/game/start.sh --fullscreen")
        self.workdir = QLineEdit(app.workdir if app else "")
        self.workdir.setPlaceholderText("（任意）")
        self.icon = QLineEdit(app.icon if app else "")
        self.icon.setPlaceholderText("（任意）アイコン名またはファイル")
        self.gpu = _gpu_combo(gpus)
        if app and app.gpu not in self.gpus:
            self.gpu.addItem(f"（未検出）{app.gpu}", app.gpu)
        if app:
            self.gpu.setCurrentIndex(max(self.gpu.findData(app.gpu), 0))
        self.env_preview = QLabel()
        self.env_preview.setWordWrap(True)
        self.gpu.currentIndexChanged.connect(self._update_preview)

        form.addRow("名前", self.name)
        form.addRow("コマンド", self._with_browse(self.command, self._browse_command))
        form.addRow("作業ディレクトリ", self._with_browse(self.workdir, self._browse_workdir))
        form.addRow("アイコン", self._with_browse(self.icon, self._browse_icon))
        form.addRow("実行するGPU", self.gpu)
        form.addRow("設定される環境変数", self.env_preview)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.resize(560, self.sizeHint().height())
        self._update_preview()

    def _with_browse(self, edit: QLineEdit, handler) -> QWidget:
        host = QWidget()
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        btn = QPushButton("参照…")
        btn.clicked.connect(handler)
        row.addWidget(edit, 1)
        row.addWidget(btn)
        return host

    def _browse_command(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "実行ファイルを選択")
        if path:
            self.command.setText(shlex.quote(path))
            if not self.name.text():
                self.name.setText(Path(path).stem)

    def _browse_workdir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "作業ディレクトリを選択")
        if path:
            self.workdir.setText(path)

    def _browse_icon(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "アイコンを選択", filter="画像 (*.png *.svg *.xpm *.ico)")
        if path:
            self.icon.setText(path)

    def _update_preview(self) -> None:
        gpu = self.gpus.get(self.gpu.currentData())
        self.env_preview.setText(launcher.env_prefix(gpu) if gpu else "（GPUが検出されていません）")

    def _accept(self) -> None:
        entry = self.entry()
        if not entry.name.strip():
            QMessageBox.warning(self, "入力エラー", "名前を入力してください。")
            return
        try:
            entry.argv()
        except ValueError as exc:
            QMessageBox.warning(self, "入力エラー", str(exc))
            return
        self.accept()

    def entry(self) -> AppEntry:
        kwargs = dict(
            name=self.name.text().strip(),
            command=self.command.text().strip(),
            gpu=self.gpu.currentData() or "",
            workdir=self.workdir.text().strip(),
            icon=self.icon.text().strip(),
        )
        if self._app:
            kwargs["id"] = self._app.id
        return AppEntry(**kwargs)


class LauncherTab(QWidget):
    status_message = Signal(str)

    def __init__(self, gpus: list[GpuInfo], store: AppStore | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.gpus = gpus
        self.gpu_by_pci = {g.pci_address: g for g in gpus}
        self.store = store or AppStore()
        self.apps = self.store.load()

        layout = QVBoxLayout(self)

        apps_box = QGroupBox("登録アプリ")
        al = QVBoxLayout(apps_box)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["名前", "GPU", ".desktop", "コマンド"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.doubleClicked.connect(lambda _i: self.launch_selected())
        al.addWidget(self.table)

        row1 = QHBoxLayout()
        self.add_btn = QPushButton("追加…")
        self.edit_btn = QPushButton("編集…")
        self.del_btn = QPushButton("削除")
        self.run_btn = QPushButton("▶ 起動")
        self.add_btn.clicked.connect(self.add_app)
        self.edit_btn.clicked.connect(self.edit_app)
        self.del_btn.clicked.connect(self.delete_app)
        self.run_btn.clicked.connect(self.launch_selected)
        for b in (self.add_btn, self.edit_btn, self.del_btn):
            row1.addWidget(b)
        row1.addStretch(1)
        row1.addWidget(self.run_btn)
        row2 = QHBoxLayout()
        self.desktop_btn = QPushButton(".desktop を作成/更新")
        self.undesktop_btn = QPushButton(".desktop を削除")
        self.app_steam_btn = QPushButton("Steam起動オプションをコピー")
        self.desktop_btn.clicked.connect(self.create_desktop)
        self.undesktop_btn.clicked.connect(self.remove_desktop)
        self.app_steam_btn.clicked.connect(self.copy_app_steam)
        row2.addWidget(self.desktop_btn)
        row2.addWidget(self.undesktop_btn)
        row2.addStretch(1)
        row2.addWidget(self.app_steam_btn)
        al.addLayout(row1)
        al.addLayout(row2)
        layout.addWidget(apps_box, 1)

        steam_box = QGroupBox("Steam 起動オプション")
        sl = QVBoxLayout(steam_box)
        sl.addWidget(QLabel(
            "Steam でゲームを右クリック →「プロパティ」→「起動オプション」に貼り付けると、選択したGPUで起動します。"
            "（Steam 本体は起動済みのため、Steam のゲームは上の「起動」ではなくこちらを使ってください）"
        ))
        srow = QHBoxLayout()
        self.steam_gpu = _gpu_combo(gpus)
        self.steam_text = QLineEdit()
        self.steam_text.setReadOnly(True)
        self.steam_copy = QPushButton("コピー")
        self.steam_gpu.currentIndexChanged.connect(self._update_steam)
        self.steam_copy.clicked.connect(lambda: self._copy(self.steam_text.text()))
        srow.addWidget(self.steam_gpu)
        srow.addWidget(self.steam_text, 1)
        srow.addWidget(self.steam_copy)
        sl.addLayout(srow)
        layout.addWidget(steam_box)
        for label in (self.findChildren(QLabel)):
            label.setWordWrap(True)

        self._reload_table()
        self._update_steam()

    # ------------------------------------------------------------ helpers

    def _reload_table(self) -> None:
        self.table.setRowCount(len(self.apps))
        for row, app in enumerate(self.apps):
            gpu = self.gpu_by_pci.get(app.gpu)
            gpu_text = f"{gpu.card}: {launcher.gpu_label(gpu)}" if gpu else f"（未検出）{app.gpu}"
            desktop = "作成済み" if launcher.desktop_path(app).exists() else "—"
            for col, text in enumerate((app.name, gpu_text, desktop, app.command)):
                self.table.setItem(row, col, QTableWidgetItem(text))
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._update_buttons()

    def _selected(self) -> AppEntry | None:
        rows = self.table.selectionModel().selectedRows()
        return self.apps[rows[0].row()] if rows else None

    def _update_buttons(self) -> None:
        has = self._selected() is not None
        for b in (self.edit_btn, self.del_btn, self.run_btn, self.desktop_btn, self.undesktop_btn, self.app_steam_btn):
            b.setEnabled(has)
        self.add_btn.setEnabled(bool(self.gpus))

    def _update_steam(self) -> None:
        gpu = self.gpu_by_pci.get(self.steam_gpu.currentData())
        self.steam_text.setText(launcher.steam_launch_options(gpu) if gpu else "")
        self.steam_copy.setEnabled(gpu is not None)

    def _copy(self, text: str) -> None:
        QGuiApplication.clipboard().setText(text)
        self.status_message.emit(f"クリップボードにコピーしました: {text}")

    def _gpu_for(self, app: AppEntry) -> GpuInfo | None:
        gpu = self.gpu_by_pci.get(app.gpu)
        if gpu is None:
            QMessageBox.warning(
                self, "GPUが見つかりません",
                f"「{app.name}」に指定された GPU {app.gpu} が検出されていません。編集してGPUを選び直してください。",
            )
        return gpu

    def _save(self) -> None:
        try:
            self.store.save(self.apps)
        except OSError as exc:
            QMessageBox.critical(self, "保存に失敗しました", str(exc))
        self._reload_table()

    # ------------------------------------------------------------ actions

    def add_app(self) -> None:
        dlg = AppDialog(self.gpus, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.apps.append(dlg.entry())
            self._save()
            self.table.selectRow(len(self.apps) - 1)

    def edit_app(self) -> None:
        app = self._selected()
        if app is None:
            return
        dlg = AppDialog(self.gpus, app, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new = dlg.entry()
        had_desktop = launcher.desktop_path(app).exists()
        if had_desktop:
            launcher.remove_desktop(app)
        self.apps[self.apps.index(app)] = new
        if had_desktop and new.gpu in self.gpu_by_pci:
            launcher.write_desktop(new, self.gpu_by_pci[new.gpu])
        self._save()

    def delete_app(self) -> None:
        app = self._selected()
        if app is None:
            return
        if QMessageBox.question(self, "確認", f"「{app.name}」を削除しますか？（.desktop も削除されます）") \
                != QMessageBox.StandardButton.Yes:
            return
        launcher.remove_desktop(app)
        self.apps.remove(app)
        self._save()

    def launch_selected(self) -> None:
        app = self._selected()
        gpu = self._gpu_for(app) if app else None
        if gpu is None:
            return
        try:
            proc = launcher.launch(app, gpu)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "起動に失敗しました", f"{app.command}\n\n{exc}")
            return
        self.status_message.emit(f"「{app.name}」を {gpu.card} ({gpu.pci_address}) で起動しました (PID {proc.pid})")

    def create_desktop(self) -> None:
        app = self._selected()
        gpu = self._gpu_for(app) if app else None
        if gpu is None:
            return
        try:
            path = launcher.write_desktop(app, gpu)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, ".desktop の作成に失敗しました", str(exc))
            return
        self.status_message.emit(f"作成しました: {path}")
        self._reload_table()

    def remove_desktop(self) -> None:
        app = self._selected()
        if app is None:
            return
        removed = launcher.remove_desktop(app)
        self.status_message.emit("削除しました" if removed else ".desktop はありませんでした")
        self._reload_table()

    def copy_app_steam(self) -> None:
        app = self._selected()
        gpu = self._gpu_for(app) if app else None
        if gpu is not None:
            self._copy(launcher.steam_launch_options(gpu))
