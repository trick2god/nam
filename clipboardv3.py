"""
Clipboard Snippets popup (Windows, no third-party packages).

Press Ctrl+V anywhere and a popup opens beside the mouse:
  1. Pick a text file from the ./clipboard folder.
  2. Pick a section. A section starts with a line ending in ":".
  3. Each line in that section is pasted into the window you were in,
     with Enter pressed after every line.

        follow up 1:            <- section header (shown in the popup)
        line one                <- pasted, then Enter
        line two                <- pasted, then Enter

Header types (colons at the end of the line):
  name:      sends every line below it, one message per line
  name::     opens a menu; every line below it is its own choice
  name:::    opens a searchable menu; blocks separated by blank lines are the choices

Send modes (chip in the popup header, or Tab; remembered in clipboard_settings.json):
  Auto send   click = send straight away
  Confirm     click = preview showing the target window, then Send / Edit / Cancel
  Edit first  click = open the editor
  Edit button or right-click on a row always opens the editor.
  "Send as one message" = all lines in ONE chat message (Shift+Enter between lines).

Placeholders in the .txt files: {name} {greeting} {date} {day} {time} {clipboard}

Saving new snippets: copy text, then press Ctrl+C again quickly (Ctrl+C twice), or pick
"+ Save copied text" in the menu. Choose freestyle, an existing file (new section / add to /
replace a section) or a new file, and the type (:  ::  :::). A preview shows exactly what
will be written; a backup of the old file goes to clipboard/.backup/. The "?" chip explains it all.

Running it again closes the copy that is already running and takes over.

Keys in the popup:
  0          paste the normal clipboard (the old Ctrl+V)
  1-9        choose an item          Up/Down + Enter  move and choose
  Backspace  go back                 Esc              close
  Esc while lines are being sent stops the sending.

Run with:  pythonw clipboard.py   (no console)  or  python clipboard.py
"""

import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path

# ----------------------------------------------------------------- settings
SNIPPET_DIR = Path(__file__).resolve().parent / "clipboard"
# "type"  = types each line directly (reliable: keystrokes stay in order, no clipboard race)
# "paste" = puts each line on the clipboard + Ctrl+V. Apps like Teams read the clipboard
#           late, so a fast next line can overwrite it (lost or doubled lines). Use PASTE_DELAY.
MODE = "type"
STEP_DELAY = 0.15         # seconds between lines in "type" mode
BEFORE_ENTER = 0.35       # wait after a line is typed/pasted so the app has it in the box before Enter
AFTER_ENTER = 0.7         # wait after Enter so the app can send the message and clear the box
# Chat apps sometimes ignore an Enter right after sending the previous message. In these apps a
# second "confirm" Enter is pressed: if the first one worked the box is empty and the second
# does nothing; if the first was ignored, the second sends the line.
CHAT_APPS = {"ms-teams.exe", "teams.exe", "slack.exe", "whatsapp.exe", "telegram.exe",
             "discord.exe", "msedge.exe", "chrome.exe"}
CONFIRM_DELAY = 0.6       # wait before the confirm Enter
PASTE_DELAY = 0.8         # seconds to wait after each Ctrl+V in "paste" mode before touching the clipboard again
RESTORE_CLIPBOARD = True  # put your original clipboard text back afterwards
POPUP_WIDTH = 360         # px at 100% scaling
SAVE_WIDTH = 440          # the "Save copied text" form is a bit wider
COPY_TWICE_WINDOW = 0.6   # Ctrl+C twice within this many seconds opens "Save copied text"
FREESTYLE_FILE = SNIPPET_DIR / "freestyle.txt"
SETTINGS_FILE = Path(__file__).resolve().parent / "clipboard_settings.json"   # remembers your choices
MUTEX_NAME = "Local\\ClipboardSnippetsPopup"
QUIT_EVENT_NAME = "Local\\ClipboardSnippetsQuit"   # a new copy uses this to close the old one

# What clicking a snippet does (switch with the chip in the popup header, or Tab)
SEND_MODES = {
    "auto":    ("Auto send", "Click sends straight away"),
    "confirm": ("Confirm",   "Click shows a preview; you press Send"),
    "edit":    ("Edit first", "Click opens the editor"),
}
DEFAULT_SETTINGS = {"mode": "auto", "one_message": False, "last_save": "freestyle.txt"}


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    if s.get("mode") not in SEND_MODES:
        s["mode"] = "auto"
    return s


def save_settings(s):
    try:
        SETTINGS_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")
    except OSError:
        pass


# Placeholders you can put in the .txt files, filled in when sending:
#   {name} first word of the file name (elena free.txt -> Elena)   {greeting} Good morning/afternoon/evening
#   {date} Nov 4   {day} Wednesday   {time} 2:40 PM   {clipboard} first line of what you copied
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def fill_placeholders(lines, file_stem=""):
    now = datetime.now()
    clip = (clip_get() or "").strip().splitlines()
    values = {
        "name": file_stem.split()[0].capitalize() if file_stem.strip() else "",
        "greeting": "Good morning" if now.hour < 12 else "Good afternoon" if now.hour < 18 else "Good evening",
        "date": f"{now:%b} {now.day}",
        "day": f"{now:%A}",
        "time": now.strftime("%I:%M %p").lstrip("0"),
        "clipboard": clip[0].strip() if clip else "",
    }
    return [_PLACEHOLDER.sub(lambda m: values.get(m.group(1).lower(), m.group(0)), l) for l in lines]

# ----------------------------------------------------------------- win32
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_SYSKEYDOWN, WM_QUIT = 0x0100, 0x0104, 0x0012
VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
VK_LWIN, VK_RWIN, VK_ESCAPE, VK_RETURN, VK_V = 0x5B, 0x5C, 0x1B, 0x0D, 0x56
WM_KEYUP, WM_SYSKEYUP, VK_C = 0x0101, 0x0105, 0x43
INPUT_KEYBOARD, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 1, 0x0002, 0x0004
CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
GA_ROOT = 2
MAGIC = 0x5C1B0A7D        # tags our own synthetic keys so the hook ignores them


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)

user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
user32.SetWindowsHookExW.restype = wt.HHOOK
user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [wt.HHOOK]
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = wt.SHORT
user32.GetForegroundWindow.restype = wt.HWND
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.BringWindowToTop.argtypes = [wt.HWND]
user32.IsWindow.argtypes = [wt.HWND]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetAncestor.restype = wt.HWND
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
user32.MonitorFromPoint.restype = wt.HMONITOR
user32.GetMonitorInfoW.argtypes = [wt.HMONITOR, ctypes.POINTER(MONITORINFO)]
user32.OpenClipboard.argtypes = [wt.HWND]
user32.GetClipboardData.argtypes = [wt.UINT]
user32.GetClipboardData.restype = wt.HANDLE
user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
user32.SetClipboardData.restype = wt.HANDLE
user32.IsClipboardFormatAvailable.argtypes = [wt.UINT]
kernel32.GetCurrentThreadId.restype = wt.DWORD
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wt.HGLOBAL
kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
kernel32.GlobalFree.argtypes = [wt.HGLOBAL]
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
kernel32.CreateMutexW.restype = wt.HANDLE


kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]


def window_exe(hwnd):
    """Lower-case exe name of the program that owns a window, e.g. 'ms-teams.exe'."""
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name.lower()
        return ""
    finally:
        kernel32.CloseHandle(h)


kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
kernel32.CreateEventW.restype = wt.HANDLE
kernel32.SetEvent.argtypes = [wt.HANDLE]
kernel32.ResetEvent.argtypes = [wt.HANDLE]
kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
kernel32.WaitForSingleObject.restype = wt.DWORD
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]


def window_title(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


def is_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


user32.MapVirtualKeyW.argtypes = [wt.UINT, wt.UINT]
user32.MapVirtualKeyW.restype = wt.UINT


def _key(vk=0, scan=0, flags=0):
    if vk and not scan:                       # real hardware scan code, so apps see a genuine key
        scan = user32.MapVirtualKeyW(vk, 0)   # MAPVK_VK_TO_VSC
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=MAGIC)
    return inp


def _send(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def press_ctrl_v():
    _send(_key(VK_CONTROL), _key(VK_V), _key(VK_V, flags=KEYEVENTF_KEYUP),
          _key(VK_CONTROL, flags=KEYEVENTF_KEYUP))


def press_shift_enter():
    """New line inside the same chat message."""
    _send(_key(VK_SHIFT), _key(VK_RETURN))
    time.sleep(0.04)
    _send(_key(VK_RETURN, flags=KEYEVENTF_KEYUP), _key(VK_SHIFT, flags=KEYEVENTF_KEYUP))


def press_enter():
    _send(_key(VK_RETURN))
    time.sleep(0.04)                          # a real-looking key press, not an instant down/up
    _send(_key(VK_RETURN, flags=KEYEVENTF_KEYUP))


def type_text(text):
    raw = text.encode("utf-16-le")
    units = [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]
    events = []
    for u in units:
        events.append(_key(scan=u, flags=KEYEVENTF_UNICODE))
        events.append(_key(scan=u, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    if events:
        _send(*events)


def wait_modifiers_released(timeout=3.0):
    end = time.time() + timeout
    while time.time() < end and any(is_down(k) for k in (VK_CONTROL, VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN)):
        time.sleep(0.02)


def force_foreground(hwnd):
    if not hwnd or not user32.IsWindow(hwnd):
        return
    cur = kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    attached = bool(fg_tid and fg_tid != cur and user32.AttachThreadInput(cur, fg_tid, True))
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    if attached:
        user32.AttachThreadInput(cur, fg_tid, False)


def _open_clipboard():
    for _ in range(20):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def clip_get():
    if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT) or not _open_clipboard():
        return None
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        p = kernel32.GlobalLock(h) if h else None
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def clip_set(text):
    buf = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(buf)
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
    p = kernel32.GlobalLock(h)
    ctypes.memmove(p, buf, size)
    kernel32.GlobalUnlock(h)
    if not _open_clipboard():
        kernel32.GlobalFree(h)
        return False
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            kernel32.GlobalFree(h)
            return False
        return True
    finally:
        user32.CloseClipboard()


def work_area_at(x, y):
    mon = user32.MonitorFromPoint(wt.POINT(x, y), 2)  # MONITOR_DEFAULTTONEAREST
    mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    user32.GetMonitorInfoW(mon, ctypes.byref(mi))
    r = mi.rcWork
    return r.left, r.top, r.right, r.bottom


# ----------------------------------------------------------------- keyboard hook
class KeyHook(threading.Thread):
    """Global low-level hook: swallows Ctrl+V and reports it; watches Esc while sending;
    reports Ctrl+C pressed twice quickly (the copy itself still happens normally)."""

    def __init__(self, on_ctrl_v, on_escape, on_copy_twice=None):
        super().__init__(daemon=True)
        self.on_ctrl_v, self.on_escape, self.on_copy_twice = on_ctrl_v, on_escape, on_copy_twice
        self.tid = None
        self._last_c = 0.0
        self._c_held = False
        self._proc = HOOKPROC(self._callback)   # keep a reference or it gets garbage-collected

    def run(self):
        self.tid = kernel32.GetCurrentThreadId()
        hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, kernel32.GetModuleHandleW(None), 0)
        if not hook:
            print("Could not install keyboard hook, error", ctypes.get_last_error())
            return
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnhookWindowsHookEx(hook)

    def stop(self):
        if self.tid:
            user32.PostThreadMessageW(self.tid, WM_QUIT, 0, 0)

    def _callback(self, n_code, w_param, l_param):
        if n_code == 0:
            kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if kb.dwExtraInfo != MAGIC:
                if w_param in (WM_KEYUP, WM_SYSKEYUP) and kb.vkCode == VK_C:
                    self._c_held = False
                elif w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    plain_ctrl = is_down(VK_CONTROL) and not any(
                        is_down(k) for k in (VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN))
                    if kb.vkCode == VK_ESCAPE:
                        self.on_escape()
                    elif kb.vkCode == VK_V and plain_ctrl:
                        self.on_ctrl_v(user32.GetForegroundWindow())
                        return 1   # swallow the key, the popup takes over
                    elif kb.vkCode == VK_C and plain_ctrl and not self._c_held and self.on_copy_twice:
                        self._c_held = True          # ignore key-repeat while C is held
                        now = time.time()
                        if now - self._last_c <= COPY_TWICE_WINDOW:
                            self._last_c = 0.0
                            self.on_copy_twice(user32.GetForegroundWindow())
                        else:
                            self._last_c = now
                        # not swallowed: the app still copies normally
        return user32.CallNextHookEx(None, n_code, w_param, l_param)


# ----------------------------------------------------------------- snippet files
def read_text(path):
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def list_files():
    if not SNIPPET_DIR.is_dir():
        return []
    return sorted(SNIPPET_DIR.glob("*.txt"), key=lambda p: p.stem.lower())


def split_blocks(raw_lines):
    """Groups lines into blocks separated by one or more blank lines."""
    blocks, current = [], []
    for raw in raw_lines:
        line = raw.strip()
        if line:
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


SECTION_KINDS = {1: "all", 2: "choices"}   # 3+ colons = "blocks"


def parse_sections(path):
    """Headers are lines ending in colons:
         name:    send all the lines below it (each line = one message)
         name::   a menu - every line below it is its own choice
         name:::  a freestyle menu - blocks separated by blank lines, each block is one choice
    """
    sections, current = [], None
    for raw in read_text(path).splitlines():
        line = raw.strip()
        if line.endswith(":"):
            colons = len(line) - len(line.rstrip(":"))
            current = {"title": line.rstrip(":").strip() or line,
                       "kind": SECTION_KINDS.get(colons, "blocks"), "raw": []}
            sections.append(current)
            continue
        if current is None:
            if not line:
                continue
            current = {"title": "(no header)", "kind": "all", "raw": []}   # text before the first header
            sections.append(current)
        current["raw"].append(raw)
    for sec in sections:
        raw = sec.pop("raw")
        sec["lines"] = [l.strip() for l in raw if l.strip()]
        if sec["kind"] == "choices":
            sec["options"] = [{"title": l, "lines": [l]} for l in sec["lines"]]
        elif sec["kind"] == "blocks":
            sec["options"] = [{"title": b[0], "lines": b} for b in split_blocks(raw)]
    return sections


def is_freestyle(path):
    return path.stem.lower().startswith("freestyle")


def parse_blocks(path):
    """Freestyle: blocks are separated by one or more blank lines; each block is one option."""
    return [{"title": b[0], "lines": b} for b in split_blocks(read_text(path).splitlines())]


def load_options(path):
    """Returns (options, freestyle). Freestyle = named freestyle*.txt, or a file with no 'Header:' lines."""
    if is_freestyle(path):
        return parse_blocks(path), True
    sections = parse_sections(path)
    if len(sections) == 1 and sections[0]["title"] == "(no header)":
        return parse_blocks(path), True
    return sections, False


# ----------------------------------------------------------------- saving new snippets
HEADER_COLONS = {"all": ":", "choices": "::", "blocks": ":::"}
KIND_HELP = {
    "all":     "name:    picking it sends EVERY line below it, one message per line.",
    "choices": "name::   opens a menu; EACH LINE is its own choice (sends just that line).",
    "blocks":  "name:::  opens a searchable menu; BLANK LINES separate the snippets.",
    "none":    "No header: the whole file works like freestyle (blank lines separate snippets).",
}


def safe_filename(name):
    name = "".join(ch for ch in name if ch not in '<>:"/\\|?*').strip().rstrip(".")
    return name[:-4] if name.lower().endswith(".txt") else name


def clean_lines(text, kind):
    """Copied text -> lines for the file. ':'/'::' drop blank lines; ':::'/freestyle keep one between blocks."""
    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if kind in ("all", "choices"):
        return [l.strip() for l in raw if l.strip()]
    out = []
    for block in split_blocks(raw):
        if out:
            out.append("")
        out.extend(block)
    return out


def file_lines(path):
    if not path.exists():
        return []
    lines = read_text(path).replace("\r\n", "\n").split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def header_info(line):
    """('title', kind) for a header line, else None."""
    s = line.strip()
    if not s.endswith(":"):
        return None
    colons = len(s) - len(s.rstrip(":"))
    return s.rstrip(":").strip() or s, SECTION_KINDS.get(colons, "blocks")


def section_ranges(lines):
    """[(title, kind, header_index, end_index)] - end excludes trailing blank lines."""
    heads = [i for i, l in enumerate(lines) if header_info(l)]
    out = []
    for n, i in enumerate(heads):
        end = heads[n + 1] if n + 1 < len(heads) else len(lines)
        while end > i + 1 and not lines[end - 1].strip():
            end -= 1
        title, kind = header_info(lines[i])
        out.append((title, kind, i, end))
    return out


def is_freestyle_file(path):
    if is_freestyle(path):
        return True
    return path.exists() and not section_ranges(file_lines(path))


def write_snippet_file(path, lines):
    """Writes the file (CRLF, UTF-8) after keeping a backup in clipboard/.backup/."""
    if path.exists():
        backup_dir = SNIPPET_DIR / ".backup"
        backup_dir.mkdir(exist_ok=True)
        (backup_dir / f"{path.name}.bak").write_text(read_text(path), encoding="utf-8")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\r\n")


# ----------------------------------------------------------------- popup
BG, ROW, HOVER, BORDER = "#1e2230", "#1e2230", "#323a52", "#5b8def"
FG, SUB, BADGE_BG, DIM = "#eef1f8", "#8f98ad", "#2c3349", "#5c6479"
WARN = "#ff8a80"
FONT = "Segoe UI"


class Popup:
    def __init__(self):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BORDER)
        self.root.report_callback_exception = self._on_error
        self.scale = self.root.winfo_fpixels("1i") / 96.0

        self.events = queue.Queue()
        self.visible = False
        self.busy = False
        self.abort = threading.Event()
        self.target = None
        self.items, self.rows, self.sel = [], [], 0
        self.back_action = None
        self.hwnd = None
        self.search_entry = None
        self.editor = None
        self._bs_was_empty = False
        self.width = POPUP_WIDTH
        self.save = None
        self.current_stem = ""
        self.settings = load_settings()
        self.quit_event = kernel32.CreateEventW(None, True, False, QUIT_EVENT_NAME)

        outer = tk.Frame(self.root, bg=BG)
        outer.pack(fill="both", expand=True, padx=1, pady=1)

        head = tk.Frame(outer, bg=BG)
        head.pack(fill="x", padx=10, pady=(8, 4))
        self.back_lbl = tk.Label(head, text="‹", bg=BG, fg=FG, font=(FONT, 14, "bold"), cursor="hand2")
        self.back_lbl.bind("<Button-1>", lambda e: self.go_back())
        self.title_lbl = tk.Label(head, text="", bg=BG, fg=FG, font=(FONT, 11, "bold"), anchor="w")
        self.title_lbl.pack(side="left", fill="x", expand=True)
        close = tk.Label(head, text="✕", bg=BG, fg=SUB, font=(FONT, 10), cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self.hide())
        self.mode_lbl = tk.Label(head, text="", bg=BADGE_BG, fg=FG, font=(FONT, 8, "bold"),
                                 cursor="hand2", padx=8, pady=2)
        self.mode_lbl.pack(side="right", padx=(0, 10))
        self.mode_lbl.bind("<Button-1>", lambda e: self.cycle_mode())
        self._update_mode_chip()
        help_lbl = tk.Label(head, text="?", bg=BADGE_BG, fg=FG, font=(FONT, 8, "bold"),
                            cursor="hand2", padx=7, pady=2)
        help_lbl.pack(side="right", padx=(0, 6))
        help_lbl.bind("<Button-1>", lambda e: self.show_help())

        self.body = tk.Frame(outer, bg=BG)
        self.body.pack(fill="both", expand=True, padx=6)

        foot = tk.Frame(outer, bg=BG)
        foot.pack(fill="x", padx=10, pady=(4, 8))
        self.hint = tk.Label(foot, text="", bg=BG, fg=DIM, font=(FONT, 8), anchor="w")
        self.hint.pack(side="left")
        quit_lbl = tk.Label(foot, text="Quit", bg=BG, fg=DIM, font=(FONT, 8, "underline"), cursor="hand2")
        quit_lbl.pack(side="right")
        quit_lbl.bind("<Button-1>", lambda e: self.quit())

        r = self.root
        r.bind("<Escape>", lambda e: self.hide())
        r.bind("<BackSpace>", lambda e: self.go_back(key="BackSpace"))
        r.bind("<Left>", lambda e: self.go_back(key="Left"))
        r.bind("<Tab>", lambda e: None if (self._typing() and not self._in_search())
               else (self.cycle_mode(), "break")[1])
        r.bind("<Up>", lambda e: self.move(-1))
        r.bind("<Down>", lambda e: self.move(1))
        r.bind("<Return>", lambda e: self.activate(self.sel))
        r.bind("<Right>", lambda e: None if self._in_search() else self.activate(self.sel))
        r.bind("<Key>", self.on_key)
        r.bind("<FocusOut>", lambda e: r.after(250, self._check_focus))

        self.hook = KeyHook(self._from_hook_ctrl_v, self._from_hook_escape, self._from_hook_copy_twice)
        self.hook.start()
        r.after(30, self._poll)

    # ---- thread-safe entry points (called from the hook thread)
    def _from_hook_ctrl_v(self, hwnd):
        self.events.put(("show", hwnd))

    def _from_hook_copy_twice(self, hwnd):
        self.events.put(("save", hwnd))

    def _from_hook_escape(self):
        if self.busy:
            self.abort.set()

    def _poll(self):
        if kernel32.WaitForSingleObject(self.quit_event, 0) == 0:   # a newer copy asked us to close
            self.quit()
            return
        try:
            while True:
                kind, arg = self.events.get_nowait()
                if kind == "show":
                    self.show(arg)
                elif kind == "save":   # give the app a moment to put the copied text on the clipboard
                    self.root.after(300, lambda h=arg: self.open_save(h))
        except queue.Empty:
            pass
        self.root.after(30, self._poll)

    # ---- window handling
    def show(self, target_hwnd):
        if self.busy:
            return
        if self.visible:
            if target_hwnd == self.hwnd and self._typing():
                self.root.focus_get().event_generate("<<Paste>>")   # normal paste inside our text boxes
                return
            self.normal_paste()               # Ctrl+V again while open = normal paste
            return
        self._open(target_hwnd, self.show_files)

    def open_save(self, hwnd):
        """Ctrl+C twice: offer to save what was just copied."""
        if self.busy or (self.visible and hwnd == self.hwnd):
            return                            # copying inside the popup itself
        text = clip_get()
        if not text or not text.strip():
            return
        if self.visible:
            self.show_save(text)
        else:
            self._open(hwnd, lambda: self.show_save(text))

    def _open(self, target_hwnd, screen):
        self.target = target_hwnd
        screen()
        self._place_at_cursor()
        self.root.deiconify()
        self.root.update_idletasks()
        self.hwnd = user32.GetAncestor(self.root.winfo_id(), GA_ROOT)
        force_foreground(self.hwnd)
        self.root.focus_force()
        self.visible = True

    def hide(self):
        self.root.withdraw()
        self.visible = False

    def _check_focus(self):
        if self.visible and self.root.focus_get() is None:
            self.hide()

    def _place_at_cursor(self):
        self.root.update_idletasks()
        w = int(self.width * self.scale)
        h = self.root.winfo_reqheight()
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        left, top, right, bottom = work_area_at(pt.x, pt.y)
        x = min(max(pt.x + 12, left), right - w)
        y = min(max(pt.y + 12, top), bottom - h)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _resize(self):
        if self.visible:
            self.root.update_idletasks()
            w = int(self.width * self.scale)
            h = self.root.winfo_reqheight()
            x, y = self.root.winfo_x(), self.root.winfo_y()
            left, top, right, bottom = work_area_at(x, y)
            self.root.geometry(f"{w}x{h}+{min(x, right - w)}+{max(top, min(y, bottom - h))}")

    def quit(self):
        self.hook.stop()
        self.root.destroy()

    def _on_error(self, exc, val, tb):
        if exc is KeyboardInterrupt:
            self.quit()
        else:
            import traceback
            traceback.print_exception(exc, val, tb)

    # ---- list rendering
    def render(self, title, items, back=None, hint="", search=False):
        """items: list of dicts {key, label, sub, action, [text]}"""
        for child in self.body.winfo_children():
            child.destroy()
        self.title_lbl.config(text=title)
        self.width = POPUP_WIDTH
        self.back_action = back
        if back:
            self.back_lbl.pack(side="left", before=self.title_lbl, padx=(0, 6))
        else:
            self.back_lbl.pack_forget()
        self.hint.config(text=hint)
        self.all_items = items
        self.search_entry = None
        self.editor = None
        self._bs_was_empty = False
        if search:
            box = tk.Frame(self.body, bg=BADGE_BG)
            box.pack(fill="x", padx=4, pady=(0, 4))
            tk.Label(box, text="\U0001F50D", bg=BADGE_BG, fg=SUB, font=(FONT, 9)).pack(side="left", padx=(6, 2))
            var = tk.StringVar()
            entry = tk.Entry(box, textvariable=var, bg=BADGE_BG, fg=FG, insertbackground=FG,
                             relief="flat", font=(FONT, 10))
            entry.pack(side="left", fill="x", expand=True, ipady=4, padx=(0, 6))
            var.trace_add("write", lambda *a: self._filter(var.get()))
            # runs before the Entry deletes the character: remember if it was already empty
            entry.bind("<BackSpace>", lambda e: setattr(self, "_bs_was_empty", not entry.get()))
            self.search_entry = entry
            entry.focus_set()
        self.list_frame = tk.Frame(self.body, bg=BG)
        self.list_frame.pack(fill="both", expand=True)
        self._render_rows(items)

    def _filter(self, query):
        words = query.lower().split()
        shown = [it for it in self.all_items
                 if all(w in it.get("text", it["label"]).lower() for w in words)]
        self._render_rows(shown)

    def _in_search(self):
        return self.search_entry is not None and self.root.focus_get() is self.search_entry

    def _in_editor(self):
        return self.editor is not None and self.root.focus_get() is self.editor

    def _typing(self):
        """True when the cursor is in any text box (search, editor, form fields)."""
        return isinstance(self.root.focus_get(), (tk.Entry, tk.Text))

    def _in_form_entry(self):
        w = self.root.focus_get()
        return isinstance(w, tk.Entry) and w is not self.search_entry

    def _render_rows(self, items, limit=12):
        for child in self.list_frame.winfo_children():
            child.destroy()
        self.items, self.rows = items[:limit], []
        wrap = int((POPUP_WIDTH - 70) * self.scale)

        if not items:
            tk.Label(self.list_frame, text="Nothing here.", bg=BG, fg=SUB, font=(FONT, 9)).pack(pady=10)
        for i, item in enumerate(self.items):
            row = tk.Frame(self.list_frame, bg=ROW, cursor="hand2")
            row.pack(fill="x", pady=1)
            if item.get("edit"):
                edit = tk.Label(row, text="Edit", bg=BADGE_BG, fg=BORDER, font=(FONT, 8, "bold"),
                                cursor="hand2", padx=6, pady=2)
                edit.pack(side="left", padx=(6, 0), pady=6, anchor="n")
                edit.bind("<Button-1>", lambda e, f=item["edit"]: f())
                edit.bind("<Enter>", lambda e, w=edit: w.config(bg=HOVER, fg=FG))
                edit.bind("<Leave>", lambda e, w=edit: w.config(bg=BADGE_BG, fg=BORDER))
            badge = tk.Label(row, text=item.get("key", ""), width=2, bg=BADGE_BG, fg=FG, font=(FONT, 9, "bold"))
            badge.pack(side="left", padx=(6, 8), pady=6, anchor="n")
            text = tk.Frame(row, bg=ROW)
            text.pack(side="left", fill="x", expand=True, pady=4)
            main = tk.Label(text, text=item["label"], bg=ROW, fg=FG, font=(FONT, 10), anchor="w", justify="left")
            main.pack(fill="x")
            widgets = [row, text, main]
            if item.get("sub"):
                sub = tk.Label(text, text=item["sub"], bg=ROW, fg=SUB, font=(FONT, 8), anchor="w",
                               justify="left", wraplength=wrap)
                sub.pack(fill="x")
                widgets.append(sub)
            for w in widgets + [badge]:
                w.bind("<Button-1>", lambda e, i=i: self.activate(i))
                w.bind("<Enter>", lambda e, i=i: self.select(i))
                if item.get("edit"):   # right-click = edit, whatever the mode
                    w.bind("<Button-3>", lambda e, f=item["edit"]: f())
            self.rows.append(widgets)
        if len(items) > limit:
            tk.Label(self.list_frame, text=f"+ {len(items) - limit} more, type to narrow down",
                     bg=BG, fg=DIM, font=(FONT, 8)).pack(pady=(2, 0))
        self.sel = 0
        self.select(self._first_selectable())
        self._resize()

    def _first_selectable(self):
        for i, it in enumerate(self.items):
            if it.get("key") not in ("0",):
                return i
        return 0

    def select(self, i):
        if not self.rows:
            return
        self.sel = max(0, min(i, len(self.rows) - 1))
        for idx, widgets in enumerate(self.rows):
            color = HOVER if idx == self.sel else ROW
            for w in widgets:
                w.config(bg=color)

    def move(self, d):
        self.select(self.sel + d)

    def activate(self, i):
        if 0 <= i < len(self.items):
            self.items[i]["action"]()

    def on_key(self, e):
        if self._typing():             # typing goes into the text box, not the shortcuts
            return
        for i, it in enumerate(self.items):
            if e.char and it.get("key") == e.char:
                self.activate(i)
                return

    def go_back(self, key=None):
        if key and (self._in_editor() or self._in_form_entry()
                    or isinstance(self.root.focus_get(), tk.Text)):
            return                     # Backspace/Left edit the text in the box
        if key and self._in_search():
            # Left moves the cursor; Backspace only goes back if the box was ALREADY empty
            if key == "Left" or not self._bs_was_empty:
                return
        self._bs_was_empty = False
        if self.back_action:
            self.back_action()

    # ---- screens
    def show_files(self):
        items = [{"key": "0", "label": "Paste clipboard normally", "sub": self._clip_preview(),
                  "action": self.normal_paste}]
        for n, path in enumerate(list_files(), start=1):
            try:
                secs, free = load_options(path)
                sub = (f"freestyle • {len(secs)} snippet(s), searchable" if free
                       else f"{len(secs)} section(s): " + ", ".join(s["title"] for s in secs))
            except OSError as exc:
                secs, sub = [], f"cannot read: {exc}"
            items.append({"key": str(n) if n < 10 else "", "label": path.stem, "sub": sub,
                          "action": lambda p=path: self.show_sections(p)})
        items.append({"key": "+", "label": "＋ Save copied text as a snippet",
                      "sub": "tip: press Ctrl+C twice quickly anywhere to open this",
                      "action": lambda: self.show_save(clip_get() or "")})
        items.append({"key": "?", "label": "?  How snippet files work",
                      "sub": "name:  name::  name:::  explained with examples",
                      "action": self.show_help})
        hint = "0-9 / + / ? • ↑↓ Enter • Tab mode • Esc close"
        if len(items) == 3:
            items[0]["sub"] = f"No .txt files yet in {SNIPPET_DIR} - use ＋ to create one"
        self.render("Clipboard snippets", items, hint=hint)

    # ---- send modes
    def _update_mode_chip(self):
        label, _ = SEND_MODES[self.settings["mode"]]
        self.mode_lbl.config(text=f"▶ {label}")

    def cycle_mode(self):
        keys = list(SEND_MODES)
        self.settings["mode"] = keys[(keys.index(self.settings["mode"]) + 1) % len(keys)]
        save_settings(self.settings)
        self._update_mode_chip()
        self.hint.config(text=f"Mode: {SEND_MODES[self.settings['mode']][1]} • Tab to switch")

    def choose(self, title, lines, back):
        """What a click on a snippet does, depending on the mode chip."""
        mode = self.settings["mode"]
        if mode == "confirm":
            self.show_confirm(title, lines, back)
        elif mode == "edit":
            self.show_editor(title, lines, back)
        else:
            self.send_lines(lines)

    def _one_message_toggle(self, parent, on_change=None):
        var = tk.BooleanVar(value=self.settings["one_message"])

        def changed():
            self.settings["one_message"] = var.get()
            save_settings(self.settings)
            if on_change:
                on_change()
        tk.Checkbutton(parent, text="Send as one message (Shift+Enter between lines)", variable=var,
                       command=changed, bg=BG, fg=SUB, activebackground=BG, activeforeground=FG,
                       selectcolor=BADGE_BG, font=(FONT, 8), anchor="w", bd=0,
                       highlightthickness=0).pack(fill="x", padx=2, pady=(0, 4))

    def _target_label(self):
        exe = window_exe(self.target) if self.target else ""
        title = window_title(self.target) if self.target else ""
        title = title if len(title) <= 40 else title[:39] + "…"
        return f"→ {title or exe or 'previous window'}"

    def show_confirm(self, title, lines, back):
        filled = fill_placeholders(lines, self.current_stem)

        def summary():
            n = len(filled)
            return f"1 message ({n} lines)" if self.settings["one_message"] and n > 1 else f"{n} message(s)"

        items = [
            {"key": "1", "label": "Send ⏎", "sub": "", "action": lambda: self.send_lines(lines)},
            {"key": "2", "label": "Edit", "sub": "change the text first",
             "action": lambda: self.show_editor(title, lines, back)},
            {"key": "3", "label": "Cancel", "sub": "", "action": back},
        ]
        self.render(f"Send? • {title}", items, back=back, hint="Enter send • 2 edit • Esc close")
        preview = tk.Frame(self.body, bg=BADGE_BG)
        preview.pack(fill="x", padx=4, pady=(0, 4), before=self.list_frame)
        tk.Label(preview, text=self._target_label(), bg=BADGE_BG, fg=BORDER, font=(FONT, 8, "bold"),
                 anchor="w").pack(fill="x", padx=8, pady=(6, 2))
        wrap = int((POPUP_WIDTH - 50) * self.scale)
        for i, line in enumerate(filled, start=1):
            tk.Label(preview, text=f"{i}.  {line}", bg=BADGE_BG, fg=FG, font=(FONT, 9), anchor="w",
                     justify="left", wraplength=wrap).pack(fill="x", padx=8)
        count = tk.Label(preview, text=summary(), bg=BADGE_BG, fg=SUB, font=(FONT, 8), anchor="w")
        count.pack(fill="x", padx=8, pady=(2, 6))
        opts = tk.Frame(self.body, bg=BG)
        opts.pack(fill="x", padx=4, before=self.list_frame)
        self._one_message_toggle(opts, on_change=lambda: count.config(text=summary()))
        self._resize()

    def show_sections(self, path):
        self.current_stem = path.stem
        options, freestyle = load_options(path)
        back = lambda: self.show_sections(path)
        items = []
        for n, sec in enumerate(options, start=1):
            key = "" if freestyle else (str(n) if n < 10 else "")
            kind = sec.get("kind", "all")
            if kind in ("choices", "blocks"):      # 'name::' or 'name:::' -> opens another menu
                opts = sec["options"]
                what = "choice(s)" if kind == "choices" else "snippet(s), searchable"
                items.append({"key": key, "label": f"{sec['title']}  ›",
                              "sub": f"menu • {len(opts)} {what}",
                              "text": " ".join([sec["title"]] + sec["lines"]),
                              "action": lambda s=sec: self.show_menu(path, s)})
            else:
                items.append(self._snippet_item(sec, key, freestyle, back))
        hint = ("Type to search • Enter pick • right-click edit • Tab mode" if freestyle
                else "Click pick • right-click edit • Tab mode • Backspace back")
        self.render(path.stem, items, back=self.show_files, hint=hint, search=freestyle)

    def show_menu(self, path, sec):
        """Submenu for 'name::' (one choice per line) and 'name:::' (blocks, searchable)."""
        blocks = sec["kind"] == "blocks"
        opts = sec["options"]
        search = blocks or len(opts) > 9
        back = lambda: self.show_menu(path, sec)
        items = [self._snippet_item(o, "" if search else str(n), blocks, back)
                 for n, o in enumerate(opts, start=1)]
        hint = ("Type to search • Enter pick • right-click edit" if search
                else "1-9 pick • right-click edit • Tab mode • Backspace back")
        self.render(f"{path.stem} › {sec['title']}", items,
                    back=lambda: self.show_sections(path), hint=hint, search=search)

    def _snippet_item(self, opt, key, block_style, back):
        """A row that sends its lines (respecting the Auto/Confirm/Edit mode)."""
        lines = opt["lines"]
        if block_style:   # label = first line, sub = the rest
            rest = lines[1:4]
            sub = (f"{len(lines)} line(s) • " + " ↵ ".join(rest) + (" …" if len(lines) > 4 else "")
                   if rest else "1 line")
        elif len(lines) == 1 and lines[0] == opt["title"]:
            sub = ""      # a single-line choice: the label already is the message
        else:
            preview = " ↵ ".join(lines[:3]) + (" …" if len(lines) > 3 else "")
            sub = f"{len(lines)} line(s) • {preview}" if lines else "(empty)"
        title = opt["title"]
        return {"key": key, "label": title, "sub": sub, "text": " ".join([title] + lines),
                "action": lambda: self.choose(title, lines, back=back),
                "edit": lambda: self.show_editor(title, lines, back=back)}

    def show_editor(self, title, lines, back):
        """Edit the lines before sending. Each non-blank line is sent, then Enter."""
        lines = fill_placeholders(lines, self.current_stem)
        self.render(f"Edit • {title}", [], back=back,
                    hint="Ctrl+Enter send • Esc close")
        for child in self.list_frame.winfo_children():
            child.destroy()
        tk.Label(self.list_frame, text=self._target_label(), bg=BG, fg=BORDER, font=(FONT, 8, "bold"),
                 anchor="w").pack(fill="x", padx=6, pady=(0, 2))
        box = tk.Frame(self.list_frame, bg=BADGE_BG)
        box.pack(fill="both", expand=True, padx=4, pady=(0, 6))
        editor = tk.Text(box, bg=BADGE_BG, fg=FG, insertbackground=FG, relief="flat", wrap="word",
                         font=(FONT, 10), height=min(max(len(lines) + 1, 4), 12), undo=True,
                         padx=6, pady=6, selectbackground=HOVER)
        editor.pack(fill="both", expand=True)
        editor.insert("1.0", "\n".join(lines))
        editor.edit_reset()
        self.editor = editor

        def send(_e=None):
            out = [l.strip() for l in editor.get("1.0", "end").splitlines() if l.strip()]
            if out:
                self.send_lines(out)
            return "break"

        self._one_message_toggle(self.list_frame)
        buttons = tk.Frame(self.list_frame, bg=BG)
        buttons.pack(fill="x", padx=4, pady=(0, 2))
        for label, cmd, color in (("Send ⏎", send, BORDER), ("Cancel", lambda e=None: back(), BADGE_BG)):
            b = tk.Label(buttons, text=label, bg=color, fg=FG, font=(FONT, 9, "bold"),
                         cursor="hand2", padx=12, pady=4)
            b.pack(side="right", padx=(6, 0))
            b.bind("<Button-1>", lambda e, c=cmd: c())
        editor.bind("<Control-Return>", send)
        editor.focus_set()
        editor.mark_set("insert", "end-1c")
        self._resize()

    # ---- small form widgets
    def _section(self, parent, text):
        tk.Label(parent, text=text, bg=BG, fg=SUB, font=(FONT, 8, "bold"), anchor="w").pack(
            fill="x", padx=6, pady=(8, 2))

    def _note(self, parent, text, color=None, mono=False):
        lbl = tk.Label(parent, text=text, bg=BG, fg=color or SUB, font=("Consolas", 8) if mono else (FONT, 8),
                       justify="left", anchor="w", wraplength=int((self.width - 40) * self.scale))
        lbl.pack(fill="x", padx=8, pady=(2, 0))
        return lbl

    def _chips(self, parent, options, current, on_pick, cols=3):
        frame = tk.Frame(parent, bg=BG)
        frame.pack(fill="x", padx=4)
        for n, (value, label) in enumerate(options):
            on = value == current
            b = tk.Label(frame, text=label, bg=BORDER if on else BADGE_BG, fg=FG,
                         font=(FONT, 8, "bold" if on else "normal"), padx=6, pady=4, cursor="hand2")
            b.grid(row=n // cols, column=n % cols, sticky="ew", padx=2, pady=2)
            b.bind("<Button-1>", lambda e, v=value: on_pick(v))
        for c in range(cols):
            frame.grid_columnconfigure(c, weight=1, uniform="chips")

    def _entry(self, parent, var, hint):
        box = tk.Frame(parent, bg=BADGE_BG)
        box.pack(fill="x", padx=6)
        e = tk.Entry(box, textvariable=var, bg=BADGE_BG, fg=FG, insertbackground=FG, relief="flat",
                     font=(FONT, 10))
        e.pack(fill="x", padx=6, ipady=3)
        e.bind("<Return>", lambda ev: (self.do_save(), "break")[1])
        tk.Label(parent, text=hint, bg=BG, fg=DIM, font=(FONT, 7), anchor="w").pack(fill="x", padx=8)
        return e

    def _buttons(self, parent, buttons):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", padx=4, pady=(8, 2))
        for label, cmd, color in buttons:
            b = tk.Label(row, text=label, bg=color, fg=FG, font=(FONT, 9, "bold"), cursor="hand2", padx=12, pady=4)
            b.pack(side="right", padx=(6, 0))
            b.bind("<Button-1>", lambda e, c=cmd: c())

    # ---- save copied text (Ctrl+C twice, or "+" in the menu)
    def show_save(self, text):
        last = SNIPPET_DIR / self.settings.get("last_save", FREESTYLE_FILE.name)
        target = last if last.exists() else FREESTYLE_FILE
        self.save = {"text": text.strip("\r\n"), "target": target, "how": "new", "kind": "all", "sec": 0,
                     "name": tk.StringVar(), "filename": tk.StringVar()}
        for var in (self.save["name"], self.save["filename"]):
            var.trace_add("write", lambda *a: self._save_preview())
        self._render_save()

    def _sync_save_text(self):
        box = self.save.get("box") if self.save else None
        if box is not None and box.winfo_exists():
            self.save["text"] = box.get("1.0", "end-1c")

    def _save_set(self, **changes):
        self._sync_save_text()
        self.save.update(changes)
        self._render_save()

    def _render_save(self):
        s = self.save
        self.render("Save copied text", [], back=self.show_files, hint="Enter save • Esc close • ? help")
        self.width = SAVE_WIDTH
        lf = self.list_frame
        for c in lf.winfo_children():
            c.destroy()
        entries = []

        self._section(lf, "1   THE TEXT   (you can still edit it)")
        boxf = tk.Frame(lf, bg=BADGE_BG)
        boxf.pack(fill="x", padx=6)
        box = tk.Text(boxf, bg=BADGE_BG, fg=FG, insertbackground=FG, relief="flat", wrap="word",
                      font=(FONT, 9), height=min(max(s["text"].count("\n") + 1, 3), 7), undo=True,
                      padx=6, pady=4, selectbackground=HOVER)
        box.pack(fill="x")
        box.insert("1.0", s["text"])
        box.edit_reset()
        box.bind("<KeyRelease>", lambda e: self._save_preview())
        box.bind("<Control-Return>", lambda e: (self.do_save(), "break")[1])
        s["box"] = self.editor = box

        self._section(lf, "2   SAVE TO")
        others = [p for p in list_files() if p != FREESTYLE_FILE]
        opts = [(FREESTYLE_FILE, "★ freestyle")] + [(p, p.stem) for p in others] + [("new", "＋ New file")]
        self._chips(lf, opts, s["target"], lambda v: self._save_set(target=v, sec=0))

        target = s["target"]
        kind_chips = [("all", ":   send all"), ("choices", "::   choices"), ("blocks", ":::   blocks")]
        if target == "new":
            self._section(lf, "File name")
            entries.append(self._entry(lf, s["filename"], "e.g.  maria   →  creates maria.txt, shown as 'maria' in the menu"))
            self._section(lf, "3   TYPE")
            self._chips(lf, kind_chips + [("none", "no header")], s["kind"], lambda v: self._save_set(kind=v), cols=4)
            self._note(lf, KIND_HELP[s["kind"]], mono=True)
            if s["kind"] != "none":
                self._section(lf, "Section name")
                entries.append(self._entry(lf, s["name"], "the title you click in the menu, e.g.  greetings"))
        elif is_freestyle_file(target):
            self._section(lf, "3   HOW")
            self._note(lf, f"Added at the end of {target.name} as a new snippet.\n"
                           "In freestyle files a blank line separates snippets, and you can search them.")
        else:
            ranges = section_ranges(file_lines(target))
            self._section(lf, "3   HOW")
            hows = [("new", "New section")]
            if ranges:
                hows += [("add", "Add to a section"), ("replace", "Replace a section")]
            how = s["how"] if ranges else "new"
            self._chips(lf, hows, how, lambda v: self._save_set(how=v))
            if how == "new":
                self._section(lf, "Section name")
                entries.append(self._entry(lf, s["name"], "the title you click in the menu, e.g.  greetings"))
                kind = s["kind"] if s["kind"] != "none" else "all"
                self._section(lf, "Type")
                self._chips(lf, kind_chips, kind, lambda v: self._save_set(kind=v))
                self._note(lf, KIND_HELP[kind], mono=True)
            else:
                sec = min(s["sec"], len(ranges) - 1)
                self._section(lf, "Which section")
                self._chips(lf, [(i, f"{t} {HEADER_COLONS[k]}") for i, (t, k, _, _) in enumerate(ranges)],
                            sec, lambda v: self._save_set(sec=v), cols=2)
                self._note(lf, KIND_HELP[ranges[sec][1]], mono=True)

        self._section(lf, "WILL BE WRITTEN TO THE FILE")
        s["preview"] = tk.Label(lf, text="", bg=BADGE_BG, fg=FG, font=("Consolas", 9), justify="left",
                                anchor="w", padx=8, pady=6, wraplength=int((SAVE_WIDTH - 50) * self.scale))
        s["preview"].pack(fill="x", padx=6)
        s["msg"] = self._note(lf, "")
        self._buttons(lf, [("Save ✓", self.do_save, BORDER), ("Cancel", self.show_files, BADGE_BG)])
        self._save_preview()
        empty = [e for e in entries if not e.get()]
        (empty[0] if empty else box).focus_set()
        if not empty:
            box.mark_set("insert", "end-1c")
        self._resize()

    def _plan(self):
        """Works out what will be written. Returns (plan, error)."""
        s = self.save
        text = s["text"]
        if not text.strip():
            return None, "Nothing to save: the text is empty."
        target = s["target"]

        def section_title():
            return s["name"].get().strip().rstrip(":").strip()

        if target == "new":
            fname = safe_filename(s["filename"].get())
            if not fname:
                return None, "Type a file name."
            path = SNIPPET_DIR / f"{fname}.txt"
            if path.exists():
                return None, f"{path.name} already exists: pick it under SAVE TO instead."
            if s["kind"] == "none":
                body = clean_lines(text, "blocks")
                return {"path": path, "lines": body, "excerpt": body,
                        "desc": f"Creates {path.name} (freestyle style)"}, ""
            title = section_title()
            if not title:
                return None, "Type a section name."
            block = [title + HEADER_COLONS[s["kind"]]] + clean_lines(text, s["kind"])
            return {"path": path, "lines": block, "excerpt": block,
                    "desc": f"Creates {path.name} with section '{title}'"}, ""

        path, lines = target, file_lines(target)
        if not path.exists() or is_freestyle_file(path):
            body = clean_lines(text, "blocks")
            return {"path": path, "lines": lines + ([""] if lines else []) + body, "excerpt": body,
                    "desc": f"New snippet at the end of {path.name}"}, ""

        ranges = section_ranges(lines)
        if s["how"] == "new" or not ranges:
            kind = s["kind"] if s["kind"] != "none" else "all"
            title = section_title()
            if not title:
                return None, "Type a section name."
            if any(t.lower() == title.lower() for t, *_ in ranges):
                return None, f"'{title}' already exists in {path.name}: choose 'Add to' or 'Replace' instead."
            block = [title + HEADER_COLONS[kind]] + clean_lines(text, kind)
            return {"path": path, "lines": lines + ([""] if lines else []) + block, "excerpt": block,
                    "desc": f"New section '{title}' at the end of {path.name}"}, ""

        title, kind, i, j = ranges[min(s["sec"], len(ranges) - 1)]
        body = clean_lines(text, kind)
        if s["how"] == "add":
            ins = ([""] if kind == "blocks" and j > i + 1 else []) + body
            excerpt = [lines[i]] + (["… (existing lines stay)"] if j > i + 1 else []) + ins
            return {"path": path, "lines": lines[:j] + ins + lines[j:], "excerpt": excerpt,
                    "desc": f"Adds to '{title}' in {path.name}"}, ""
        return {"path": path, "lines": lines[:i + 1] + body + lines[j:], "excerpt": [lines[i]] + body,
                "desc": f"Replaces the {j - i - 1} line(s) of '{title}' in {path.name} (a backup is kept)"}, ""

    def _save_preview(self):
        s = self.save
        if not s or "preview" not in s or not s["preview"].winfo_exists():
            return
        self._sync_save_text()
        plan, err = self._plan()
        if err:
            s["preview"].config(text="—", fg=DIM)
            s["msg"].config(text=err, fg=WARN)
            return
        shown = plan["excerpt"][:10] + (["…"] if len(plan["excerpt"]) > 10 else [])
        s["preview"].config(text="\n".join(l if l else "(blank line)" for l in shown), fg=FG)
        msg = plan["desc"]
        header_lookalikes = [l for l in clean_lines(s["text"], "blocks") if header_info(l)]
        if header_lookalikes:
            s["msg"].config(text=f"{msg}\n⚠ '{header_lookalikes[0]}' ends with ':' so it will be read as a "
                                 "new header. Remove the colon at the end if it's a message.", fg=WARN)
        else:
            s["msg"].config(text=msg, fg=SUB)

    def do_save(self):
        if not self.save:
            return
        self._sync_save_text()
        plan, err = self._plan()
        if err:
            self._save_preview()
            return
        try:
            write_snippet_file(plan["path"], plan["lines"])
        except OSError as exc:
            self.save["msg"].config(text=f"Could not save: {exc}", fg=WARN)
            return
        self.settings["last_save"] = plan["path"].name
        save_settings(self.settings)
        self.save = None
        self.show_saved(plan)

    def show_saved(self, plan):
        path = plan["path"]
        items = [
            {"key": "1", "label": "Done", "sub": "", "action": self.hide},
            {"key": "2", "label": f"Open '{path.stem}' in the menu", "sub": "check it or send it now",
             "action": lambda: self.show_sections(path)},
            {"key": "3", "label": "Open the file in Notepad", "sub": path.name,
             "action": lambda: (os.startfile(path), self.hide())},
        ]
        self.render("Saved ✓", items, hint="Esc close")
        info = tk.Frame(self.body, bg=BADGE_BG)
        info.pack(fill="x", padx=4, pady=(0, 4), before=self.list_frame)
        tk.Label(info, text=plan["desc"], bg=BADGE_BG, fg=BORDER, font=(FONT, 8, "bold"), anchor="w",
                 justify="left", wraplength=int((POPUP_WIDTH - 40) * self.scale)).pack(fill="x", padx=8, pady=(6, 2))
        shown = plan["excerpt"][:8] + (["…"] if len(plan["excerpt"]) > 8 else [])
        tk.Label(info, text="\n".join(l if l else " " for l in shown), bg=BADGE_BG, fg=FG, font=("Consolas", 8),
                 anchor="w", justify="left").pack(fill="x", padx=8, pady=(0, 6))
        self.root.focus_set()
        self._resize()

    # ---- help
    def show_help(self):
        self.render("How snippet files work", [], back=self.show_files, hint="Backspace back • Esc close")
        self.width = SAVE_WIDTH
        lf = self.list_frame
        for c in lf.winfo_children():
            c.destroy()
        self._note(lf, "Each .txt file in the clipboard folder is one item in the first menu "
                       "(elena.txt → elena). Inside a file, a line that ENDS with colons is a header:")
        examples = [
            (":     send all", ["mass:", "Hi Elena!", "See you at 5"],
             "Click 'mass' → sends 2 messages, one per line."),
            ("::    choices", ["bumps::", "Any update on this?", "Just checking in"],
             "Click 'bumps ›' → a menu; pick ONE line to send."),
            (":::   blocks", ["replies:::", "Sure, on it", "Will update you", "", "Thanks a lot!"],
             "Click 'replies ›' → search, pick a block; its lines are sent one by one."),
        ]
        for head, code, result in examples:
            card = tk.Frame(lf, bg=BADGE_BG)
            card.pack(fill="x", padx=6, pady=(6, 0))
            tk.Label(card, text=head, bg=BADGE_BG, fg=BORDER, font=("Consolas", 9, "bold"), anchor="w").pack(
                fill="x", padx=8, pady=(6, 0))
            tk.Label(card, text="\n".join(l or "(blank line)" for l in code), bg=BADGE_BG, fg=FG,
                     font=("Consolas", 9), anchor="w", justify="left").pack(fill="x", padx=16, pady=2)
            tk.Label(card, text=result, bg=BADGE_BG, fg=SUB, font=(FONT, 8), anchor="w", justify="left").pack(
                fill="x", padx=8, pady=(0, 6))
        self._note(lf, "freestyle.txt (or any file with no headers) works like ::: for the whole file.")
        self._note(lf, "Avoid ending a message line with ':' - it would become a header.", color=WARN)
        self._section(lf, "HANDY")
        self._note(lf, "Ctrl+V          open this menu        (0 = normal paste)\n"
                       "Ctrl+C twice    save what you copied  (new file, section, freestyle...)\n"
                       "Tab             Auto send / Confirm / Edit first\n"
                       "Right-click     edit a snippet before sending\n"
                       "Esc             close, or stop sending\n"
                       "{name} {greeting} {day} {date} {time} {clipboard}   filled in when sent", mono=True)
        self._buttons(lf, [("Back", self.show_files, BADGE_BG),
                           ("Open clipboard folder", lambda: (os.startfile(SNIPPET_DIR), self.hide()), BORDER)])
        self.root.focus_set()
        self._resize()

    @staticmethod
    def _clip_preview():
        text = clip_get()
        if text is None:
            return "(clipboard is empty or not text)"
        one = " ".join(text.split())
        return one[:60] + ("…" if len(one) > 60 else "")

    # ---- actions
    def _return_to_target(self):
        self.hide()
        force_foreground(self.target)

    def normal_paste(self):
        self._return_to_target()

        def job():
            time.sleep(0.12)
            wait_modifiers_released()
            press_ctrl_v()
        threading.Thread(target=job, daemon=True).start()

    def send_lines(self, lines):
        lines = fill_placeholders([l for l in lines if l.strip()], self.current_stem)
        if not lines:
            return
        # one message = all lines in one chat message (Shift+Enter between), else one message per line
        messages = [lines] if self.settings["one_message"] else [[l] for l in lines]
        self._return_to_target()
        self.busy = True
        self.abort.clear()
        threading.Thread(target=self._send_job, args=(messages,), daemon=True).start()

    def _send_job(self, messages):
        original = clip_get() if (MODE == "paste" and RESTORE_CLIPBOARD) else None
        try:
            time.sleep(0.15)
            wait_modifiers_released()
            confirm = window_exe(self.target) in CHAT_APPS
            for msg in messages:
                # stop if Esc was pressed or you clicked into another window
                if self.abort.is_set() or user32.GetForegroundWindow() != self.target:
                    break
                if MODE == "paste":
                    clip_set("\r\n".join(msg))
                    time.sleep(0.05)
                    press_ctrl_v()
                    time.sleep(PASTE_DELAY)       # let the app finish reading the clipboard
                else:
                    for i, line in enumerate(msg):
                        if i:
                            press_shift_enter()   # new line inside the same message
                            time.sleep(0.05)
                        type_text(line)
                        time.sleep(STEP_DELAY + len(line) * 0.002)   # long lines get a bit more time
                time.sleep(BEFORE_ENTER)
                if self.abort.is_set():
                    break
                press_enter()
                if confirm:
                    time.sleep(CONFIRM_DELAY)
                    press_enter()                 # no-op if already sent, sends it if Enter #1 was ignored
                time.sleep(AFTER_ENTER)
        finally:
            if original is not None:
                time.sleep(PASTE_DELAY)
                clip_set(original)
            self.busy = False

    def run(self):
        self.root.mainloop()


def main():
    if sys.platform != "win32":
        sys.exit("This tool only runs on Windows.")
    def grab_mutex():
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if ctypes.get_last_error() == 183:   # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return None
        return handle

    if not grab_mutex():
        # another copy is running: ask it to close, then take over (so restarting = just run it again)
        print("Closing the copy that is already running...")
        ev = kernel32.CreateEventW(None, True, False, QUIT_EVENT_NAME)
        kernel32.SetEvent(ev)
        for _ in range(25):
            time.sleep(0.2)
            if grab_mutex():
                break
        else:
            sys.exit("An older copy is still running and did not close.\n"
                     "End pythonw.exe / python.exe in Task Manager > Details, then run this again.")
        kernel32.ResetEvent(ev)
    print(f"Clipboard snippets running. Ctrl+V opens the popup. Folder: {SNIPPET_DIR}")
    print("Close it with 'Quit' in the popup or Ctrl+C here.")
    Popup().run()


if __name__ == "__main__":
    main()
