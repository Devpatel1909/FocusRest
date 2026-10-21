"""
FocusRest - Rest Reminder + Usage Insights for Windows 11
---------------------------------------------------------
1) Every 20 minutes -> notification + 20-second rest timer.
   When the 20 seconds are over -> notification (+ alarm sound if Beep is ON).
2) Tracks how long you use each application and, in browsers, each website.
   Idle time (no keyboard/mouse for 2 minutes) is not counted.
3) Dashboard with charts: hourly activity, top apps, weekly trend, top websites,
   categories, focus score, rests taken, longest session.
   All data stays on your PC, in the folder RestReminder inside %APPDATA%.
4) Beep sound toggle (default OFF) in the sidebar and in the Rest Timer settings.
5) Runs in the background: starts hidden in the system tray (near the clock). Click the
   tray icon to open the dashboard; the X button hides it again. Quit from the tray menu.
   Tray packages (pystray, pillow) are installed automatically on first run.

Run: double-click rest_reminder.pyw (needs Python 3 from python.org).
     Add  --show  to open the window at start-up instead of hiding it.
Optional, for exact website names:  python -m pip install uiautomation
"""
import base64
import ctypes
import datetime
import json
import os
import re
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk
from urllib.parse import urlparse

try:
    import winsound
except ImportError:  # not on Windows (only for testing)
    winsound = None

try:
    import uiautomation as auto  # optional: reads the browser address bar
except Exception:
    auto = None

DEFAULT_INTERVAL_MIN = 20
DEFAULT_REST_SEC = 20
IDLE_LIMIT_SEC = 120          # no input for this long -> not counted as usage
SAVE_EVERY_SEC = 30
KEEP_DAYS = 90
APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
DATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "RestReminder")
DATA_FILE = os.path.join(DATA_DIR, "usage.json")

# --------------------------------------------------------------------------
# Windows API helpers
# --------------------------------------------------------------------------
if os.name == "nt":
    from ctypes import wintypes as wt

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetForegroundWindow.restype = wt.HWND
    user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel32.OpenProcess.restype = wt.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    kernel32.GetTickCount.restype = wt.DWORD

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


def idle_seconds() -> float:
    if os.name != "nt":
        return 0.0
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return ((kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


def get_foreground():
    """Returns (hwnd, exe_name_lowercase, window_title) or None."""
    if os.name != "nt":
        return None
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    tbuf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, tbuf, 512)
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = ""
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if handle:
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wt.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                exe = os.path.basename(buf.value).lower()
        finally:
            kernel32.CloseHandle(handle)
    return hwnd, exe, tbuf.value


# --------------------------------------------------------------------------
# Naming helpers (pure functions)
# --------------------------------------------------------------------------
FRIENDLY = {
    "chrome.exe": "Google Chrome", "msedge.exe": "Microsoft Edge", "firefox.exe": "Firefox",
    "brave.exe": "Brave", "opera.exe": "Opera", "vivaldi.exe": "Vivaldi",
    "code.exe": "VS Code", "explorer.exe": "File Explorer", "winword.exe": "Word",
    "excel.exe": "Excel", "powerpnt.exe": "PowerPoint", "outlook.exe": "Outlook",
    "onenote.exe": "OneNote", "teams.exe": "Microsoft Teams", "ms-teams.exe": "Microsoft Teams",
    "whatsapp.exe": "WhatsApp", "telegram.exe": "Telegram", "spotify.exe": "Spotify",
    "discord.exe": "Discord", "slack.exe": "Slack", "zoom.exe": "Zoom",
    "notepad.exe": "Notepad", "calculator.exe": "Calculator", "calculatorapp.exe": "Calculator",
    "windowsterminal.exe": "Windows Terminal", "cmd.exe": "Command Prompt",
    "powershell.exe": "PowerShell", "vlc.exe": "VLC", "acrord32.exe": "Adobe Reader",
    "taskmgr.exe": "Task Manager", "systemsettings.exe": "Settings", "claude.exe": "Claude",
}
BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe"}
SKIP_EXES = {"lockapp.exe", "searchhost.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe"}

KNOWN_SITES = [
    ("youtube", "youtube.com"), ("github", "github.com"), ("gmail", "mail.google.com"),
    ("whatsapp", "web.whatsapp.com"), ("instagram", "instagram.com"), ("facebook", "facebook.com"),
    ("linkedin", "linkedin.com"), ("reddit", "reddit.com"), ("chatgpt", "chatgpt.com"),
    ("claude", "claude.ai"), ("netflix", "netflix.com"), ("stack overflow", "stackoverflow.com"),
    ("wikipedia", "wikipedia.org"), ("google search", "google.com"), ("google docs", "docs.google.com"),
    ("google sheets", "docs.google.com"), ("google drive", "drive.google.com"),
    ("amazon", "amazon.in"), ("flipkart", "flipkart.com"), ("telegram", "web.telegram.org"),
]


def app_name(exe: str, title: str) -> str:
    if exe == "applicationframehost.exe":      # Store / UWP apps: the title is the app name
        return title or "Windows app"
    if not exe:
        return title.split(" - ")[-1].strip() or "Unknown (admin app)"
    if exe in FRIENDLY:
        return FRIENDLY[exe]
    return exe[:-4].replace("_", " ").title() if exe.endswith(".exe") else exe


def clean_title(title: str) -> str:
    t = re.sub(
        r"\s+[-–—]\s+(?:(?:Personal|Work|School|Guest|Profile \d+|Person \d+)\s+[-–—]\s+)?"
        r"(?:Google Chrome|Microsoft.{0,2}Edge|Mozilla Firefox|Brave|Opera|Vivaldi)\s*$",
        "", title)
    t = re.sub(r"\s+and \d+ more pages?$", "", t)
    return t.strip()


def domain_from(text: str):
    """Turn an address-bar value into a domain; None if it does not look like a URL."""
    text = (text or "").strip()
    if not text or " " in text:
        return None
    low = text.lower()
    if low.startswith(("chrome://", "edge://", "brave://", "about:", "vivaldi://", "opera://")):
        return "Browser pages"
    if "://" not in low:
        low = "http://" + low
    try:
        host = urlparse(low).hostname or ""
    except ValueError:
        return None
    if host.startswith("www."):
        host = host[4:]
    if host == "localhost" or re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}", host):
        return host
    return None


def site_from_title(title: str) -> str:
    t = clean_title(title)
    low = title.lower()
    for key, site in KNOWN_SITES:
        if key in low:
            return site
    if not t:
        return "New tab / unknown"
    return (t[:45] + "...") if len(t) > 45 else t


def is_private(title: str) -> bool:
    low = title.lower()
    return "incognito" in low or "inprivate" in low or "private browsing" in low


def read_browser_url(hwnd):
    """Read the address bar through Windows UI Automation (needs `pip install uiautomation`)."""
    if auto is None:
        return None
    try:
        win = auto.ControlFromHandle(hwnd)
        for sub in ("Address and search bar", "enter address", "Address bar"):
            edit = win.EditControl(SubName=sub)
            if edit.Exists(0, 0):
                return edit.GetValuePattern().Value
    except Exception:
        return None
    return None


def fmt_dur(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {sec:02d}s"


def fmt_hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


# --------------------------------------------------------------------------
# Categories (used for the Focus score).  Simple keyword rules - edit freely.
# --------------------------------------------------------------------------
CATEGORIES = ["Work", "Communication", "Entertainment", "Other"]
CAT_RULES_APPS = {
    "Work": ("code", "studio", "pycharm", "idea", "terminal", "powershell", "command prompt", "word", "excel",
             "powerpoint", "outlook", "onenote", "notepad", "reader", "claude", "docker", "postman", "figma",
             "photoshop", "illustrator", "git", "sublime", "notion", "obsidian", "anaconda", "jupyter"),
    "Communication": ("whatsapp", "telegram", "discord", "slack", "teams", "zoom", "skype", "signal"),
    "Entertainment": ("spotify", "vlc", "netflix", "steam", "game", "media player", "prime video", "epic"),
}
CAT_RULES_SITES = {
    "Work": ("github", "gitlab", "stackoverflow", "stackexchange", "claude.ai", "chatgpt", "docs.google",
             "drive.google", "localhost", "notion", "figma", "wikipedia", "w3schools", "geeksforgeeks",
             "leetcode", "coursera", "udemy", "kaggle", "developer.", "docs.", "learn.microsoft", "python.org",
             "npmjs", "medium.com", "atlassian", "jira", "trello"),
    "Communication": ("mail.google", "outlook", "whatsapp", "telegram", "slack", "discord", "teams", "zoom",
                      "linkedin", "calendar.google"),
    "Entertainment": ("youtube", "netflix", "instagram", "facebook", "reddit", "x.com", "twitter", "tiktok",
                      "hotstar", "primevideo", "spotify", "twitch", "9gag", "pinterest", "snapchat"),
}


def categorize(name: str, rules: dict) -> str:
    low = name.lower()
    for cat in ("Work", "Communication", "Entertainment"):
        if any(k in low for k in rules[cat]):
            return cat
    return "Other"


def category_totals(snap: dict) -> dict:
    """Seconds per category. Browser time is split by website, other apps by app name."""
    out = {c: 0.0 for c in CATEGORIES}
    for app, sec in snap["apps"]:
        sites = snap["sites"].get(app)
        if sites:
            tracked = 0.0
            for site, s in sites:
                out[categorize(site, CAT_RULES_SITES)] += s
                tracked += s
            out["Other"] += max(0.0, sec - tracked)
        else:
            out[categorize(app, CAT_RULES_APPS)] += sec
    return out


# --------------------------------------------------------------------------
# Usage tracker (background thread)
# --------------------------------------------------------------------------
def blank_day() -> dict:
    return {"apps": {}, "bsites": {}, "hours": {}, "rests": 0, "longest": 0.0}


class UsageTracker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.paused = False
        self.streak = 0.0
        self._halt = threading.Event()
        self._site_cache = (None, 0.0, "")
        self.data = self._load()

    # storage
    def _load(self):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            cutoff = (datetime.date.today() - datetime.timedelta(days=KEEP_DAYS)).isoformat()
            return {d: v for d, v in data.items() if d >= cutoff}
        except Exception:
            return {}

    def save(self):
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with self.lock:
                payload = json.dumps(self.data, indent=1, ensure_ascii=False)
            tmp = DATA_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, DATA_FILE)
        except Exception:
            pass

    def stop(self):
        self._halt.set()

    # writes (lock must be held by caller of _day)
    def _day(self, day=None):
        day = day or datetime.date.today().isoformat()
        d = self.data.setdefault(day, blank_day())
        for k, v in blank_day().items():
            d.setdefault(k, v)
        return d

    def record(self, app: str, site, seconds: float):
        hour = str(datetime.datetime.now().hour)
        with self.lock:
            d = self._day()
            d["apps"][app] = d["apps"].get(app, 0.0) + seconds
            if site is not None:
                b = d["bsites"].setdefault(app, {})
                b[site] = b.get(site, 0.0) + seconds
            d["hours"][hour] = d["hours"].get(hour, 0.0) + seconds
            self.streak += seconds
            if self.streak > d["longest"]:
                d["longest"] = self.streak

    def add_rest(self):
        with self.lock:
            self._day()["rests"] += 1

    # reads (UI thread)
    def dates(self):
        today = datetime.date.today().isoformat()
        with self.lock:
            ds = set(self.data.keys())
        ds.add(today)
        return sorted(ds, reverse=True)

    def snapshot(self, day: str) -> dict:
        with self.lock:
            d = self.data.get(day) or blank_day()
            apps = dict(d.get("apps", {}))
            sites = {b: dict(s) for b, s in d.get("bsites", {}).items()}
            hours = dict(d.get("hours", {}))
            rests = int(d.get("rests", 0))
            longest = float(d.get("longest", 0.0))
        apps_l = sorted(apps.items(), key=lambda kv: kv[1], reverse=True)
        return {
            "apps": apps_l,
            "sites": {b: sorted(s.items(), key=lambda kv: kv[1], reverse=True) for b, s in sites.items()},
            "hours": [float(hours.get(str(h), 0.0)) for h in range(24)],
            "rests": rests,
            "longest": longest,
            "total": sum(s for _, s in apps_l),
        }

    def series(self, end_day: str, n: int = 7):
        """[(date, total_seconds, rests)] for the n days ending at end_day."""
        end = datetime.date.fromisoformat(end_day)
        out = []
        with self.lock:
            for i in range(n - 1, -1, -1):
                day = (end - datetime.timedelta(days=i)).isoformat()
                d = self.data.get(day) or {}
                out.append((day, sum(d.get("apps", {}).values()), int(d.get("rests", 0))))
        return out

    # sampling
    def _site_for(self, hwnd, title):
        key = (hwnd, title)
        cached_key, ts, site = self._site_cache
        if cached_key == key and time.monotonic() - ts < 15:
            return site
        if is_private(title):
            site = "Private browsing (not recorded)"
        else:
            site = domain_from(read_browser_url(hwnd)) or site_from_title(title)
        self._site_cache = (key, time.monotonic(), site)
        return site

    def _loop(self):
        last = time.monotonic()
        last_save = time.monotonic()
        while not self._halt.wait(1.0):
            now = time.monotonic()
            dt = now - last
            last = now
            if dt > 5:          # PC was asleep / thread stalled: don't credit that gap
                dt = 1.0
            try:
                if self.paused or idle_seconds() >= IDLE_LIMIT_SEC:
                    self.streak = 0.0
                else:
                    fg = get_foreground()
                    if fg:
                        hwnd, exe, title = fg
                        own = exe in ("python.exe", "pythonw.exe") and title.startswith(("FocusRest", "Take a rest"))
                        if exe not in SKIP_EXES and not own:
                            name = app_name(exe, title)
                            site = self._site_for(hwnd, title) if exe in BROWSERS else None
                            self.record(name, site, dt)
            except Exception:
                pass
            if now - last_save >= SAVE_EVERY_SEC:
                self.save()
                last_save = now
        self.save()

    def run(self):
        if auto is not None:
            try:
                with auto.UIAutomationInitializerInThread():
                    self._loop()
                return
            except Exception:
                pass
        self._loop()


