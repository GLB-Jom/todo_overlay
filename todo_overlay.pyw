# -*- coding: utf-8 -*-
"""
Todo Overlay — minimal desktop todo list + calendar for Windows 10/11
- Borderless overlay that blends with the wallpaper (panel or ghost mode)
- Drag to move, position is remembered
- Tasks: title, note, status (wait/doing/done), priority (high/medium/low)
- Priority colors: high=red, medium=yellow, low=gray
- Done tasks: strikethrough + faded; toggle show/hide done
- Created date + optional due date (can show/hide due dates)
- Month calendar with Google Calendar read-only sync via secret iCal URL
  (days with events are highlighted; click a day to list its events)
- Settings: font, size, opacity, ghost mode, topmost, autostart, iCal URL
- Data stored in todo_data.json / ical_cache.json next to this file

Run: double-click (requires Python 3.8+ on Windows). The .pyw extension
runs without a console window.

Known limitations (kept simple on purpose):
- Recurring events: basic DAILY/WEEKLY/MONTHLY/YEARLY with INTERVAL,
  COUNT, UNTIL only. Complex rules (e.g. BYDAY=MO,WE,FR) follow the
  start day's weekday only.
- Event times/timezones are ignored; only the date part is used.
"""

import calendar
import http.server
import json
import re
import shutil
import socketserver
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path

import tkinter as tk
from tkinter import ttk, font as tkfont, messagebox, filedialog

APP_NAME = "TodoOverlay"
APP_VERSION = "1.8"
APP_DIR = Path(__file__).resolve().parent
DATA_FILE = APP_DIR / "todo_data.json"
BACKUP_FILE = APP_DIR / "todo_data.backup.json"
BACKUP_FILE2 = APP_DIR / "todo_data.backup2.json"
CACHE_FILE = APP_DIR / "ical_cache.json"
WORKLOG_FILE = APP_DIR / "worklog.json"
TOKEN_FILE = APP_DIR / "google_token.json"

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
GOOGLE_EVENTS_URL = ("https://www.googleapis.com/calendar/v3/"
                     "calendars/primary/events")

MAGIC = "#010203"        # color key made fully transparent in ghost mode

# Theme color sets — chosen set is applied to module globals (TEXT,
# PANEL_BG, ...) via apply_theme(), so the rest of the code keeps
# using the same constant names.
THEMES = {
    "dark": dict(
        PANEL_BG="#1c1c22",   # panel background (normal mode)
        ROW_BG="#26262e",     # task row background (normal mode)
        TEXT="#e6e3f0",       # soft pastel white (not stark white)
        DIM="#9a9aa2",
        ACCENT="#4aa3ff",
        EVENT_FG="#ffce54",   # day-number color when the day has events
        WEEKEND_FG="#ff8a80",  # Sat-Sun day-number color
        DIVIDER="#3a3a44",
        DRAG_TINT="#3a3a55",  # row color while dragging to reorder
        TODAY_FG="#101014",   # day-number color on the accent "today" cell
        OVERDUE_FG="#ff6b5e",
    ),
    "light": dict(
        PANEL_BG="#f0ece4",   # soft cream
        ROW_BG="#e5e0d4",
        TEXT="#2c2a32",
        DIM="#7a7680",
        ACCENT="#2f6fd0",
        EVENT_FG="#b07d00",
        WEEKEND_FG="#d05c50",
        DIVIDER="#cfc8bb",
        DRAG_TINT="#d8d2c2",
        TODAY_FG="#ffffff",
        OVERDUE_FG="#c0392b",
    ),
}


def apply_theme(name):
    """Load a theme's colors into module globals."""
    globals().update(THEMES.get(name, THEMES["dark"]))


apply_theme("dark")      # defaults until settings are loaded

STATUS_ORDER = ["wait", "doing", "done"]
STATUS_LABEL = {"wait": "รอ", "doing": "กำลังทำ", "done": "ทำแล้ว"}
STATUS_ICON = {"wait": "○", "doing": "◐", "done": "✔"}

PRIORITY_ORDER = ["high", "medium", "low"]
PRIORITY_LABEL = {"high": "สูง", "medium": "กลาง", "low": "ต่ำ"}
PRIORITY_COLOR = {"high": "#e74c3c", "medium": "#f1c40f", "low": "#95a5a6"}

TH_MONTHS = ["มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม",
             "มิถุนายน", "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม",
             "พฤศจิกายน", "ธันวาคม"]
TH_WEEKDAYS = ["อา", "จ", "อ", "พ", "พฤ", "ศ", "ส"]  # Sunday first

FETCH_INTERVAL_MS = 30 * 60 * 1000   # refresh iCal every 30 minutes

DEFAULT_DATA = {
    "settings": {
        "theme": "dark",          # "dark" | "light"
        "font_family": "Segoe UI",
        "font_size": 11,
        "opacity": 0.88,
        "ghost_mode": False,
        "show_done": True,
        "show_due": True,
        "show_note": True,
        "show_calendar": True,
        "cal_source": "url",      # "url" | "file" | "api"
        "ical_url": "",
        "ics_path": "",
        "g_client_id": "",
        "g_client_secret": "",
        "topmost": False,
        "autostart": False,
        "pos": [120, 120],
    },
    "tasks": [],
}


# ----------------------------------------------------------------------
# Data layer
# ----------------------------------------------------------------------
def backup_data_file(has_tasks):
    """Keep 2 rotating backups of todo_data.json, made at startup.
    Skipped when there are no tasks, so a freshly-created empty file
    can never overwrite a good backup."""
    if not has_tasks:
        return
    try:
        if BACKUP_FILE.exists():
            shutil.copy2(BACKUP_FILE, BACKUP_FILE2)
        shutil.copy2(DATA_FILE, BACKUP_FILE)
    except OSError:
        pass


def load_data():
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in DEFAULT_DATA["settings"].items():
                data.setdefault("settings", {}).setdefault(k, v)
            data.setdefault("tasks", [])
            backup_data_file(bool(data["tasks"]))
            return data
        except (json.JSONDecodeError, OSError):
            try:
                DATA_FILE.rename(DATA_FILE.with_suffix(".json.bak"))
            except OSError:
                pass
    return json.loads(json.dumps(DEFAULT_DATA))


def save_data(data):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as e:
        messagebox.showerror(APP_NAME, f"บันทึกไฟล์ไม่สำเร็จ:\n{e}")


