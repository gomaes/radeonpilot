"""Per-GPU control page. All writes go through the daemon."""

from __future__ import annotations

import html
import math

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import control
from ..control import OD_FIELDS, ControlState
from ..protocol import DaemonClient, DaemonError, DaemonUnavailable
from ..sysfs import GpuInfo, is_asleep, wake
from .fan_curve import FanCurveEditor

PERF_LEVEL_LABELS = {
    "auto": "auto — 自動（既定）",
    "low": "low — 最低クロックに固定",
    "high": "high — 最高クロックに固定",
    "manual": "manual — 手動",
    "profile_standard": "profile_standard — 標準クロックに固定（計測用）",
    "profile_min_sclk": "profile_min_sclk — コアクロック最小",
    "profile_min_mclk": "profile_min_mclk — メモリクロック最小",
    "profile_peak": "profile_peak — ピーク（最大クロック固定）",
}

OD_HELP = """
<b>OverDrive（クロック・電圧・ファンカーブ）は利用できません。</b><br>
理由: {reason}<br><br>
有効にするには、カーネルパラメータ <code>amdgpu.ppfeaturemask=0xffffffff</code> を追加して再起動してください。
<ul>
<li>GRUB: <code>/etc/default/grub</code> の <code>GRUB_CMDLINE_LINUX_DEFAULT</code> に追記し、
<code>sudo update-grub</code>（Ubuntu） / <code>sudo grub-mkconfig -o /boot/grub/grub.cfg</code>（Arch）</li>
<li>Fedora: <code>sudo grubby --update-kernel=ALL --args="amdgpu.ppfeaturemask=0xffffffff"</code></li>
<li>systemd-boot: <code>/boot/loader/entries/*.conf</code> の <code>options</code> 行に追記</li>
</ul>
再起動後、<code>cat /sys/module/amdgpu/parameters/ppfeaturemask</code> が <code>0xffffffff</code> になっていることを確認してください。<br>
注意: OverDrive を有効にするとカーネルが taint 状態になります。電力上限とパフォーマンスレベルは OverDrive なしでも変更できます。
"""

SOFT_LIMIT_TEXT = (
    "{watts} W はドライバの下限 {min_w:g} W より低いため、次のように動作します。\n\n"
    "・ハードウェアの電力上限はドライバの下限 {min_w:g} W に設定します。\n"
    "・デーモンが消費電力を監視し、{target} を自動で下げて {watts} W に近づけます"
    "（ソフトウェア制限・実験的）。\n"
    "・負荷が急に増えると、数秒間は目標を超えることがあります。\n"
    "・クロックを下限まで下げても目標に届かない場合は、状態欄に表示します。\n"
    "・GPU がアイドル/スリープ中は読み書きしません（深いアイドルを妨げません）。\n"
    "・デーモンが停止するとクロックを元に戻します。\n\n"
    "適用しますか？"
)

LIMIT_STATUS = {
    "starting": "開始待ち",
    "sleeping": "GPU はスリープ中（何も読み書きしていません）",
    "idle": "アイドル（スリープを妨げないよう読み取りを間引いています）",
    "ok": "目標以下で動作中",
    "limiting": "制限中",
    "floor": "⚠ クロックを下限まで下げても目標を超えています",
    "missing": "GPU が見つかりません",
}

CONFIRM_TEXT = (
    "以下の変更を適用します。\n\n{changes}\n\n"
    "クロックや電圧の変更は、システムの不安定化・フリーズ・データ破損、"
    "場合によってはハードウェアの損傷を引き起こす可能性があります。"
    "値はドライバが報告する範囲内で検証されますが、その範囲内でも安定動作は保証されません。\n\n"
    "適用しますか？"
)


def _banner(kind: str) -> QLabel:
    colors = {"error": ("#5c1a1a", "#ffd9d9"), "warning": ("#5c4a12", "#fff1c2")}
    fg, bg = colors[kind]
    label = QLabel()
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.RichText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setStyleSheet(f"QLabel {{ color: {fg}; background: {bg}; border-radius: 4px; padding: 8px; }}")
    label.hide()
    return label


