#!/usr/bin/env python3
# -*- coding: utf-8 -*-


from __future__ import annotations

import os
import sys
import time
import re
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple, Set

from PySide6.QtCore import Qt, QTimer, Signal, QObject, QRect, QUrl
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QDialog, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QPushButton, QComboBox, QDoubleSpinBox, QSpinBox, QSlider,
    QTabWidget, QGroupBox, QFileDialog, QMessageBox, QPlainTextEdit,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QSizePolicy, QFrame
)
from PySide6.QtGui import QIcon, QDesktopServices
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineUrlRequestInterceptor


import matplotlib
matplotlib.use("QtAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas

import imslib
from imslib import MHz, Percent, Degrees, RFChannel


# ----------------------------
# Resource path helper (PyInstaller-friendly)
# ----------------------------
def resource_path(relative: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


# Rendered LUT table size (matches typical device expectation / example LUTs)
TABLE_RENDER_SIZE = 2048


# -----------------------------
# Logging
# -----------------------------
def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


class ConsoleLogger:
    def __init__(self, widget: QPlainTextEdit):
        self._w = widget

    def log(self, msg: str):
        self._w.appendPlainText(f"{ts()}  {msg}")
        sb = self._w.verticalScrollBar()
        sb.setValue(sb.maximum())


# -----------------------------
# EventWaiter (compatible with your provided method)
# -----------------------------
import queue
import threading


class EventWaiter(imslib.IEventHandler):
    """
    Minimal event waiter:
      - listen_for(list_of_msgs)
      - waiter._watched is a set of msgs
      - wait(timeout) -> (msg, params)
    """
    def __init__(self):
        super().__init__()
        self._watched: Set[int] = set()
        self._q: "queue.Queue[Tuple[int, tuple]]" = queue.Queue()
        self._lock = threading.Lock()

    def listen_for(self, msgs: List[int]):
        with self._lock:
            self._watched.update(msgs)

    def EventAction(self, sender, message, *args):
        with self._lock:
            if message in self._watched:
                self._q.put((message, args))

    def wait(self, timeout: float = None) -> Tuple[int, tuple]:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("Timed out waiting for event.")


def _wait_for_terminal_event(waiter: EventWaiter, terminal_events: set, timeout_s: float) -> tuple[int, tuple]:
    deadline = time.perf_counter() + timeout_s
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise TimeoutError("Timed out waiting for terminal event.")
        msg, params = waiter.wait(timeout=remaining)
        if msg in terminal_events:
            return msg, params


def download_and_verify_compensation(
    ims: imslib.IMSSystem,
    comp: imslib.CompensationTable,
    timeout_s: float = 120.0,
    log: Optional[ConsoleLogger] = None,
    channel: Optional[imslib.RFChannel] = None,
) -> tuple[bool, float, float, int | None]:
    """
    Event-driven Download + Verify for CompensationTable.
    Returns (ok, download_seconds, verify_seconds, verify_error_code_or_None)
    """
    target = "GLOBAL"

    def _log(s: str):
        if log:
            log.log(s)

    try:
        if channel is None:
            ctdl = imslib.CompensationTableDownload(ims, comp)
        else:
            ctdl = imslib.CompensationTableDownload(ims, comp, channel)
    except TypeError as e:
        _log(f"[CTDL] ctor(ims, comp, channel) not supported ({e}); using global ctor.")
        ctdl = imslib.CompensationTableDownload(ims, comp)

    waiter = EventWaiter()

    DOWNLOAD_OK = {imslib.CompensationEvents_DOWNLOAD_FINISHED}
    DOWNLOAD_FAIL = {imslib.CompensationEvents_DOWNLOAD_ERROR}
    VERIFY_OK = {imslib.CompensationEvents_VERIFY_SUCCESS}
    VERIFY_FAIL = {imslib.CompensationEvents_VERIFY_FAIL}

    WATCHED = DOWNLOAD_OK | DOWNLOAD_FAIL | VERIFY_OK | VERIFY_FAIL
    waiter.listen_for(list(WATCHED))

    _log(f"[CTDL:{target}] Subscribing to {len(waiter._watched)} events: {sorted(list(waiter._watched))}")

    subscribed = []
    for evt in waiter._watched:
        try:
            ctdl.CompensationTableDownloadEventSubscribe(evt, waiter)
            subscribed.append(evt)
            _log(f"[CTDL:{target}] Subscribed evt={evt}")
        except Exception as e:
            _log(f"[CTDL:{target}] Subscribe FAILED evt={evt}: {e}")

    verify_err = None
    try:
        _log(f"[CTDL:{target}] Downloading Compensation Table ...")
        t0 = time.perf_counter()

        ok_start = ctdl.StartDownload()
        _log(f"[CTDL:{target}] StartDownload() -> {ok_start}")
        if not ok_start:
            return False, 0.0, 0.0, None

        dmsg, dparams = _wait_for_terminal_event(
            waiter, terminal_events=(DOWNLOAD_OK | DOWNLOAD_FAIL), timeout_s=timeout_s
        )
        download_s = time.perf_counter() - t0
        _log(f"[CTDL:{target}] Download terminal event={dmsg}, params={dparams}")
        _log(f"[CTDL:{target}] Download time: {download_s:.3f} s")

        if dmsg not in DOWNLOAD_OK:
            return False, download_s, 0.0, None

        _log(f"[CTDL:{target}] Verifying Compensation Table ...")
        t1 = time.perf_counter()

        ok_v = ctdl.StartVerify()
        _log(f"[CTDL:{target}] StartVerify() -> {ok_v}")
        if not ok_v:
            return False, download_s, 0.0, None

        vmsg, vparams = _wait_for_terminal_event(
            waiter, terminal_events=(VERIFY_OK | VERIFY_FAIL), timeout_s=timeout_s
        )
        verify_s = time.perf_counter() - t1
        _log(f"[CTDL:{target}] Verify terminal event={vmsg}, params={vparams}")
        _log(f"[CTDL:{target}] Verify time: {verify_s:.3f} s")

        if vmsg in VERIFY_OK:
            return True, download_s, verify_s, None

        try:
            verify_err = ctdl.GetVerifyError()
            _log(f"[CTDL:{target}] GetVerifyError() -> {verify_err}")
        except Exception as e:
            _log(f"[CTDL:{target}] GetVerifyError() failed: {e}")

        return False, download_s, verify_s, verify_err

    finally:
        for evt in subscribed:
            try:
                ctdl.CompensationTableDownloadEventUnsubscribe(evt, waiter)
                _log(f"[CTDL:{target}] Unsubscribed evt={evt}")
            except Exception:
                pass


# -----------------------------
# Mouse wheel adjust
# -----------------------------
from PySide6.QtCore import QEvent


class WheelFineAdjust(QObject):
    # Wheel            -> ± singleStep
    # CTRL + Wheel     -> ± 1 unit
    # SHIFT + Wheel    -> ± 5×pageStep
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Wheel and isinstance(obj, QSlider):
            delta = event.angleDelta().y()
            if delta == 0:
                return True

            sign = 1 if delta > 0 else -1
            mods = event.modifiers()

            if mods & Qt.ControlModifier:
                step = 1
            elif mods & Qt.ShiftModifier:
                step = 5 * obj.pageStep()
            else:
                step = obj.singleStep()

            obj.setValue(obj.value() + sign * step)
            return True
        return False


# -----------------------------
# LUT data
# -----------------------------
@dataclass
class LUTPoint:
    f_mhz: float
    ampl_pct: float
    phase_deg: float


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# -----------------------------
# Pre-connection device chooser
# -----------------------------
def scan_systems() -> list[imslib.IMSSystem]:
    conn = imslib.ConnectionList()
    systems = conn.Scan()
    return list(systems)


class DeviceSelectDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select iVCS / iVSA Device")
        self.setModal(True)

        self._systems: list[imslib.IMSSystem] = []
        self._splash_closed = False

        self._secret_keys = []
        self.trial_mode_requested = False

        self.setFocusPolicy(Qt.StrongFocus)

        lay = QVBoxLayout(self)

        self.lbl = QLabel("Scanning for devices...")
        lay.addWidget(self.lbl)

        row = QHBoxLayout()
        row.addWidget(QLabel("Device:"))
        self.cmb = QComboBox()
        row.addWidget(self.cmb, 1)
        lay.addLayout(row)

        self.btn_refresh = QPushButton("Rescan")
        self.btn_connect = QPushButton("Connect")
        self.btn_close = QPushButton("Close")

        btn_row = QHBoxLayout()
        btn_row.addWidget(self.btn_refresh)
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_connect)
        btn_row.addWidget(self.btn_close)
        lay.addLayout(btn_row)

        self.btn_refresh.clicked.connect(self.rescan)
        self.btn_connect.clicked.connect(self._on_connect)
        self.btn_close.clicked.connect(self.reject)

        self.rescan()

    # Ensures that .exe splash screen closes just before the connection dialogue appears.
    def showEvent(self, event):
        super().showEvent(event)
        self.activateWindow()
        self.raise_()
        self.setFocus()

        if not self._splash_closed:
            try:
                import pyi_splash
                pyi_splash.close()
            except Exception:
                pass
            self._splash_closed = True
    
    #allows boot without device
    def keyPressEvent(self, event):
        if event.modifiers() & Qt.ShiftModifier:
            key = event.key()

            if key == Qt.Key_I:
                self._secret_keys.append("I")
            elif key == Qt.Key_U:
                self._secret_keys.append("U")
            elif key == Qt.Key_K:
                self._secret_keys.append("K")
            else:
                self._secret_keys.clear()
                super().keyPressEvent(event)
                return

            self._secret_keys = self._secret_keys[-3:]

            if self._secret_keys == ["I", "U", "K"]:
                self.trial_mode_requested = True
                self.accept()
                return
        else:
            self._secret_keys.clear()

        super().keyPressEvent(event)

    def _system_label(self, s: imslib.IMSSystem) -> str:
        try:
            port = str(s.ConnPort())
        except Exception:
            port = "UnknownPort"
        try:
            model = str(s.Synth().Model())
        except Exception:
            model = "UnknownModel"
        return f"{port}  |  {model}"

    def rescan(self):
        self.cmb.clear()
        try:
            self.lbl.setText("Scanning for devices...")
            QApplication.processEvents()
            self._systems = list(scan_systems())
        except Exception as e:
            self._systems = []
            self.lbl.setText(f"Scan failed: {e}")
            self.btn_connect.setEnabled(False)
            return

        if not self._systems:
            self.lbl.setText("No iVCS/iVSA devices found.")
            self.btn_connect.setEnabled(False)
            return

        self.lbl.setText(f"Found {len(self._systems)} device(s). Select one and click Connect.")
        for s in self._systems:
            self.cmb.addItem(self._system_label(s))
        self.cmb.setCurrentIndex(0)
        self.btn_connect.setEnabled(True)

    def selected_system(self) -> imslib.IMSSystem | None:
        if not self._systems:
            return None
        i = self.cmb.currentIndex()
        if i < 0 or i >= len(self._systems):
            return None
        return self._systems[i]

    def _on_connect(self):
        sysobj = self.selected_system()
        if sysobj is None:
            return
        try:
            try:
                sysobj.Connect()
            except Exception as e_connect:
                try:
                    ok = sysobj.Open()
                except Exception as e_open:
                    raise RuntimeError(f"Connect() failed ({e_connect}); Open() also failed ({e_open})")
                if not ok:
                    raise RuntimeError(f"Connect() failed ({e_connect}); Open() returned False")
                sysobj.Connect()

            synth = sysobj.Synth()
            if not synth.IsValid():
                raise RuntimeError("Synthesiser is not valid on this system (check interface/connection).")

        except Exception as e:
            QMessageBox.critical(self, "Connect Failed", str(e))
            return

        self.accept()

    @staticmethod
    def get_connected_system(parent=None) -> imslib.IMSSystem | str | None:
        dlg = DeviceSelectDialog(parent)
        if dlg.exec() != QDialog.Accepted:
            return None
        if dlg.trial_mode_requested:
            return "trial_mode"
        return dlg.selected_system()


# -----------------------------
# Help Overlay (HTML from files, QTextBrowser)
# -----------------------------
# -----------------------------
# Help Overlay (HTML from files, QtWebEngine)
# -----------------------------
class _HelpPage(QWebEnginePage):
    """
    Custom page to intercept navigation:
    - help://open?file=... opens another local help HTML file
    - local file links are allowed
    - external links are blocked
    """
    def __init__(self, overlay, parent=None):
        super().__init__(parent)
        self._overlay = overlay

    def acceptNavigationRequest(self, url: QUrl, nav_type, isMainFrame: bool):
        u = url.toString()
        scheme = (url.scheme() or "").lower()

        # Internal navigation: help://open?file=...
        if scheme == "help" and url.host().lower() == "open":
            q = url.query()
            file_val = None
            for part in q.split("&"):
                if part.startswith("file="):
                    file_val = part.split("=", 1)[1]
                    break
            if file_val:
                self._overlay.load_help_file(file_val)
            return False
        
        if scheme in ("http", "https"):
            QDesktopServices.openUrl(url)   # opens default browser
            return False                    # don't navigate inside overlay

        # QtWebEngine uses these for setHtml() and internal pages
        if scheme in ("data", "about", "qrc", "chrome"):
            return True

        # Allow local files (css/images/html in your help folder)
        if url.isLocalFile() or scheme == "file":
            return True

        # Block only truly external navigation
        if scheme in ("http", "https", "mailto", "ftp"):
            QMessageBox.information(self._overlay, "Link", f"External link blocked:\n{u}")
            return False

        # Default: allow (prevents accidental blocking of internal engine schemes)
        return True


class HelpOverlay(QWidget):
    """
    Semi-transparent overlay that blocks interaction with underlying widgets.
    - Loads per-tab help from external HTML files (full HTML+CSS supported).
    - Scrollable (QtWebEngine).
    - Internal navigation inside overlay:
        <a href="help://open?file=tab2_tuning_advanced.html">Advanced</a>
    - Click outside panel (or press Esc / click a tab) closes.
    """
    closed = Signal()

    def __init__(self, parent: QWidget, html_file_provider):
        super().__init__(parent)
        self._html_file_provider = html_file_provider
        self._tab_index = 0

        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.setFocusPolicy(Qt.StrongFocus)

        self._panel = QFrame(self)
        self._panel.setObjectName("helpPanel")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._panel, 0, Qt.AlignCenter)

        panel_lay = QVBoxLayout(self._panel)
        panel_lay.setContentsMargins(14, 12, 14, 12)
        panel_lay.setSpacing(10)

        self._title = QLabel("Help")
        self._title.setStyleSheet("font-weight: 700; font-size: 14px; color: #ffffff;")
        panel_lay.addWidget(self._title)

        # --- WebEngine browser ---
        self._browser = QWebEngineView()
        self._page = _HelpPage(self, self._browser)
        self._browser.setPage(self._page)
        panel_lay.addWidget(self._browser, 1)

        self._hint = QLabel("Click outside this panel (or press Esc / click a tab) to close.")
        self._hint.setStyleSheet("color: #ffffff; font-size: 12px;")
        panel_lay.addWidget(self._hint)

        self.setStyleSheet("""
             HelpOverlay { background: rgba(255, 255, 255, 255); }
             QFrame#helpPanel { background: rgba(120, 120, 120, 255); border-radius: 12px; }
         """)

        self._help_dir = resource_path("help")

    def show_for_tab(self, tab_index: int, exclude_top_px: int = 0):
        self._tab_index = int(tab_index)

        parent = self.parentWidget()
        if parent is not None:
            r = parent.rect()
            self.setGeometry(QRect(0, exclude_top_px, r.width(), max(0, r.height() - exclude_top_px)))

        ow, oh = max(1, self.width()), max(1, self.height())
        self._panel.setMinimumWidth(int(0.72 * ow))
        self._panel.setMaximumWidth(int(0.86 * ow))
        self._panel.setMinimumHeight(int(0.7 * oh))
        self._panel.setMaximumHeight(int(0.9 * oh))

        self._load_tab_default()
        self.raise_()
        self.show()
        self.setFocus(Qt.OtherFocusReason)
        

    def _load_tab_default(self):
        title, html_path = self._html_file_provider(self._tab_index)
        self._title.setText(title)
        self._load_html_path(html_path)

    def load_help_file(self, file_name: str):
        html_path = os.path.join(self._help_dir, file_name)
        self._title.setText("Help")
        self._load_html_path(html_path)

    def _load_html_path(self, html_path: str):
        if not os.path.exists(html_path):
            # Render a simple inline page for missing file
            self._browser.setHtml(f"<h3>Missing help file</h3><p>{html_path}</p>")
            return

        # Set the base URL to the /help/ folder so relative CSS/images resolve
        base_url = QUrl.fromLocalFile(os.path.abspath(self._help_dir) + os.sep)

        try:
            with open(html_path, "r", encoding="utf-8") as f:
                html = f.read()
        except Exception as e:
            self._browser.setHtml(f"<h3>Failed to read help file</h3><p>{html_path}</p><pre>{e}</pre>", base_url)
            return

        # Load HTML with a base URL so <link href="css/help.css"> and <img src="images/..."> work.
        self._browser.setHtml(html, base_url)
        self._browser.setZoomFactor(0.85)   # 1.0 = 100%, 1.25 = 125%, 1.5 = 150%

    def hideEvent(self, e):
        super().hideEvent(e)
        self.closed.emit()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.hide()
            e.accept()
            return
        super().keyPressEvent(e)

    def mousePressEvent(self, e):
        p = e.position().toPoint()
        if not self._panel.geometry().contains(p):
            self.hide()
            e.accept()
            return
        super().mousePressEvent(e)