def load_worklog():
    """Return the worklog as a list (empty list on missing/broken file)."""
    try:
        with open(WORKLOG_FILE, "r", encoding="utf-8") as f:
            log = json.load(f)
        return log if isinstance(log, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_worklog(log):
    try:
        with open(WORKLOG_FILE, "w", encoding="utf-8") as f:
            json.dump(log, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def append_worklog(task):
    """Append a finished task to worklog.json — a permanent, append-only
    history used for the yearly performance review. Deleting the task
    later does not touch this file.
    Skips when the same task was already logged today, so toggling a
    task's status back and forth doesn't create duplicates.
    star/impact are filled in later via the worklog editor — no data
    migration needed."""
    today = date.today().isoformat()
    log = load_worklog()
    for e in log:
        if e.get("id") == task.get("id") and e.get("done_date") == today:
            return
    log.append({
        "id": task.get("id", ""),
        "title": task.get("title", ""),
        "note": task.get("note", ""),
        "priority": task.get("priority", "medium"),
        "created": task.get("created", ""),
        "done_date": today,
        "star": False,
        "impact": "",
    })
    save_worklog(log)


def load_event_cache():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("events", {})
    except (json.JSONDecodeError, OSError):
        return {}


def save_event_cache(events):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"fetched": datetime.now().isoformat(),
                       "events": events}, f, ensure_ascii=False)
    except OSError:
        pass


# ----------------------------------------------------------------------
# Autostart (HKCU Run registry key) — Windows only
# ----------------------------------------------------------------------
def set_autostart(enable: bool) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE,
        )
        if enable:
            exe = Path(sys.executable)
            pyw = exe.with_name("pythonw.exe")
            runner = pyw if pyw.exists() else exe
            cmd = f'"{runner}" "{Path(__file__).resolve()}"'
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return True
    except OSError as e:
        messagebox.showerror(APP_NAME, f"ตั้งค่า autostart ไม่สำเร็จ:\n{e}")
        return False


# ----------------------------------------------------------------------
# iCal (.ics) parsing — date-level only, basic recurrence
# ----------------------------------------------------------------------
def parse_ics_date(val):
    m = re.match(r"(\d{4})(\d{2})(\d{2})", val)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def add_months(d, n):
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def expand_rrule(start, rrule, win_start, win_end):
    """Expand an event into dates inside [win_start, win_end]."""
    if start is None:
        return []
    if not rrule:
        return [start] if win_start <= start <= win_end else []
    parts = dict(p.split("=", 1) for p in rrule.split(";") if "=" in p)
    freq = parts.get("FREQ", "")
    try:
        interval = max(1, int(parts.get("INTERVAL", "1")))
    except ValueError:
        interval = 1
    count = None
    if parts.get("COUNT", "").isdigit():
        count = int(parts["COUNT"])
    until = parse_ics_date(parts["UNTIL"]) if "UNTIL" in parts else None

    out, d, n = [], start, 0
    for _ in range(2000):                       # hard safety cap
        if d > win_end or (until and d > until):
            break
        if count is not None and n >= count:
            break
        if d >= win_start:
            out.append(d)
        n += 1
        if freq == "DAILY":
            d += timedelta(days=interval)
        elif freq == "WEEKLY":
            d += timedelta(weeks=interval)
        elif freq == "MONTHLY":
            d = add_months(d, interval)
        elif freq == "YEARLY":
            d = add_months(d, 12 * interval)
        else:
            break
    return out


def parse_ics(text, win_start, win_end):
    """Return {iso_date: [event titles]} within the window."""
    # unfold folded lines (lines starting with space/tab continue previous)
    lines = []
    for raw in text.splitlines():
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)

    events = {}
    in_ev, cur = False, {}
    for line in lines:
        if line.startswith("BEGIN:VEVENT"):
            in_ev, cur = True, {}
        elif line.startswith("END:VEVENT"):
            in_ev = False
            for d in expand_rrule(cur.get("start"), cur.get("rrule"),
                                  win_start, win_end):
                events.setdefault(d.isoformat(), []).append(
                    cur.get("summary", "(ไม่มีชื่อ)"))
        elif in_ev:
            if line.startswith("DTSTART"):
                cur["start"] = parse_ics_date(line.split(":", 1)[-1].strip())
            elif line.startswith("SUMMARY"):
                cur["summary"] = line.split(":", 1)[-1].strip()
            elif line.startswith("RRULE"):
                cur["rrule"] = line.split(":", 1)[-1].strip()
    return events


# ----------------------------------------------------------------------
# Google Calendar API (OAuth 2.0 installed-app flow, stdlib only)
# ----------------------------------------------------------------------
def _token_request(payload):
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(GOOGLE_TOKEN_URL, data=data)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def google_login(client_id, client_secret):
    """Open browser for consent, catch redirect on localhost, save token."""
    holder = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            holder["code"] = q.get("code", [None])[0]
            holder["error"] = q.get("error", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = ("เชื่อมต่อสำเร็จ ปิดหน้านี้แล้วกลับไปที่แอพได้เลย"
                   if holder.get("code") else "การเชื่อมต่อล้มเหลว")
            self.wfile.write(f"<h3>{msg}</h3>".encode())

        def log_message(self, *args):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    srv.timeout = 180                     # give up after 3 minutes
    port = srv.server_address[1]
    redirect = f"http://127.0.0.1:{port}"
    params = urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect,
        "response_type": "code", "scope": GOOGLE_SCOPE,
        "access_type": "offline", "prompt": "consent"})
    webbrowser.open(f"{GOOGLE_AUTH_URL}?{params}")
    srv.handle_request()                  # wait for one redirect or timeout
    srv.server_close()

    if holder.get("error"):
        raise RuntimeError(f"Google ปฏิเสธการเชื่อมต่อ: {holder['error']}")
    if not holder.get("code"):
        raise RuntimeError("ไม่ได้รับการยืนยันจากเบราว์เซอร์ (หมดเวลา 3 นาที)")
    tok = _token_request({
        "code": holder["code"], "client_id": client_id,
        "client_secret": client_secret, "redirect_uri": redirect,
        "grant_type": "authorization_code"})
    tok["obtained"] = time.time()
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(tok, f)
    return tok


def google_access_token(client_id, client_secret):
    """Return a valid access token, refreshing it when expired."""
    if not TOKEN_FILE.exists():
        raise RuntimeError("ยังไม่ได้เชื่อมต่อบัญชี Google\n"
                           "ไปที่ ⚙ ตั้งค่า แล้วกด \"เชื่อมต่อบัญชี Google\"")
    with open(TOKEN_FILE, "r", encoding="utf-8") as f:
        tok = json.load(f)
    expired = time.time() > (tok.get("obtained", 0)
                             + tok.get("expires_in", 0) - 60)
    if expired:
        if "refresh_token" not in tok:
            raise RuntimeError("การเชื่อมต่อหมดอายุ กรุณาเชื่อมต่อใหม่ในตั้งค่า")
        new = _token_request({
            "refresh_token": tok["refresh_token"], "client_id": client_id,
            "client_secret": client_secret, "grant_type": "refresh_token"})
        tok.update(new)
        tok["obtained"] = time.time()
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump(tok, f)
    return tok["access_token"]


