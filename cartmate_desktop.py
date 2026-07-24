"""
CartMate — Desktop (PySide6) single-file app

Features:
- Add items (name, price, quantity). Decimal prices allowed.
- Validate input and show friendly messages.
- Running subtotal, tax (by ZIP or default), and total.
- Save sessions to local SQLite history; restore sessions.
- Settings (upper-right): simple/advanced, voice output on/off, tax API URL.
- Voice output via pyttsx3 (offline). Simulated voice input or hook to STT endpoint.
- Export CSV.
- Modern Gen-Z friendly styling with large buttons and rounded cards.

CAVEATS / NOTES:
- For accurate tax rates by address use a tax API server (TaxJar/Avalara). Set TAX_API_URL in Settings to your server endpoint:
    e.g. https://your-backend.example.com/api/tax/zipcode?zip={zip}
  The app will attempt a GET to that URL (replace {zip}) if provided.
- Voice input (real STT) is not enabled by default; you can configure an STT server URL in Settings that
  accepts an audio upload and returns text. The UI includes a "Simulate voice input" workflow for demo.
- The app stores a local SQLite DB at ~/.cartmate/cartmate.db
"""

import sys
import os
import sqlite3
import csv
import json
import threading
from datetime import datetime
from pathlib import Path
from decimal import Decimal, InvalidOperation

# UI imports
from PySide6.QtWidgets import (
    QApplication, QWidget, QMainWindow, QLabel, QLineEdit, QPushButton, QVBoxLayout, QHBoxLayout,
    QListWidget, QListWidgetItem, QMessageBox, QSpinBox, QFormLayout, QDialog, QCheckBox,
    QFileDialog, QComboBox, QFrame
)
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QFont

# Optional imports
try:
    import requests
except Exception:
    requests = None

try:
    import pyttsx3
except Exception:
    pyttsx3 = None

APP_NAME = "CartMate"
DB_DIR = Path.home() / ".cartmate"
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "cartmate.db"

# Mock ZIP tax lookup table (small set). Use TAX_API_URL in settings if you want a real server.
MOCK_ZIP_TAX = {
    "10001": 0.08875,  # NYC
    "94103": 0.085,    # SF
    "30301": 0.08,     # Atlanta
    "90210": 0.095,    # Beverly Hills
    "73301": 0.0825    # Austin
}
DEFAULT_TAX_RATE = 0.07

# -----------------------
# Database helpers
# -----------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        store TEXT,
        zip TEXT,
        tax_rate REAL,
        subtotal REAL,
        tax REAL,
        total REAL,
        date TEXT
    )
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT,
        name TEXT,
        price REAL,
        qty REAL,
        FOREIGN KEY(session_id) REFERENCES sessions(id)
    )
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)
    conn.commit()
    conn.close()

def save_setting(key, value):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, json.dumps(value)))
    conn.commit()
    conn.close()

def load_setting(key, default=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    if row:
        return json.loads(row[0])
    return default

def save_session_to_db(session_id, store, zip_code, tax_rate, subtotal, tax, total, items):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO sessions(id, store, zip, tax_rate, subtotal, tax, total, date) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
              (session_id, store, zip_code, tax_rate, subtotal, tax, total, datetime.utcnow().isoformat()))
    c.execute("DELETE FROM items WHERE session_id = ?", (session_id,))
    for it in items:
        c.execute("INSERT INTO items(session_id, name, price, qty) VALUES (?, ?, ?, ?)",
                  (session_id, it['name'], it['price'], it['qty']))
    conn.commit()
    conn.close()

def load_history_from_db(limit=200):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, store, zip, tax_rate, subtotal, tax, total, date FROM sessions ORDER BY date DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    sessions = []
    for r in rows:
        session = {
            "id": r[0],
            "store": r[1],
            "zip": r[2],
            "tax_rate": r[3],
            "subtotal": r[4],
            "tax": r[5],
            "total": r[6],
            "date": r[7],
            "items": []
        }
        c2 = conn.cursor()
        c2.execute("SELECT name, price, qty FROM items WHERE session_id = ?", (r[0],))
        its = c2.fetchall()
        session['items'] = [{"name": i[0], "price": i[1], "qty": i[2]} for i in its]
        sessions.append(session)
    conn.close()
    return sessions