# -----------------------------
# Main GUI
# -----------------------------
class CompensationTool(QMainWindow):
    def __init__(self, ims_connected: imslib.IMSSystem):
        super().__init__()
        self.setWindowTitle("AO Device Compensation Tool")

        ico = resource_path("Isomet.ico")
        if os.path.exists(ico):
            self.setWindowIcon(QIcon(ico))

        self._wheel_filter = WheelFineAdjust(self)
        self._cursor_anno = None

        # SDK state
        self.ims: Optional[imslib.IMSSystem] = ims_connected
        self.synth = None
        self.cap = None
        self.vco: Optional[imslib.VCO] = None
        self.sp: Optional[imslib.SignalPath] = None
        self.sf = None

        # LUT model
        self.lut_points_global: List[LUTPoint] = []
        self._last_phase_cfunc = None
        self._last_ao_model = None
        self._last_ao_wavelength_um = None

        # LUT interpolation styles (SDK-backed)
        # These control how the CompensationFunction is rendered into the CompensationTable.
        self._interp_items = [
            ("Spot", imslib.CompensationFunction.InterpolationStyle_SPOT),
            ("Step", imslib.CompensationFunction.InterpolationStyle_STEP),
            ("Linear", imslib.CompensationFunction.InterpolationStyle_LINEAR),
            ("Linear Extend", imslib.CompensationFunction.InterpolationStyle_LINEXTEND),
            ("B-Spline", imslib.CompensationFunction.InterpolationStyle_BSPLINE),
        ]
        self.amp_interp_style = imslib.CompensationFunction.InterpolationStyle_LINEAR
        self.phase_interp_style = imslib.CompensationFunction.InterpolationStyle_LINEAR

        # Flags
        self._table_updating = False
        self._slider_updating = False
        self.sync_wipers = False
        self._sync_guard = False

        # UI root
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # Tabs
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        # -----------------------------
        # Help button row (below tabs, above console) — PROOF placement
        # -----------------------------
        help_row = QHBoxLayout()
        help_row.setContentsMargins(0, 0, 0, 0)
        help_row.setSpacing(8)
        help_row.addStretch(1)

        self.btn_help = QPushButton("Help")
        self.btn_help.setCheckable(True)
        self.btn_help.setChecked(False)
        self.btn_help.setToolTip("Show help for the current tab")
        self.btn_help.setMinimumWidth(90)

        help_row.addWidget(self.btn_help)
        root.addLayout(help_row)
        # -----------------------------

        # Build tabs
        self._build_tab_setup_generate()
        self._build_tab_tuning()
        self._build_tab_export_store()

        # Console
        root.addWidget(QLabel("Console"))
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(5000)
        root.addWidget(self.console, 0)
        self.log = ConsoleLogger(self.console)

        # Help overlay (blocks page interaction; tab-bar remains clickable)
        self._help_overlay = HelpOverlay(self.tabs, self._help_file_for_tab)
        self._help_overlay.hide()

        self.btn_help.toggled.connect(self._on_help_toggled)
        self._help_overlay.closed.connect(self._on_help_closed)
        self.tabs.currentChanged.connect(self._on_tab_changed_for_help)
        try:
            self.tabs.tabBarClicked.connect(self._on_tabbar_clicked_hide_help)
        except Exception:
            pass

        # Connected system
        try:
            self.synth = self.ims.Synth()
            if not self.synth.IsValid():
                raise RuntimeError("Synthesiser not valid (check connection/interface).")
            self.cap = self.synth.GetCap()
            self.sp = imslib.SignalPath(self.ims)
            self.sf = imslib.SystemFunc(self.ims)
            self.vco = imslib.VCO(self.ims)

            port = self.ims.ConnPort()
            model = self.synth.Model()
            self.log.log(f"[Connect] Connected to: {port} | {model}")
        except Exception:
            pass

        self.refresh_ao_models()
        self._set_enabled_state(True)

        # Apply startup settings (existing handlers)
        self.apply_startup_settings()

        self.log.log("[Init] Ready.")
        self.log.log("[UI] Help button created (below tabs) and connected.")
        self.log.log("[Safety] Overdrive indication: deflected optical power decreases as RF amplitude increases.")
        self.log.log("[Safety] Never maintain an overdriven condition for a prolonged period (risk of permanent AO damage).")

        # Tone debounce (ms)
        self._tone_debounce = QTimer(self)
        self._tone_debounce.setSingleShot(True)
        self._tone_debounce.setInterval(0.5)  # 50 ms
        self._tone_debounce.timeout.connect(self._apply_selected_point_to_tone)

    # ----------------- Help (external HTML per tab) -----------------
    def _help_file_for_tab(self, tab_index: int):
        """
        Return (title, html_path) for the current tab.
        NOTE: Adjust mapping if your tab count changes.
        """
        help_dir = resource_path("help")

        if tab_index == 0:
            return "Help — 1) Setup / Generate", os.path.join(help_dir, "tab1_setup.html")
        elif tab_index == 1:
            return "Help — 2) Tuning", os.path.join(help_dir, "tab2_tuning.html")
        else:
            return "Help — 3) Save / Store", os.path.join(help_dir, "tab3_store.html")

    def _on_help_toggled(self, checked: bool):
        if checked:
            tab_h = 0
            try:
                tab_h = self.tabs.tabBar().height()
            except Exception:
                pass
            # Leave tab bar clickable; overlay blocks the page controls
            self._help_overlay.show_for_tab(self.tabs.currentIndex(), exclude_top_px=tab_h)
        else:
            self._help_overlay.hide()

    def _on_help_closed(self):
        if self.btn_help.isChecked():
            self.btn_help.blockSignals(True)
            self.btn_help.setChecked(False)
            self.btn_help.blockSignals(False)

    def _on_tab_changed_for_help(self, idx: int):
        if self._help_overlay.isVisible():
            tab_h = 0
            try:
                tab_h = self.tabs.tabBar().height()
            except Exception:
                pass
            self._help_overlay.show_for_tab(idx, exclude_top_px=tab_h)

    def _on_tabbar_clicked_hide_help(self, _idx: int):
        if self._help_overlay.isVisible():
            self.btn_help.setChecked(False)

    # ----------------- Startup initialisation -----------------
    def apply_startup_settings(self):
        """Apply startup RF settings (DDS, CH1, CH2) using existing handlers."""
        if self.sp is None:
            return
        try:
            dds_v = int(self.sld_dds.value())
            w1_v = int(self.sld_w1.value())
            w2_v = int(self.sld_w2.value())

            self.log.log(
                f"[Startup] Applying startup settings: DDS={dds_v/10.0:.2f}%  CH1={w1_v/10.0:.2f}%  CH2={w2_v/10.0:.2f}%"
            )

            self.on_dds_changed(dds_v)
            self.on_w1_changed(w1_v)
            self.on_w2_changed(w2_v)

            self.sf.EnableAmplifier(False)
            self.sp.EnableImagePathCompensation(False, False)
            self.sp.SwitchRFAmplitudeControlSource(imslib.SignalPath.AmplitudeControl_INDEPENDENT)
            self.sp.ClearTone()

            self.log.log("[Startup] Startup RF settings applied.")
        except Exception as e:
            self.log.log(f"[Startup] Failed to apply startup settings: {e}")

    # ----------------- Tab 1: Setup / Generate -----------------
    def _build_tab_setup_generate(self):
        tab = QWidget()
        lay = QVBoxLayout(tab)

        box = QGroupBox("Starting LUT")
        bl = QVBoxLayout(box)
        form = QFormLayout()

        self.spin_fmin = QDoubleSpinBox()
        self.spin_fmin.setRange(1.0, 10000.0)
        self.spin_fmin.setDecimals(3)
        self.spin_fmin.setValue(100.0)
        self.spin_fmin.setSuffix(" MHz")

        self.spin_fmax = QDoubleSpinBox()
        self.spin_fmax.setRange(1.0, 10000.0)
        self.spin_fmax.setDecimals(3)
        self.spin_fmax.setValue(140.0)
        self.spin_fmax.setSuffix(" MHz")

        self.spin_points = QSpinBox()
        self.spin_points.setRange(8, 4096)
        self.spin_points.setValue(16)

        self.spin_start_ampl = QDoubleSpinBox()
        self.spin_start_ampl.setRange(0.0, 100.0)
        self.spin_start_ampl.setDecimals(1)
        self.spin_start_ampl.setValue(60.0)
        self.spin_start_ampl.setSuffix(" %")

        self.spin_wl_nm = QDoubleSpinBox()
        self.spin_wl_nm.setRange(0.1, 20000.0)
        self.spin_wl_nm.setDecimals(1)
        self.spin_wl_nm.setValue(532.0)
        self.spin_wl_nm.setSuffix(" nm")
        self.spin_wl_nm.setToolTip("Optical wavelength used for AODevice theoretical phase (beam-steer).")

        self.ao_model = QComboBox()

        form.addRow("f_min:", self.spin_fmin)
        form.addRow("f_max:", self.spin_fmax)
        form.addRow("LUT points:", self.spin_points)
        form.addRow("Start amplitude:", self.spin_start_ampl)
        form.addRow("AO model:", self.ao_model)
        form.addRow("Wavelength:", self.spin_wl_nm)

        bl.addLayout(form)

        btnrow = QHBoxLayout()
        self.btn_generate_download = QPushButton("Generate starting LUT")
        btnrow.addWidget(self.btn_generate_download)
        bl.addLayout(btnrow)

        lay.addWidget(box)
        lay.addStretch(1)

        self.btn_generate_download.clicked.connect(self.on_generate_LUT)
        self.tabs.addTab(tab, "1) Setup / Generate")

    def refresh_ao_models(self):
        self.ao_model.clear()
        self.ao_model.addItem("None")
        try:
            models = imslib.AODeviceList.GetList()
            try:
                n = len(models)
                for i in range(n):
                    self.ao_model.addItem(str(models[i]))
            except Exception:
                for m in models:
                    self.ao_model.addItem(str(m))
            self.log.log(f"[AO models] Loaded {self.ao_model.count()-1} model(s).")
        except Exception as e:
            self.log.log(f"[AO models] Could not load AO model list: {e}")

    def _get_wavelength_um(self) -> float | None:
        try:
            wl_nm = float(self.spin_wl_nm.value())
            if wl_nm <= 0:
                return None
            return wl_nm / 1000.0
        except Exception:
            return None

    def _aodevice_get_cfunc(self, model_name: str, wavelength_um: float | None):
        if not model_name or model_name.lower() == "none":
            return None, None
        aod = imslib.AODevice(model_name)
        if wavelength_um is None:
            cfunc = aod.GetCompensationFunction()
            try:
                self.log.log(f"[AO] Using device operating wavelength: {aod.OperatingWavelength} µm")
            except Exception:
                self.log.log("[AO] Using device operating wavelength")
        else:
            wl = imslib.Micrometre(float(wavelength_um))
            cfunc = aod.GetCompensationFunction(wl)
            self.log.log(f"[AO] Using wavelength: {float(wavelength_um):.4f} µm")

        try:
            n = len(cfunc)
        except Exception:
            n = 0
            try:
                for _ in cfunc:
                    n += 1
            except Exception:
                n = -1
        self.log.log(f"[AO] CompensationFunction points: {n}")
        return aod, cfunc

    @staticmethod
    def _interp_piecewise_linear(x: float, pairs):
        if not pairs:
            return 0.0
        if x <= pairs[0][0]:
            return pairs[0][1]
        if x >= pairs[-1][0]:
            return pairs[-1][1]

        lo, hi = 0, len(pairs) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if pairs[mid][0] <= x:
                lo = mid
            else:
                hi = mid

        x0, y0 = pairs[lo]
        x1, y1 = pairs[hi]
        if x1 == x0:
            return y0
        t = (x - x0) / (x1 - x0)
        return y0 + t * (y1 - y0)

    def _seed_phase_from_aodevice(self, model_name: str, freqs_mhz, wavelength_um: float | None):
        aod, cfunc = self._aodevice_get_cfunc(model_name, wavelength_um)
        self._last_phase_cfunc = cfunc
        self._last_ao_model = model_name

        pairs = []
        try:
            for spec in cfunc:
                pairs.append((float(spec.Freq.value), float(spec.Spec.Phase.value)))
        except Exception:
            n = len(cfunc)
            for i in range(n):
                spec = cfunc[i]
                pairs.append((float(spec.Freq.value), float(spec.Spec.Phase.value)))

        if not pairs:
            self.log.log("[AO] Phase cfunc had 0 points; seeding 0°.")
            return [0.0] * len(freqs_mhz)

        pairs.sort(key=lambda p: p[0])
        phases = [float(self._interp_piecewise_linear(float(f), pairs)) for f in freqs_mhz]
        self.log.log(
            f"[AO] Seed phase OK: cfunc_pts={len(pairs)} f=[{pairs[0][0]:.3f}..{pairs[-1][0]:.3f}] MHz"
        )
        return phases

    def _make_starting_points(self) -> List[LUTPoint]:
        f_min = float(self.spin_fmin.value())
        f_max = float(self.spin_fmax.value())
        n = int(self.spin_points.value())
        start_ampl = float(self.spin_start_ampl.value())
        if n < 2:
            n = 2

        if f_max <= f_min:
            raise ValueError("f_max must be greater than f_min")


        guard_step = (f_max - f_min) / n
        inner_freqs = [f_min + (f_max - f_min) * (i / (n - 1)) for i in range(n)]
        freqs = [f_min - guard_step] + inner_freqs + [f_max + guard_step]

        amplitudes = [0.0] + [start_ampl] * n + [0.0]

        phases = [0.0] * len(freqs)
        model_name = self.ao_model.currentText().strip()
        if model_name and model_name.lower() != "none":
            try:
                wl_um = self._get_wavelength_um()
                phases = self._seed_phase_from_aodevice(model_name, freqs, wl_um)
                self.log.log(f"[LUT] Phase seeded from AO model '{model_name}'.")
            except Exception as e:
                self.log.log(f"[LUT] Phase seeding failed: {e}")

        return [LUTPoint(freqs[i], amplitudes[i], phases[i]) for i in range(len(freqs))]

    def _func_add(self, func, spec):
        if hasattr(func, "append"):
            func.append(spec)
        elif hasattr(func, "push_back"):
            func.push_back(spec)
        else:
            raise AttributeError("CompensationFunction has no append/push_back")

    def _points_to_table_applyfunction(self, pts: List[LUTPoint]) -> imslib.CompensationTable:
        if not pts:
            raise ValueError("No points")
        n = TABLE_RENDER_SIZE
        f_min = pts[0].f_mhz
        f_max = pts[-1].f_mhz

        default_pt = imslib.CompensationPoint(Percent(0.0), Degrees(0.0))
        tbl = imslib.CompensationTable(n, MHz(f_min), MHz(f_max), default_pt)



        amp_func = imslib.CompensationFunction()
        ph_func = imslib.CompensationFunction()

        # Apply interpolation style choices (matches SDK usage: per-feature styles on CompensationFunction)
        try:
            amp_func.AmplitudeInterpolationStyle = int(self.amp_interp_style)
        except Exception:
            pass
        try:
            ph_func.PhaseInterpolationStyle = int(self.phase_interp_style)
        except Exception:
            pass

        for p in pts:
            pt_a = imslib.CompensationPoint(Percent(p.ampl_pct), Degrees(0.0))
            self._func_add(amp_func, imslib.CompensationPointSpecification(pt_a, MHz(p.f_mhz)))

            pt_p = imslib.CompensationPoint(Percent(0.0), Degrees(p.phase_deg))
            self._func_add(ph_func, imslib.CompensationPointSpecification(pt_p, MHz(p.f_mhz)))

        ok_a = tbl.ApplyFunction(amp_func, imslib.CompensationFeature_AMPLITUDE, imslib.CompensationModifier_REPLACE)
        try:
            ok_p = tbl.ApplyFunction(ph_func, imslib.CompensationFeature_PHASE, imslib.CompensationModifier_REPLACE)
        except TypeError:
            ok_p = tbl.ApplyFunction(ph_func, imslib.CompensationFeature_PHASE)

        self.log.log(
            f"[LUT] Build table: size={n} f=[{f_min:.6f}..{f_max:.6f}] MHz  "
            f"ApplyFunction amp={ok_a}({self.cmb_amp_interp.currentText() if hasattr(self,'cmb_amp_interp') else self.amp_interp_style}), "
            f"phase={ok_p}({self.cmb_phase_interp.currentText() if hasattr(self,'cmb_phase_interp') else self.phase_interp_style})"
        )

        cp0 = tbl[0]
        cp1 = tbl[1]
        self.log.log(f"[DBG] tbl[0].Amp={float(cp0.Amplitude.value):.3f} tbl[1].Amp={float(cp1.Amplitude.value):.3f}")
        self.log.log(f"[DBG] first control amp={pts[0].ampl_pct:.3f}")

        try:
            last = int(tbl.Size()) - 1


            p0 = tbl[0]
            pN = tbl[last]

            tbl[0] = imslib.CompensationPoint(Percent(pts[0].ampl_pct), Degrees(float(p0.Phase.value)))
            tbl[last] = imslib.CompensationPoint(Percent(pts[-1].ampl_pct), Degrees(float(pN.Phase.value)))


            p0 = tbl[0]
            pN = tbl[last]
            tbl[0] = imslib.CompensationPoint(Percent(float(p0.Amplitude.value)), Degrees(pts[0].phase_deg))
            tbl[last] = imslib.CompensationPoint(Percent(float(pN.Amplitude.value)), Degrees(pts[-1].phase_deg))
        except Exception as e:
            self.log.log(f"[LUT] Endpoint force failed: {e}")

        cp0 = tbl[0]
        cp1 = tbl[1]
        self.log.log(f"[DBG] tbl[0].Amp={float(cp0.Amplitude.value):.3f} tbl[1].Amp={float(cp1.Amplitude.value):.3f}")
        self.log.log(f"[DBG] first control amp={pts[0].ampl_pct:.3f}")

        return tbl

    def on_generate_LUT(self):
        if self.ims is None:
            QMessageBox.information(self, "Not connected", "Connect to a device first.")
            return
        try:
            g = self._make_starting_points()
            self.lut_points_global = g
            self.log.log(f"[LUT] Generated {len(g)} points: {self.spin_points.value()} user point(s) plus 2 zero-amplitude guard points.")
            self._populate_table_widget()
            self._update_plot()
            QMessageBox.information(self, "LUT", "Proceed to tuning tab.")
        except Exception as e:
            self.log.log(f"[LUT] Generate error: {e}")
            QMessageBox.critical(self, "Generate error", str(e))

    # ----------------- Tab 2: Tuning -----------------
    def _build_tab_tuning(self):
        tab = QWidget()
        lay = QVBoxLayout(tab)

        top = QGroupBox("Tuning")
        tl = QVBoxLayout(top)

        ctrl = QHBoxLayout()
        self.btn_stop_tone = QPushButton("Stop tone")

        self.tgl_amp_enable = QPushButton("Amplifier")
        self.tgl_amp_enable.setCheckable(True)
        self.tgl_amp_enable.setChecked(False)
        self.tgl_amp_enable.setToolTip("Enable/disable amplifier output. Default OFF on startup.")

        self.btn_sync_wipers = QPushButton("Sync channel wipers")
        self.btn_sync_wipers.setCheckable(True)
        self.btn_sync_wipers.setChecked(False)
        self.btn_sync_wipers.setToolTip("When enabled, CH1 and CH2 wipers track each other.")

        ctrl.addWidget(self.btn_stop_tone)
        ctrl.addWidget(self.tgl_amp_enable)
        ctrl.addWidget(self.btn_sync_wipers)
        ctrl.addStretch(1)
        tl.addLayout(ctrl)

        # Drive controls
        wbox = QGroupBox("RF Drive Controls")
        wl = QFormLayout(wbox)

        self.sld_dds = QSlider(Qt.Horizontal)
        self.sld_dds.setRange(0, 1000)
        self.sld_dds.setValue(500)
        self.lbl_dds = QLabel("50.0 %")
        drow = QHBoxLayout(); drow.addWidget(self.sld_dds, 1); drow.addWidget(self.lbl_dds)
        dw = QWidget(); dw.setLayout(drow)
        wl.addRow("DDS power:", dw)

        self.sld_w1 = QSlider(Qt.Horizontal)
        self.sld_w1.setRange(0, 1000)
        self.sld_w1.setValue(500)
        self.lbl_w1 = QLabel("50.0 %")
        w1row = QHBoxLayout(); w1row.addWidget(self.sld_w1, 1); w1row.addWidget(self.lbl_w1)
        w1w = QWidget(); w1w.setLayout(w1row)
        wl.addRow("Channel 1:", w1w)

        self.sld_w2 = QSlider(Qt.Horizontal)
        self.sld_w2.setRange(0, 1000)
        self.sld_w2.setValue(500)
        self.lbl_w2 = QLabel("50.0 %")
        w2row = QHBoxLayout(); w2row.addWidget(self.sld_w2, 1); w2row.addWidget(self.lbl_w2)
        w2w = QWidget(); w2w.setLayout(w2row)
        wl.addRow("Channel 2:", w2w)

        tl.addWidget(wbox)

        # LUT interpolation styles (affects how the table is rendered + what the graph shows)
        ibox = QGroupBox("LUT Interpolation (rendering)")
        il = QFormLayout(ibox)

        self.cmb_amp_interp = QComboBox()
        self.cmb_phase_interp = QComboBox()

        for name, _val in self._interp_items:
            self.cmb_amp_interp.addItem(name)
            self.cmb_phase_interp.addItem(name)

        # defaults (match self.amp_interp_style / self.phase_interp_style)
        self.cmb_amp_interp.setCurrentIndex(self._interp_index_for_value(self.amp_interp_style))
        self.cmb_phase_interp.setCurrentIndex(self._interp_index_for_value(self.phase_interp_style))

        il.addRow("Amplitude:", self.cmb_amp_interp)
        il.addRow("Phase:", self.cmb_phase_interp)
        tl.addWidget(ibox)

        # Table + editor + plot
        mid = QHBoxLayout()

        self.tbl = QTableWidget(0, 3)
        self.tbl.setHorizontalHeaderLabels(["Freq (MHz)", "Ampl (%)", "Phase (deg)"])
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl.setSelectionMode(QTableWidget.SingleSelection)
        mid.addWidget(self.tbl, 2)

        editbox = QGroupBox("Selected point edit")
        el = QVBoxLayout(editbox)

        row = QHBoxLayout()

        amp_col = QVBoxLayout()
        amp_col.setAlignment(Qt.AlignHCenter)
        lbl_amp_title = QLabel("Amplitude")
        lbl_amp_title.setAlignment(Qt.AlignHCenter)
        amp_col.addWidget(lbl_amp_title)

        self.sld_ampl = QSlider(Qt.Vertical)
        self.sld_ampl.setRange(0, 10000)
        self.sld_ampl.setSingleStep(10)   # 0.1%
        self.sld_ampl.setPageStep(100)    # 1.0%
        self.sld_ampl.setTickPosition(QSlider.TicksRight)
        self.sld_ampl.setTickInterval(500)
        amp_col.addWidget(self.sld_ampl, 1, Qt.AlignHCenter)

        self.lbl_ampl = QLabel("0.00 %")
        self.lbl_ampl.setAlignment(Qt.AlignHCenter)
        amp_col.addWidget(self.lbl_ampl)

        ph_col = QVBoxLayout()
        ph_col.setAlignment(Qt.AlignHCenter)
        lbl_ph_title = QLabel("Phase")
        lbl_ph_title.setAlignment(Qt.AlignHCenter)
        ph_col.addWidget(lbl_ph_title)

        self.sld_phase = QSlider(Qt.Vertical)
        self.sld_phase.setRange(-18000, 18000)
        self.sld_phase.setSingleStep(10)  # 0.1°
        self.sld_phase.setPageStep(100)   # 1.0°
        self.sld_phase.setTickPosition(QSlider.TicksRight)
        self.sld_phase.setTickInterval(3000)
        ph_col.addWidget(self.sld_phase, 1, Qt.AlignHCenter)

        self.lbl_phase = QLabel("0.00 °")
        self.lbl_phase.setAlignment(Qt.AlignHCenter)
        ph_col.addWidget(self.lbl_phase)

        row.addLayout(amp_col, 1)
        row.addSpacing(14)
        row.addLayout(ph_col, 1)

        el.addLayout(row, 1)
        mid.addWidget(editbox, 1)

        self.fig = Figure(figsize=(5, 3))
        self.canvas = FigureCanvas(self.fig)
        self.canvas.setMinimumHeight(260)
        self.canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.ax_amp = self.fig.add_subplot(111)
        self.ax_phase = self.ax_amp.twinx()
        mid.addWidget(self.canvas, 2)

        tl.addLayout(mid)
        lay.addWidget(top)
        lay.addStretch(1)

        self.sld_ampl.installEventFilter(self._wheel_filter)
        self.sld_phase.installEventFilter(self._wheel_filter)

        # Signals
        self.btn_stop_tone.clicked.connect(self.on_stop_tone)
        self.tgl_amp_enable.toggled.connect(self.on_amp_enable_toggled)
        self.btn_sync_wipers.toggled.connect(self.on_sync_wipers_toggled)

        self.sld_dds.valueChanged.connect(self.on_dds_changed)
        self.sld_w1.valueChanged.connect(self.on_w1_changed)
        self.sld_w2.valueChanged.connect(self.on_w2_changed)

        self.tbl.itemSelectionChanged.connect(self.on_point_selected)
        self.tbl.itemChanged.connect(self.on_table_item_changed)
        self.sld_ampl.valueChanged.connect(self.on_point_ampl_changed)
        self.sld_phase.valueChanged.connect(self.on_point_phase_changed)

        self.cmb_amp_interp.currentIndexChanged.connect(self._on_interp_changed)
        self.cmb_phase_interp.currentIndexChanged.connect(self._on_interp_changed)

        self.tabs.addTab(tab, "2) Tuning")

    # ----------------- LUT interpolation (SDK-backed) -----------------
    def _interp_index_for_value(self, value: int) -> int:
        for i, (_name, v) in enumerate(self._interp_items):
            if int(v) == int(value):
                return i
        return 2  # default to Linear

    def _interp_value_for_index(self, idx: int) -> int:
        idx = int(idx)
        if idx < 0 or idx >= len(self._interp_items):
            return int(imslib.CompensationFunction.InterpolationStyle_LINEAR)
        return int(self._interp_items[idx][1])

    def _on_interp_changed(self, _idx: int = 0):
        # Store selection and refresh graph
        try:
            self.amp_interp_style = self._interp_value_for_index(self.cmb_amp_interp.currentIndex())
            self.phase_interp_style = self._interp_value_for_index(self.cmb_phase_interp.currentIndex())
            self.log.log(
                f"[Interp] Amplitude={self.cmb_amp_interp.currentText()} ({self.amp_interp_style})  "
                f"Phase={self.cmb_phase_interp.currentText()} ({self.phase_interp_style})"
            )
        except Exception:
            pass
        self._update_plot()

    def _active_points(self) -> List[LUTPoint]:
        return self.lut_points_global

    def _selected_index(self) -> Optional[int]:
        rows = self.tbl.selectionModel().selectedRows()
        if not rows:
            return None
        return rows[0].row()

    def _populate_table_widget(self):
        self._table_updating = True
        pts = self._active_points()
        if not pts:
            self.tbl.setRowCount(0)
            self._table_updating = False
            return
        self.tbl.setRowCount(len(pts))
        for r, p in enumerate(pts):
            self.tbl.setItem(r, 0, QTableWidgetItem(f"{p.f_mhz:.6f}"))
            self.tbl.setItem(r, 1, QTableWidgetItem(f"{p.ampl_pct:.2f}"))
            self.tbl.setItem(r, 2, QTableWidgetItem(f"{p.phase_deg:.2f}"))
        if pts:
            self.tbl.selectRow(0)
            self.log.log(f"[Tuning] Loaded {len(pts)} point(s). Row 0 selected.")
        self._table_updating = False

    def _dedupe_x_keep_last(self, xs, ys1, ys2, tol=1e-12):
        """Remove duplicate/near-duplicate x entries (keep the last y for each x)."""
        if not xs:
            return xs, ys1, ys2
        out_x, out_y1, out_y2 = [], [], []
        for x, a, p in zip(xs, ys1, ys2):
            if out_x and abs(x - out_x[-1]) <= tol:
                # overwrite last (prevents vertical segment at same x)
                out_x[-1] = x
                out_y1[-1] = a
                out_y2[-1] = p
            else:
                out_x.append(x); out_y1.append(a); out_y2.append(p)
        return out_x, out_y1, out_y2

    def _update_plot(self):
        pts_raw = self._active_points()
        self.fig.clear()

        self.ax_amp = self.fig.add_subplot(111)
        self.ax_phase = self.ax_amp.twinx()

        if not pts_raw:
            self.canvas.draw_idle()
            return

        # Sort by frequency for rendering (table generation expects monotonic)
        pts = sorted(pts_raw, key=lambda p: p.f_mhz)

        # Control points (user-editable)
        xs = [p.f_mhz for p in pts]
        amps = [p.ampl_pct for p in pts]
        phs = [p.phase_deg for p in pts]

        # Rendered LUT (SDK interpolation) for graphing
        xs_r, amps_r, phs_r = xs, amps, phs
              
        try:
            tbl = self._points_to_table_applyfunction(pts)

            def _num(v):
                try:
                    return float(v.value)
                except Exception:
                    return float(v)

            size = int(tbl.Size())
            if size > 0:
                step = max(1, size // 1024)  # cap graph density
                xs_r = []
                amps_r = []
                phs_r = []
                for i in range(0, size, step):
                    f = _num(tbl.FrequencyAt(i))
                    cp = tbl[i]
                    xs_r.append(f)
                    amps_r.append(_num(cp.Amplitude))
                    phs_r.append(_num(cp.Phase))
        except Exception as e:
            # If rendering fails for any reason, fall back to control points
            self.log.log(f"[Plot] Render-table failed (using control points): {e}")



        amp_color = "tab:blue"
        phase_color = "tab:orange"

        # Rendered curves
        self.ax_amp.plot(xs_r, amps_r, color=amp_color, label="Amplitude (rendered)")
        self.ax_phase.plot(xs_r, phs_r, color=phase_color, label="Phase (rendered)")

        # Control points on top
        self.ax_amp.plot(xs, amps, linestyle="None", marker="None", markersize=4, color=amp_color, alpha=0.6)
        self.ax_phase.plot(xs, phs, linestyle="None", marker="None", markersize=4, color=phase_color, alpha=0.6)

        self.ax_amp.set_xlabel("Frequency (MHz)")
        self.ax_amp.set_ylabel("Amplitude (%)", color=amp_color)
        self.ax_phase.set_ylabel("Phase (deg)", color=phase_color)

        self.ax_amp.tick_params(axis="y", colors=amp_color)
        self.ax_phase.tick_params(axis="y", colors=phase_color)

        # Cursor at currently selected *table row* (maps to control points list order in the UI table)
        sel = self._selected_index()
        if sel is not None:
            # The UI table reflects self.lut_points_global order, not sorted order.
            # Try to place cursor at the selected point's frequency.
            try:
                p_sel = self._active_points()[sel]
                x = p_sel.f_mhz
            except Exception:
                x = xs[0]

            ymin, ymax = self.ax_amp.get_ylim()
            yr = (ymax - ymin) if (ymax - ymin) != 0 else 1.0
            y_base = ymin + 0.02 * yr
            y_tip = ymin + 0.12 * yr

            try:
                self._cursor_anno.remove()
            except Exception:
                pass

            self._cursor_anno = self.ax_amp.annotate(
                "",
                xy=(x, y_base),
                xytext=(x, y_tip),
                arrowprops=dict(arrowstyle="-|>", lw=1),
                annotation_clip=False,
            )

        self.fig.tight_layout()
        self.canvas.draw_idle()

    def on_amp_enable_toggled(self, enabled: bool):
        if self.sf is None:
            self.log.log("[Amp] SystemFunc not available.")
            return
        try:
            self.sf.EnableAmplifier(bool(enabled))
            self.log.log(f"[Amp] Amplifier {'ENABLED' if enabled else 'DISABLED'}.")
        except Exception as e:
            self.log.log(f"[Amp] Amplifier toggle failed: {e}")
            self.tgl_amp_enable.blockSignals(True)
            self.tgl_amp_enable.setChecked(not enabled)
            self.tgl_amp_enable.blockSignals(False)

    def on_sync_wipers_toggled(self, enabled: bool):
        self.sync_wipers = bool(enabled)
        self.log.log(f"[Wipers] Sync {'ENABLED' if self.sync_wipers else 'DISABLED'}")

        if self.sync_wipers:
            try:
                self._sync_guard = True
                if self.sld_w1.value() < self.sld_w2.value():
                    self.sld_w2.setValue(self.sld_w1.value())
                else:
                    self.sld_w1.setValue(self.sld_w2.value())
            finally:
                self._sync_guard = False

    def on_stop_tone(self):
        if self.sp is None:
            return
        try:
            self.sp.ClearTone()
            self.log.log("[Tuning] Tone cleared.")
        except Exception as e:
            self.log.log(f"[Tuning] Stop tone error: {e}")

    def _apply_selected_point_to_tone(self):
        if self.sp is None:
            return
        pts = self._active_points()
        idx = self._selected_index()
        if idx is None or idx >= len(pts):
            return
        p = pts[idx]
        try:
            fap = imslib.FAP(MHz(p.f_mhz), Percent(p.ampl_pct), Degrees(p.phase_deg))
            self.sp.SetCalibrationTone(fap)
            self.sp.PhaseResync()
            self.log.log(f"[Tone] idx={idx} f={p.f_mhz:.6f}MHz amp={p.ampl_pct:.2f}% ph={p.phase_deg:.2f}°")
        except Exception as e:
            self.log.log(f"[Tone] Failed: {e}")

    def on_dds_changed(self, v: int):
        self.lbl_dds.setText(f"{v/10.0:.2f} %")
        if self.sp is None:
            return
        try:
            self.sp.UpdateDDSPowerLevel(Percent(v/10.0))
        except Exception as e:
            self.log.log(f"[DDS] Update error: {e}")

    def on_w1_changed(self, v: int):
        self.lbl_w1.setText(f"{v/10.0:.2f} %")
        if self.sp is None:
            return

        if self.sync_wipers and not self._sync_guard:
            try:
                self._sync_guard = True
                self.sld_w2.setValue(v)
            finally:
                self._sync_guard = False

        try:
            self.sp.UpdateRFAmplitude(imslib.SignalPath.AmplitudeControl_WIPER_1, Percent(v/10.0), RFChannel(1))
        except Exception as e:
            self.log.log(f"[Wiper1] Update error: {e}")

    def on_w2_changed(self, v: int):
        self.lbl_w2.setText(f"{v/10.0:.2f} %")
        if self.sp is None:
            return

        if self.sync_wipers and not self._sync_guard:
            try:
                self._sync_guard = True
                self.sld_w1.setValue(v)
            finally:
                self._sync_guard = False

        try:
            self.sp.UpdateRFAmplitude(imslib.SignalPath.AmplitudeControl_WIPER_2, Percent(v/10.0), RFChannel(2))
        except Exception as e:
            self.log.log(f"[Wiper2] Update error: {e}")

    def on_point_selected(self):
        pts = self._active_points()
        idx = self._selected_index()
        if idx is None or idx >= len(pts):
            return
        p = pts[idx]
        self.sld_ampl.blockSignals(True)
        self.sld_phase.blockSignals(True)
        self.sld_ampl.setValue(int(round(p.ampl_pct * 100.0)))
        self.sld_phase.setValue(int(round(p.phase_deg * 100.0)))
        self.sld_ampl.blockSignals(False)
        self.sld_phase.blockSignals(False)
        self.lbl_ampl.setText(f"{p.ampl_pct:.2f} %")
        self.lbl_phase.setText(f"{p.phase_deg:.2f} °")
        self._update_plot()
        self._tone_debounce.start()

    def on_table_item_changed(self, item: QTableWidgetItem):
        if self._table_updating:
            return
        pts = self._active_points()
        r = item.row()
        c = item.column()
        if r < 0 or r >= len(pts):
            return

        txt = item.text().strip()
        try:
            val = float(txt)
        except Exception:
            self._table_updating = True
            try:
                p = pts[r]
                if c == 0:
                    item.setText(f"{p.f_mhz:.6f}")
                elif c == 1:
                    item.setText(f"{p.ampl_pct:.2f}")
                elif c == 2:
                    item.setText(f"{p.phase_deg:.2f}")
            finally:
                self._table_updating = False
            return

        p = pts[r]

        if c == 0:
            p.f_mhz = max(0.0, val)
        elif c == 1:
            p.ampl_pct = clamp(val, 0.0, 100.0)
        elif c == 2:
            ph = val
            while ph > 180.0:
                ph -= 360.0
            while ph < -180.0:
                ph += 360.0
            p.phase_deg = ph
            if abs(ph - val) > 1e-6:
                self._table_updating = True
                try:
                    item.setText(f"{ph:.2f}")
                finally:
                    self._table_updating = False

        sel = self._selected_index()
        if sel == r:
            self._slider_updating = True
            try:
                self.sld_ampl.setValue(int(round(p.ampl_pct * 100.0)))
                self.sld_phase.setValue(int(round(p.phase_deg * 100.0)))
                self.lbl_ampl.setText(f"{p.ampl_pct:.2f} %")
                self.lbl_phase.setText(f"{p.phase_deg:.2f} °")
            finally:
                self._slider_updating = False
            self._tone_debounce.start()

        self._update_plot()

    def on_point_ampl_changed(self, v: int):
        if self._slider_updating:
            return
        pts = self._active_points()
        idx = self._selected_index()
        if idx is None or idx >= len(pts):
            return
        new_a = clamp(v / 100.0, 0.0, 100.0)
        pts[idx].ampl_pct = new_a
        self.lbl_ampl.setText(f"{new_a:.2f} %")
        try:
            self.tbl.item(idx, 1).setText(f"{new_a:.2f}")
        except Exception:
            pass
        self._update_plot()
        self._tone_debounce.start()

    def on_point_phase_changed(self, v: int):
        if self._slider_updating:
            return
        pts = self._active_points()
        idx = self._selected_index()
        if idx is None or idx >= len(pts):
            return
        ph = v / 100.0
        while ph > 180.0:
            ph -= 360.0
        while ph < -180.0:
            ph += 360.0
        pts[idx].phase_deg = ph
        self.lbl_phase.setText(f"{ph:.2f} °")
        try:
            self.tbl.item(idx, 2).setText(f"{ph:.2f}")
        except Exception:
            pass
        self._update_plot()
        self._tone_debounce.start()

    # ----------------- Tab 3: Save / Store -----------------
    def _build_tab_export_store(self):
        tab = QWidget()
        lay = QVBoxLayout(tab)

        box = QGroupBox("Save LUT to file / Store to synthesiser")
        bl = QVBoxLayout(box)

        self.btn_save_lut = QPushButton("Save LUT (.lut)")
        self.btn_store_default = QPushButton("Store as DEFAULT on synthesiser")

        self.spin_timeout = QDoubleSpinBox()
        self.spin_timeout.setRange(5.0, 600.0)
        self.spin_timeout.setDecimals(1)
        self.spin_timeout.setValue(60.0)
        self.spin_timeout.setSuffix(" s")

        bt = QFormLayout()
        bt.addRow("DL/Verify timeout:", self.spin_timeout)
        bl.addLayout(bt)

        row = QHBoxLayout()
        row.addWidget(self.btn_save_lut)
        row.addWidget(self.btn_store_default)
        row.addStretch(1)
        bl.addLayout(row)

        lay.addWidget(box)
        lay.addStretch(1)

        self.btn_save_lut.clicked.connect(self.on_save_lut)
        self.btn_store_default.clicked.connect(self.on_store_default)

        self.tabs.addTab(tab, "3) Save / Store")

    def on_save_lut(self):
        
        pts = self._active_points()
        if not pts:
            QMessageBox.information(self, "No LUT", "Generate a LUT first.")
            return

        fn, _ = QFileDialog.getSaveFileName(
            self, "Save LUT", "compensation.lut", "LUT files (*.lut);;All files (*.*)"
        )
        if not fn:
            return

        def _num(v, default=None):
            """Convert SDK unit objects/properties to float/int-friendly values."""
            try:
                if hasattr(v, "value"):
                    return float(v.value)
                if hasattr(v, "Value"):
                    return float(v.Value)
                return float(v)
            except Exception:
                return default

        def _cap_attr(*names, default=None):
            candidates = []
            if self.cap is not None:
                candidates.append(self.cap)
            try:
                if self.synth is not None:
                    candidates.append(self.synth.GetCap())
            except Exception:
                pass

            for obj in candidates:
                for name in names:
                    try:
                        val = getattr(obj, name)
                        if callable(val):
                            val = val()
                        return val
                    except Exception:
                        pass
            return default

        def _rf_channel_count() -> int:
            val = _cap_attr("Channels", "RFChannels", "NumChannels", default=4)
            try:
                return max(1, int(_num(val, 4)))
            except Exception:
                return 4

        def _make_export_table(control_pts: List[LUTPoint]) -> imslib.CompensationTable:
            control_pts = sorted(control_pts, key=lambda p: p.f_mhz)
            default_pt = imslib.CompensationPoint(Percent(0.0), Degrees(0.0))

            # Preferred path: let the SDK size/range the CompensationTable from the IMS system.
            # This avoids exporting the smaller plotted/rendered table.
            tbl = None
            if self.ims is not None and not isinstance(self.ims, str):
                try:
                    tbl = imslib.CompensationTable(self.ims, default_pt)
                    self.log.log("[Save] Export table created from IMS capabilities.")
                except Exception as e1:
                    try:
                        tbl = imslib.CompensationTable(self.ims)
                        self.log.log(f"[Save] Export table created from IMS capabilities without default point ({e1}).")
                    except Exception as e2:
                        self.log.log(f"[Save] IMS-sized export table unavailable: {e2}")
                        tbl = None

            # Fallback path: construct from capability properties if the IMS constructor is unavailable.
            if tbl is None:
                lut_depth = _cap_attr("LUTDepth", "LutDepth", "lut_depth", default=None)
                lower = _cap_attr("LowerFreq", "LowerFrequency", "Lower", default=None)
                upper = _cap_attr("UpperFreq", "UpperFrequency", "Upper", default=None)

                lut_depth_i = None
                try:
                    lut_depth_i = int(_num(lut_depth, None))
                except Exception:
                    lut_depth_i = None

                lower_f = _num(lower, control_pts[0].f_mhz)
                upper_f = _num(upper, control_pts[-1].f_mhz)

                if lut_depth_i is not None and lower_f is not None and upper_f is not None:
                    tbl = imslib.CompensationTable(lut_depth_i, MHz(lower_f), MHz(upper_f), default_pt)
                    self.log.log(
                        f"[Save] Export table created from capability fields: "
                        f"depth/size={lut_depth_i} f=[{lower_f:.6f}..{upper_f:.6f}] MHz"
                    )
                else:
                    self.log.log("[Save] Falling back to current render-table builder for export.")
                    return self._points_to_table_applyfunction(control_pts)

            amp_func = imslib.CompensationFunction()
            ph_func = imslib.CompensationFunction()

            try:
                amp_func.AmplitudeInterpolationStyle = int(self.amp_interp_style)
            except Exception:
                pass
            try:
                ph_func.PhaseInterpolationStyle = int(self.phase_interp_style)
            except Exception:
                pass

            for p in control_pts:
                self._func_add(
                    amp_func,
                    imslib.CompensationPointSpecification(
                        imslib.CompensationPoint(Percent(p.ampl_pct), Degrees(0.0)),
                        MHz(p.f_mhz),
                    ),
                )
                self._func_add(
                    ph_func,
                    imslib.CompensationPointSpecification(
                        imslib.CompensationPoint(Percent(0.0), Degrees(p.phase_deg)),
                        MHz(p.f_mhz),
                    ),
                )

            ok_a = tbl.ApplyFunction(
                amp_func,
                imslib.CompensationFeature_AMPLITUDE,
                imslib.CompensationModifier_REPLACE,
            )
            try:
                ok_p = tbl.ApplyFunction(
                    ph_func,
                    imslib.CompensationFeature_PHASE,
                    imslib.CompensationModifier_REPLACE,
                )
            except TypeError:
                ok_p = tbl.ApplyFunction(ph_func, imslib.CompensationFeature_PHASE)

            try:
                size = int(tbl.Size())
            except Exception:
                size = -1

            try:
                lo = _num(tbl.LowerFrequency())
                hi = _num(tbl.UpperFrequency())
                range_txt = f" f=[{lo:.6f}..{hi:.6f}] MHz"
            except Exception:
                range_txt = ""

            self.log.log(
                f"[Save] Built SDK export table: size={size}{range_txt}  "
                f"ApplyFunction amp={ok_a}, phase={ok_p}"
            )
            return tbl

        try:
            self.log.log(f"[Save] Export requested: {fn}")
            tbl = _make_export_table(pts)

            rf_channels = _rf_channel_count()
            self.log.log(f"[Save] Exporter channel count: {rf_channels}")

            try:
                exp = imslib.CompensationTableExporter(rf_channels)
            except Exception as e:
                self.log.log(f"[Save] CompensationTableExporter({rf_channels}) failed ({e}); using default exporter.")
                exp = imslib.CompensationTableExporter()

            exp.ProvideGlobalTable(tbl)
            ok = exp.ExportGlobalLUT(fn)
            self.log.log(f"[Save] ExportGlobalLUT returned: {ok}")

            if not ok:
                raise RuntimeError("ExportGlobalLUT returned False")

            try:
                sz = os.path.getsize(fn)
                self.log.log(f"[Save] File size: {sz} bytes")
                if sz < 2048:
                    self.log.log("[Save] WARNING: LUT file is unusually small; verify contents.")
            except Exception as e_sz:
                self.log.log(f"[Save] Could not stat saved file: {e_sz}")

            QMessageBox.information(self, "Saved", f"Saved LUT:\n{fn}")
        except Exception as e:
            self.log.log(f"[Save] Error: {e}")
            QMessageBox.critical(self, "Save error", str(e))

    def on_store_default(self):
        if self.ims is None:
            QMessageBox.information(self, "Not connected", "Connect to a device first.")
            return
        pts = self._active_points()
        if not pts:
            QMessageBox.information(self, "No LUT", "Generate a LUT first.")
            return

        try:
            default_name = "LUTDFLT"
            self.log.log(f"[Store] Store DEFAULT requested. name='{default_name}'")

            timeout_s = float(self.spin_timeout.value())

            def _get_file_default_token():
                if hasattr(imslib, "FileDefault_DEFAULT"):
                    return getattr(imslib, "FileDefault_DEFAULT")
                fd = getattr(imslib, "FileDefault", None)
                if fd is not None and hasattr(fd, "DEFAULT"):
                    return getattr(fd, "DEFAULT")
                raise AttributeError("No FileDefault token found (expected FileDefault_DEFAULT or FileDefault.DEFAULT).")

            file_default = _get_file_default_token()
            self.log.log(f"[Store] FileDefault token resolved: {file_default}")

            tbl = self._points_to_table_applyfunction(sorted(self.lut_points_global, key=lambda p: p.f_mhz))

            ok, dl_s, v_s, v_err = download_and_verify_compensation(
                ims=self.ims, comp=tbl, timeout_s=timeout_s, log=self.log, channel=None
            )
            if not ok:
                raise RuntimeError(f"Download/verify failed before store (verify_err={v_err})")

            ctdl = imslib.CompensationTableDownload(self.ims, tbl)

            self.log.log(f"[Store] Calling Store(file_default={file_default}, name='{default_name}') ...")
            idx = ctdl.Store(file_default, default_name)
            self.log.log(f"[Store] Store() returned idx={idx}")

            if idx is None or int(idx) < 0:
                raise RuntimeError("Failed to store LUT as DEFAULT (Store() returned <0).")

            self.log.log(f"[Store] ✅ Stored LUT as DEFAULT startup table: '{default_name}' (index={idx})")
            QMessageBox.information(self, "Stored", "Stored as DEFAULT (see console for details).")

        except Exception as e:
            self.log.log(f"[Store] Error: {e}")
            QMessageBox.critical(self, "Store error", str(e))

    # ----------------- Enable/disable -----------------
    def _set_enabled_state(self, connected: bool):
        for w in [
            self.btn_generate_download,
            self.spin_fmin, self.spin_fmax, self.spin_points, self.spin_start_ampl, self.ao_model, self.spin_wl_nm,
            self.btn_stop_tone, self.tgl_amp_enable, self.btn_sync_wipers,
            self.sld_dds, self.sld_w1, self.sld_w2,
            self.tbl, self.sld_ampl, self.sld_phase,
            self.cmb_amp_interp, self.cmb_phase_interp,
            self.btn_save_lut, self.btn_store_default, self.spin_timeout
        ]:
            w.setEnabled(connected)
        if not connected:
            self.tbl.setRowCount(0)
            self.fig.clear()
            self.canvas.draw_idle()


def main():
    app = QApplication(sys.argv)

    ico = resource_path("Isomet.ico")
    if os.path.exists(ico):
        app.setWindowIcon(QIcon(ico))


    ims = DeviceSelectDialog.get_connected_system(None)
    if ims is None:
        return 0
    if ims == "trial_mode":
        QMessageBox.information(None, "Mode", "Trial mode selected.")

    try:
        w = CompensationTool(ims)
        w.resize(1000, 700)
        w.setMinimumSize(600, 750)
        w.show()
        return app.exec()
    finally:
        try:
            ims.Disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())