def fetch_google_events(client_id, client_secret, win_start, win_end):
    """Return {iso_date: [titles]} from the primary calendar via REST API.
    singleEvents=true makes Google expand recurring events for us."""
    token = google_access_token(client_id, client_secret)
    events, page = {}, None
    while True:
        params = {
            "timeMin": f"{win_start.isoformat()}T00:00:00Z",
            "timeMax": f"{win_end.isoformat()}T00:00:00Z",
            "singleEvents": "true", "maxResults": "2500"}
        if page:
            params["pageToken"] = page
        req = urllib.request.Request(
            GOOGLE_EVENTS_URL + "?" + urllib.parse.urlencode(params),
            headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        for it in data.get("items", []):
            st = it.get("start", {})
            d = st.get("date") or st.get("dateTime", "")[:10]
            if d:
                events.setdefault(d, []).append(
                    it.get("summary", "(ไม่มีชื่อ)"))
        page = data.get("nextPageToken")
        if not page:
            break
    return events


def parse_due(text: str):
    """Return ISO date string or None; raise ValueError if invalid."""
    text = text.strip()
    if not text:
        return None
    return datetime.strptime(text, "%Y-%m-%d").date().isoformat()


# ----------------------------------------------------------------------
# Main application
# ----------------------------------------------------------------------
class TodoOverlay:
    def __init__(self):
        self.data = load_data()
        self.events = load_event_cache()
        s = self.data["settings"]

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.overrideredirect(True)          # borderless
        x, y = s.get("pos", [120, 120])
        self.root.geometry(f"+{int(x)}+{int(y)}")
        self.root.attributes("-topmost", bool(s["topmost"]))

        self._drag_off = None
        self._row_drag = None
        self._day_popup = None
        self.main = None
        today = date.today()
        self.cal_view = date(today.year, today.month, 1)

        self.apply_appearance()
        self.build_ui()
        self.root.after(800, self.schedule_fetch)  # first fetch after start

    # ---------------- appearance ----------------
    def apply_appearance(self):
        s = self.data["settings"]
        apply_theme(s.get("theme", "dark"))
        if s["ghost_mode"]:
            self.bg = MAGIC
            self.row_bg = MAGIC
            self.root.attributes("-alpha", 1.0)
            self.root.configure(bg=MAGIC)
            self.root.attributes("-transparentcolor", MAGIC)
        else:
            self.bg = PANEL_BG
            self.row_bg = ROW_BG
            try:
                self.root.attributes("-transparentcolor", "")
            except tk.TclError:
                pass
            self.root.configure(bg=PANEL_BG)
            self.root.attributes("-alpha", float(s["opacity"]))
        self.root.attributes("-topmost", bool(s["topmost"]))
        self.make_fonts()

    def make_fonts(self):
        s = self.data["settings"]
        fam, size = s["font_family"], int(s["font_size"])
        self.f_title = tkfont.Font(family=fam, size=size, weight="bold")
        self.f_done = tkfont.Font(family=fam, size=size, weight="bold",
                                  overstrike=1)
        self.f_note = tkfont.Font(family=fam, size=max(size - 2, 7))
        self.f_note_done = tkfont.Font(family=fam, size=max(size - 2, 7),
                                       overstrike=1)
        self.f_small = tkfont.Font(family=fam, size=max(size - 3, 7))
        self.f_dialog = tkfont.Font(family=fam, size=size)
        self.f_head = tkfont.Font(family=fam, size=size + 1, weight="bold")
        self.f_icon = tkfont.Font(family=fam, size=size + 2)
        self.f_cal = tkfont.Font(family=fam, size=max(size - 1, 8))
        self.f_cal_b = tkfont.Font(family=fam, size=max(size - 1, 8),
                                   weight="bold")

    # ---------------- UI ----------------
    def build_ui(self):
        if self.main is not None:
            self.main.destroy()
        self.main = tk.Frame(self.root, bg=self.bg, padx=10, pady=8)
        self.main.pack(fill="both", expand=True)

        # header (drag area + buttons)
        head = tk.Frame(self.main, bg=self.bg)
        head.pack(fill="x", pady=(0, 6))
        title = tk.Label(head, text="📝 To-Do", font=self.f_head,
                         fg=TEXT, bg=self.bg)
        title.pack(side="left")
        ver = tk.Label(head, text=f"v{APP_VERSION}", font=self.f_small,
                       fg=DIM, bg=self.bg)
        ver.pack(side="left", padx=(4, 0), pady=(3, 0))
        for w in (head, title, ver):
            w.bind("<ButtonPress-1>", self.drag_start)
            w.bind("<B1-Motion>", self.drag_move)
            w.bind("<ButtonRelease-1>", self.drag_end)

        def hbtn(text, cmd, tip_fg=DIM):
            b = tk.Label(head, text=text, font=self.f_icon, fg=tip_fg,
                         bg=self.bg, cursor="hand2", padx=4)
            b.pack(side="right")
            b.bind("<Button-1>", lambda e: cmd())
            b.bind("<Enter>", lambda e: b.config(fg=TEXT))
            b.bind("<Leave>", lambda e: b.config(fg=tip_fg))
            return b

        hbtn("✕", self.quit)
        hbtn("⚙", self.open_settings)
        hbtn("📅", self.toggle_calendar)
        hbtn("＋", lambda: self.open_task_dialog(None), tip_fg=ACCENT)

        # calendar container (above the todo list)
        self.cal_frame = tk.Frame(self.main, bg=self.bg)
        self.cal_frame.pack(fill="x")

        # task list container
        self.list_frame = tk.Frame(self.main, bg=self.bg)
        self.list_frame.pack(fill="both", expand=True)

        self.build_calendar()
        self.refresh()

    def refresh(self):
        for w in self.list_frame.winfo_children():
            w.destroy()
        self._rows = []          # [(row_widget, task)] in display order
        s = self.data["settings"]
        tasks = self.data["tasks"]
        if not s["show_done"]:
            tasks = [t for t in tasks if t["status"] != "done"]
        # show done tasks at the bottom (display only; data order unchanged)
        tasks = ([t for t in tasks if t["status"] != "done"]
                 + [t for t in tasks if t["status"] == "done"])

        if not tasks:
            tk.Label(self.list_frame, text="ยังไม่มีงาน — กด ＋ เพื่อเพิ่ม",
                     font=self.f_note, fg=DIM, bg=self.bg,
                     pady=10).pack()
        for t in tasks:
            self.build_row(t)
        self.root.geometry("")          # let the window shrink to fit
        self.root.update_idletasks()

    def build_row(self, t):
        done = t["status"] == "done"
        fg = DIM if done else TEXT
        bar_color = DIM if done else PRIORITY_COLOR[t["priority"]]

        row = tk.Frame(self.list_frame, bg=self.row_bg)
        row.pack(fill="x", pady=2)

        bar = tk.Frame(row, bg=bar_color, width=4)
        bar.pack(side="left", fill="y")

        st = tk.Label(row, text=STATUS_ICON[t["status"]], font=self.f_icon,
                      fg=bar_color, bg=self.row_bg, cursor="hand2", padx=6)
        st.pack(side="left")
        st.bind("<Button-1>", lambda e, task=t: self.cycle_status(task))

        body = tk.Frame(row, bg=self.row_bg)
        body.pack(side="left", fill="x", expand=True, pady=3)

        top = tk.Frame(body, bg=self.row_bg)
        top.pack(fill="x")
        tk.Label(top, text=t["title"],
                 font=self.f_done if done else self.f_title,
                 fg=fg, bg=self.row_bg, anchor="w", justify="left",
                 wraplength=220).pack(side="left")

        if self.data["settings"]["show_due"] and t.get("due"):
            overdue = (not done) and t["due"] < date.today().isoformat()
            due_fg = OVERDUE_FG if overdue else DIM
            d = datetime.strptime(t["due"], "%Y-%m-%d")
            tk.Label(top, text=f"📅 {d.strftime('%d/%m')}",
                     font=self.f_small, fg=due_fg, bg=self.row_bg,
                     padx=6).pack(side="right")

        if t.get("note") and self.data["settings"]["show_note"]:
            tk.Label(body, text=t["note"],
                     font=self.f_note_done if done else self.f_note,
                     fg=DIM, bg=self.row_bg, anchor="w", justify="left",
                     wraplength=230).pack(fill="x")

        menu = tk.Menu(row, tearoff=0)
        menu.add_command(label="แก้ไข",
                         command=lambda task=t: self.open_task_dialog(task))
        stmenu = tk.Menu(menu, tearoff=0)
        for key in STATUS_ORDER:
            stmenu.add_command(
                label=STATUS_LABEL[key],
                command=lambda task=t, k=key: self.set_status(task, k))
        menu.add_cascade(label="เปลี่ยนสถานะ", menu=stmenu)
        menu.add_separator()
        menu.add_command(label="ลบ",
                         command=lambda task=t: self.delete_task(task))

        widgets = [row, bar, body, top] + list(top.winfo_children()) \
                  + list(body.winfo_children())
        for w in widgets:
            w.bind("<Button-3>",
                   lambda e, m=menu: m.tk_popup(e.x_root, e.y_root))
            w.bind("<Double-Button-1>",
                   lambda e, task=t: self.open_task_dialog(task))
            # drag to reorder (activates after a small movement threshold)
            w.bind("<ButtonPress-1>",
                   lambda e, task=t, r=row, b=bar: self.row_press(e, task, r, b))
            w.bind("<B1-Motion>", self.row_motion)
            w.bind("<ButtonRelease-1>", self.row_release)
        self._rows.append((row, t))

    # ---------------- drag tasks to reorder ----------------
    def row_press(self, e, task, row, bar):
        self._row_drag = {"task": task, "row": row, "bar": bar,
                          "y": e.y_root, "active": False}

    def row_motion(self, e):
        d = self._row_drag
        if not d:
            return
        if not d["active"] and abs(e.y_root - d["y"]) > 6:
            d["active"] = True
            self._tint_row(d["row"], d["bar"], DRAG_TINT)

    def row_release(self, e):
        d = self._row_drag
        self._row_drag = None
        if not d or not d["active"]:
            return                      # plain click — other bindings handle it
        # find which displayed task the pointer was released above
        target = None
        for row, task in self._rows:
            if task is d["task"]:
                continue
            mid = row.winfo_rooty() + row.winfo_height() / 2
            if e.y_root < mid:
                target = task
                break
        tasks = self.data["tasks"]
        tasks.remove(d["task"])
        if target is None:
            tasks.append(d["task"])     # dropped below everything -> last
        else:
            tasks.insert(tasks.index(target), d["task"])
        save_data(self.data)
        self.refresh()

    def _tint_row(self, row, bar, color):
        """Recolor a row (and children) while dragging; keep priority bar."""
        def walk(w):
            if w is bar:
                return
            try:
                w.configure(bg=color)
            except tk.TclError:
                pass
            for c in w.winfo_children():
                walk(c)
        walk(row)

    # ---------------- calendar ----------------
    def build_calendar(self):
        for w in self.cal_frame.winfo_children():
            w.destroy()
        if not self.data["settings"]["show_calendar"]:
            self.root.geometry("")      # let the window shrink to fit
            self.root.update_idletasks()
            return

        nav = tk.Frame(self.cal_frame, bg=self.bg)
        nav.pack(fill="x")

        def nbtn(text, delta):
            b = tk.Label(nav, text=text, font=self.f_cal_b, fg=DIM,
                         bg=self.bg, cursor="hand2", padx=6)
            b.bind("<Button-1>", lambda e: self.shift_month(delta))
            b.bind("<Enter>", lambda e: b.config(fg=TEXT))
            b.bind("<Leave>", lambda e: b.config(fg=DIM))
            return b

        nbtn("◀", -1).pack(side="left")
        y, m = self.cal_view.year, self.cal_view.month
        mon = tk.Label(nav, text=f"{TH_MONTHS[m - 1]} {y}",
                       font=self.f_cal_b, fg=TEXT, bg=self.bg,
                       cursor="hand2")
        mon.pack(side="left", expand=True)
        mon.bind("<Button-1>", lambda e: self.goto_today())  # click = today
        nbtn("▶", 1).pack(side="right")
        rf = tk.Label(nav, text="🔄", font=self.f_cal_b, fg=DIM,
                      bg=self.bg, cursor="hand2", padx=4)
        rf.pack(side="right")
        rf.bind("<Button-1>", lambda e: self.fetch_ical(notify=True))
        rf.bind("<Enter>", lambda e: rf.config(fg=TEXT))
        rf.bind("<Leave>", lambda e: rf.config(fg=DIM))

        grid = tk.Frame(self.cal_frame, bg=self.bg)
        grid.pack(pady=(4, 0))

        for c, wd in enumerate(TH_WEEKDAYS):
            hd_fg = WEEKEND_FG if c in (0, 6) else DIM
            tk.Label(grid, text=wd, font=self.f_small, fg=hd_fg, bg=self.bg,
                     width=3).grid(row=0, column=c, pady=(0, 2))

        today_iso = date.today().isoformat()
        weeks = calendar.Calendar(firstweekday=6).monthdayscalendar(y, m)
        for r, week in enumerate(weeks, start=1):
            for c, day in enumerate(week):
                if day == 0:
                    tk.Label(grid, text="", bg=self.bg,
                             width=3).grid(row=r, column=c)
                    continue
                iso = date(y, m, day).isoformat()
                has_ev = iso in self.events
                fg, bg, fnt = TEXT, self.bg, self.f_cal
                if c in (0, 6):                     # Sunday / Saturday
                    fg = WEEKEND_FG
                if has_ev:
                    fg, fnt = EVENT_FG, self.f_cal_b
                if iso == today_iso:
                    fg, bg = TODAY_FG, ACCENT
                cell = tk.Label(grid, text=str(day), font=fnt, fg=fg, bg=bg,
                                width=3, pady=1,
                                cursor="hand2" if has_ev else "")
                cell.grid(row=r, column=c, padx=1, pady=1)
                if has_ev:
                    cell.bind("<Button-1>",
                              lambda e, d=iso: self.show_day_events(e, d))
        tk.Frame(self.cal_frame, bg=DIVIDER, height=1).pack(
            fill="x", pady=(6, 6))
        self.root.geometry("")          # let the window shrink to fit
        self.root.update_idletasks()

    def shift_month(self, delta):
        self.cal_view = add_months(self.cal_view, delta).replace(day=1)
        self.build_calendar()

    def goto_today(self):
        t = date.today()
        self.cal_view = date(t.year, t.month, 1)
        self.build_calendar()

    def show_day_events(self, e, iso):
        self.close_day_popup()
        evs = self.events.get(iso, [])
        if not evs:
            return
        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.configure(bg=PANEL_BG, padx=10, pady=8)
        top.geometry(f"+{e.x_root + 8}+{e.y_root + 8}")
        d = datetime.strptime(iso, "%Y-%m-%d")
        tk.Label(top, text=d.strftime("%d/%m/%Y"), font=self.f_cal_b,
                 fg=ACCENT, bg=PANEL_BG, anchor="w").pack(fill="x")
        for ev in evs[:10]:
            tk.Label(top, text=f"• {ev}", font=self.f_note, fg=TEXT,
                     bg=PANEL_BG, anchor="w", justify="left",
                     wraplength=240).pack(fill="x")
        if len(evs) > 10:
            tk.Label(top, text=f"…และอีก {len(evs) - 10} รายการ",
                     font=self.f_small, fg=DIM, bg=PANEL_BG,
                     anchor="w").pack(fill="x")
        top.bind("<Button-1>", lambda ev: self.close_day_popup())
        top.bind("<Escape>", lambda ev: self.close_day_popup())
        top.after(8000, self.close_day_popup)   # auto-close after 8 s
        self._day_popup = top

    def close_day_popup(self):
        if self._day_popup is not None:
            try:
                self._day_popup.destroy()
            except tk.TclError:
                pass
            self._day_popup = None

    # ---------------- iCal fetching ----------------
    def schedule_fetch(self):
        """Background refresh loop (every FETCH_INTERVAL_MS)."""
        self.fetch_ical(notify=False)
        self.root.after(FETCH_INTERVAL_MS, self.schedule_fetch)

    def fetch_ical(self, notify=False):
        s = self.data["settings"]
        src = s.get("cal_source", "url")

        def done(msg, error=False):
            if not notify:
                return
            if error:
                messagebox.showerror(APP_NAME, msg)
            else:
                messagebox.showinfo(APP_NAME, msg)

        def work():
            try:
                today = date.today()
                ws = today - timedelta(days=120)
                we = today + timedelta(days=400)

                if src == "api":
                    cid = s.get("g_client_id", "").strip()
                    csec = s.get("g_client_secret", "").strip()
                    if not (cid and csec):
                        raise RuntimeError(
                            "ยังไม่ได้ใส่ Client ID / Client Secret ในตั้งค่า")
                    events = fetch_google_events(cid, csec, ws, we)
                else:
                    if src == "file":
                        path = s.get("ics_path", "").strip()
                        if not path:
                            raise RuntimeError("ยังไม่ได้เลือกไฟล์ .ics ในตั้งค่า")
                        with open(path, "r", encoding="utf-8",
                                  errors="replace") as f:
                            text = f.read()
                    else:                                  # src == "url"
                        url = s.get("ical_url", "").strip()
                        if not url:
                            raise RuntimeError(
                                "ยังไม่ได้ใส่ลิงก์ iCal ในตั้งค่า")
                        if url.startswith("webcal://"):
                            url = "https://" + url[len("webcal://"):]
                        req = urllib.request.Request(
                            url, headers={"User-Agent": APP_NAME})
                        with urllib.request.urlopen(req, timeout=20) as resp:
                            text = resp.read().decode("utf-8", "replace")
                    if "BEGIN:VCALENDAR" not in text:
                        raise RuntimeError(
                            "เนื้อหาที่ได้ไม่ใช่ไฟล์ปฏิทิน (.ics)\n"
                            "ตรวจสอบลิงก์/ไฟล์อีกครั้ง")
                    events = parse_ics(text, ws, we)

                save_event_cache(events)
                self.events = events
                self.root.after(0, self.build_calendar)
                n = len(events)
                self.root.after(0, lambda: done(
                    f"เชื่อมต่อสำเร็จ พบ event ใน {n} วัน "
                    f"(ช่วง ±1 ปีจากวันนี้)" if n else
                    "เชื่อมต่อสำเร็จ แต่ไม่พบ event ในช่วง ±1 ปีจากวันนี้"))
            except Exception as ex:
                self.root.after(0, lambda ex=ex: done(
                    f"ดึงข้อมูลไม่สำเร็จ:\n{type(ex).__name__}: {ex}",
                    error=True))
        threading.Thread(target=work, daemon=True).start()

    # ---------------- actions ----------------
    def cycle_status(self, task):
        i = STATUS_ORDER.index(task["status"])
        task["status"] = STATUS_ORDER[(i + 1) % len(STATUS_ORDER)]
        if task["status"] == "done":
            append_worklog(task)
        save_data(self.data)
        self.refresh()

    def set_status(self, task, status):
        was_done = task["status"] == "done"
        task["status"] = status
        if status == "done" and not was_done:
            append_worklog(task)
        save_data(self.data)
        self.refresh()

    def delete_task(self, task):
        if messagebox.askyesno(APP_NAME, f"ลบงาน \"{task['title']}\" ?"):
            self.data["tasks"].remove(task)
            save_data(self.data)
            self.refresh()

    def toggle_calendar(self):
        s = self.data["settings"]
        s["show_calendar"] = not s["show_calendar"]
        save_data(self.data)
        self.build_calendar()

    def quit(self):
        save_data(self.data)
        self.root.destroy()

    # ---------------- dragging ----------------
    def drag_start(self, e):
        self._drag_off = (e.x_root - self.root.winfo_x(),
                          e.y_root - self.root.winfo_y())

    def drag_move(self, e):
        if self._drag_off:
            x = e.x_root - self._drag_off[0]
            y = e.y_root - self._drag_off[1]
            self.root.geometry(f"+{x}+{y}")

    def drag_end(self, e):
        self._drag_off = None
        self.data["settings"]["pos"] = [self.root.winfo_x(),
                                        self.root.winfo_y()]
        save_data(self.data)

    # ---------------- task dialog ----------------
    def open_task_dialog(self, task):
        dlg = tk.Toplevel(self.root)
        dlg.title("เพิ่มงาน" if task is None else "แก้ไขงาน")
        dlg.configure(bg=PANEL_BG, padx=14, pady=12)
        dlg.attributes("-topmost", True)
        dlg.resizable(False, False)
        dlg.geometry(f"+{self.root.winfo_x() + 30}"
                     f"+{self.root.winfo_y() + 30}")
        dlg.grab_set()

        # dropdown lists of comboboxes follow the configured font too
        dlg.option_add("*TCombobox*Listbox.font", self.f_dialog)

        def lab(text, r):
            tk.Label(dlg, text=text, fg=TEXT, bg=PANEL_BG,
                     font=self.f_dialog).grid(row=r, column=0, sticky="w",
                                              pady=3, padx=(0, 8))

        lab("หัวข้อ *", 0)
        e_title = tk.Entry(dlg, width=32, font=self.f_dialog)
        e_title.grid(row=0, column=1, pady=3)

        lab("หมายเหตุ", 1)
        e_note = tk.Entry(dlg, width=32, font=self.f_dialog)
        e_note.grid(row=1, column=1, pady=3)

        lab("ความสำคัญ", 2)
        cb_pri = ttk.Combobox(dlg, state="readonly", width=12,
                              font=self.f_dialog,
                              values=[PRIORITY_LABEL[k] for k in PRIORITY_ORDER])
        cb_pri.grid(row=2, column=1, sticky="w", pady=3)

        lab("สถานะ", 3)
        cb_st = ttk.Combobox(dlg, state="readonly", width=12,
                             font=self.f_dialog,
                             values=[STATUS_LABEL[k] for k in STATUS_ORDER])
        cb_st.grid(row=3, column=1, sticky="w", pady=3)

        lab("วันเป้าหมาย", 4)
        e_due = tk.Entry(dlg, width=16, font=self.f_dialog)
        e_due.grid(row=4, column=1, sticky="w", pady=3)
        tk.Label(dlg, text="รูปแบบ YYYY-MM-DD (เว้นว่างได้)", fg=DIM,
                 bg=PANEL_BG, font=self.f_small).grid(row=5, column=1,
                                                      sticky="w")

        if task:
            e_title.insert(0, task["title"])
            e_note.insert(0, task.get("note", ""))
            cb_pri.set(PRIORITY_LABEL[task["priority"]])
            cb_st.set(STATUS_LABEL[task["status"]])
            if task.get("due"):
                e_due.insert(0, task["due"])
        else:
            cb_pri.set(PRIORITY_LABEL["medium"])
            cb_st.set(STATUS_LABEL["wait"])

        def ok():
            title = e_title.get().strip()
            if not title:
                messagebox.showwarning(APP_NAME, "กรุณาใส่หัวข้อ", parent=dlg)
                return
            try:
                due = parse_due(e_due.get())
            except ValueError:
                messagebox.showwarning(
                    APP_NAME, "วันเป้าหมายไม่ถูกต้อง ใช้รูปแบบ YYYY-MM-DD",
                    parent=dlg)
                return
            pri = PRIORITY_ORDER[
                [PRIORITY_LABEL[k] for k in PRIORITY_ORDER].index(cb_pri.get())]
            stt = STATUS_ORDER[
                [STATUS_LABEL[k] for k in STATUS_ORDER].index(cb_st.get())]
            if task is None:
                new_task = {
                    "id": uuid.uuid4().hex[:8],
                    "title": title, "note": e_note.get().strip(),
                    "priority": pri, "status": stt,
                    "created": date.today().isoformat(), "due": due,
                }
                self.data["tasks"].append(new_task)
                if stt == "done":
                    append_worklog(new_task)
            else:
                was_done = task["status"] == "done"
                task.update(title=title, note=e_note.get().strip(),
                            priority=pri, status=stt, due=due)
                if stt == "done" and not was_done:
                    append_worklog(task)
            save_data(self.data)
            dlg.destroy()
            self.refresh()

        btns = tk.Frame(dlg, bg=PANEL_BG)
        btns.grid(row=6, column=0, columnspan=2, pady=(10, 0))
        tk.Button(btns, text="บันทึก", width=10, command=ok).pack(
            side="left", padx=4)
        tk.Button(btns, text="ยกเลิก", width=10,
                  command=dlg.destroy).pack(side="left", padx=4)
        e_title.focus_set()
        dlg.bind("<Return>", lambda e: ok())
        dlg.bind("<Escape>", lambda e: dlg.destroy())

    # ---------------- settings dialog ----------------
    def open_settings(self):
        s = self.data["settings"]
        dlg = tk.Toplevel(self.root)
        dlg.title("ตั้งค่า")
        dlg.configure(bg=PANEL_BG, padx=14, pady=12)
        dlg.attributes("-topmost", True)
        dlg.resizable(False, False)
        dlg.geometry(f"+{self.root.winfo_x() + 30}"
                     f"+{self.root.winfo_y() + 30}")
        dlg.grab_set()

        # dropdown lists of comboboxes follow the configured font too
        dlg.option_add("*TCombobox*Listbox.font", self.f_dialog)

        def lab(text, r):
            tk.Label(dlg, text=text, fg=TEXT, bg=PANEL_BG,
                     font=self.f_dialog).grid(row=r, column=0, sticky="nw",
                                              pady=4, padx=(0, 8))

        def masked_entry(parent, value, width):
            """Entry that hides its content like a password, with 👁 toggle."""
            fr = tk.Frame(parent, bg=PANEL_BG)
            ent = tk.Entry(fr, width=width, font=self.f_dialog, show="•")
            ent.insert(0, value)
            ent.pack(side="left")
            eye = tk.Label(fr, text="👁", bg=PANEL_BG, fg=DIM,
                           cursor="hand2", padx=4)
            eye.pack(side="left")
            eye.bind("<Button-1>", lambda e: ent.config(
                show="" if ent.cget("show") else "•"))
            return fr, ent

        lab("ฟอนต์", 0)
        fams = sorted(set(tkfont.families()))
        cb_font = ttk.Combobox(dlg, values=fams, width=26,
                               font=self.f_dialog)
        cb_font.set(s["font_family"])
        cb_font.grid(row=0, column=1, sticky="w", pady=4)

        lab("ขนาดตัวอักษร", 1)
        sp_size = tk.Spinbox(dlg, from_=8, to=28, width=6,
                             font=self.f_dialog)
        sp_size.delete(0, "end")
        sp_size.insert(0, s["font_size"])
        sp_size.grid(row=1, column=1, sticky="w", pady=4)

        lab("ความทึบ (opacity)", 2)
        v_op = tk.DoubleVar(value=s["opacity"])
        tk.Scale(dlg, variable=v_op, from_=0.30, to=1.00, resolution=0.05,
                 orient="horizontal", length=200, bg=PANEL_BG, fg=TEXT,
                 highlightthickness=0).grid(row=2, column=1, sticky="w")

        # ---- calendar source ----
        lab("แหล่งปฏิทิน", 3)
        v_src = tk.StringVar(value=s.get("cal_source", "url"))
        src_fr = tk.Frame(dlg, bg=PANEL_BG)
        src_fr.grid(row=3, column=1, sticky="w")
        for val, txt in (("url", "ลิงก์ iCal"),
                         ("file", "ไฟล์ .ics"),
                         ("api", "Google API")):
            tk.Radiobutton(src_fr, text=txt, value=val, variable=v_src,
                           fg=TEXT, bg=PANEL_BG, selectcolor=ROW_BG,
                           activebackground=PANEL_BG,
                           activeforeground=TEXT,
                           font=self.f_dialog).pack(side="left", padx=(0, 8))

        lab("ลิงก์ Secret iCal", 4)
        url_fr, e_url = masked_entry(dlg, s.get("ical_url", ""), 38)
        url_fr.grid(row=4, column=1, sticky="w", pady=2)

        lab("ไฟล์ .ics", 5)
        file_fr = tk.Frame(dlg, bg=PANEL_BG)
        file_fr.grid(row=5, column=1, sticky="w", pady=2)
        e_path = tk.Entry(file_fr, width=32, font=self.f_dialog)
        e_path.insert(0, s.get("ics_path", ""))
        e_path.pack(side="left")

        def browse():
            p = filedialog.askopenfilename(
                parent=dlg, title="เลือกไฟล์ปฏิทิน",
                filetypes=[("iCalendar", "*.ics"), ("ทุกไฟล์", "*.*")])
            if p:
                e_path.delete(0, "end")
                e_path.insert(0, p)
                v_src.set("file")
        tk.Button(file_fr, text="เลือก…", command=browse).pack(
            side="left", padx=4)

        lab("Client ID", 6)
        e_cid = tk.Entry(dlg, width=40, font=self.f_dialog)
        e_cid.insert(0, s.get("g_client_id", ""))
        e_cid.grid(row=6, column=1, sticky="w", pady=2)

        lab("Client Secret", 7)
        sec_fr, e_csec = masked_entry(dlg, s.get("g_client_secret", ""), 38)
        sec_fr.grid(row=7, column=1, sticky="w", pady=2)

        def connect_google():
            cid = e_cid.get().strip()
            csec = e_csec.get().strip()
            if not (cid and csec):
                messagebox.showwarning(
                    APP_NAME, "ใส่ Client ID และ Client Secret ก่อนครับ",
                    parent=dlg)
                return
            s["g_client_id"], s["g_client_secret"] = cid, csec
            save_data(self.data)

            def work():
                try:
                    google_login(cid, csec)
                    self.root.after(0, lambda: messagebox.showinfo(
                        APP_NAME, "เชื่อมต่อบัญชี Google สำเร็จ\n"
                                  "เลือกแหล่งปฏิทินเป็น Google API "
                                  "แล้วกดบันทึกได้เลย", parent=dlg))
                except Exception as ex:
                    self.root.after(0, lambda ex=ex: messagebox.showerror(
                        APP_NAME, f"เชื่อมต่อไม่สำเร็จ:\n{ex}", parent=dlg))
            threading.Thread(target=work, daemon=True).start()
            messagebox.showinfo(
                APP_NAME, "กำลังเปิดเบราว์เซอร์เพื่อ login Google…\n"
                          "อนุญาตสิทธิ์แล้วรอข้อความยืนยันในแอพ",
                parent=dlg)

        gfr = tk.Frame(dlg, bg=PANEL_BG)
        gfr.grid(row=8, column=1, sticky="w", pady=(2, 4))
        tk.Button(gfr, text="เชื่อมต่อบัญชี Google",
                  command=connect_google).pack(side="left")
        tk.Label(gfr, text="(ใช้กับแหล่ง Google API เท่านั้น)",
                 fg=DIM, bg=PANEL_BG, font=self.f_small).pack(
                     side="left", padx=6)

        lab("ธีมสี", 9)
        v_theme = tk.StringVar(value=s.get("theme", "dark"))
        th_fr = tk.Frame(dlg, bg=PANEL_BG)
        th_fr.grid(row=9, column=1, sticky="w", pady=2)
        for val, txt in (("dark", "เข้ม (dark)"), ("light", "สว่าง (light)")):
            tk.Radiobutton(th_fr, text=txt, value=val, variable=v_theme,
                           fg=TEXT, bg=PANEL_BG, selectcolor=ROW_BG,
                           activebackground=PANEL_BG,
                           activeforeground=TEXT,
                           font=self.f_dialog).pack(side="left", padx=(0, 8))

        v_ghost = tk.BooleanVar(value=s["ghost_mode"])
        v_done = tk.BooleanVar(value=s["show_done"])
        v_due = tk.BooleanVar(value=s["show_due"])
        v_note = tk.BooleanVar(value=s["show_note"])
        v_cal = tk.BooleanVar(value=s["show_calendar"])
        v_top = tk.BooleanVar(value=s["topmost"])
        v_auto = tk.BooleanVar(value=s["autostart"])

        def chk(text, var, r):
            tk.Checkbutton(dlg, text=text, variable=var, fg=TEXT,
                           bg=PANEL_BG, selectcolor=ROW_BG,
                           activebackground=PANEL_BG, activeforeground=TEXT,
                           font=self.f_dialog).grid(row=r, column=0,
                                                    columnspan=2, sticky="w")

        chk("โหมดล่องหน (ตัวหนังสือลอยบน wallpaper ไม่มีแผง)", v_ghost, 10)
        chk("แสดงปฏิทิน", v_cal, 11)
        chk("แสดงงานที่เสร็จแล้ว", v_done, 12)
        chk("แสดงหมายเหตุ", v_note, 13)
        chk("แสดงวันเป้าหมาย", v_due, 14)
        chk("อยู่บนสุดเสมอ (topmost)", v_top, 15)
        chk("เปิดพร้อม Windows อัตโนมัติ", v_auto, 16)

        def ok():
            try:
                size = max(8, min(28, int(sp_size.get())))
            except ValueError:
                size = s["font_size"]
            if v_auto.get() != s["autostart"]:
                if set_autostart(v_auto.get()):
                    s["autostart"] = v_auto.get()
            src_changed = (
                v_src.get() != s.get("cal_source")
                or e_url.get().strip() != s.get("ical_url", "")
                or e_path.get().strip() != s.get("ics_path", ""))
            s.update(theme=v_theme.get(),
                     font_family=cb_font.get() or s["font_family"],
                     font_size=size, opacity=round(v_op.get(), 2),
                     ghost_mode=v_ghost.get(), show_done=v_done.get(),
                     show_due=v_due.get(), show_note=v_note.get(),
                     show_calendar=v_cal.get(), topmost=v_top.get(),
                     cal_source=v_src.get(),
                     ical_url=e_url.get().strip(),
                     ics_path=e_path.get().strip(),
                     g_client_id=e_cid.get().strip(),
                     g_client_secret=e_csec.get().strip())
            save_data(self.data)
            dlg.destroy()
            self.apply_appearance()
            self.build_ui()
            if src_changed:
                self.fetch_ical(notify=True)

        wl_fr = tk.Frame(dlg, bg=PANEL_BG)
        wl_fr.grid(row=17, column=0, columnspan=2, sticky="w", pady=(8, 0))
        tk.Button(wl_fr, text="🏆 ผลงานที่บันทึก…",
                  command=self.open_worklog).pack(side="left")
        tk.Label(wl_fr, text="(ประวัติงานที่ทำเสร็จ ใส่ดาว/impact ได้)",
                 fg=DIM, bg=PANEL_BG, font=self.f_small).pack(
                     side="left", padx=6)

        btns = tk.Frame(dlg, bg=PANEL_BG)
        btns.grid(row=18, column=0, columnspan=2, pady=(10, 0))
        tk.Button(btns, text="บันทึก", width=10, command=ok).pack(
            side="left", padx=4)
        tk.Button(btns, text="ยกเลิก", width=10,
                  command=dlg.destroy).pack(side="left", padx=4)
        dlg.bind("<Escape>", lambda e: dlg.destroy())

    # ---------------- worklog viewer / editor ----------------
    def open_worklog(self):
        log = load_worklog()

        dlg = tk.Toplevel(self.root)
        dlg.title("ผลงานที่บันทึก")
        dlg.configure(bg=PANEL_BG)
        dlg.attributes("-topmost", True)
        dlg.geometry(f"520x560+{self.root.winfo_x() + 30}"
                     f"+{self.root.winfo_y() + 30}")
        dlg.grab_set()

        header = tk.Frame(dlg, bg=PANEL_BG, padx=12, pady=8)
        header.pack(fill="x")
        tk.Label(header, text="🏆 ผลงานที่บันทึก", font=self.f_head,
                 fg=TEXT, bg=PANEL_BG).pack(side="left")
        tk.Label(header, text=f"{len(log)} รายการ", font=self.f_small,
                 fg=DIM, bg=PANEL_BG).pack(side="left", padx=8)

        # scrollable body (Canvas + inner frame + scrollbar + mousewheel)
        body = tk.Frame(dlg, bg=PANEL_BG)
        body.pack(fill="both", expand=True, padx=(12, 0))
        canvas = tk.Canvas(body, bg=PANEL_BG, highlightthickness=0)
        vsb = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas, bg=PANEL_BG)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(win, width=e.width))

        def on_wheel(e):
            canvas.yview_scroll(int(-e.delta / 120), "units")
        canvas.bind_all("<MouseWheel>", on_wheel)

        def unbind_wheel():
            try:
                canvas.unbind_all("<MouseWheel>")
            except tk.TclError:
                pass

        # newest first; group with a month header
        entries = sorted(
            log, key=lambda e: e.get("done_date", ""), reverse=True)
        editors = []           # [(entry, star_var, impact_entry, note_entry)]

        if not entries:
            tk.Label(inner, text="ยังไม่มีผลงานที่บันทึก\n"
                                  "งานจะถูกบันทึกอัตโนมัติเมื่อติ๊กเป็น "
                                  "\"ทำแล้ว\"",
                     font=self.f_dialog, fg=DIM, bg=PANEL_BG,
                     justify="left", pady=20).pack(anchor="w", padx=4)

        cur_month = None
        for e in entries:
            dd = e.get("done_date", "")
            month_key = dd[:7]
            if month_key != cur_month:
                cur_month = month_key
                label = month_key
                try:
                    y, mo = month_key.split("-")
                    label = f"{TH_MONTHS[int(mo) - 1]} {y}"
                except (ValueError, IndexError):
                    pass
                tk.Label(inner, text=label, font=self.f_cal_b, fg=ACCENT,
                         bg=PANEL_BG, anchor="w").pack(
                             fill="x", pady=(10, 2), padx=4)
                tk.Frame(inner, bg=DIVIDER, height=1).pack(
                    fill="x", padx=4)

            card = tk.Frame(inner, bg=ROW_BG, padx=8, pady=6)
            card.pack(fill="x", pady=3, padx=4)

            top = tk.Frame(card, bg=ROW_BG)
            top.pack(fill="x")
            star_var = tk.BooleanVar(value=bool(e.get("star")))
            star = tk.Label(top, font=self.f_icon, bg=ROW_BG,
                            cursor="hand2", width=2)

            def render_star(lbl=star, var=star_var):
                lbl.config(text="⭐" if var.get() else "☆",
                           fg="#f1c40f" if var.get() else DIM)
            render_star()
            star.pack(side="left")

            def toggle(ev, lbl=star, var=star_var):
                var.set(not var.get())
                render_star(lbl, var)
            star.bind("<Button-1>", toggle)

            pri = e.get("priority", "medium")
            tk.Frame(top, bg=PRIORITY_COLOR.get(pri, DIM),
                     width=4, height=18).pack(side="left", padx=(2, 6))
            tk.Label(top, text=e.get("title", "(ไม่มีชื่อ)"),
                     font=self.f_dialog, fg=TEXT, bg=ROW_BG, anchor="w",
                     justify="left", wraplength=360).pack(side="left")
            tk.Label(top, text=dd, font=self.f_small, fg=DIM,
                     bg=ROW_BG).pack(side="right")

            impact_fr = tk.Frame(card, bg=ROW_BG)
            impact_fr.pack(fill="x", pady=(4, 0))
            tk.Label(impact_fr, text="impact", font=self.f_small, fg=DIM,
                     bg=ROW_BG, width=6, anchor="w").pack(side="left")
            e_impact = tk.Entry(impact_fr, font=self.f_dialog)
            e_impact.insert(0, e.get("impact", ""))
            e_impact.pack(side="left", fill="x", expand=True)

            note_fr = tk.Frame(card, bg=ROW_BG)
            note_fr.pack(fill="x", pady=(3, 0))
            tk.Label(note_fr, text="note", font=self.f_small, fg=DIM,
                     bg=ROW_BG, width=6, anchor="w").pack(side="left")
            e_note = tk.Entry(note_fr, font=self.f_dialog)
            e_note.insert(0, e.get("note", ""))
            e_note.pack(side="left", fill="x", expand=True)

            editors.append((e, star_var, e_impact, e_note))

        def commit():
            for entry, sv, imp, nt in editors:
                entry["star"] = sv.get()
                entry["impact"] = imp.get().strip()
                entry["note"] = nt.get().strip()
            save_worklog(log)

        def save_close():
            commit()
            unbind_wheel()
            dlg.destroy()

        def cancel():
            unbind_wheel()
            dlg.destroy()

        btns = tk.Frame(dlg, bg=PANEL_BG, padx=12, pady=10)
        btns.pack(fill="x")
        tk.Button(btns, text="บันทึก", width=10, command=save_close).pack(
            side="left", padx=4)
        tk.Button(btns, text="ปิด", width=10, command=cancel).pack(
            side="left", padx=4)
        dlg.protocol("WM_DELETE_WINDOW", save_close)
        dlg.bind("<Escape>", lambda ev: cancel())

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    TodoOverlay().run()