# -----------------------
# Tax lookup
# -----------------------
def get_tax_rate_for_zip(zip_code, tax_api_url=None):
    zip_code = (zip_code or "").strip()
    if tax_api_url and requests:
        try:
            url = tax_api_url.replace("{zip}", zip_code)
            resp = requests.get(url, timeout=4)
            if resp.status_code == 200:
                data = resp.json()
                # expects {"rate": 0.0825} or {"combined_rate": 0.0825}
                if isinstance(data, dict):
                    if "rate" in data:
                        return float(data["rate"])
                    if "combined_rate" in data:
                        return float(data["combined_rate"])
            # fall through to mock if fails
        except Exception:
            pass
    # mock table
    return MOCK_ZIP_TAX.get(zip_code, DEFAULT_TAX_RATE)

# -----------------------
# Voice (TTS) helpers
# -----------------------
class VoiceEngine:
    def __init__(self, enabled=True, voice_name=None):
        self.enabled = enabled and (pyttsx3 is not None)
        self.engine = None
        if self.enabled:
            try:
                self.engine = pyttsx3.init()
                # set a slightly country-ish male-ish voice if available by name/props
                if voice_name:
                    try:
                        voices = self.engine.getProperty('voices')
                        for v in voices:
                            if voice_name.lower() in v.name.lower():
                                self.engine.setProperty('voice', v.id)
                                break
                    except Exception:
                        pass
            except Exception:
                self.engine = None
                self.enabled = False

    def speak(self, text):
        if not self.enabled or not self.engine:
            return False
        # run speech in thread to avoid blocking UI
        threading.Thread(target=self._speak_blocking, args=(text,), daemon=True).start()
        return True

    def _speak_blocking(self, t):
        try:
            self.engine.say(t)
            self.engine.runAndWait()
        except Exception:
            pass

