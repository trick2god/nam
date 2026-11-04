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

Keys in the popup:
  0          paste the normal clipboard (the old Ctrl+V)
  1-9        choose an item          Up/Down + Enter  move and choose
  Backspace  go back                 Esc              close
  Esc while lines are being sent stops the sending.

Run with:  pythonw clipboard.py   (no console)  or  python clipboard.py
"""

import ctypes
import ctypes.wintypes as wt
import queue
import sys
import threading
import time
import tkinter as tk
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

# ----------------------------------------------------------------- win32
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_SYSKEYDOWN, WM_QUIT = 0x0100, 0x0104, 0x0012
VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
VK_LWIN, VK_RWIN, VK_ESCAPE, VK_RETURN, VK_V = 0x5B, 0x5C, 0x1B, 0x0D, 0x56
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
    """Global low-level hook: swallows Ctrl+V and reports it; watches Esc while sending."""

    def __init__(self, on_ctrl_v, on_escape):
        super().__init__(daemon=True)
        self.on_ctrl_v, self.on_escape = on_ctrl_v, on_escape
        self.tid = None
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
        if n_code == 0 and w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
            kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if kb.dwExtraInfo != MAGIC:
                if kb.vkCode == VK_ESCAPE:
                    self.on_escape()
                elif (kb.vkCode == VK_V and is_down(VK_CONTROL)
                      and not any(is_down(k) for k in (VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN))):
                    self.on_ctrl_v(user32.GetForegroundWindow())
                    return 1   # swallow the key, the popup takes over
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


def parse_sections(path):
    """'Header:' lines start a section; the non-blank lines under it are its lines."""
    lines = [l.strip() for l in read_text(path).splitlines() if l.strip()]
    sections, current = [], None
    for line in lines:
        if line.endswith(":"):
            current = {"title": line[:-1].strip() or line, "lines": []}
            sections.append(current)
        else:
            if current is None:  # text before the first header
                current = {"title": "(no header)", "lines": []}
                sections.append(current)
            current["lines"].append(line)
    return sections


def is_freestyle(path):
    return path.stem.lower().startswith("freestyle")


def parse_blocks(path):
    """Freestyle: blocks are separated by one or more blank lines; each block is one option."""
    blocks, current = [], []
    for raw in read_text(path).splitlines():
        line = raw.strip()
        if line:
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return [{"title": b[0], "lines": b} for b in blocks]


def load_options(path):
    """Returns (options, freestyle). Freestyle = named freestyle*.txt, or a file with no 'Header:' lines."""
    if is_freestyle(path):
        return parse_blocks(path), True
    sections = parse_sections(path)
    if len(sections) == 1 and sections[0]["title"] == "(no header)":
        return parse_blocks(path), True
    return sections, False


# ----------------------------------------------------------------- popup
BG, ROW, HOVER, BORDER = "#1e2230", "#1e2230", "#323a52", "#5b8def"
FG, SUB, BADGE_BG, DIM = "#eef1f8", "#8f98ad", "#2c3349", "#5c6479"
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
        r.bind("<BackSpace>", lambda e: self.go_back(from_key=True))
        r.bind("<Left>", lambda e: self.go_back(from_key=True))
        r.bind("<Up>", lambda e: self.move(-1))
        r.bind("<Down>", lambda e: self.move(1))
        r.bind("<Return>", lambda e: self.activate(self.sel))
        r.bind("<Right>", lambda e: None if self._in_search() else self.activate(self.sel))
        r.bind("<Key>", self.on_key)
        r.bind("<FocusOut>", lambda e: r.after(250, self._check_focus))

        self.hook = KeyHook(self._from_hook_ctrl_v, self._from_hook_escape)
        self.hook.start()
        r.after(30, self._poll)

    # ---- thread-safe entry points (called from the hook thread)
    def _from_hook_ctrl_v(self, hwnd):
        self.events.put(("show", hwnd))

    def _from_hook_escape(self):
        if self.busy:
            self.abort.set()

    def _poll(self):
        try:
            while True:
                kind, arg = self.events.get_nowait()
                if kind == "show":
                    self.show(arg)
        except queue.Empty:
            pass
        self.root.after(30, self._poll)

    # ---- window handling
    def show(self, target_hwnd):
        if self.busy:
            return
        if self.visible:                      # Ctrl+V again while open = normal paste
            self.normal_paste()
            return
        self.target = target_hwnd
        self.show_files()
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
        w = int(POPUP_WIDTH * self.scale)
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
            w = int(POPUP_WIDTH * self.scale)
            h = self.root.winfo_reqheight()
            x, y = self.root.winfo_x(), self.root.winfo_y()
            left, top, right, bottom = work_area_at(x, y)
            self.root.geometry(f"{w}x{h}+{x}+{min(y, bottom - h)}")

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
        self.back_action = back
        if back:
            self.back_lbl.pack(side="left", before=self.title_lbl, padx=(0, 6))
        else:
            self.back_lbl.pack_forget()
        self.hint.config(text=hint)
        self.all_items = items
        self.search_entry = None
        if search:
            box = tk.Frame(self.body, bg=BADGE_BG)
            box.pack(fill="x", padx=4, pady=(0, 4))
            tk.Label(box, text="\U0001F50D", bg=BADGE_BG, fg=SUB, font=(FONT, 9)).pack(side="left", padx=(6, 2))
            var = tk.StringVar()
            entry = tk.Entry(box, textvariable=var, bg=BADGE_BG, fg=FG, insertbackground=FG,
                             relief="flat", font=(FONT, 10))
            entry.pack(side="left", fill="x", expand=True, ipady=4, padx=(0, 6))
            var.trace_add("write", lambda *a: self._filter(var.get()))
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
        if self._in_search():          # typing goes into the search box
            return
        for i, it in enumerate(self.items):
            if e.char and it.get("key") == e.char:
                self.activate(i)
                return

    def go_back(self, from_key=False):
        if from_key and self._in_search() and self.search_entry.get():
            return                     # Backspace/Left edits the search text
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
        hint = f"{SNIPPET_DIR.name}\\ • 0-9 / ↑↓ Enter • Esc close"
        if len(items) == 1:
            items[0]["sub"] = f"No .txt files in {SNIPPET_DIR}"
        self.render("Clipboard snippets", items, hint=hint)

    def show_sections(self, path):
        options, freestyle = load_options(path)
        items = []
        for n, sec in enumerate(options, start=1):
            lines = sec["lines"]
            if freestyle:   # label = first line, sub = the rest
                rest = lines[1:4]
                sub = f"{len(lines)} line(s) • " + " ↵ ".join(rest) + (" …" if len(lines) > 4 else "") \
                    if rest else "1 line"
            else:
                preview = " ↵ ".join(lines[:3]) + (" …" if len(lines) > 3 else "")
                sub = f"{len(lines)} line(s) • {preview}" if lines else "(empty)"
            items.append({"key": "" if freestyle else (str(n) if n < 10 else ""),
                          "label": sec["title"], "sub": sub,
                          "text": " ".join([sec["title"]] + lines),
                          "action": lambda l=lines: self.send_lines(l)})
        hint = ("Type to search • ↑↓ Enter • Esc close" if freestyle
                else "Each line is pasted, then Enter • Backspace back")
        self.render(path.stem, items, back=self.show_files, hint=hint, search=freestyle)

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
        if not lines:
            return
        self._return_to_target()
        self.busy = True
        self.abort.clear()
        threading.Thread(target=self._send_job, args=(list(lines),), daemon=True).start()

    def _send_job(self, lines):
        original = clip_get() if (MODE == "paste" and RESTORE_CLIPBOARD) else None
        try:
            time.sleep(0.15)
            wait_modifiers_released()
            confirm = window_exe(self.target) in CHAT_APPS
            for line in lines:
                # stop if Esc was pressed or you clicked into another window
                if self.abort.is_set() or user32.GetForegroundWindow() != self.target:
                    break
                if MODE == "paste":
                    clip_set(line)
                    time.sleep(0.05)
                    press_ctrl_v()
                    time.sleep(PASTE_DELAY)       # let the app finish reading the clipboard
                else:
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
    kernel32.CreateMutexW(None, False, "Local\\ClipboardSnippetsPopup")
    if ctypes.get_last_error() == 183:   # ERROR_ALREADY_EXISTS
        sys.exit("Clipboard snippets is already running.")
    print(f"Clipboard snippets running. Ctrl+V opens the popup. Folder: {SNIPPET_DIR}")
    print("Close it with 'Quit' in the popup or Ctrl+C here.")
    Popup().run()


if __name__ == "__main__":
    main()