def describe_settings(settings: dict) -> str:
    lines = []
    if "perf_level" in settings:
        lines.append(f"パフォーマンスレベル: {settings['perf_level']}")
    if "power_cap_w" in settings:
        lines.append(f"電力上限: {settings['power_cap_w']} W")
    if "power_target_w" in settings:
        lines.append(f"電力目標: {settings['power_target_w']:g} W（ソフトウェア制限）")
    for key, value in (settings.get("od") or {}).items():
        if key in OD_FIELDS:
            lines.append(f"{OD_FIELDS[key][1]}: {value} {OD_FIELDS[key][2]}")
    if settings.get("fan_curve"):
        pts = ", ".join(f"{t}°C→{p}%" for t, p in settings["fan_curve"])
        lines.append(f"ファンカーブ: {pts}")
    return "\n".join(lines) or "（設定なし）"


class ControlTab(QWidget):
    status_message = Signal(str)

    def __init__(self, gpu: GpuInfo, client: DaemonClient, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.gpu = gpu
        self.client = client
        self.state: ControlState | None = None
        self.daemon_ok = False
        self.profiles: dict = {}
        self.boot_profile: str | None = None
        self.od_spins: dict[str, QSpinBox] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        layout = QVBoxLayout(body)

        self.daemon_banner = _banner("error")
        self.od_banner = _banner("warning")
        layout.addWidget(self.daemon_banner)
        layout.addWidget(self.od_banner)
        self.sleep_box = QWidget()
        sl = QHBoxLayout(self.sleep_box)
        sl.setContentsMargins(0, 0, 0, 0)
        self.sleep_label = QLabel(
            "💤 この GPU は省電力スリープ中（runtime suspend）です。"
            "スリープを妨げないよう、設定は読み取っていません。"
        )
        self.sleep_label.setWordWrap(True)
        self.wake_btn = QPushButton("GPU を起こして読み込む")
        self.wake_btn.clicked.connect(self.wake_gpu)
        sl.addWidget(self.sleep_label, 1)
        sl.addWidget(self.wake_btn)
        self.sleep_box.hide()
        layout.addWidget(self.sleep_box)

        # --- power cap
        self.power_box = QGroupBox("電力上限 / 電力目標 (power1_cap)")
        pl = QVBoxLayout(self.power_box)
        row = QHBoxLayout()
        self.power_slider = QSlider(Qt.Orientation.Horizontal)
        self.power_spin = QSpinBox()
        self.power_spin.setSuffix(" W")
        self.power_slider.valueChanged.connect(self.power_spin.setValue)
        self.power_spin.valueChanged.connect(self.power_slider.setValue)
        self.power_apply = QPushButton("適用")
        self.power_apply.clicked.connect(self.apply_power)
        self.power_pct = QLabel()
        self.power_spin.valueChanged.connect(self._update_power_pct)
        row.addWidget(self.power_slider, 1)
        row.addWidget(self.power_spin)
        row.addWidget(self.power_pct)
        row.addWidget(self.power_apply)
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("プリセット（既定比）:"))
        self.preset_buttons: dict[int, QPushButton] = {}
        for pct in control.POWER_PRESETS_PCT:
            btn = QPushButton(f"{pct:+d}%" if pct else "既定")
            btn.clicked.connect(lambda _=False, p=pct: self.set_power_preset(p))
            self.preset_buttons[pct] = btn
            preset_row.addWidget(btn)
        preset_row.addStretch(1)
        self.power_info = QLabel()
        self.power_info.setWordWrap(True)
        self.soft_status = QLabel()
        self.soft_status.setWordWrap(True)
        pl.addLayout(row)
        pl.addLayout(preset_row)
        pl.addWidget(self.power_info)
        pl.addWidget(self.soft_status)
        layout.addWidget(self.power_box)

        # --- performance level
        self.perf_box = QGroupBox("パフォーマンスレベル (power_dpm_force_performance_level)")
        prow = QHBoxLayout(self.perf_box)
        self.perf_combo = QComboBox()
        for level in control.PERF_LEVELS:
            self.perf_combo.addItem(PERF_LEVEL_LABELS[level], level)
        self.perf_apply = QPushButton("適用")
        self.perf_apply.clicked.connect(self.apply_perf)
        prow.addWidget(self.perf_combo, 1)
        prow.addWidget(self.perf_apply)
        layout.addWidget(self.perf_box)

        # --- OverDrive clocks / voltage
        self.od_box = QGroupBox("クロック / 電圧 (pp_od_clk_voltage)")
        ol = QVBoxLayout(self.od_box)
        self.od_form = QFormLayout()
        ol.addLayout(self.od_form)
        orow = QHBoxLayout()
        self.od_apply = QPushButton("適用…")
        self.od_apply.clicked.connect(self.apply_od)
        self.od_reset = QPushButton("クロック/電圧をデフォルトに戻す")
        self.od_reset.clicked.connect(self.reset_od)
        orow.addStretch(1)
        orow.addWidget(self.od_reset)
        orow.addWidget(self.od_apply)
        ol.addLayout(orow)
        layout.addWidget(self.od_box)

        # --- fan curve
        self.fan_box = QGroupBox("ファンカーブ (gpu_od/fan_ctrl/fan_curve)")
        fl = QVBoxLayout(self.fan_box)
        self.fan_status = QLabel()
        self.fan_status.setWordWrap(True)
        self.fan_editor = FanCurveEditor()
        frow = QHBoxLayout()
        self.fan_suggest = QPushButton("推奨カーブを入力")
        self.fan_suggest.clicked.connect(self.suggest_fan)
        self.fan_reset = QPushButton("ドライバの自動制御に戻す")
        self.fan_reset.clicked.connect(self.reset_fan)
        self.fan_apply = QPushButton("適用")
        self.fan_apply.clicked.connect(self.apply_fan)
        frow.addWidget(self.fan_suggest)
        frow.addStretch(1)
        frow.addWidget(self.fan_reset)
        frow.addWidget(self.fan_apply)
        fl.addWidget(self.fan_status)
        fl.addWidget(self.fan_editor)
        fl.addLayout(frow)
        layout.addWidget(self.fan_box)

        # --- profiles
        self.profile_box = QGroupBox("プロファイル（/etc/radeonpilot/config.json）")
        vl = QVBoxLayout(self.profile_box)
        r1 = QHBoxLayout()
        self.profile_combo = QComboBox()
        self.profile_combo.currentIndexChanged.connect(self._sync_boot_check)
        self.profile_apply = QPushButton("適用…")
        self.profile_apply.clicked.connect(self.apply_profile)
        self.profile_delete = QPushButton("削除")
        self.profile_delete.clicked.connect(self.delete_profile)
        r1.addWidget(self.profile_combo, 1)
        r1.addWidget(self.profile_apply)
        r1.addWidget(self.profile_delete)
        r2 = QHBoxLayout()
        self.profile_save = QPushButton("現在適用中の設定を保存…")
        self.profile_save.clicked.connect(self.save_profile)
        self.boot_check = QCheckBox("起動時にこのプロファイルを自動適用")
        self.boot_check.clicked.connect(self.toggle_boot)
        r2.addWidget(self.profile_save)
        r2.addStretch(1)
        r2.addWidget(self.boot_check)
        self.profile_info = QLabel()
        self.profile_info.setWordWrap(True)
        vl.addLayout(r1)
        vl.addLayout(r2)
        vl.addWidget(self.profile_info)
        layout.addWidget(self.profile_box)

        # --- global actions
        grow = QHBoxLayout()
        self.reload_btn = QPushButton("再読込")
        self.reload_btn.clicked.connect(self.refresh)
        self.reset_all_btn = QPushButton("すべてデフォルトに戻す…")
        self.reset_all_btn.clicked.connect(self.reset_all)
        grow.addWidget(self.reload_btn)
        grow.addStretch(1)
        grow.addWidget(self.reset_all_btn)
        layout.addLayout(grow)
        layout.addStretch(1)

        self.soft_limit: dict | None = None
        self.od_shown: dict[str, int] = {}
        # Live limiter status: asks the daemon only (never touches the GPU).
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self._poll_limiter)
        self.status_timer.start(2000)
        self.refresh()

    # ------------------------------------------------------------ state -> UI

    def refresh(self) -> None:
        self.daemon_ok, err = self.client.available()
        if self.daemon_ok:
            self.daemon_banner.hide()
        else:
            self.daemon_banner.setText(
                "<b>デーモンに接続できないため、設定は変更できません（表示のみ）。</b><br>" + html.escape(err or "")
            )
            self.daemon_banner.show()
        if is_asleep(self.gpu):
            # Reading the settings would keep (or on older kernels, wake) the GPU up.
            self.state = None
            self.sleep_box.show()
            self.od_banner.hide()
            self._fill_profiles()
            self._update_enabled()
            return
        self.sleep_box.hide()
        self.state = st = control.read_control_state(self.gpu)
        self.soft_limit = None
        if self.daemon_ok:
            try:
                self.soft_limit = self.client.request("limiter_status", gpu=self.gpu.pci_address)
            except DaemonError:
                pass
        if st.od_available:
            self.od_banner.hide()
        else:
            self.od_banner.setText(OD_HELP.format(reason=html.escape(st.od_block_reason)))
            self.od_banner.show()

        self._fill_power(st)
        idx = self.perf_combo.findData(st.perf_level)
        if idx >= 0:
            self.perf_combo.setCurrentIndex(idx)
        self._fill_od(st)
        self._fill_fan(st)
        self._fill_profiles()
        self._update_enabled()

    def _power_bounds(self, st: ControlState) -> tuple[int, int, bool]:
        """(lowest selectable W, highest W, whether the low end uses the software limiter)."""
        p = st.power
        soft = self.daemon_ok and control.sclk_limit_key(st.od) is not None
        floor = control.soft_floor_uw(p) if soft else None
        lo = math.ceil(floor / 1_000_000) if floor else int(p.min_w)
        return min(lo, int(p.min_w)), int(p.max_w), soft

    def _fill_power(self, st: ControlState) -> None:
        p = st.power
        if p is None or p.min_w is None or p.max_w is None:
            self.power_info.setText("このGPUは電力上限の範囲を報告していません。")
            return
        lo, hi, soft = self._power_bounds(st)
        value = self.soft_limit["target_w"] if self.soft_limit else (p.current_w or p.min_w)
        for w in (self.power_slider, self.power_spin):
            w.blockSignals(True)
            w.setRange(lo, hi)
            w.setValue(int(round(value)))
            w.blockSignals(False)
        self._update_power_pct()
        default = f"{p.default_w:g} W" if p.default_w is not None else "不明"
        text = (f"ハードウェア上限: {p.current_w:g} W　ドライバの範囲: {p.min_w:g}〜{p.max_w:g} W"
                f"　デフォルト: {default}")
        if soft:
            text += f"\n{int(p.min_w) - 1} W 以下はソフトウェア制限（クロックを自動で下げる・実験的）で実現します。"
        elif self.daemon_ok:
            text += "\nドライバの下限未満にはできません（ソフトウェア制限には OverDrive が必要です）。"
        self.power_info.setText(text)
        self._show_soft_status()

    def _update_power_pct(self) -> None:
        p = self.state.power if self.state else None
        if p is None or not p.default_w:
            self.power_pct.setText("")
            return
        pct = (self.power_spin.value() / p.default_w - 1) * 100
        below = p.min_w is not None and self.power_spin.value() < p.min_w
        self.power_pct.setText(f"既定比 {pct:+.0f}%" + ("（ソフト）" if below else ""))

    def _show_soft_status(self) -> None:
        sl = self.soft_limit
        if not sl:
            self.soft_status.setText("")
            return
        text = f"ソフトウェア制限: 目標 {sl['target_w']:g} W — {LIMIT_STATUS.get(sl['status'], sl['status'])}"
        unit = "MHz"
        name = "コアクロック上限" if sl["key"] == "sclk_max" else "コアクロック オフセット"
        if sl["current"] != sl["base"]:
            text += f"（{name} {sl['current']} {unit}、設定値 {sl['base']} {unit}、下限 {sl['floor']} {unit}）"
        if sl.get("last_power_w") is not None and sl["status"] not in ("sleeping",):
            text += f"　直近の消費電力: {sl['last_power_w']:.0f} W"
        self.soft_status.setText(text)

    def _poll_limiter(self) -> None:
        if not self.isVisible() or not self.daemon_ok or not self.soft_limit:
            return
        try:
            self.soft_limit = self.client.request("limiter_status", gpu=self.gpu.pci_address)
        except DaemonError:
            return
        self._show_soft_status()

    def set_power_preset(self, pct: int) -> None:
        st = self.state
        if st is None or st.power is None or not st.power.default_w:
            return
        want = round(st.power.default_w * (1 + pct / 100))
        lo, hi, soft = self._power_bounds(st)
        value = min(max(want, lo), hi)
        self.power_spin.setValue(value)
        if value != want:
            why = "OverDrive が無効なため" if not soft else "ソフトウェア制限の下限のため"
            self.status_message.emit(f"{pct:+d}% ({want} W) は{why}選べません。{value} W にしました")

    def _fill_od(self, st: ControlState) -> None:
        keys = st.od.supported() if st.od else []
        if list(self.od_spins) != keys:
            while self.od_form.rowCount():
                self.od_form.removeRow(0)
            self.od_spins = {}
            for key in keys:
                spin = QSpinBox()
                self.od_spins[key] = spin
                _, label, unit = OD_FIELDS[key]
                spin.setSuffix(f" {unit}")
                self.od_form.addRow(label, spin)
            if not keys:
                self.od_form.addRow(QLabel("利用可能な項目がありません。"))
        self.od_shown = dict(st.od.values) if st.od else {}
        if self.soft_limit and self.soft_limit["key"] in self.od_shown:
            self.od_shown[self.soft_limit["key"]] = self.soft_limit["base"]  # the user's value
        for key, spin in self.od_spins.items():
            rng = st.od.range_for(key)
            spin.setRange(rng.lo, rng.hi)
            spin.setValue(self.od_shown[key])
            spin.setToolTip(f"ドライバの報告範囲: {rng.lo}〜{rng.hi} {OD_FIELDS[key][2]}")
            label = self.od_form.labelForField(spin)
            if label is not None:
                label.setText(f"{OD_FIELDS[key][1]}（{rng.lo}〜{rng.hi}）")

    def _fill_fan(self, st: ControlState) -> None:
        fc = st.fan_curve
        if fc is None:
            self.fan_status.setText(
                "ファンカーブは利用できません。" if st.od_available else "OverDrive が無効なため利用できません。"
            )
            return
        if fc.is_driver_default:
            self.fan_status.setText(
                "現在: ドライバの自動制御（カスタムカーブ未設定）。下は編集用の推奨カーブです。"
            )
            points = control.suggested_fan_curve(fc)
        else:
            self.fan_status.setText("現在: カスタムカーブを適用中。")
            points = fc.points
        self.fan_status.setText(
            self.fan_status.text()
            + f"　範囲: {fc.temp_range.lo}〜{fc.temp_range.hi}°C / {fc.pwm_range.lo}〜{fc.pwm_range.hi}%"
            + "（温度は junction/hotspot 基準）"
        )
        self.fan_editor.set_curve(points, fc.temp_range, fc.pwm_range)

    def _fill_profiles(self) -> None:
        current = self.profile_combo.currentData()
        self.profiles, self.boot_profile = {}, None
        if self.daemon_ok:
            try:
                data = self.client.request("list_profiles", gpu=self.gpu.pci_address)
                self.profiles, self.boot_profile = data["profiles"], data["boot_profile"]
            except DaemonError:
                pass
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        for name in sorted(self.profiles):
            label = f"{name}（起動時に適用）" if name == self.boot_profile else name
            self.profile_combo.addItem(label, name)
        idx = self.profile_combo.findData(current if current in self.profiles else self.boot_profile)
        self.profile_combo.setCurrentIndex(max(idx, 0))
        self.profile_combo.blockSignals(False)
        self._sync_boot_check()

    def _sync_boot_check(self) -> None:
        name = self.profile_combo.currentData()
        self.boot_check.setChecked(name is not None and name == self.boot_profile)
        if name in self.profiles:
            self.profile_info.setText(describe_settings(self.profiles[name]).replace("\n", "　/　"))
        else:
            self.profile_info.setText("保存済みのプロファイルはありません。")
        self._update_enabled()

    def _update_enabled(self) -> None:
        st = self.state
        ok = self.daemon_ok and st is not None
        power_ok = ok and st.power is not None and st.power.min_w is not None and st.power.max_w is not None
        for w in (self.power_slider, self.power_spin, self.power_apply, *self.preset_buttons.values()):
            w.setEnabled(power_ok)
        if st is None:  # asleep: nothing read, nothing to edit
            for box in (self.perf_box, self.od_box, self.fan_box):
                box.setEnabled(False)
            self.reset_all_btn.setEnabled(self.daemon_ok)
            self.profile_save.setEnabled(False)
            has_profile = self.profile_combo.currentData() is not None
            for w in (self.profile_apply, self.profile_delete, self.boot_check):
                w.setEnabled(self.daemon_ok and has_profile)
            return
        self.perf_box.setEnabled(ok and st.perf_level is not None)
        self.od_box.setEnabled(ok and st.od_available and bool(self.od_spins))
        self.fan_box.setEnabled(ok and st.od_available and st.fan_curve is not None)
        has_profile = self.profile_combo.currentData() is not None
        self.profile_save.setEnabled(ok)
        for w in (self.profile_apply, self.profile_delete, self.boot_check):
            w.setEnabled(ok and has_profile)
        self.reset_all_btn.setEnabled(ok)

    # ------------------------------------------------------------ actions

    def _call(self, cmd: str, done: str, **params) -> tuple[bool, object]:
        try:
            result = self.client.request(cmd, gpu=self.gpu.pci_address, **params)
        except DaemonUnavailable as exc:
            QMessageBox.critical(self, "デーモンに接続できません", str(exc))
            self.refresh()
            return False, None
        except DaemonError as exc:
            title = {
                "validation": "値が拒否されました（何も書き込まれていません）",
                "write_failed": "書き込みに失敗しました",
                "denied": "アクセスが拒否されました",
            }.get(exc.kind, "エラー")
            QMessageBox.critical(self, title, str(exc))
            self.refresh()
            return False, None
        self.status_message.emit(f"{self.gpu.card}: {done}")
        self.refresh()
        return True, result

    def _confirm(self, title: str, text: str) -> bool:
        answer = QMessageBox.question(
            self, title, text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def apply_power(self) -> None:
        watts = self.power_spin.value()
        p = self.state.power if self.state else None
        if p is not None and p.min_w is not None and watts < p.min_w:
            key = control.sclk_limit_key(self.state.od)
            target = "コアクロック上限" if key == "sclk_max" else "コアクロック オフセット"
            if not self._confirm("ソフトウェア電力制限の確認",
                                 SOFT_LIMIT_TEXT.format(watts=watts, min_w=p.min_w, target=target)):
                return
            self._call("set_power_target", f"電力目標を {watts} W に設定しました（ソフトウェア制限）", watts=watts)
        else:
            self._call("set_power_target", f"電力上限を {watts} W に設定しました", watts=watts)

    def wake_gpu(self) -> None:
        try:
            if self.daemon_ok:
                self.client.request("wake", gpu=self.gpu.pci_address)
            else:
                wake(self.gpu)
        except (DaemonError, OSError) as exc:
            QMessageBox.warning(self, "GPU を起こせませんでした", str(exc))
        self.refresh()

    def apply_perf(self) -> None:
        level = self.perf_combo.currentData()
        self._call("set_perf_level", f"パフォーマンスレベルを {level} に設定しました", level=level)

    def od_changes(self) -> dict[str, int]:
        if not self.state or not self.state.od:
            return {}
        return {k: s.value() for k, s in self.od_spins.items() if s.value() != self.od_shown.get(k)}

    def apply_od(self) -> None:
        changes = self.od_changes()
        if not changes:
            QMessageBox.information(self, "変更なし", "現在の値から変更されていません。")
            return
        lines = [
            f"・{OD_FIELDS[k][1]}: {self.od_shown[k]} → {v} {OD_FIELDS[k][2]}"
            for k, v in changes.items()
        ]
        if not self._confirm("クロック/電圧の変更の確認", CONFIRM_TEXT.format(changes="\n".join(lines))):
            return
        self._call("set_od", "クロック/電圧を適用しました", values=changes)

    def reset_od(self) -> None:
        if self._confirm("確認", "クロック/電圧をドライバのデフォルトに戻しますか？"):
            self._call("reset_od", "クロック/電圧をデフォルトに戻しました")

    def suggest_fan(self) -> None:
        fc = self.state.fan_curve if self.state else None
        if fc:
            self.fan_editor.set_curve(control.suggested_fan_curve(fc), fc.temp_range, fc.pwm_range)

    def apply_fan(self) -> None:
        self._call("set_fan_curve", "ファンカーブを適用しました", points=self.fan_editor.points())

    def reset_fan(self) -> None:
        self._call("reset_fan_curve", "ファンをドライバの自動制御に戻しました")

    def reset_all(self) -> None:
        if self._confirm(
            "確認",
            "このGPUのパフォーマンスレベル・電力上限・クロック/電圧・ファンカーブを"
            "すべてドライバのデフォルトに戻しますか？\n（保存済みプロファイルは削除されません）",
        ):
            self._call("reset", "すべての設定をデフォルトに戻しました")

    def save_profile(self) -> None:
        name, ok = QInputDialog.getText(
            self, "プロファイルの保存",
            "プロファイル名（現在GPUに適用されている設定が保存されます）:",
            text=self.profile_combo.currentData() or "",
        )
        if not ok or not name:
            return
        if name in self.profiles and not self._confirm("上書きの確認", f"プロファイル「{name}」を上書きしますか？"):
            return
        good, _ = self._call("save_profile", f"プロファイル「{name}」を保存しました", name=name)
        if good:
            idx = self.profile_combo.findData(name)
            if idx >= 0:
                self.profile_combo.setCurrentIndex(idx)

    def apply_profile(self) -> None:
        name = self.profile_combo.currentData()
        if name is None:
            return
        settings = self.profiles.get(name, {})
        text = f"プロファイル「{name}」を適用します。\n\n{describe_settings(settings)}"
        if settings.get("od"):
            text = CONFIRM_TEXT.format(changes=f"プロファイル「{name}」\n{describe_settings(settings)}")
        if self._confirm("プロファイルの適用", text):
            self._call("apply_profile", f"プロファイル「{name}」を適用しました", name=name)

    def delete_profile(self) -> None:
        name = self.profile_combo.currentData()
        if name is not None and self._confirm("確認", f"プロファイル「{name}」を削除しますか？"):
            self._call("delete_profile", f"プロファイル「{name}」を削除しました", name=name)

    def toggle_boot(self, checked: bool) -> None:
        name = self.profile_combo.currentData()
        if name is None:
            return
        if checked:
            settings = self.profiles.get(name, {})
            if settings.get("od") and not self._confirm(
                "起動時自動適用の確認",
                f"プロファイル「{name}」はクロック/電圧の設定を含みます。\n"
                "起動のたびに自動で適用されます。不安定な設定だと起動直後から不安定になる可能性があります。\n"
                "（問題が起きた場合は sudo systemctl disable radeonpilot-daemon で無効化できます）\n\n"
                "有効にしますか？",
            ):
                self.boot_check.setChecked(False)
                return
            self._call("set_boot_profile", f"起動時に「{name}」を適用するよう設定しました", name=name)
        else:
            self._call("set_boot_profile", "起動時の自動適用を解除しました", name=None)