# --------------------------------------------------------------------------
# Notifications / sounds
# --------------------------------------------------------------------------
def toast(title: str, message: str) -> None:
    """Show a Windows 11 toast notification (no extra packages needed)."""
    def esc(s):
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("'", "&apos;")

    ps = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml('<toast scenario="reminder"><visual><binding template="ToastGeneric"><text>{esc(title)}</text><text>{esc(message)}</text></binding></visual></toast>')
$n = [Windows.UI.Notifications.ToastNotification]::new($x)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{APP_ID}').Show($n)
"""
    encoded = base64.b64encode(ps.encode("utf-16le")).decode()
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-EncodedCommand", encoded],
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
    except Exception:
        pass


def play_alarm() -> None:
    """Loud-ish alarm, played in a background thread."""
    def run():
        if not winsound:
            return
        for _ in range(4):
            for freq in (880, 1175, 880, 1175):
                winsound.Beep(freq, 250)
            time.sleep(0.3)
    threading.Thread(target=run, daemon=True).start()


def short_beep() -> None:
    if winsound:
        threading.Thread(target=lambda: winsound.Beep(700, 300), daemon=True).start()


# --------------------------------------------------------------------------
# System tray (optional).  pystray + pillow are installed automatically on first run.
# --------------------------------------------------------------------------
import queue
import sys
import tkinter.font as tkfont

pystray = Image = ImageDraw = None


def _import_tray():
    global pystray, Image, ImageDraw
    import pystray as _p
    from PIL import Image as _i, ImageDraw as _d
    pystray, Image, ImageDraw = _p, _i, _d


try:
    _import_tray()
except Exception:
    pystray = None
    if os.name == "nt":
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "pystray", "pillow"],
                           creationflags=0x08000000, timeout=240,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _import_tray()
        except Exception:
            pystray = None

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_enabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, "FocusRest")
            return True
    except Exception:
        return False


def set_autostart(on: bool) -> None:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                exe = sys.executable
                if exe.lower().endswith("python.exe"):
                    exe = exe[:-10] + "pythonw.exe"
                winreg.SetValueEx(k, "FocusRest", 0, winreg.REG_SZ, f'"{exe}" "{os.path.abspath(__file__)}"')
            else:
                winreg.DeleteValue(k, "FocusRest")
    except Exception:
        pass


def make_tray_icon():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 62, 62), radius=16, fill=(166, 217, 77, 255))
    d.ellipse((17, 17, 47, 47), outline=(17, 17, 17, 255), width=5)
    d.ellipse((27, 27, 37, 37), fill=(17, 17, 17, 255))
    return img


# --------------------------------------------------------------------------
# UI toolkit: light theme (white rounded cards, lime-green accents, black pills)
# --------------------------------------------------------------------------
BG = "#ececec"        # page
SIDE = "#f7f7f7"      # sidebar
CARD = "#ffffff"
SOFT = "#f3f3f3"
TRACK = "#efefef"
BORDER = "#e0e0e0"
GRID = "#e6e6e6"
TEXT = "#111111"
MUTED = "#8a8a8a"
INK = "#111111"
GREEN = "#8cc63f"
LIME = "#a8d94f"
LIME_HI = "#bfe86d"
YELLOW = "#f1ea62"
ORANGE = "#fbb26b"
GRAYBAR = "#d9d9d9"
GOOD = "#4c9a2a"      # green text
WARN = "#d9822b"
BAD = "#dc4f3d"
CAT_COLORS = {"Work": GREEN, "Communication": YELLOW, "Entertainment": ORANGE, "Other": "#cfcfcf"}
TILE_COLORS = ["#d6efa4", "#fbd9b5", "#f6f1a6", "#e3e3e3", "#c9e6a0", "#fcc9a0"]
BAR_COLORS = [GREEN, YELLOW, ORANGE, "#c9c9c9", LIME, "#e8c36a"]
FONT = "Segoe UI" if os.name == "nt" else "Inter"
SC = 1.0
_FONTS = {}


def S(v) -> int:
    """Scale a pixel size for the current display DPI."""
    return int(round(v * SC))


def f(size, bold=False):
    return (FONT, size, "bold") if bold else (FONT, size)


def fnt(size, bold=False):
    key = (size, bold)
    if key not in _FONTS:
        _FONTS[key] = tkfont.Font(family=FONT, size=size, weight="bold" if bold else "normal")
    return _FONTS[key]


def lerp_color(c1, c2, t):
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def short(text, n):
    return text if len(text) <= n else text[:max(1, n - 1)] + "…"


def rrect(c, x1, y1, x2, y2, r, **kw):
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


def _inside(px, py, x1, y1, x2, y2, r):
    if px < x1 or px > x2 or py < y1 or py > y2:
        return False
    cx = x1 + r if px < x1 + r else (x2 - r if px > x2 - r else None)
    cy = y1 + r if py < y1 + r else (y2 - r if py > y2 - r else None)
    if cx is None or cy is None:
        return True
    return (px - cx) ** 2 + (py - cy) ** 2 <= r * r


def hatch(c, x1, y1, x2, y2, r, color, gap=7, tags=()):
    """Diagonal stripes clipped to a rounded rectangle (the textured fill from the reference design)."""
    w, h = x2 - x1, y2 - y1
    if w < 4 or h < 4:
        return
    r = max(0, min(r, w / 2, h / 2))
    off = gap / 2
    while off < w + h:
        run = []
        t = 0.0
        while t <= h:
            x, y = x1 + off - t, y1 + t
            if x < x1 - 1:
                break
            if _inside(x, y, x1, y1, x2, y2, r):
                run.append((x, y))
            elif run:
                if len(run) > 1:
                    c.create_line(*[v for p in run for v in p], fill=color, tags=tags)
                run = []
            t += 2
        if len(run) > 1:
            c.create_line(*[v for p in run for v in p], fill=color, tags=tags)
        off += gap


def icon(c, kind, cx, cy, s, color, tags=()):
    """Small outline icons drawn with canvas primitives (no icon fonts needed)."""
    u = s / 12.0
    lw = max(1.5, u * 1.5)
    kw = dict(fill="", outline=color, width=lw, tags=tags)
    lk = dict(fill=color, width=lw, capstyle="round", tags=tags)
    if kind == "dash":
        for dx in (-1, 1):
            for dy in (-1, 1):
                rrect(c, cx + dx * 5.5 * u - 4 * u, cy + dy * 5.5 * u - 4 * u,
                      cx + dx * 5.5 * u + 4 * u, cy + dy * 5.5 * u + 4 * u, 1.5 * u, **kw)
    elif kind == "apps":
        rrect(c, cx - 10 * u, cy - 8 * u, cx + 10 * u, cy + 8 * u, 2.5 * u, **kw)
        c.create_line(cx - 10 * u, cy - 3 * u, cx + 10 * u, cy - 3 * u, **lk)
        c.create_oval(cx - 7 * u, cy - 6.4 * u, cx - 6 * u, cy - 5.4 * u, fill=color, outline=color, tags=tags)
    elif kind == "globe":
        c.create_oval(cx - 9.5 * u, cy - 9.5 * u, cx + 9.5 * u, cy + 9.5 * u, **kw)
        c.create_oval(cx - 4 * u, cy - 9.5 * u, cx + 4 * u, cy + 9.5 * u, **kw)
        c.create_line(cx - 9.5 * u, cy, cx + 9.5 * u, cy, **lk)
    elif kind == "timer":
        c.create_oval(cx - 9 * u, cy - 8 * u, cx + 9 * u, cy + 10 * u, **kw)
        c.create_line(cx, cy + 1 * u, cx, cy - 4 * u, **lk)
        c.create_line(cx, cy + 1 * u, cx + 3.5 * u, cy + 3 * u, **lk)
        c.create_line(cx - 3 * u, cy - 11 * u, cx + 3 * u, cy - 11 * u, **lk)
    elif kind == "search":
        c.create_oval(cx - 8 * u, cy - 8 * u, cx + 4 * u, cy + 4 * u, **kw)
        c.create_line(cx + 2 * u, cy + 2 * u, cx + 9 * u, cy + 9 * u, **lk)
    elif kind == "gear":
        c.create_oval(cx - 6 * u, cy - 6 * u, cx + 6 * u, cy + 6 * u, **kw)
        c.create_oval(cx - 2.2 * u, cy - 2.2 * u, cx + 2.2 * u, cy + 2.2 * u, **kw)
        import math
        for i in range(8):
            a = i * math.pi / 4
            c.create_line(cx + 6 * u * math.cos(a), cy + 6 * u * math.sin(a),
                          cx + 9 * u * math.cos(a), cy + 9 * u * math.sin(a), **lk)
    elif kind in ("speaker", "speaker_off"):
        c.create_polygon(cx - 9 * u, cy - 3 * u, cx - 5 * u, cy - 3 * u, cx - 0.5 * u, cy - 7.5 * u,
                         cx - 0.5 * u, cy + 7.5 * u, cx - 5 * u, cy + 3 * u, cx - 9 * u, cy + 3 * u,
                         fill="", outline=color, width=lw, joinstyle="round", tags=tags)
        if kind == "speaker":
            c.create_arc(cx - 3 * u, cy - 5 * u, cx + 4 * u, cy + 5 * u, start=-55, extent=110, style="arc",
                         outline=color, width=lw, tags=tags)
            c.create_arc(cx - 4 * u, cy - 9 * u, cx + 9 * u, cy + 9 * u, start=-55, extent=110, style="arc",
                         outline=color, width=lw, tags=tags)
        else:
            c.create_line(cx + 3 * u, cy - 4 * u, cx + 9 * u, cy + 4 * u, **lk)
            c.create_line(cx + 9 * u, cy - 4 * u, cx + 3 * u, cy + 4 * u, **lk)
    elif kind == "pause":
        rrect(c, cx - 6 * u, cy - 7 * u, cx - 2 * u, cy + 7 * u, 1 * u, fill=color, outline="", tags=tags)
        rrect(c, cx + 2 * u, cy - 7 * u, cx + 6 * u, cy + 7 * u, 1 * u, fill=color, outline="", tags=tags)
    elif kind == "play":
        c.create_polygon(cx - 4 * u, cy - 7 * u, cx + 7 * u, cy, cx - 4 * u, cy + 7 * u, fill=color, outline=color,
                         width=lw, joinstyle="round", tags=tags)
    elif kind == "eye":
        c.create_polygon(cx - 10 * u, cy, cx - 4 * u, cy - 5.5 * u, cx + 4 * u, cy - 5.5 * u, cx + 10 * u, cy,
                         cx + 4 * u, cy + 5.5 * u, cx - 4 * u, cy + 5.5 * u, smooth=True, fill="", outline=color,
                         width=lw, tags=tags)
        c.create_oval(cx - 3 * u, cy - 3 * u, cx + 3 * u, cy + 3 * u, **kw)
    elif kind == "reset":
        c.create_arc(cx - 8 * u, cy - 8 * u, cx + 8 * u, cy + 8 * u, start=60, extent=270, style="arc",
                     outline=color, width=lw, tags=tags)
        c.create_polygon(cx + 4 * u, cy - 11 * u, cx + 4 * u, cy - 3 * u, cx + 11 * u, cy - 6 * u, fill=color,
                         outline=color, tags=tags)
    elif kind == "bolt":
        c.create_polygon(cx + 2 * u, cy - 10 * u, cx - 6 * u, cy + 2 * u, cx - 1 * u, cy + 2 * u, cx - 2 * u,
                         cy + 10 * u, cx + 6 * u, cy - 3 * u, cx + 1 * u, cy - 3 * u, fill=color, outline=color,
                         joinstyle="round", tags=tags)
    elif kind == "pencil":
        c.create_line(cx - 6 * u, cy + 6 * u, cx + 6 * u, cy - 6 * u, **lk)
        c.create_line(cx - 8 * u, cy + 8 * u, cx - 4 * u, cy + 7 * u, **lk)
        c.create_line(cx - 8 * u, cy + 8 * u, cx - 7 * u, cy + 4 * u, **lk)
    elif kind == "chev":
        c.create_line(cx - 4 * u, cy - 2 * u, cx, cy + 2 * u, cx + 4 * u, cy - 2 * u, **lk)
    elif kind == "chev_r":
        c.create_line(cx - 2 * u, cy - 4 * u, cx + 2 * u, cy, cx - 2 * u, cy + 4 * u, **lk)
    elif kind == "hide":
        c.create_line(cx + 1 * u, cy - 5 * u, cx - 4 * u, cy, cx + 1 * u, cy + 5 * u, **lk)
        c.create_line(cx + 7 * u, cy - 5 * u, cx + 2 * u, cy, cx + 7 * u, cy + 5 * u, **lk)
    elif kind == "download":
        c.create_line(cx, cy - 8 * u, cx, cy + 3 * u, **lk)
        c.create_line(cx - 4 * u, cy - 1 * u, cx, cy + 3 * u, cx + 4 * u, cy - 1 * u, **lk)
        c.create_line(cx - 8 * u, cy + 8 * u, cx + 8 * u, cy + 8 * u, **lk)
    elif kind == "up":
        c.create_line(cx - 4 * u, cy + 4 * u, cx + 4 * u, cy - 4 * u, **lk)
        c.create_line(cx - 1 * u, cy - 4 * u, cx + 4 * u, cy - 4 * u, cx + 4 * u, cy + 1 * u, **lk)
    elif kind == "down":
        c.create_line(cx - 4 * u, cy - 4 * u, cx + 4 * u, cy + 4 * u, **lk)
        c.create_line(cx - 1 * u, cy + 4 * u, cx + 4 * u, cy + 4 * u, cx + 4 * u, cy - 1 * u, **lk)


def setup_style(root):
    st = ttk.Style(root)
    try:
        st.theme_use("clam")
    except tk.TclError:
        pass
    root.configure(bg=BG)
    root.option_add("*TCombobox*Listbox.background", CARD)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", LIME)
    root.option_add("*TCombobox*Listbox.selectForeground", INK)
    root.option_add("*TCombobox*Listbox.font", f(10))
    st.configure(".", font=f(10), background=BG, foreground=TEXT)
    st.configure("TFrame", background=BG)
    st.configure("TCombobox", fieldbackground=CARD, background=CARD, foreground=TEXT, arrowcolor=INK,
                 bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD, padding=(S(10), S(6)), relief="flat")
    st.map("TCombobox", fieldbackground=[("readonly", CARD)], foreground=[("readonly", TEXT)],
           selectbackground=[("readonly", CARD)], selectforeground=[("readonly", TEXT)],
           background=[("active", SOFT)], bordercolor=[("focus", LIME)])
    st.configure("TSpinbox", fieldbackground=SOFT, background=SOFT, foreground=TEXT, arrowcolor=INK,
                 bordercolor=SOFT, lightcolor=SOFT, darkcolor=SOFT, insertcolor=TEXT, padding=(S(8), S(5)))


# --------------------------------------------------------------------------
# Basic widgets
# --------------------------------------------------------------------------
class RCard(tk.Canvas):
    """White rounded card.  Put content into `.inner`."""

    def __init__(self, parent, bg=BG, fill=CARD, radius=18, pad=18, min_w=0, min_h=0):
        super().__init__(parent, bg=bg, highlightthickness=0, width=S(min_w), height=S(min_h))
        self.fill, self.r, self.pad = fill, S(radius), S(pad)
        self.min_h, self.min_w = S(min_h), S(min_w)
        self.inner = tk.Frame(self, bg=fill)
        self._win = self.create_window(self.pad, self.pad, window=self.inner, anchor="nw")
        self.bind("<Configure>", self._resize)

    def _resize(self, e):
        self.delete("bg")
        rrect(self, 0, 0, e.width, e.height, self.r, fill=self.fill, outline="", tags="bg")
        self.tag_lower("bg")
        self.itemconfigure(self._win, width=max(1, e.width - 2 * self.pad), height=max(1, e.height - 2 * self.pad))

    def fit(self):
        self.update_idletasks()
        self.configure(height=max(self.min_h, self.inner.winfo_reqheight() + 2 * self.pad),
                       width=max(self.min_w, self.inner.winfo_reqwidth() + 2 * self.pad))


def card_header(parent, title, right_widget_factory=None, fill=CARD, size=11):
    head = tk.Frame(parent, bg=fill)
    head.pack(fill="x")
    tk.Label(head, text=title, bg=fill, fg=TEXT, font=f(size, True)).pack(side="left")
    w = None
    if right_widget_factory:
        w = right_widget_factory(head)
        w.pack(side="right")
    return head, w


class Pill(tk.Canvas):
    """Rounded button. style: black | light | soft | lime"""

    STYLES = {
        "black": (INK, "#2d2d2d", "white", INK),
        "light": (CARD, SOFT, TEXT, "#d6d6d6"),
        "soft": (SOFT, "#e6e6e6", TEXT, SOFT),
        "lime": (LIME, LIME_HI, INK, LIME),
    }

    def __init__(self, parent, text, command=None, style="black", icon_kind=None, bg=CARD, h=36, size=10,
                 pad=16, fixed_w=None, icon_after=None):
        self.style, self.command, self.icon_kind = style, command, icon_kind
        self.icon_after = icon_after
        self.text, self.size, self.pad_x, self.hh = text, size, S(pad), S(h)
        super().__init__(parent, bg=bg, highlightthickness=0, height=self.hh, cursor="hand2")
        self.fixed_w = fixed_w
        self._hover = False
        self._measure()
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<Button-1>", lambda e: self.command() if self.command else None)
        self.bind("<Configure>", lambda e: self._draw())

    def _measure(self):
        tw = fnt(self.size, True).measure(self.text) if self.text else 0
        iw = S(20) if self.icon_kind else 0
        gap = S(8) if (self.icon_kind and self.text) else 0
        aw = S(22) if self.icon_after else 0
        self._tw, self._iw, self._gap, self._aw = tw, iw, gap, aw
        w = self.fixed_w and S(self.fixed_w) or (tw + iw + gap + aw + 2 * self.pad_x)
        self.configure(width=w)

    def set_text(self, text, icon_kind=None):
        self.text = text
        if icon_kind is not None:
            self.icon_kind = icon_kind
        self._measure()
        self._draw()

    def _set_hover(self, on):
        self._hover = on
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4:
            return
        fill, hov, fg, outline = self.STYLES[self.style]
        rrect(self, 1, 1, w - 1, h - 1, h / 2, fill=hov if self._hover else fill, outline=outline, width=1)
        total = self._tw + self._iw + self._gap + self._aw
        x = (w - total) / 2
        if self.icon_kind:
            icon(self, self.icon_kind, x + self._iw / 2, h / 2, S(8.5), fg)
            x += self._iw + self._gap
        if self.text:
            self.create_text(x, h / 2, text=self.text, anchor="w", fill=fg, font=f(self.size, True))
        if self.icon_after:
            icon(self, self.icon_after, x + self._tw + S(14), h / 2 + 1, S(8), fg)


class CircleBtn(tk.Canvas):
    def __init__(self, parent, kind, command, bg=BG, size=42):
        super().__init__(parent, width=S(size), height=S(size), bg=bg, highlightthickness=0, cursor="hand2")
        self.kind, self.command, self.size, self._hover = kind, command, S(size), False
        self.bind("<Enter>", lambda e: self._h(True))
        self.bind("<Leave>", lambda e: self._h(False))
        self.bind("<Button-1>", lambda e: self.command())
        self._draw()

    def _h(self, on):
        self._hover = on
        self._draw()

    def set_kind(self, kind):
        self.kind = kind
        self._draw()

    def _draw(self):
        self.delete("all")
        s = self.size
        self.create_oval(1, 1, s - 1, s - 1, fill="#f5f5f5" if self._hover else CARD, outline=BORDER)
        icon(self, self.kind, s / 2, s / 2, S(8), INK)


class Seg(tk.Canvas):
    """Small segmented switch, e.g. 24h | 7d."""

    def __init__(self, parent, options, command, value=None, bg=CARD):
        super().__init__(parent, bg=bg, highlightthickness=0, height=S(32), cursor="hand2")
        self.options, self.command = options, command
        self.value = value or options[0]
        self.ws = [fnt(9, True).measure(o) + S(24) for o in options]
        self.configure(width=sum(self.ws) + S(8))
        self.bind("<Button-1>", self._click)
        self._draw()

    def _click(self, e):
        x = S(4)
        for opt, w in zip(self.options, self.ws):
            if x <= e.x < x + w:
                if opt != self.value:
                    self.value = opt
                    self._draw()
                    self.command(opt)
                return
            x += w

    def _draw(self):
        self.delete("all")
        h = S(32)
        rrect(self, 0, 0, sum(self.ws) + S(8), h, h / 2, fill=SOFT, outline="")
        x = S(4)
        for opt, w in zip(self.options, self.ws):
            on = opt == self.value
            if on:
                rrect(self, x, S(4), x + w, h - S(4), (h - S(8)) / 2, fill=INK, outline="")
            self.create_text(x + w / 2, h / 2, text=opt, fill="white" if on else MUTED, font=f(9, True))
            x += w


class Toggle(tk.Canvas):
    def __init__(self, parent, var, command=None, bg=CARD):
        super().__init__(parent, width=S(44), height=S(24), bg=bg, highlightthickness=0, cursor="hand2")
        self.var, self.command = var, command
        self.bind("<Button-1>", self._flip)
        var.trace_add("write", lambda *a: self._draw())
        self._draw()

    def _flip(self, e=None):
        self.var.set(not self.var.get())
        if self.command:
            self.command()

    def _draw(self):
        self.delete("all")
        w, h = S(44), S(24)
        on = self.var.get()
        rrect(self, 0, 0, w, h, h / 2, fill=GREEN if on else "#d4d4d4", outline="")
        k = h - S(8)
        x = w - S(4) - k if on else S(4)
        self.create_oval(x, S(4), x + k, S(4) + k, fill="white", outline="")


class NavItem(tk.Canvas):
    def __init__(self, parent, text, kind, command):
        super().__init__(parent, height=S(46), bg=SIDE, highlightthickness=0, cursor="hand2")
        self.text, self.kind, self.command = text, kind, command
        self.active = self.hover = False
        self.bind("<Button-1>", lambda e: command())
        self.bind("<Enter>", lambda e: self._h(True))
        self.bind("<Leave>", lambda e: self._h(False))
        self.bind("<Configure>", lambda e: self._draw())

    def _h(self, on):
        self.hover = on
        self._draw()

    def set_active(self, on):
        self.active = on
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10:
            return
        if self.active or self.hover:
            rrect(self, 0, 1, w, h - 1, S(14), fill=CARD if self.active else "#efefef",
                  outline=BORDER if self.active else "")
        col = INK if (self.active or self.hover) else "#555555"
        icon(self, self.kind, S(26), h / 2, S(8.5), col)
        self.create_text(S(48), h / 2, text=self.text, anchor="w", fill=col, font=f(10, self.active))


class StatBlock(tk.Frame):
    def __init__(self, parent, title, bg=BG):
        super().__init__(parent, bg=bg)
        tk.Label(self, text=title, bg=bg, fg=INK, font=f(10, True), anchor="w").pack(fill="x")
        self.value = tk.Label(self, text="-", bg=bg, fg=TEXT, font=f(19, True), anchor="w")
        self.value.pack(fill="x", pady=(S(2), 0))
        self.sub = tk.Label(self, text="", bg=bg, fg=MUTED, font=f(9), anchor="w")
        self.sub.pack(fill="x")

    def set(self, value, sub, color=MUTED):
        self.value.config(text=value)
        self.sub.config(text=sub, fg=color)


# --------------------------------------------------------------------------
# Canvas charts
# --------------------------------------------------------------------------
class Chart(tk.Canvas):
    """Canvas that redraws itself when resized or given new data."""

    def __init__(self, parent, height=220, bg=CARD):
        super().__init__(parent, bg=bg, highlightthickness=0, height=S(height), width=S(120))
        self.data = None
        self.empty_text = "No data yet"
        self.bind("<Configure>", lambda e: self.redraw())

    def set(self, *data):
        self.data = data
        self.redraw()

    def has_data(self) -> bool:
        return self.data is not None

    def redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 30 or h < 20:
            return
        if not self.has_data():
            self.create_text(w / 2, h / 2, text=self.empty_text, fill=MUTED, font=f(10))
            return
        self.paint(w, h)

    def paint(self, w, h):
        raise NotImplementedError


class BarChart(Chart):
    """Grey tracks with value bars; the selected / hovered bar turns lime with a tooltip."""

    def __init__(self, parent, height=200, bg=CARD):
        super().__init__(parent, height, bg)
        self.hover = None
        self._geo = None
        self.bind("<Motion>", self._move)
        self.bind("<Leave>", self._leave)

    def paint(self, w, h):
        labels, values, ymax, yticks, hl, tips = self.data
        L, R, T, B = S(38), S(2), S(10), S(26)
        pw, ph = w - L - R, h - T - B
        for v, lab in yticks:
            y = T + ph - ph * v / ymax
            self.create_line(L, y, w - R, y, fill=GRID, dash=(2, 4))
            self.create_text(L - S(8), y, text=lab, anchor="e", fill=MUTED, font=f(8))
        n = len(values)
        slot = pw / n
        bw = min(slot * 0.74, S(36))
        self._geo = (L, slot, n)
        sel = self.hover if self.hover is not None else hl
        tip_at = None
        for i, v in enumerate(values):
            x0 = L + slot * i + (slot - bw) / 2
            rrect(self, x0, T, x0 + bw, T + ph, min(S(7), bw / 2), fill=TRACK, outline="")
            bh = max(S(5), ph * min(v, ymax) / ymax) if v > 0 else 0
            if bh:
                y0 = T + ph - bh
                if i == sel:
                    rrect(self, x0, y0, x0 + bw, T + ph, min(S(7), bw / 2, bh / 2), fill=LIME, outline="")
                    hatch(self, x0, y0, x0 + bw, T + ph, min(S(7), bw / 2, bh / 2), LIME_HI, gap=S(6))
                else:
                    rrect(self, x0, y0, x0 + bw, T + ph, min(S(7), bw / 2, bh / 2), fill=GRAYBAR, outline="")
                if i == self.hover:
                    tip_at = (x0 + bw / 2, y0)
            if labels[i]:
                self.create_text(L + slot * i + slot / 2, h - B + S(14), text=labels[i], fill=MUTED, font=f(8))
        if tip_at and tips[self.hover]:
            self._tooltip(tips[self.hover], tip_at[0], tip_at[1], w, T)
        if not any(v > 0 for v in values):
            self.create_text(L + pw / 2, T + ph / 2, text="No activity recorded yet", fill=MUTED, font=f(10))

    def _tooltip(self, tip, x, ytop, w, T):
        title, rows = tip
        tf, lf, vf = fnt(8), fnt(9), fnt(9, True)
        pad, rowh = S(10), S(18)
        inner_w = max(tf.measure(title), max(S(14) + lf.measure(lab) + S(18) + vf.measure(val) for _, lab, val in rows))
        bw_, bh_ = inner_w + 2 * pad, pad * 2 + S(16) + rowh * len(rows)
        x0 = min(max(S(4), x - bw_ / 2), w - bw_ - S(4))
        y0 = max(T, ytop - bh_ - S(10))
        rrect(self, x0 + 1, y0 + 3, x0 + bw_ + 1, y0 + bh_ + 3, S(10), fill="#dcdcdc", outline="", tags="tip")
        rrect(self, x0, y0, x0 + bw_, y0 + bh_, S(10), fill=CARD, outline=BORDER, tags="tip")
        self.create_text(x0 + pad, y0 + pad, text=title, anchor="nw", fill=MUTED, font=f(8), tags="tip")
        y = y0 + pad + S(16) + rowh / 2
        for color, lab, val in rows:
            self.create_oval(x0 + pad, y - S(3), x0 + pad + S(7), y + S(4), fill=color, outline="", tags="tip")
            self.create_text(x0 + pad + S(14), y, text=lab, anchor="w", fill=TEXT, font=f(9), tags="tip")
            self.create_text(x0 + bw_ - pad, y, text=val, anchor="e", fill=TEXT, font=f(9, True), tags="tip")
            y += rowh

    def _move(self, e):
        if not self._geo or not self.data:
            return
        L, slot, n = self._geo
        i = int((e.x - L) // slot)
        i = i if 0 <= i < n and self.data[1][i] > 0 else None
        if i != self.hover:
            self.hover = i
            self.redraw()

    def _leave(self, e):
        if self.hover is not None:
            self.hover = None
            self.redraw()


class Ring(Chart):
    """Circular progress ring with centre text and round ends."""

    def __init__(self, parent, size=200, bg=CARD, thick=0.10, main_size=28, sub_size=10):
        super().__init__(parent, height=size, bg=bg)
        self.configure(width=S(size))
        self.thick, self.main_size, self.sub_size = thick, main_size, sub_size
        self.track = TRACK if bg == CARD else "#e2e2e2"

    def paint(self, w, h):
        import math
        frac, main, sub, color = self.data
        d = min(w, h) - S(8)
        thick = max(S(6), d * self.thick)
        cx, cy, r = w / 2, h / 2, d / 2 - thick / 2
        box = (cx - r, cy - r, cx + r, cy + r)
        self.create_oval(*box, outline=self.track, width=thick)
        frac = max(0.0, min(1.0, frac))
        if frac > 0.002:
            self.create_arc(*box, start=90, extent=-min(359.9, frac * 360), style="arc", outline=color, width=thick)
            for ang in (math.pi / 2, math.pi / 2 - frac * 2 * math.pi):
                px, py = cx + r * math.cos(ang), cy - r * math.sin(ang)
                self.create_oval(px - thick / 2, py - thick / 2, px + thick / 2, py + thick / 2, fill=color, outline="")
        if main:
            self.create_text(cx, cy - (S(self.sub_size) if sub else 0), text=main, fill=TEXT, font=f(self.main_size, True))
        if sub:
            self.create_text(cx, cy + S(self.main_size) * 0.75, text=sub, fill=MUTED, font=f(self.sub_size))


class Gauge(Chart):
    """Semi-circle meter made of small segments (focus score)."""

    def paint(self, w, h):
        frac, main, sub = self.data
        thick = max(S(14), min(w, h * 2) * 0.12)
        d = min(w - S(16), (h - S(6)) * 2)
        r = d / 2 - thick / 2
        cx, cy = w / 2, h - S(6)
        box = (cx - r, cy - r, cx + r, cy + r)
        n = 36
        step = 180.0 / n
        for i in range(n):
            t = (i + 0.5) / n
            on = t <= frac
            col = lerp_color("#d8e86a", GREEN, t) if on else "#ececec"
            self.create_arc(*box, start=180 - i * step - 0.5, extent=-(step - 1.6), style="arc", outline=col, width=thick)
        inner = r - thick / 2
        big = 24 if inner > S(70) else (18 if inner > S(52) else 14)
        self.create_text(cx, cy - inner * 0.42, text=main, fill=TEXT, font=f(big, True))
        if inner > S(52):
            self.create_text(cx, cy - inner * 0.42 + S(big) * 1.3, text=sub, fill=MUTED, font=f(8))


class ProgressBar(Chart):
    """Tall pill progress bar with the hatched lime fill."""

    def paint(self, w, h):
        frac, color = self.data
        bh = min(h - 2, S(26))
        y0 = (h - bh) / 2
        rrect(self, 0, y0, w, y0 + bh, bh / 2, fill=TRACK, outline="")
        fw = max(bh, (w) * max(0.0, min(1.0, frac))) if frac > 0 else 0
        if fw:
            rrect(self, 0, y0, fw, y0 + bh, bh / 2, fill=color, outline="")
            hatch(self, 0, y0, fw, y0 + bh, bh / 2, lerp_color(color, "#ffffff", 0.35), gap=S(6))


class SegBar(Chart):
    """Segmented horizontal bar (category split)."""

    def paint(self, w, h):
        segs = self.data[0]
        total = sum(fr for _, fr in segs) or 1
        gap = S(3)
        bh = min(h - 2, S(34))
        y0 = (h - bh) / 2
        avail = w - gap * (len(segs) - 1)
        x = 0
        for color, fr in segs:
            sw = max(S(8), avail * fr / total)
            rrect(self, x, y0, x + sw, y0 + bh, min(S(9), sw / 2), fill=color, outline="")
            hatch(self, x, y0, x + sw, y0 + bh, min(S(9), sw / 2), lerp_color(color, "#ffffff", 0.45), gap=S(6))
            x += sw + gap


def tile(c, x, y, size, color, letter, radius=None):
    rrect(c, x, y, x + size, y + size, radius or size * 0.28, fill=color, outline="")
    c.create_text(x + size / 2, y + size / 2, text=(letter or "?")[:1].upper(), fill=INK, font=f(int(size / S(1) * 0.38), True))


def tile_color(name):
    return TILE_COLORS[sum(ord(ch) for ch in name) % len(TILE_COLORS)]


class RankList(Chart):
    """Goal-tracker style rows: tile, name, value, thin progress bar, caption."""

    def has_data(self):
        return bool(self.data and self.data[0])

    def paint(self, w, h):
        rows = self.data[0]
        rowh = max(S(52), min(S(64), (h - S(2)) / max(1, min(len(rows), 4))))
        fit = max(1, int((h - S(2)) // rowh))
        y = S(2)
        for label, right, caption, frac, color in rows[:fit]:
            ts = S(38)
            tile(self, 0, y + S(4), ts, tile_color(label), label)
            x0 = ts + S(12)
            maxc = max(8, int((w - x0 - S(80)) / S(7.5)))
            self.create_text(x0, y + S(10), text=short(label, maxc), anchor="w", fill=TEXT, font=f(10, True))
            self.create_text(w, y + S(10), text=right, anchor="e", fill=TEXT, font=f(9, True))
            by = y + S(23)
            rrect(self, x0, by, w, by + S(10), S(5), fill=TRACK, outline="")
            bw = max(S(10), (w - x0) * max(0.0, min(1.0, frac)))
            rrect(self, x0, by, x0 + bw, by + S(10), S(5), fill=color, outline="")
            self.create_text(x0, by + S(20), text=caption, anchor="w", fill=MUTED, font=f(8))
            y += rowh


class AppList(Chart):
    """Transaction-history style list: tile, name, caption, time, share."""

    def has_data(self):
        return bool(self.data and self.data[0])

    def paint(self, w, h):
        rows = self.data[0]
        self.create_text(0, S(8), text="Application", anchor="w", fill=MUTED, font=f(8))
        self.create_text(w, S(8), text="Time", anchor="e", fill=MUTED, font=f(8))
        top = S(24)
        rowh = max(S(46), min(S(54), (h - top) / max(1, min(len(rows), 8))))
        fit = max(1, int((h - top) // rowh))
        y = top
        for name, cap, right, status, scolor in rows[:fit]:
            ts = S(34)
            tile(self, 0, y + (rowh - ts) / 2, ts, tile_color(name), name)
            x0 = ts + S(12)
            maxc = max(6, int((w - x0 - S(70)) / S(7.5)))
            self.create_text(x0, y + rowh / 2 - S(8), text=short(name, maxc), anchor="w", fill=TEXT, font=f(10, True))
            self.create_text(x0, y + rowh / 2 + S(9), text=cap, anchor="w", fill=MUTED, font=f(8))
            self.create_text(w, y + rowh / 2 - S(8), text=right, anchor="e", fill=TEXT, font=f(10, True))
            self.create_text(w, y + rowh / 2 + S(9), text=status, anchor="e", fill=scolor, font=f(8))
            y += rowh


class AvatarRow(Chart):
    """Round letter avatars with names underneath; click one to open it in Applications."""

    def __init__(self, parent, height=84, bg=CARD):
        super().__init__(parent, height, bg)
        self.on_pick = lambda name: None
        self._hit = []
        self.bind("<Button-1>", self._click)

    def has_data(self):
        return bool(self.data and self.data[0])

    def paint(self, w, h):
        items = self.data[0]
        n = max(1, min(len(items), max(1, int(w // S(46)))))
        slot = w / n
        d = min(S(46), slot - S(8))
        self._hit = []
        for i, (name, color) in enumerate(items[:n]):
            cx = slot * i + slot / 2
            self.create_oval(cx - d / 2, S(2), cx + d / 2, S(2) + d, fill=color, outline="")
            self.create_text(cx, S(2) + d / 2, text=name[:1].upper(), fill=INK, font=f(13, True))
            self.create_text(cx, S(2) + d + S(12), text=short(name.split()[0], 8), fill=MUTED, font=f(8))
            self._hit.append((slot * i, slot * (i + 1), name))

    def _click(self, e):
        for x0, x1, name in self._hit:
            if x0 <= e.x < x1:
                self.on_pick(name)
                return


class QAction(tk.Frame):
    """Rounded square quick-action button with a caption underneath."""

    def __init__(self, parent, kind, text, command, bg=CARD):
        super().__init__(parent, bg=bg)
        self.kind, self.command, self._hover = kind, command, False
        self.cv = tk.Canvas(self, width=S(50), height=S(50), bg=bg, highlightthickness=0, cursor="hand2")
        self.cv.pack()
        self.lbl = tk.Label(self, text=text, bg=bg, fg=MUTED, font=f(8))
        self.lbl.pack(pady=(S(4), 0))
        self.cv.bind("<Enter>", lambda e: self._h(True))
        self.cv.bind("<Leave>", lambda e: self._h(False))
        self.cv.bind("<Button-1>", lambda e: self.command())
        self._draw()

    def _h(self, on):
        self._hover = on
        self._draw()

    def set(self, kind=None, text=None):
        if kind:
            self.kind = kind
        if text is not None:
            self.lbl.config(text=text)
        self._draw()

    def _draw(self):
        self.cv.delete("all")
        s = S(50)
        rrect(self.cv, 1, 1, s - 1, s - 1, S(14), fill=SOFT if self._hover else CARD, outline=BORDER)
        icon(self.cv, self.kind, s / 2, s / 2, S(8.5), INK)


class TimerCard(tk.Canvas):
    """The big lime 'card' with the countdown (like the bank card in the reference)."""

    def __init__(self, parent, bg=CARD):
        super().__init__(parent, bg=bg, highlightthickness=0, height=S(124), width=S(240))
        self.state = None
        self._static_key = None
        self.bind("<Configure>", lambda e: self._draw())

    def set(self, chip, time_text, frac, left, right, color=LIME):
        self.state = (chip, time_text, frac, left, right, color)
        self._draw()

    def _draw(self):
        w, h = self.winfo_width(), self.winfo_height()
        if w < 40 or not self.state:
            return
        chip, time_text, frac, left, right, color = self.state
        key = (w, h, color)
        if key != self._static_key:
            self._static_key = key
            self.delete("static")
            rrect(self, 0, 0, w, h, S(16), fill=color, outline="", tags="static")
            hatch(self, 0, 0, w, h, S(16), lerp_color(color, "#ffffff", 0.28), gap=S(8), tags="static")
            self.tag_lower("static")
        self.delete("dyn")
        pad = S(16)
        self.create_text(pad, S(20), text=left[0], anchor="w", fill="#2b3a10", font=f(9), tags="dyn")
        cw = fnt(8, True).measure(chip) + S(18)
        rrect(self, w - pad - cw, S(10), w - pad, S(30), S(10), fill=INK, outline="", tags="dyn")
        self.create_text(w - pad - cw / 2, S(20), text=chip, fill="white", font=f(8, True), tags="dyn")
        self.create_text(pad, h / 2 + S(2), text=time_text, anchor="w", fill=INK, font=f(30, True), tags="dyn")
        by = h - S(40)
        rrect(self, pad, by, w - pad, by + S(6), S(3), fill=lerp_color(color, "#000000", 0.18), outline="", tags="dyn")
        if frac > 0:
            rrect(self, pad, by, pad + max(S(6), (w - 2 * pad) * frac), by + S(6), S(3), fill=INK, outline="", tags="dyn")
        self.create_text(pad, h - S(18), text=left[1], anchor="w", fill="#2b3a10", font=f(8), tags="dyn")
        self.create_text(w - pad, h - S(18), text=right, anchor="e", fill="#2b3a10", font=f(8), tags="dyn")


class TableList(Chart):
    """Scrollable ranked table with expandable rows (replaces the old grey tree view)."""

    ROW, KID, HEAD = 48, 38, 34

    def __init__(self, parent, cols, bar_x, empty="No data yet", bg=CARD):
        super().__init__(parent, 300, bg)
        self.cols, self.bar_x = cols, bar_x          # cols: [(header, x fraction)], bar_x: fraction
        self.empty_text = empty
        self.rows, self.open, self.off, self.hover = [], {}, 0, None
        self._vis = []
        self.bind("<MouseWheel>", lambda e: self._wheel(-e.delta / 120 * S(40)))
        self.bind("<Button-4>", lambda e: self._wheel(-S(40)))
        self.bind("<Button-5>", lambda e: self._wheel(S(40)))
        self.bind("<Motion>", self._move)
        self.bind("<Leave>", lambda e: self._set_hover(None))
        self.bind("<Button-1>", self._click)

    def set_rows(self, rows):
        """rows: dict(name, kid, cols[list], frac, color, has_kids)"""
        self.rows = rows
        self.data = True if rows else None
        self.redraw()

    def _layout(self):
        vis, y = [], 0
        parent_open = True
        for r in self.rows:
            if r["kid"] and not parent_open:
                continue
            if not r["kid"]:
                parent_open = self.open.get(r["name"], True)
            hh = S(self.KID if r["kid"] else self.ROW)
            vis.append((y, hh, r))
            y += hh
        self._vis, self._total = vis, y

    def _wheel(self, d):
        h = self.winfo_height() - S(self.HEAD)
        self.off = max(0, min(max(0, self._total - h), self.off + d))
        self.redraw()

    def _row_at(self, y):
        yy = y - S(self.HEAD) + self.off
        for i, (y0, hh, r) in enumerate(self._vis):
            if y0 <= yy < y0 + hh:
                return i
        return None

    def _move(self, e):
        self._set_hover(self._row_at(e.y) if e.y > S(self.HEAD) else None)

    def _set_hover(self, i):
        if i != self.hover:
            self.hover = i
            self.redraw()

    def _click(self, e):
        i = self._row_at(e.y)
        if i is not None:
            r = self._vis[i][2]
            if r.get("has_kids"):
                self.open[r["name"]] = not self.open.get(r["name"], True)
                self.redraw()

    def paint(self, w, h):
        self._layout()
        head = S(self.HEAD)
        vh = h - head
        self.off = max(0, min(max(0, self._total - vh), self.off))
        for i, (y0, hh, r) in enumerate(self._vis):
            y = head + y0 - self.off
            if y + hh < head or y > h:
                continue
            if i == self.hover:
                rrect(self, 0, y + S(2), w - S(8), y + hh - S(2), S(10), fill=SOFT, outline="")
            kid = r["kid"]
            x = S(40) if kid else S(8)
            if r.get("has_kids"):
                icon(self, "chev" if self.open.get(r["name"], True) else "chev_r", S(14), y + hh / 2, S(6), MUTED)
                x = S(30)
            if kid:
                self.create_oval(x, y + hh / 2 - S(3), x + S(6), y + hh / 2 + S(3), fill=r["color"], outline="")
                nx = x + S(16)
            else:
                ts = S(30)
                tile(self, x, y + (hh - ts) / 2, ts, tile_color(r["name"]), r["name"])
                nx = x + ts + S(12)
            maxc = max(8, int((w * self.cols[0][1] - nx) / S(7.2)))
            self.create_text(nx, y + hh / 2, text=short(r["name"], maxc), anchor="w",
                             fill=MUTED if kid else TEXT, font=f(9 if kid else 10, not kid))
            for (hd, xf), val in zip(self.cols, r["cols"]):
                self.create_text(w * xf, y + hh / 2, text=val, anchor="w", fill=MUTED if kid else TEXT,
                                 font=f(9, False if kid else True))
            bx0, bx1 = w * self.bar_x, w - S(28)
            if bx1 - bx0 > S(30):
                rrect(self, bx0, y + hh / 2 - S(4), bx1, y + hh / 2 + S(4), S(4), fill=TRACK, outline="")
                bw = max(S(8), (bx1 - bx0) * max(0.0, min(1.0, r["frac"])))
                rrect(self, bx0, y + hh / 2 - S(4), bx0 + bw, y + hh / 2 + S(4), S(4), fill=r["color"], outline="")
        self.create_rectangle(0, 0, w, head, fill=self["bg"], outline="")
        self.create_text(S(8), head / 2, text=self.first_header, anchor="w", fill=MUTED, font=f(8, True))
        for hd, xf in self.cols:
            self.create_text(w * xf, head / 2, text=hd, anchor="w", fill=MUTED, font=f(8, True))
        self.create_text(w * self.bar_x, head / 2, text="Usage", anchor="w", fill=MUTED, font=f(8, True))
        self.create_line(0, head - 1, w, head - 1, fill=GRID)
        if self._total > vh:
            th = max(S(24), vh * vh / self._total)
            ty = head + (vh - th) * self.off / max(1, self._total - vh)
            rrect(self, w - S(5), ty, w - S(1), ty + th, S(2), fill="#d2d2d2", outline="")

    first_header = ""


class DropPill(Pill):
    """Pill that shows the current value and opens a small menu to pick another one."""

    def __init__(self, parent, var, values_fn, on_change=None, bg=BG, h=44, style="light", size=10):
        self.var, self.values_fn, self.on_change = var, values_fn, on_change
        super().__init__(parent, var.get(), self._open, style, bg=bg, h=h, size=size, icon_after="chev")
        var.trace_add("write", lambda *a: self.set_text(self.var.get()))

    def _open(self):
        menu = tk.Menu(self, tearoff=0, font=f(10), bg=CARD, fg=TEXT, activebackground=LIME, activeforeground=INK,
                       relief="flat", bd=1)
        for v in self.values_fn():
            menu.add_command(label=v, command=lambda v=v: self._pick(v))
        try:
            menu.tk_popup(self.winfo_rootx(), self.winfo_rooty() + self.winfo_height() + S(4))
        finally:
            menu.grab_release()

    def _pick(self, v):
        self.var.set(v)
        if self.on_change:
            self.on_change()


class LegendList(Chart):
    """Colour swatch + name + value rows."""

    def has_data(self):
        return bool(self.data and self.data[0])

    def paint(self, w, h):
        rows = self.data[0]
        rowh = max(S(24), min(S(32), (h - S(2)) / max(1, len(rows))))
        y = rowh / 2 + S(1)
        for color, name, right in rows:
            rrect(self, 0, y - S(6), S(12), y + S(6), S(4), fill=color, outline="")
            self.create_text(S(22), y, text=name, anchor="w", fill=TEXT, font=f(10))
            self.create_text(w, y, text=right, anchor="e", fill=MUTED, font=f(10))
            y += rowh


class PixelDecor(tk.Canvas):
    """Little tile pattern used on the tips card."""

    PATTERN = ["..g.o", ".gg.o", "gyg..", ".o.gg", "o..yg"]

    def __init__(self, parent, bg=CARD):
        super().__init__(parent, bg=bg, highlightthickness=0, width=S(108), height=S(90))
        cols = {"g": GREEN, "o": ORANGE, "y": YELLOW}
        s, gap = S(17), S(3)
        for r, row in enumerate(self.PATTERN):
            for c_, ch in enumerate(row):
                if ch in cols:
                    x, y = c_ * (s + gap), r * (s + gap) - S(6)
                    rrect(self, x, y, x + s, y + s, S(4), fill=cols[ch], outline="")
                    hatch(self, x, y, x + s, y + s, S(4), lerp_color(cols[ch], "#ffffff", 0.4), gap=S(5))


class SearchEntry(tk.Frame):
    def __init__(self, parent, var=None, placeholder="", width_px=300, on_enter=None, bg=BG, fill=CARD, h=44):
        super().__init__(parent, bg=bg)
        hh = S(h)
        self.var = var if var is not None else tk.StringVar()
        self.cv = tk.Canvas(self, width=S(width_px), height=hh, bg=bg, highlightthickness=0)
        self.cv.pack()
        rrect(self.cv, 1, 1, S(width_px) - 1, hh - 1, hh / 2, fill=fill, outline=BORDER if fill == CARD else fill)
        icon(self.cv, "search", S(24), hh / 2, S(7.5), MUTED)
        self.entry = tk.Entry(self.cv, textvariable=self.var, bd=0, relief="flat", bg=fill, fg=TEXT,
                              insertbackground=TEXT, font=f(10), highlightthickness=0)
        self.cv.create_window(S(42), hh / 2, window=self.entry, anchor="w", width=S(width_px) - S(60))
        self.phl = tk.Label(self.cv, text=placeholder, bg=fill, fg=MUTED, font=f(10), cursor="xterm")
        self._phw = self.cv.create_window(S(42), hh / 2, window=self.phl, anchor="w")
        self.phl.bind("<Button-1>", lambda e: self.entry.focus_set())
        self._focused = False
        self.entry.bind("<FocusIn>", lambda e: self._focus(True))
        self.entry.bind("<FocusOut>", lambda e: self._focus(False))
        self.var.trace_add("write", lambda *a: self._upd())
        if on_enter:
            self.entry.bind("<Return>", lambda e: on_enter(self.text()))
        self._upd()

    def _focus(self, on):
        self._focused = on
        self._upd()

    def _upd(self):
        hide = bool(self.var.get()) or self._focused
        self.cv.itemconfigure(self._phw, state="hidden" if hide else "normal")

    def text(self):
        return self.var.get().strip()


class UserChip(tk.Canvas):
    def __init__(self, parent, name, sub, bg=BG):
        self.name, self.sub = name, sub
        w = max(fnt(10, True).measure(name), fnt(8).measure(sub)) + S(70)
        super().__init__(parent, width=w, height=S(46), bg=bg, highlightthickness=0)
        h = S(46)
        rrect(self, 1, 1, w - 1, h - 1, h / 2, fill=CARD, outline=BORDER)
        self.create_oval(S(6), S(6), h - S(6), h - S(6), fill=LIME, outline="")
        self.create_text(h / 2, h / 2, text=name[:1].upper(), fill=INK, font=f(12, True))
        self.create_text(h + S(4), h / 2 - S(8), text=name, anchor="w", fill=TEXT, font=f(10, True))
        self.create_text(h + S(4), h / 2 + S(8), text=sub, anchor="w", fill=MUTED, font=f(8))


# --------------------------------------------------------------------------
# Table pages (Applications / Websites)
# --------------------------------------------------------------------------
CAT_TEXT = {"Work": GOOD, "Communication": "#a88f00", "Entertainment": WARN, "Other": MUTED}


class TablePage(tk.Frame):
    def __init__(self, parent, title, cols, first_header, bar_x, query_var, placeholder="Filter"):
        super().__init__(parent, bg=BG)
        self.card = RCard(self, min_h=300, pad=22)
        self.card.pack(fill="both", expand=True)
        inner = self.card.inner
        head = tk.Frame(inner, bg=CARD)
        head.pack(fill="x")
        tk.Label(head, text=title, bg=CARD, fg=TEXT, font=f(16, True)).pack(side="left")
        self.total = tk.Label(head, text="", bg=CARD, fg=MUTED, font=f(10))
        self.total.pack(side="right")
        bar_ = tk.Frame(inner, bg=CARD)
        bar_.pack(fill="x", pady=(S(14), S(10)))
        SearchEntry(bar_, var=query_var, placeholder=placeholder, width_px=300, bg=CARD, fill=SOFT, h=40).pack(side="left")
        self.extra = tk.Frame(bar_, bg=CARD)
        self.extra.pack(side="left", padx=S(14))
        self.table = TableList(inner, cols, bar_x)
        self.table.first_header = first_header
        self.table.pack(fill="both", expand=True)


class AppsPage(TablePage):
    def __init__(self, parent, query_var):
        super().__init__(parent, "Applications", [("Time", 0.52), ("Share", 0.64)], "Application   (browsers expand to websites)",
                         0.74, query_var, "Filter applications and websites")
        self.table.empty_text = "No applications recorded for this day"

    def refresh(self, snap, needle):
        apps, sites, total = snap["apps"], snap["sites"], snap["total"]
        rows, shown = [], 0
        for name, sec in apps:
            kids = sites.get(name, [])
            self_match = not needle or needle in name.lower()
            kid_rows = [(s, v) for s, v in kids if self_match or needle in s.lower()]
            if not self_match and not kid_rows:
                continue
            frac = sec / total if total else 0
            color = LIME if kids else CAT_COLORS[categorize(name, CAT_RULES_APPS)]
            rows.append(dict(name=name, kid=False, has_kids=bool(kid_rows), frac=frac, color=color,
                             cols=[fmt_dur(sec), f"{frac * 100:.0f}%"]))
            shown += 1
            for site, ssec in kid_rows:
                sf = ssec / sec if sec else 0
                rows.append(dict(name=site, kid=True, has_kids=False, frac=sf,
                                 color=CAT_COLORS[categorize(site, CAT_RULES_SITES)],
                                 cols=[fmt_dur(ssec), f"{sf * 100:.0f}%"]))
        self.table.set_rows(rows)
        self.total.config(text=f"{shown} applications   ·   Total {fmt_dur(total)}")


class SitesPage(TablePage):
    ALL = "All browsers"

    def __init__(self, parent, query_var):
        super().__init__(parent, "Websites", [("Browser", 0.44), ("Time", 0.60), ("Share", 0.72)], "Website", 0.82,
                         query_var, "Filter websites")
        self.table.empty_text = "No website data for this day"
        tk.Label(self.extra, text="Browser", bg=CARD, fg=MUTED, font=f(10)).pack(side="left")
        self.browser_var = tk.StringVar(value=self.ALL)
        self.browser_names = [self.ALL]
        self.on_pick = lambda: None
        DropPill(self.extra, self.browser_var, lambda: self.browser_names, lambda: self.on_pick(), bg=CARD,
                 h=40, style="soft").pack(side="left", padx=S(8))

    def refresh(self, snap, needle):
        sites = snap["sites"]
        names = [self.ALL] + sorted(sites)
        self.browser_names = names
        if self.browser_var.get() not in names:
            self.browser_var.set(self.ALL)
        sel = self.browser_var.get()
        agg = {}
        for browser, lst in sites.items():
            if sel != self.ALL and browser != sel:
                continue
            for site, sec in lst:
                e = agg.setdefault(site, [0.0, set()])
                e[0] += sec
                e[1].add(browser)
        found = sorted(((s, v) for s, v in agg.items() if not needle or needle in s.lower()),
                       key=lambda kv: kv[1][0], reverse=True)
        total = sum(v[0] for _, v in agg.items())
        rows = []
        for site, (sec, browsers) in found:
            frac = sec / total if total else 0
            rows.append(dict(name=site, kid=False, has_kids=False, frac=frac,
                             color=CAT_COLORS[categorize(site, CAT_RULES_SITES)],
                             cols=[short(", ".join(sorted(browsers)), 18), fmt_dur(sec), f"{frac * 100:.0f}%"]))
        self.table.set_rows(rows)
        self.total.config(text=f"{len(found)} websites   ·   Total {fmt_dur(total)}")


def nice_max(v: float, steps) -> float:
    for s in steps:
        if v <= s:
            return s
    return steps[-1]


TIPS = [
    "Follow the 20-20-20 rule: every 20 minutes, look at something 20 feet away for 20 seconds.",
    "Blink slowly ten times - screens make us blink far less than we normally do.",
    "Keep the top of your screen at or just below eye level, about an arm's length away.",
    "Match your screen brightness to the room around you to cut glare.",
    "Stand up and stretch your neck and shoulders during every other rest.",
    "Drink a glass of water now - it helps tired eyes too.",
    "Try palming: cover your closed eyes with warm hands for 20 seconds.",
]


# --------------------------------------------------------------------------
# Main application
# --------------------------------------------------------------------------
class App:
    PAGES = [("dashboard", "Dashboard", "dash"), ("apps", "Applications", "apps"), ("sites", "Websites", "globe"),
             ("timer", "Rest Timer", "timer")]

    def __init__(self, root: tk.Tk, tracker: UsageTracker, show: bool = False):
        global SC
        self.root = root
        self.tracker = tracker
        try:
            SC = max(1.0, root.winfo_fpixels("1i") / 96.0)
        except tk.TclError:
            SC = 1.0
        setup_style(root)
        root.title("FocusRest")
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        ww, wh = min(S(1320), sw - S(40)), min(S(860), sh - S(90))
        root.geometry(f"{ww}x{wh}+{max(0, (sw - ww) // 2)}+{max(0, (sh - wh) // 2 - S(20))}")
        root.minsize(min(S(1120), sw - S(40)), min(S(700), sh - S(90)))

        self.state = "work"        # work | rest
        self.paused = False
        self.deadline = 0.0
        self.phase_total = 1.0
        self.paused_remaining = 0.0
        self.break_win = None
        self.break_ring = None
        self.page = "dashboard"
        self.chart_mode = "24h"
        self._q = queue.Queue()
        self.tray = None
        self._last_tip = ""
        self._beep_on = False
        self._today = datetime.date.today().isoformat()
        self._pretty = ""
        self.date_var = tk.StringVar(value=self._today)
        self.query = tk.StringVar(value="")
        self.interval_var = tk.IntVar(value=DEFAULT_INTERVAL_MIN)
        self.rest_var = tk.IntVar(value=DEFAULT_REST_SEC)
        self.track_var = tk.BooleanVar(value=True)
        self.beep_var = tk.BooleanVar(value=False)       # beep sound: OFF by default
        self.auto_var = tk.BooleanVar(value=autostart_enabled())
        self.beep_var.trace_add("write", lambda *a: self._beep_changed())

        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)
        self.cards = []
        self._build_sidebar()
        main = tk.Frame(root, bg=BG)
        main.grid(row=0, column=1, sticky="nsew", padx=S(22), pady=S(16))
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)
        self._build_topbar(main)
        host = tk.Frame(main, bg=BG)
        host.grid(row=1, column=0, sticky="nsew", pady=(S(14), 0))
        host.columnconfigure(0, weight=1)
        host.rowconfigure(0, weight=1)
        self.pages = {}
        for key, _, _ in self.PAGES:
            fr = tk.Frame(host, bg=BG)
            fr.grid(row=0, column=0, sticky="nsew")
            self.pages[key] = fr
        self._build_dashboard(self.pages["dashboard"])
        self.apps_page = AppsPage(self.pages["apps"], self.query)
        self.apps_page.pack(fill="both", expand=True)
        self.sites_page = SitesPage(self.pages["sites"], self.query)
        self.sites_page.pack(fill="both", expand=True)
        self.sites_page.on_pick = self.refresh_now
        self.query.trace_add("write", lambda *a: self.refresh_now())
        self._build_timer(self.pages["timer"])
        for c in self.cards:
            c.fit()

        root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)      # X = hide to the tray, not quit
        self.reset()
        self.show("dashboard")
        self.tick()
        self.refresh_loop()
        self._start_tray(force_show=show)

    def _card(self, parent, **kw):
        c = RCard(parent, **kw)
        self.cards.append(c)
        return c

    def _beep_changed(self):
        on = bool(self.beep_var.get())
        self._beep_on = on
        if hasattr(self, "sound_btn"):
            self.sound_btn.set_kind("speaker" if on else "speaker_off")
            self.qa_sound.set(kind="speaker" if on else "speaker_off", text="Sound on" if on else "Sound off")

    # ------------------------------------------------------------------ layout
    def _build_sidebar(self):
        side = tk.Frame(self.root, bg=SIDE, width=S(240))
        side.grid(row=0, column=0, sticky="ns")
        side.grid_propagate(False)
        side.pack_propagate(False)
        logo = tk.Frame(side, bg=SIDE)
        logo.pack(fill="x", padx=S(22), pady=(S(24), S(22)))
        cv = tk.Canvas(logo, width=S(36), height=S(36), bg=SIDE, highlightthickness=0)
        cv.pack(side="left")
        rrect(cv, 0, 0, S(36), S(36), S(11), fill=LIME, outline="")
        cv.create_oval(S(9), S(9), S(27), S(27), outline=INK, width=S(3))
        cv.create_oval(S(15), S(15), S(21), S(21), fill=INK, outline="")
        tk.Label(logo, text="FocusRest", bg=SIDE, fg=TEXT, font=f(15, True)).pack(side="left", padx=S(10))

        self.nav = {}
        navbox = tk.Frame(side, bg=SIDE)
        navbox.pack(fill="x", padx=S(14))
        for key, text, kind in self.PAGES:
            it = NavItem(navbox, text, kind, lambda k=key: self.show(k))
            it.pack(fill="x", pady=2)
            self.nav[key] = it

        bottom = tk.Frame(side, bg=SIDE)
        bottom.pack(side="bottom", fill="x", padx=S(16), pady=(0, S(16)))
        hide = tk.Canvas(bottom, height=S(40), bg=SIDE, highlightthickness=0, cursor="hand2")
        hide.pack(fill="x", side="bottom", pady=(S(8), 0))
        self._hide_cv = hide
        hide.bind("<Button-1>", lambda e: self.hide_to_tray())
        hide.bind("<Configure>", lambda e: self._draw_hide())

        promo = self._card(bottom, bg=SIDE, fill=CARD, radius=16, pad=14)
        promo.pack(fill="x", side="bottom")
        pi = promo.inner
        top = tk.Frame(pi, bg=CARD)
        top.pack(fill="x")
        ic = tk.Canvas(top, width=S(32), height=S(32), bg=CARD, highlightthickness=0)
        ic.pack(side="left")
        rrect(ic, 1, 1, S(31), S(31), S(9), fill=CARD, outline=BORDER)
        icon(ic, "bolt", S(16), S(16), S(8), INK)
        self.promo_cap = tk.Label(top, text="Next rest in", bg=CARD, fg=MUTED, font=f(9))
        self.promo_cap.pack(side="left", padx=S(10))
        self.promo_time = tk.Label(pi, text="20:00", bg=CARD, fg=TEXT, font=f(22, True), anchor="w")
        self.promo_time.pack(fill="x", pady=(S(8), 0))
        tk.Label(pi, text="Protect your eyes with a short break every 20 minutes.", bg=CARD, fg=MUTED, font=f(9),
                 wraplength=S(165), justify="left", anchor="w").pack(fill="x", pady=(S(2), S(10)))
        Pill(pi, "Rest now", self.start_rest, "black", bg=CARD, fixed_w=170, h=38).pack()

        rows = tk.Frame(bottom, bg=SIDE)
        rows.pack(fill="x", side="bottom", pady=(0, S(14)))
        for text, var, cmd in (("Track usage", self.track_var, self.toggle_tracking), ("Beep sound", self.beep_var, None)):
            r = tk.Frame(rows, bg=SIDE)
            r.pack(fill="x", pady=S(5))
            tk.Label(r, text=text, bg=SIDE, fg="#555555", font=f(10)).pack(side="left", padx=S(8))
            Toggle(r, var, cmd, bg=SIDE).pack(side="right")

    def _draw_hide(self):
        c = self._hide_cv
        c.delete("all")
        icon(c, "hide", S(22), S(20), S(7.5), "#555555")
        c.create_text(S(40), S(20), anchor="w", fill="#555555", font=f(10),
                      text="Hide to tray" if (self.tray is not None or pystray is not None) else "Quit app")

    def _build_topbar(self, main):
        bar_ = tk.Frame(main, bg=BG)
        bar_.grid(row=0, column=0, sticky="ew")
        self.search = SearchEntry(bar_, placeholder="Quick search apps & websites", width_px=330,
                                  on_enter=self.do_search, h=46)
        self.search.pack(side="left")
        right = tk.Frame(bar_, bg=BG)
        right.pack(side="right")
        Pill(right, "Export CSV", self.export_csv, "light", icon_kind="download", bg=BG, h=44).pack(side="right")
        name = os.environ.get("USERNAME") or os.environ.get("USER") or "You"
        UserChip(right, name[:18], "Data stays on this PC").pack(side="right", padx=S(12))
        CircleBtn(right, "gear", lambda: self.show("timer"), size=46).pack(side="right")
        self.sound_btn = CircleBtn(right, "speaker_off", lambda: self.beep_var.set(not self.beep_var.get()), size=46)
        self.sound_btn.pack(side="right", padx=S(10))
        self.date_pill = DropPill(right, self.date_var, lambda: self.tracker.dates(), self.refresh_now, bg=BG, h=46)
        self.date_pill.pack(side="right", padx=(0, S(4)))

    def do_search(self, text):
        self.query.set(text)
        self.show("apps")

    # ------------------------------------------------------------- dashboard
    def _build_dashboard(self, p):
        p.columnconfigure(0, weight=1)
        p.columnconfigure(1, weight=0)
        p.rowconfigure(0, weight=1)
        left = tk.Frame(p, bg=BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, S(14)))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=3)
        left.rowconfigure(2, weight=2)

        # --- row 0: hero chart + three headline numbers
        r0 = tk.Frame(left, bg=BG)
        r0.grid(row=0, column=0, sticky="nsew")
        r0.rowconfigure(0, weight=1)
        r0.columnconfigure(0, weight=1)
        hero = self._card(r0, min_h=270, pad=20)
        hero.grid(row=0, column=0, sticky="nsew")
        hi = hero.inner
        top = tk.Frame(hi, bg=CARD)
        top.pack(fill="x")
        tl = tk.Frame(top, bg=CARD)
        tl.pack(side="left")
        self.hero_total = tk.Label(tl, text="0m", bg=CARD, fg=TEXT, font=f(28, True), anchor="w")
        self.hero_total.pack(anchor="w")
        self.hero_sub = tk.Label(tl, text="", bg=CARD, fg=MUTED, font=f(9), anchor="w")
        self.hero_sub.pack(anchor="w")
        Seg(top, ["24h", "7d"], self.set_chart_mode, "24h").pack(side="right")
        self.legend = tk.Frame(hi, bg=CARD)
        self.legend.pack(fill="x", pady=(S(8), S(2)))
        self.hero_chart = BarChart(hi, height=170)
        self.hero_chart.pack(fill="both", expand=True)

        stats = tk.Frame(r0, bg=BG)
        stats.grid(row=0, column=1, sticky="ns", padx=(S(26), S(6)))
        stats.columnconfigure(0, minsize=S(168))
        self.stat_screen = StatBlock(stats, "Total screen time")
        self.stat_rests = StatBlock(stats, "Rests taken")
        self.stat_long = StatBlock(stats, "Longest session")
        for i, w in enumerate((self.stat_screen, self.stat_rests, self.stat_long)):
            w.grid(row=i, column=0, sticky="ew", pady=(0, S(12)))

        # --- row 1: goal + tip
        r1 = tk.Frame(left, bg=BG)
        r1.grid(row=1, column=0, sticky="ew", pady=S(14))
        r1.columnconfigure(0, weight=4)
        r1.columnconfigure(1, weight=5)
        goal = self._card(r1, min_h=124)
        goal.grid(row=0, column=0, sticky="nsew", padx=(0, S(7)))
        gi = goal.inner
        gh = tk.Frame(gi, bg=CARD)
        gh.pack(fill="x")
        tk.Label(gh, text="Daily rest goal", bg=CARD, fg=TEXT, font=f(11, True)).pack(side="left")
        pen = tk.Canvas(gh, width=S(22), height=S(22), bg=CARD, highlightthickness=0, cursor="hand2")
        pen.pack(side="right")
        icon(pen, "pencil", S(11), S(11), S(7), INK)
        pen.bind("<Button-1>", lambda e: self.show("timer"))
        self.goal_bar = ProgressBar(gi, height=34)
        self.goal_bar.pack(fill="x", pady=(S(8), S(4)))
        gf = tk.Frame(gi, bg=CARD)
        gf.pack(fill="x")
        self.goal_left = tk.Label(gf, text="0 rests", bg=CARD, fg=TEXT, font=f(12, True))
        self.goal_left.pack(side="left")
        self.goal_right = tk.Label(gf, text="", bg=CARD, fg=MUTED, font=f(10))
        self.goal_right.pack(side="right")

        tipc = self._card(r1, min_h=124)
        tipc.grid(row=0, column=1, sticky="nsew", padx=(S(7), 0))
        ti = tipc.inner
        PixelDecor(ti).pack(side="right", padx=(S(8), 0))
        tl = tk.Frame(ti, bg=CARD)
        tl.pack(side="left", fill="both", expand=True)
        tk.Label(tl, text="Rest tip of the day", bg=CARD, fg=TEXT, font=f(11, True), anchor="w").pack(fill="x")
        self.tip_lbl = tk.Label(tl, text="", bg=CARD, fg="#555555", font=f(9), anchor="nw", justify="left", wraplength=S(200))
        self.tip_lbl.pack(fill="x", pady=(S(4), S(6)))
        rl = tk.Label(tl, text="Rest now  →", bg=CARD, fg=TEXT, font=f(10, True), cursor="hand2")
        rl.pack(anchor="w")
        rl.bind("<Button-1>", lambda e: self.start_rest())
        self.tip_lbl.config(text=TIPS[datetime.date.today().toordinal() % len(TIPS)])

        # --- row 2: category, focus, websites
        r2 = tk.Frame(left, bg=BG)
        r2.grid(row=2, column=0, sticky="nsew")
        r2.rowconfigure(0, weight=1)
        r2.columnconfigure((0, 1, 2), weight=1, uniform="r2")
        cat = self._card(r2, min_h=214)
        cat.grid(row=0, column=0, sticky="nsew", padx=(0, S(7)))
        ci = cat.inner
        card_header(ci, "Time by category")
        self.cat_total = tk.Label(ci, text="0m", bg=CARD, fg=TEXT, font=f(22, True), anchor="w")
        self.cat_total.pack(fill="x", pady=(S(8), S(2)))
        tk.Label(ci, text="Tracked screen time", bg=CARD, fg=MUTED, font=f(9), anchor="w").pack(fill="x")
        self.cat_bar = SegBar(ci, height=38)
        self.cat_bar.pack(fill="x", pady=(S(8), S(8)))
        self.cat_list = LegendList(ci, height=84)
        self.cat_list.empty_text = "No data yet"
        self.cat_list.pack(fill="both", expand=True)

        foc = self._card(r2, min_h=214)
        foc.grid(row=0, column=1, sticky="nsew", padx=S(7))
        fi = foc.inner
        card_header(fi, "Focus score")
        self.foc_big = tk.Label(fi, text="0m", bg=CARD, fg=TEXT, font=f(22, True), anchor="w")
        self.foc_big.pack(fill="x", pady=(S(8), 0))
        self.foc_sub = tk.Label(fi, text="", bg=CARD, fg=MUTED, font=f(9), anchor="w")
        self.foc_sub.pack(fill="x")
        self.gauge = Gauge(fi, height=96)
        self.gauge.pack(fill="both", expand=True, pady=(S(6), S(4)))

        web = self._card(r2, min_h=214)
        web.grid(row=0, column=2, sticky="nsew", padx=(S(7), 0))
        wi = web.inner
        card_header(wi, "Top websites", lambda par: Pill(par, "View all", lambda: self.show("sites"), "light", bg=CARD, h=28, size=9, pad=12))
        self.sites_list = RankList(wi, height=140)
        self.sites_list.empty_text = "No website data yet"
        self.sites_list.pack(fill="both", expand=True, pady=(S(8), 0))

        # --- right column
        right = tk.Frame(p, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1, minsize=S(296))
        right.rowconfigure(2, weight=1)

        tc = self._card(right, min_h=0)
        tc.grid(row=0, column=0, sticky="ew")
        ti = tc.inner
        hd = tk.Frame(ti, bg=CARD)
        hd.pack(fill="x")
        tk.Label(hd, text="Rest timer", bg=CARD, fg=TEXT, font=f(12, True)).pack(side="left")
        Pill(hd, "Settings", lambda: self.show("timer"), "light", icon_kind="gear", bg=CARD, h=30, size=9, pad=12).pack(side="right")
        tk.Label(ti, text="Quick actions", bg=CARD, fg=MUTED, font=f(9), anchor="w").pack(fill="x", pady=(0, S(10)))
        self.timer_card = TimerCard(ti)
        self.timer_card.pack(fill="x")
        qa = tk.Frame(ti, bg=CARD)
        qa.pack(fill="x", pady=(S(10), 0))
        qa.columnconfigure((0, 1, 2, 3), weight=1, uniform="qa")
        self.qa_pause = QAction(qa, "pause", "Pause", self.toggle_pause)
        self.qa_rest = QAction(qa, "eye", "Rest now", self.start_rest)
        self.qa_reset = QAction(qa, "reset", "Reset", self.reset)
        self.qa_sound = QAction(qa, "speaker_off", "Sound off", lambda: self.beep_var.set(not self.beep_var.get()))
        for i, w in enumerate((self.qa_pause, self.qa_rest, self.qa_reset, self.qa_sound)):
            w.grid(row=0, column=i)

        ta = self._card(right, min_h=0)
        ta.grid(row=1, column=0, sticky="ew", pady=S(12))
        tai = ta.inner
        card_header(tai, "Top apps", lambda par: tk.Label(par, text="tap to filter", bg=CARD, fg=MUTED, font=f(8)), size=12)
        self.avatars = AvatarRow(tai, height=74)
        self.avatars.on_pick = self.do_search
        self.avatars.empty_text = "No apps recorded yet"
        self.avatars.pack(fill="x", pady=(S(10), 0))

        hc = self._card(right, min_h=0)
        hc.grid(row=2, column=0, sticky="nsew")
        hi = hc.inner
        card_header(hi, "App usage", lambda par: tk.Label(par, text="Today", bg=SOFT, fg=TEXT, font=f(9, True), padx=S(10), pady=S(3)), size=12)
        self.app_list = AppList(hi, height=150)
        self.app_list.empty_text = "No activity yet"
        self.app_list.pack(fill="both", expand=True, pady=(S(8), 0))

    # ------------------------------------------------------------- timer page
    def _setting_row(self, parent, label, factory, last=False):
        r = tk.Frame(parent, bg=CARD)
        r.pack(fill="x")
        tk.Label(r, text=label, bg=CARD, fg=TEXT, font=f(10)).pack(side="left", pady=S(12))
        factory(r).pack(side="right")
        if not last:
            tk.Frame(parent, bg=GRID, height=1).pack(fill="x")

    def _build_timer(self, p):
        p.columnconfigure(0, weight=3, uniform="t")
        p.columnconfigure(1, weight=2, uniform="t")
        p.rowconfigure(0, weight=1)
        c = self._card(p, min_h=420, pad=22)
        c.grid(row=0, column=0, sticky="nsew", padx=(0, S(7)))
        body = c.inner
        hd = tk.Frame(body, bg=CARD)
        hd.pack(fill="x")
        tk.Label(hd, text="20-20-20 rest reminder", bg=CARD, fg=TEXT, font=f(16, True)).pack(side="left")
        self.timer_status = tk.Label(hd, text="Running", bg=SOFT, fg=GOOD, font=f(9, True), padx=S(12), pady=S(4))
        self.timer_status.pack(side="right")
        self.timer_caption = tk.Label(body, text="Next rest in", bg=CARD, fg=MUTED, font=f(11))
        self.timer_caption.pack(pady=(S(14), 0))
        self.timer_ring = Ring(body, size=300, thick=0.075, main_size=46, sub_size=11)
        self.timer_ring.pack(fill="both", expand=True, padx=S(20), pady=S(6))
        btns = tk.Frame(body, bg=CARD)
        btns.pack(pady=S(10))
        self.pause_btn = Pill(btns, "Pause", self.toggle_pause, "black", icon_kind="pause", h=42, pad=22)
        self.pause_btn.pack(side="left", padx=S(6))
        Pill(btns, "Rest now", self.start_rest, "soft", icon_kind="eye", h=42, pad=22).pack(side="left", padx=S(6))
        Pill(btns, "Reset", self.reset, "soft", icon_kind="reset", h=42, pad=22).pack(side="left", padx=S(6))

        col = tk.Frame(p, bg=BG)
        col.grid(row=0, column=1, sticky="nsew", padx=(S(7), 0))
        col.columnconfigure(0, weight=1)
        sc = self._card(col, min_h=260, pad=22)
        sc.grid(row=0, column=0, sticky="ew")
        si = sc.inner
        tk.Label(si, text="Settings", bg=CARD, fg=TEXT, font=f(14, True), anchor="w").pack(fill="x", pady=(0, S(4)))
        self._setting_row(si, "Remind me every (minutes)", lambda par: ttk.Spinbox(
            par, from_=1, to=120, width=6, textvariable=self.interval_var, command=self.on_interval_change))
        self._setting_row(si, "Rest for (seconds)", lambda par: ttk.Spinbox(par, from_=5, to=600, width=6, textvariable=self.rest_var))
        self._setting_row(si, "Track app & website usage", lambda par: Toggle(par, self.track_var, self.toggle_tracking))
        self._setting_row(si, "Beep sound", lambda par: Toggle(par, self.beep_var))
        self._setting_row(si, "Start with Windows", lambda par: Toggle(par, self.auto_var, lambda: set_autostart(self.auto_var.get())),
                          last=True)

        st = self._card(col, min_h=190, pad=22)
        st.grid(row=1, column=0, sticky="ew", pady=(S(14), 0))
        sti = st.inner
        tk.Label(sti, text="Rest statistics", bg=CARD, fg=TEXT, font=f(14, True), anchor="w").pack(fill="x", pady=(0, S(8)))
        g = tk.Frame(sti, bg=CARD)
        g.pack(fill="x")
        g.columnconfigure((0, 1), weight=1, uniform="s")
        self.rest_stats = {}
        for i, (key, title) in enumerate((("today", "Rests today"), ("week", "Last 7 days"),
                                          ("avg", "Daily average"), ("next", "Next rest at"))):
            cell = tk.Frame(g, bg=SOFT)
            cell.grid(row=i // 2, column=i % 2, sticky="nsew", padx=S(4), pady=S(4))
            tk.Label(cell, text=title, bg=SOFT, fg=MUTED, font=f(9)).pack(anchor="w", padx=S(14), pady=(S(10), 0))
            lbl = tk.Label(cell, text="-", bg=SOFT, fg=TEXT, font=f(18, True))
            lbl.pack(anchor="w", padx=S(14), pady=(0, S(10)))
            self.rest_stats[key] = lbl

        tk.Label(col, text="The 20-20-20 rule: every 20 minutes look at something 20 feet away for 20 seconds.",
                 bg=BG, fg=MUTED, font=f(9), wraplength=S(330), justify="left").grid(row=2, column=0, sticky="w", pady=(S(14), 0))
        if auto is None:
            tk.Label(col, text="Tip: run  python -m pip install uiautomation  for exact website names.",
                     bg=BG, fg=WARN, font=f(9), wraplength=S(330), justify="left").grid(row=3, column=0, sticky="w", pady=(S(8), 0))

    # ------------------------------------------------------------- navigation
    def show(self, key):
        self.page = key
        self.pages[key].tkraise()
        for k, it in self.nav.items():
            it.set_active(k == key)
        self.refresh_now()

    def set_chart_mode(self, mode):
        self.chart_mode = mode
        self.hero_chart.hover = None
        self.refresh_now()

    # ------------------------------------------------------------------ data
    def toggle_tracking(self):
        self.tracker.paused = not self.track_var.get()

    def on_interval_change(self):
        pass

    def refresh_loop(self):
        self.refresh_now()
        self.root.after(2000, self.refresh_loop)

    def refresh_now(self):
        today = datetime.date.today().isoformat()
        if today != self._today:                      # a new day started
            if self.date_var.get() == self._today:
                self.date_var.set(today)
            self._today = today
            self.tip_lbl.config(text=TIPS[datetime.date.today().toordinal() % len(TIPS)])
        day = self.date_var.get()
        try:
            dd = datetime.date.fromisoformat(day)
            self._pretty = dd.strftime("%A, %d %B %Y") + ("  ·  Today" if day == today else "")
            self._short = "Today" if day == today else dd.strftime("%d %b")
        except ValueError:
            self._pretty, self._short = day, day
        snap = self.tracker.snapshot(day)
        needle = self.query.get().strip().lower()
        if self.page == "dashboard":
            self.update_dashboard(day, snap)
        elif self.page == "apps":
            self.apps_page.refresh(snap, needle)
        elif self.page == "sites":
            self.sites_page.refresh(snap, needle)
        else:
            self.update_timer_stats(today)

    def _set_legend(self, items):
        for ch in self.legend.winfo_children():
            ch.destroy()
        for color, text in items:
            d = tk.Canvas(self.legend, width=S(10), height=S(10), bg=CARD, highlightthickness=0)
            d.create_oval(0, 0, S(10), S(10), fill=color, outline="")
            d.pack(side="left", padx=(0, S(5)))
            tk.Label(self.legend, text=text, bg=CARD, fg=MUTED, font=f(9)).pack(side="left", padx=(0, S(16)))

    def update_dashboard(self, day, snap):
        total = snap["total"]
        series = self.tracker.series(day, 7)
        prev = series[-2][1] if len(series) > 1 else 0.0

        self.hero_total.config(text=fmt_dur(total) if total > 0 else "0m")
        self.hero_sub.config(text=f"Screen time overview  ·  {self._pretty}")

        if total <= 0:
            self.stat_screen.set("0m", "No activity recorded")
        elif prev > 0:
            ch = (total - prev) / prev * 100
            self.stat_screen.set(fmt_dur(total), f"{'▲' if ch >= 0 else '▼'} {abs(ch):.0f}% vs previous day",
                                 WARN if ch >= 0 else GOOD)
        else:
            self.stat_screen.set(fmt_dur(total), "No data for previous day")

        try:
            iv = max(1, int(self.interval_var.get()))
        except (tk.TclError, ValueError):
            iv = DEFAULT_INTERVAL_MIN
        suggested = int(total // 60 // iv)
        self.stat_rests.set(str(snap["rests"]), f"of ~{suggested} suggested" if suggested else "Keep it up",
                            GOOD if snap["rests"] >= suggested and suggested else MUTED)
        self.stat_long.set(fmt_dur(snap["longest"]) if snap["longest"] else "0m", "continuous activity")

        # hero chart
        if self.chart_mode == "24h":
            hours = snap["hours"]
            mins = [s / 60 for s in hours]
            peak = max(range(24), key=lambda h: hours[h]) if total > 0 else None
            tips = [(f"{h:02d}:00 - {h:02d}:59", [(LIME, "Screen time", fmt_dur(hours[h]))]) if hours[h] > 0 else None
                    for h in range(24)]
            self.hero_chart.set([f"{h:02d}" if h % 3 == 0 else "" for h in range(24)], mins, 60,
                                [(0, "0"), (15, "15m"), (30, "30m"), (45, "45m"), (60, "60m")], peak, tips)
            self._set_legend([(LIME, f"Peak hour {peak:02d}:00" if peak is not None else "Peak hour"), (GRAYBAR, "Other hours")])
        else:
            vals = [t / 3600 for _, t, _ in series]
            ymax = nice_max(max(vals), [1, 2, 3, 4, 6, 8, 10, 12, 16, 24])
            ticks = [(ymax * i / 4, f"{ymax * i / 4:g}h") for i in range(5)]
            labs = [datetime.date.fromisoformat(d).strftime("%a") for d, _, _ in series]
            tips = [(datetime.date.fromisoformat(d).strftime("%A, %d %b"),
                     [(LIME, "Screen time", fmt_dur(t)), (YELLOW, "Rests", str(r))]) if t > 0 else None
                    for d, t, r in series]
            self.hero_chart.set(labs, vals, ymax, ticks, len(vals) - 1, tips)
            active = [t for _, t, _ in series if t > 0]
            self._set_legend([(LIME, "Selected day"), (GRAYBAR, "Previous days")] +
                             ([(YELLOW, f"Avg {fmt_dur(sum(active) / len(active))} / day")] if active else []))

        # daily rest goal
        goal = max(suggested, 1)
        frac = min(1.0, snap["rests"] / goal)
        self.goal_bar.set(frac, GREEN if frac >= 1 else LIME)
        self.goal_left.config(text=f"{snap['rests']} rest{'s' if snap['rests'] != 1 else ''}")
        self.goal_right.config(text=f"of {goal} suggested")

        # categories + focus
        cats = category_totals(snap)
        self.cat_total.config(text=fmt_dur(total) if total > 0 else "0m")
        if total > 0:
            self.cat_bar.set([(CAT_COLORS[c], cats[c]) for c in CATEGORIES if cats[c] > 0])
            self.cat_list.set([(CAT_COLORS[c], c, f"{cats[c] * 100 / total:.0f}%")
                               for c in CATEGORIES if cats[c] > 0])
        else:
            self.cat_bar.set([("#e4e4e4", 1)])
            self.cat_list.set([])
        score = int(round(cats["Work"] * 100 / total)) if total > 0 else 0
        if total <= 0:
            label, col = "No data yet", MUTED
        elif score >= 70:
            label, col = "Excellent focus", GOOD
        elif score >= 50:
            label, col = "Good focus", GOOD
        elif score >= 30:
            label, col = "Fair focus", WARN
        else:
            label, col = "Mostly leisure / other", BAD
        self.foc_big.config(text=fmt_dur(cats["Work"]) if total > 0 else "0m")
        self.foc_sub.config(text=label, fg=col)
        self.gauge.set(score / 100, f"{score}%", "of time on work")

        # top websites
        agg = {}
        for lst in snap["sites"].values():
            for site, sec in lst:
                agg[site] = agg.get(site, 0.0) + sec
        top = sorted(agg.items(), key=lambda kv: kv[1], reverse=True)
        stotal = sum(agg.values())
        self.sites_list.set([(s, fmt_dur(v), f"{v * 100 / stotal:.0f}% of browsing", v / top[0][1], BAR_COLORS[i % len(BAR_COLORS)])
                             for i, (s, v) in enumerate(top[:5])])

        # top apps + usage list
        apps = snap["apps"]
        self.avatars.set([(n, tile_color(n)) for n, _ in apps[:6]])
        rows = []
        for n, s in apps[:8]:
            cat = "Browser" if n in snap["sites"] else categorize(n, CAT_RULES_APPS)
            rows.append((n, cat, fmt_dur(s), f"{s * 100 / total:.0f}% of day" if total else "",
                         CAT_TEXT.get(cat, MUTED) if cat != "Browser" else GOOD))
        self.app_list.set(rows)

    def update_timer_stats(self, today):
        ser = self.tracker.series(today, 7)
        week = sum(r for _, _, r in ser)
        active_days = [1 for _, t, _ in ser if t > 0]
        self.rest_stats["today"].config(text=str(ser[-1][2]))
        self.rest_stats["week"].config(text=str(week))
        self.rest_stats["avg"].config(text=f"{week / len(active_days):.1f}" if active_days else "0")
        if not self.paused:
            at = datetime.datetime.now() + datetime.timedelta(seconds=max(0, self.deadline - time.monotonic()))
            self.rest_stats["next"].config(text=at.strftime("%H:%M") if self.state == "work" else "Resting")
        else:
            self.rest_stats["next"].config(text="Paused")

    def export_csv(self):
        from tkinter import filedialog
        import csv
        day = self.date_var.get()
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile=f"usage_{day}.csv",
                                            filetypes=[("CSV file", "*.csv")], title="Export usage")
        if not path:
            return
        snap = self.tracker.snapshot(day)
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh)
                w.writerow(["Date", "Type", "Application / Browser", "Website", "Seconds", "Time"])
                for app_, sec in snap["apps"]:
                    w.writerow([day, "App", app_, "", int(sec), fmt_hms(sec)])
                for browser, lst in snap["sites"].items():
                    for site, sec in lst:
                        w.writerow([day, "Website", browser, site, int(sec), fmt_hms(sec)])
        except OSError:
            pass

    # ------------------------------------------------------------ system tray
    def _ui(self, fn):
        self._q.put(fn)

    def _start_tray(self, force_show=False):
        if pystray is None or force_show:
            if pystray is not None:
                self._build_tray()
            self.root.deiconify()
            return
        self._build_tray()
        self.root.withdraw()
        toast("FocusRest is running", "It lives in the system tray near the clock. Click the icon to open it.")

    def _build_tray(self):
        M = pystray.MenuItem
        menu = pystray.Menu(
            M("Open FocusRest", lambda i, it: self._ui(self.show_window), default=True),
            M(lambda it: "Resume" if self.paused else "Pause", lambda i, it: self._ui(self.toggle_pause)),
            M("Rest now", lambda i, it: self._ui(self.start_rest)),
            M("Beep sound", lambda i, it: self._ui(lambda: self.beep_var.set(not self.beep_var.get())),
              checked=lambda it: getattr(self, "_beep_on", False)),
            M("Start with Windows", lambda i, it: self._ui(lambda: (self.auto_var.set(not autostart_enabled()),
                                                                    set_autostart(self.auto_var.get()))),
              checked=lambda it: autostart_enabled()),
            pystray.Menu.SEPARATOR,
            M("Quit FocusRest", lambda i, it: self._ui(self.quit_app)),
        )
        self.tray = pystray.Icon("FocusRest", make_tray_icon(), "FocusRest", menu)
        self.tray.run_detached()

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.refresh_now()

    def hide_to_tray(self):
        if self.tray is None:
            self.quit_app()
        else:
            self.root.withdraw()

    def quit_app(self):
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.tracker.stop()
        self.tracker.save()
        self.root.destroy()

    # ------------------------------------------------------------- timer logic
    def interval_sec(self) -> float:
        try:
            return max(1, int(self.interval_var.get())) * 60
        except (tk.TclError, ValueError):
            return DEFAULT_INTERVAL_MIN * 60

    def rest_sec(self) -> float:
        try:
            return max(5, int(self.rest_var.get()))
        except (tk.TclError, ValueError):
            return DEFAULT_REST_SEC

    @staticmethod
    def fmt(seconds: float) -> str:
        seconds = max(0, int(seconds + 0.999))
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    def _paint_pause_ui(self):
        self.pause_btn.set_text("Resume" if self.paused else "Pause", "play" if self.paused else "pause")
        self.qa_pause.set(kind="play" if self.paused else "pause", text="Resume" if self.paused else "Pause")
        if self.paused:
            self.timer_status.config(text="Paused", fg=WARN)
        else:
            self.timer_status.config(text="Running" if self.state == "work" else "Resting",
                                     fg=GOOD if self.state == "work" else WARN)

    def _begin(self, state: str, seconds: float):
        self.state = state
        self.paused = False
        self.phase_total = max(1.0, float(seconds))
        self.deadline = time.monotonic() + seconds
        self.timer_caption.config(text="Next rest in" if state == "work" else "Rest time left")
        self.promo_cap.config(text="Next rest in" if state == "work" else "Resting")
        self._paint_pause_ui()
        self._paint_timer(seconds)

    def reset(self):
        self.close_break_window()
        self._begin("work", self.interval_sec())

    def toggle_pause(self):
        if not self.paused:
            self.paused = True
            self.paused_remaining = self.deadline - time.monotonic()
        else:
            self.paused = False
            self.deadline = time.monotonic() + self.paused_remaining
        self._paint_pause_ui()
        self._paint_timer(self.paused_remaining if self.paused else self.deadline - time.monotonic())

    def start_rest(self):
        if self.state == "rest":
            return
        self._begin("rest", self.rest_sec())
        toast("Time to rest your eyes", f"Look away from the screen for {int(self.rest_sec())} seconds.")
        if self.beep_var.get():
            short_beep()
        self.open_break_window()

    def end_rest(self):
        self.close_break_window()
        self.tracker.add_rest()
        toast("Rest over", "Great! You can get back to work now.")
        if self.beep_var.get():
            play_alarm()
        self._begin("work", self.interval_sec())

    def open_break_window(self):
        self.close_break_window()
        w = tk.Toplevel(self.root)
        w.title("Take a rest")
        w.attributes("-topmost", True)
        w.configure(bg=CARD)
        sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
        ww, wh = S(480), S(520)
        w.geometry(f"{ww}x{wh}+{(sw - ww) // 2}+{(sh - wh) // 2}")
        w.resizable(False, False)
        w.protocol("WM_DELETE_WINDOW", lambda: None)
        cv = tk.Canvas(w, width=S(54), height=S(54), bg=CARD, highlightthickness=0)
        cv.pack(pady=(S(28), 0))
        rrect(cv, 0, 0, S(54), S(54), S(16), fill=LIME, outline="")
        icon(cv, "eye", S(27), S(27), S(13), INK)
        tk.Label(w, text="Rest your eyes", font=f(22, True), fg=TEXT, bg=CARD).pack(pady=(S(12), 0))
        tk.Label(w, text="Look at something 20 feet away", font=f(11), fg=MUTED, bg=CARD).pack(pady=(S(4), 0))
        self.break_ring = Ring(w, size=260, thick=0.08, main_size=44, sub_size=10)
        self.break_ring.pack(fill="both", expand=True, padx=S(30), pady=S(16))
        tk.Label(w, text=TIPS[datetime.date.today().toordinal() % len(TIPS)], font=f(9), fg=MUTED, bg=CARD,
                 wraplength=S(400), justify="center").pack(pady=(0, S(22)))
        self.break_win = w
        w.deiconify()
        w.lift()

    def close_break_window(self):
        if self.break_win is not None:
            try:
                self.break_win.destroy()
            except tk.TclError:
                pass
            self.break_win = None
            self.break_ring = None

    def _paint_timer(self, remaining):
        frac = max(0.0, min(1.0, remaining / self.phase_total))
        txt = self.fmt(remaining)
        work = self.state == "work"
        ring_color = GREEN if work else ORANGE
        self.timer_ring.set(frac, txt, f"of {self.fmt(self.phase_total)}", ring_color)
        self.promo_time.config(text=txt)
        chip = "PAUSED" if self.paused else ("RUNNING" if work else "RESTING")
        card_color = "#d3d3d3" if self.paused else (LIME if work else YELLOW)
        self.timer_card.set(chip, txt, frac, ("Next rest in" if work else "Rest time left",
                                              f"Every {int(self.interval_sec() // 60)} min"),
                            f"{int(self.rest_sec())} s rest", card_color)
        if self.break_ring is not None:
            self.break_ring.set(frac, txt, "seconds left" if remaining < 60 else "", ORANGE)
        if self.tray is not None:
            tip = f"FocusRest - {'next rest in' if work else 'resting'} {txt}" + (" (paused)" if self.paused else "")
            if tip != self._last_tip:
                self._last_tip = tip
                try:
                    self.tray.title = tip
                except Exception:
                    pass

    def tick(self):
        while True:
            try:
                fn = self._q.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                pass
        if not self.paused:
            remaining = self.deadline - time.monotonic()
            self._paint_timer(remaining)
            if remaining <= 0:
                if self.state == "work":
                    self.start_rest()
                else:
                    self.end_rest()
        self.root.after(250, self.tick)


if __name__ == "__main__":
    if os.name == "nt":
        try:                                    # sharp text on high-DPI screens
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "FocusRest_SingleInstance")
        if ctypes.windll.kernel32.GetLastError() == 183:      # already running (look in the tray)
            sys.exit(0)
    tracker = UsageTracker()
    tracker.start()
    root = tk.Tk()
    root.withdraw()                              # start hidden: the app lives in the tray
    App(root, tracker, show="--show" in sys.argv)
    root.mainloop()