# -----------------------
# Main App Window
# -----------------------
class CartMateUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(QSize(760, 580))
        init_db()

        # load settings
        self.simple_mode = load_setting("simple_mode", True)
        self.voice_out = load_setting("voice_out", True)
        self.tax_api_url = load_setting("tax_api_url", "")
        self.default_currency = load_setting("default_currency", "$")
        self.voice_engine = VoiceEngine(enabled=self.voice_out, voice_name="male")

        # current cart
        self.items = []
        self.store = ""
        self.zip_code = ""
        self.tax_rate = DEFAULT_TAX_RATE

        # Build UI
        self._build_ui()
        self._load_history()

    def _build_ui(self):
        font_header = QFont("Inter", 16, QFont.Bold)
        font_label = QFont("Inter", 11)

        main = QWidget()
        self.setCentralWidget(main)
        layout = QVBoxLayout()
        layout.setSpacing(12)
        layout.setContentsMargins(12, 12, 12, 12)
        main.setLayout(layout)

        # Header row with title and settings button upper-right
        header_row = QHBoxLayout()
        title = QLabel("CartMate  🛒")
        title.setFont(QFont("Inter", 20, QFont.Bold))
        title.setStyleSheet("color: #0d3b66;")
        header_row.addWidget(title)
        header_row.addStretch()
        self.settings_btn = QPushButton("⚙️ Settings")
        self.settings_btn.setFixedWidth(120)
        self.settings_btn.clicked.connect(self.open_settings)
        header_row.addWidget(self.settings_btn)
        layout.addLayout(header_row)

        # Store and tax controls
        card_store = QFrame()
        card_store.setFrameShape(QFrame.StyledPanel)
        card_store.setStyleSheet(self.card_style())
        store_layout = QFormLayout()
        self.store_input = QLineEdit()
        self.store_input.setPlaceholderText("Store name (optional)")
        self.store_input.textChanged.connect(lambda v: setattr(self, "store", v))
        self.zip_input = QLineEdit()
        self.zip_input.setPlaceholderText("ZIP code for tax")
        self.zip_input.setMaxLength(10)
        self.zip_input.textChanged.connect(self._zip_changed)
        detect_btn = QPushButton("Detect (simulate)")
        detect_btn.clicked.connect(self._simulate_detect_location)
        detect_btn.setToolTip("Simulated GPS detection — for demo it picks a nearby area.")
        detect_row = QHBoxLayout()
        detect_row.addWidget(self.zip_input)
        detect_row.addWidget(detect_btn)
        store_layout.addRow(QLabel("Store:"), self.store_input)
        store_layout.addRow(QLabel("Tax (ZIP):"), detect_row)
        card_store.setLayout(store_layout)
        layout.addWidget(card_store)

        # Item input card
        card_item = QFrame()
        card_item.setFrameShape(QFrame.StyledPanel)
        card_item.setStyleSheet(self.card_style())
        il = QHBoxLayout()
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Item name")
        self.price_input = QLineEdit()
        self.price_input.setPlaceholderText("Price (e.g., 12.50)")
        self.qty_input = QLineEdit("1")
        self.qty_input.setFixedWidth(70)
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self.add_item)
        add_btn.setFixedWidth(120)
        il.addWidget(self.name_input)
        il.addWidget(self.price_input)
        il.addWidget(self.qty_input)
        il.addWidget(add_btn)
        card_item.setLayout(il)
        layout.addWidget(card_item)

        # Items list + totals
        card_items = QFrame()
        card_items.setFrameShape(QFrame.StyledPanel)
        card_items.setStyleSheet(self.card_style())
        v = QVBoxLayout()
        v.setSpacing(8)
        self.list_widget = QListWidget()
        v.addWidget(self.list_widget)

        tot_row = QHBoxLayout()
        self.subtotal_label = QLabel("Subtotal: $0.00")
        self.tax_label = QLabel("Tax: $0.00")
        self.total_label = QLabel("<b>Total: $0.00</b>")
        tot_row.addWidget(self.subtotal_label)
        tot_row.addStretch()
        tot_row.addWidget(self.tax_label)
        tot_row.addStretch()
        tot_row.addWidget(self.total_label)
        v.addLayout(tot_row)

        btn_row = QHBoxLayout()
        save_btn = QPushButton("Save Session")
        save_btn.clicked.connect(self.save_session)
        export_btn = QPushButton("Export CSV")
        export_btn.clicked.connect(self.export_csv)
        clear_btn = QPushButton("Clear Cart")
        clear_btn.clicked.connect(self.clear_cart)
        speak_btn = QPushButton("Fred: Read totals")
        speak_btn.clicked.connect(self._speak_totals)
        btn_row.addWidget(save_btn)
        btn_row.addWidget(export_btn)
        btn_row.addWidget(clear_btn)
        btn_row.addWidget(speak_btn)
        v.addLayout(btn_row)

        card_items.setLayout(v)
        layout.addWidget(card_items, stretch=1)

        # History card
        card_hist = QFrame()
        card_hist.setFrameShape(QFrame.StyledPanel)
        card_hist.setStyleSheet(self.card_style())
        hl = QVBoxLayout()
        hl.addWidget(QLabel("History (click to restore session)"))
        self.history_list = QListWidget()
        self.history_list.itemClicked.connect(self._history_clicked)
        hl.addWidget(self.history_list)
        card_hist.setLayout(hl)
        layout.addWidget(card_hist, stretch=1)

        # Footer / small help
        footer = QLabel("Simple Mode: quick big controls | Advanced Mode: more options (use Settings).")
        footer.setStyleSheet("color: #666;")
        layout.addWidget(footer)

        # initial refresh
        self._refresh_ui()

    def card_style(self):
        return """
            background: #fff;
            border-radius: 12px;
            padding: 10px;
            box-shadow: 0px 8px 20px rgba(13,59,102,0.06);
        """

    def _zip_changed(self, v):
        self.zip_code = v
        # update tax rate immediately (uses setting TAX_API_URL if set)
        url = load_setting("tax_api_url", "")
        rate = get_tax_rate_for_zip(v, tax_api_url=url)
        self.tax_rate = rate
        self._refresh_totals()

    def _simulate_detect_location(self):
        # Simulate detection: rotate through mock zips
        keys = list(MOCK_ZIP_TAX.keys())
        import random
        picked = random.choice(keys)
        self.zip_input.setText(picked)
        QMessageBox.information(self, "Detected location", f"Simulated ZIP: {picked}")

    def add_item(self):
        name = (self.name_input.text() or "Item").strip()
        price_s = self.price_input.text().strip()
        qty_s = self.qty_input.text().strip() or "1"
        # Validation
        try:
            price = Decimal(price_s)
            if price < 0 or price > Decimal("1000000"):
                raise InvalidOperation()
        except Exception:
            QMessageBox.warning(self, "Invalid price", "Please enter a valid numeric price between 0 and 1,000,000.")
            return
        try:
            qty = Decimal(qty_s)
            if qty <= 0:
                raise InvalidOperation()
        except Exception:
            QMessageBox.warning(self, "Invalid qty", "Please enter a valid quantity (>= 1).")
            return
        item = {"name": name, "price": float(price), "qty": float(qty)}
        self.items.append(item)
        self.name_input.clear()
        self.price_input.clear()
        self.qty_input.setText("1")
        # Add to list widget
        self._append_item_to_list(item)
        self._refresh_totals()

    def _append_item_to_list(self, item):
        text = f"{item['name']}  x{item['qty']}    {self.default_currency}{(item['price']*item['qty']):.2f}"
        lw = QListWidgetItem(text)
        self.list_widget.addItem(lw)

    def _refresh_totals(self):
        subtotal = sum(it['price']*it['qty'] for it in self.items)
        tax = subtotal * (self.tax_rate or DEFAULT_TAX_RATE)
        total = subtotal + tax
        self.subtotal_label.setText(f"Subtotal: {self.default_currency}{subtotal:.2f}")
        self.tax_label.setText(f"Tax ({(self.tax_rate or DEFAULT_TAX_RATE)*100:.2f}%): {self.default_currency}{tax:.2f}")
        self.total_label.setText(f"<b>Total: {self.default_currency}{total:.2f}</b>")

    def save_session(self):
        if not self.items:
            QMessageBox.warning(self, "No items", "Add items before saving a session.")
            return
        session_id = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
        subtotal = sum(it['price']*it['qty'] for it in self.items)
        tax = subtotal * (self.tax_rate or DEFAULT_TAX_RATE)
        total = subtotal + tax
        save_session_to_db(session_id, self.store_input.text(), self.zip_input.text(), self.tax_rate, subtotal, tax, total, self.items)
        QMessageBox.information(self, "Saved", "Shopping session saved to history.")
        # clear current cart
        self.items = []
        self.list_widget.clear()
        self._refresh_totals()
        self._load_history()

    def clear_cart(self):
        self.items = []
        self.list_widget.clear()
        self._refresh_totals()

    def export_csv(self):
        history = load_history_from_db()
        if not history:
            QMessageBox.information(self, "No history", "No saved sessions to export.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export CSV", str(Path.home() / "cartmate_history.csv"), "CSV Files (*.csv)")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["date", "store", "zip", "tax_rate", "subtotal", "tax", "total", "items"])
            for s in history:
                items_str = "; ".join([f"{i['name']} x{i['qty']} @{i['price']:.2f}" for i in s['items']])
                writer.writerow([s['date'], s['store'], s['zip'], s['tax_rate'], s['subtotal'], s['tax'], s['total'], items_str])
        QMessageBox.information(self, "Exported", f"History exported to {path}")

    def _load_history(self):
        self.history_list.clear()
        sessions = load_history_from_db()
        for s in sessions:
            txt = f"{s['date'][:19]}  | {s.get('store') or 'Store'}  | Total: {self.default_currency}{s['total']:.2f}"
            it = QListWidgetItem(txt)
            it.setData(Qt.UserRole, s)
            self.history_list.addItem(it)

    def _history_clicked(self, item):
        s = item.data(Qt.UserRole)
        # show options dialog: restore or delete
        dlg = QMessageBox(self)
        dlg.setWindowTitle("Session")
        dlg.setText(f"Session on {s['date'][:19]}\nStore: {s.get('store') or ''}\nTotal: {self.default_currency}{s['total']:.2f}")
        restore = dlg.addButton("Restore", QMessageBox.ActionRole)
        delete = dlg.addButton("Delete", QMessageBox.DestructiveRole)
        dlg.addButton("Close", QMessageBox.RejectRole)
        dlg.exec_()
        if dlg.clickedButton() == restore:
            # restore items into current cart
            self.items = [dict(name=i['name'], price=i['price'], qty=i['qty']) for i in s['items']]
            self.zip_input.setText(s.get('zip') or "")
            self.store_input.setText(s.get('store') or "")
            self.list_widget.clear()
            for it in self.items:
                self._append_item_to_list(it)
            self.tax_rate = s.get('tax_rate') or DEFAULT_TAX_RATE
            self._refresh_totals()
        elif dlg.clickedButton() == delete:
            # delete session from DB
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("DELETE FROM items WHERE session_id = ?", (s['id'],))
            c.execute("DELETE FROM sessions WHERE id = ?", (s['id'],))
            conn.commit()
            conn.close()
            QMessageBox.information(self, "Deleted", "Session removed.")
            self._load_history()

    def _speak_totals(self):
        subtotal = sum(it['price']*it['qty'] for it in self.items)
        tax = subtotal * (self.tax_rate or DEFAULT_TAX_RATE)
        total = subtotal + tax
        txt = f"Current subtotal is {self.default_currency}{subtotal:.2f}. Tax {self.default_currency}{tax:.2f}. Total {self.default_currency}{total:.2f}."
        if self.voice_engine and self.voice_engine.speak(txt):
            # also show a small message
            QMessageBox.information(self, "Fred", "Fred is speaking the totals.")
        else:
            QMessageBox.information(self, "Fred (text)", txt)

    def open_settings(self):
        dlg = SettingsDialog(parent=self)
        dlg.exec_()
        # reload settings
        self.simple_mode = load_setting("simple_mode", True)
        self.voice_out = load_setting("voice_out", True)
        self.tax_api_url = load_setting("tax_api_url", "")
        self.default_currency = load_setting("default_currency", "$")
        # recreate voice engine
        self.voice_engine = VoiceEngine(enabled=self.voice_out, voice_name="male")
        self._refresh_totals()
        self._load_history()

# -----------------------
# Settings dialog
# -----------------------
class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(480)
        self._build_ui()

    def _build_ui(self):
        v = QVBoxLayout()
        form = QFormLayout()

        # Simple/Advanced mode
        self.simple_checkbox = QCheckBox("Enable Simple Mode (large friendly controls)")
        self.simple_checkbox.setChecked(load_setting("simple_mode", True))
        form.addRow(self.simple_checkbox)

        # Voice out toggle
        self.voice_out_checkbox = QCheckBox("Enable voice output (Fred)")
        self.voice_out_checkbox.setChecked(load_setting("voice_out", True))
        form.addRow(self.voice_out_checkbox)

        # Tax API URL
        self.tax_api_input = QLineEdit(load_setting("tax_api_url", ""))
        self.tax_api_input.setPlaceholderText("Tax API URL (use {zip} as placeholder) e.g. https://your.api/tax?zip={zip}")
        form.addRow(QLabel("Tax API URL (optional):"), self.tax_api_input)

        # Currency selection (simple)
        currency_cb = QComboBox()
        currency_cb.addItems(["$", "€", "£", "¥"])
        current_cur = load_setting("default_currency", "$")
        currency_cb.setCurrentText(current_cur)
        form.addRow(QLabel("Currency:"), currency_cb)
        self.currency_cb = currency_cb

        # Buttons
        btn_row = QHBoxLayout()
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self._save)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(save_btn)
        btn_row.addWidget(cancel_btn)
        v.addLayout(form)
        v.addLayout(btn_row)
        self.setLayout(v)
        # layout spacing tweaks
        v.setContentsMargins(12, 12, 12, 12)

    def _save(self):
        save_setting("simple_mode", bool(self.simple_checkbox.isChecked()))
        save_setting("voice_out", bool(self.voice_out_checkbox.isChecked()))
        save_setting("tax_api_url", str(self.tax_api_input.text().strip()))
        save_setting("default_currency", str(self.currency_cb.currentText()))
        QMessageBox.information(self, "Saved", "Settings saved.")
        self.accept()

# -----------------------
# Main entry
# -----------------------
def main():
    app = QApplication(sys.argv)
    # basic app stylesheet (Gen Z aesthetic)
    app.setStyleSheet("""
        QWidget { font-family: Inter, Arial, Helvetica, sans-serif; color: #081222; }
        QPushButton { padding: 8px; border-radius: 10px; border: none; background: qlineargradient(spread:pad, x1:0, y1:0, x2:1, y2:0, stop:0 #7b61ff, stop:1 #ff61a6); color: white; font-weight: 600; }
        QPushButton:disabled { background: #ccc; color: #666; }
        QLineEdit { padding: 8px; border-radius: 8px; border: 1px solid #eee; background: #f6f8fb; }
        QListWidget { background: #fff; border-radius: 8px; border: 1px solid #f0f0f0; padding: 6px; }
    """)
    win = CartMateUI()
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
