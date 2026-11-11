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
  (Confirm and Edit have Send = with Enter, and Paste = into the box only, no Enter)
  Options: Send = fast type + Enter, Type = every key (boxes that block paste), Paste = clipboard
  Auto send   click = send straight away
  Confirm     click = preview showing the target window, then Send / Edit / Cancel
  Edit first  click = open the editor
  Right-click on a row opens the editor (change the text just for this send).
  "Send as one message" = all lines in ONE chat message (Shift+Enter between lines).

Hidden sections: put * before the colons (password*:  logins*::  keys*:::). The text shows as
********** everywhere in the popup (and as bars in View, with a "show hidden" toggle) but the
real text is what gets typed. Hidden text is not searchable and has no Edit button.

Placeholders in the .txt files: {name} {greeting} {date} {day} {time} {clipboard}

Saving new snippets: copy text, then press Ctrl+C again quickly (Ctrl+C twice), or pick
"+ Save copied text" in the menu. Choose freestyle, an existing file (new section / add to /
replace a section) or a new file, and the type (:  ::  :::). A preview shows exactly what
will be written; a backup of the old file goes to clipboard/.backup/. The "?" chip explains it all.

Text from a picture: copy a screenshot (Win+Shift+S) or an image, press Ctrl+V and pick
"Text from copied picture". The OCR built into Windows reads it (nothing installed, nothing
leaves the PC) and the text opens in the editor: Send / Type / Paste it, Copy it, or save it.

Turn it on / off: press Ctrl+X twice quickly (any app), or the power button in the popup. While off,
Ctrl+V pastes normally (no popup) and Ctrl+C twice shows a small box with "Turn on" (it doesn't take
focus). Starting the tool again also turns it on.

Running it again closes the copy that is already running and takes over.

Keys in the popup:
  Enter      right after Ctrl+V: paste the normal clipboard (the old Ctrl+V)
  1-9        choose an item          Up/Down + Enter  move and choose
  Backspace  go back                 Esc              close
  Esc or Ctrl+Z while lines are being sent stops the sending
  (Ctrl+Z is normal undo the rest of the time). After saving, Ctrl+Z undoes the save.

Run with:  pythonw clipboard.py   (no console)  or  python clipboard.py
"""

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from pathlib import Path

# ----------------------------------------------------------------- settings
SNIPPET_DIR = Path(__file__).resolve().parent / "clipboard"
# How the text gets into the box (pick it in the Confirm / Edit screens; remembered):
#   fast      = types the text in one go (quick; the default)
#   keys      = presses real keys one by one, like a person typing. Slower, but works in boxes
#               that block paste or ignore fast typing (some web forms, Citrix, remote desktop)
#   clipboard = puts each line on the clipboard + Ctrl+V. Apps like Teams read the clipboard
#               late, so a fast next line can overwrite it (lost or doubled lines). Use PASTE_DELAY.
INPUT_METHODS = {
    "fast":      ("Fast type",     "types the whole line in one go (quickest)"),
    "keys":      ("Type each key", "real key presses one by one; for boxes that block paste"),
    "clipboard": ("Clipboard",     "copies each line and presses Ctrl+V"),
}
KEY_DELAY = 0.012         # seconds between key presses in "Type each key"
STEP_DELAY = 0.1         # seconds between lines in "type" mode
BEFORE_ENTER = 0.1       # wait after a line is typed/pasted so the app has it in the box before Enter
AFTER_ENTER = 0.1        # wait after Enter so the app can send the message and clear the box
# Chat apps sometimes ignore an Enter right after sending the previous message. In these apps a
# second "confirm" Enter is pressed: if the first one worked the box is empty and the second
# does nothing; if the first was ignored, the second sends the line.
CHAT_APPS = {"ms-teams.exe", "teams.exe", "slack.exe", "whatsapp.exe", "telegram.exe",
             "discord.exe", "msedge.exe", "chrome.exe"}
CONFIRM_DELAY = 0.1      # wait before the confirm Enter
PASTE_DELAY = 0.1         # seconds to wait after each Ctrl+V in "paste" mode before touching the clipboard again
RESTORE_CLIPBOARD = True  # put your original clipboard text back afterwards
# Apps where Ctrl+V / Ctrl+C twice are left completely alone (no popup). File Explorer: its rename
# box (F2) and address bar close the moment they lose focus, so a popup would make the paste
# land nowhere. Add more in clipboard_settings.json -> "skip_apps": ["explorer.exe", ...]
SKIP_APPS = ["explorer.exe"]
POPUP_WIDTH = 360         # px at 100% scaling
SAVE_WIDTH = 440          # the "Save copied text" form is a bit wider
VIEW_WIDTH = 700          # the View / Edit file screen
TOGGLE_TWICE_WINDOW = 0.45  # Ctrl+X twice within this many seconds turns the tool on / off
COPY_TWICE_WINDOW = 0.45  # Ctrl+C twice within this many seconds opens "Save copied text" (if switched on)
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
DEFAULT_SETTINGS = {"mode": "confirm", "one_message": False, "last_save": "freestyle.txt",
                    "input": "fast", "copy_twice": True, "skip_apps": SKIP_APPS,
                    "enabled": True}   # False = turned off: Ctrl+V pastes normally


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    if s.get("mode") not in SEND_MODES:
        s["mode"] = "confirm"
    if s.get("input") not in INPUT_METHODS:
        s["input"] = "fast"
    if not isinstance(s.get("skip_apps"), list):
        s["skip_apps"] = list(SKIP_APPS)
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
WM_KEYUP, WM_SYSKEYUP, VK_C, VK_Z, VK_X = 0x0101, 0x0105, 0x43, 0x5A, 0x58
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


_EXE_BY_PID = {}


def foreground_exe():
    """Exe name of the active window, cached per process (the keyboard hook asks on every Ctrl+V)."""
    hwnd = user32.GetForegroundWindow()
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = _EXE_BY_PID.get(pid.value)
    if exe is None:
        exe = window_exe(hwnd)
        if len(_EXE_BY_PID) > 200:
            _EXE_BY_PID.clear()
        _EXE_BY_PID[pid.value] = exe
    return exe


kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
kernel32.CreateEventW.restype = wt.HANDLE
kernel32.SetEvent.argtypes = [wt.HANDLE]
kernel32.ResetEvent.argtypes = [wt.HANDLE]
kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
kernel32.WaitForSingleObject.restype = wt.DWORD
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]


GWL_EXSTYLE, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TOPMOST = -20, 0x08000000, 0x00000080, 0x00000008
SW_HIDE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0, 0x0010, 0x0040
HWND_TOPMOST = wt.HWND(-1)
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = wt.LONG
user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, wt.LONG]
user32.SetWindowLongW.restype = wt.LONG
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]


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


user32.VkKeyScanW.argtypes = [wt.WCHAR]
user32.VkKeyScanW.restype = wt.SHORT
user32.GetKeyState.argtypes = [ctypes.c_int]
user32.GetKeyState.restype = wt.SHORT
VK_CAPITAL, VK_TAB = 0x14, 0x09


def type_keys(text, abort=None, delay=KEY_DELAY):
    """Types like a person: one real key press per character (with Shift where needed).
    Characters that have no key on this keyboard layout are typed as Unicode instead."""
    caps = bool(user32.GetKeyState(VK_CAPITAL) & 1)
    for ch in text:
        if abort is not None and abort.is_set():
            return
        if ch == "\t":
            vk, state = VK_TAB, 0
        else:
            res = user32.VkKeyScanW(ch)
            vk, state = res & 0xFF, (res >> 8) & 0xFF
            if res == -1 or (state & 6) == 2:     # no key for it, or a Ctrl-only shortcut: use Unicode
                type_text(ch)
                time.sleep(delay)
                continue
        shift = bool(state & 1)
        if caps and ch.isalpha():
            shift = not shift                     # Caps Lock flips letters
        mods = ([VK_SHIFT] if shift else []) + ([VK_CONTROL] if state & 2 else []) + \
               ([VK_MENU] if state & 4 else [])
        _send(*[_key(m) for m in mods], _key(vk))
        _send(_key(vk, flags=KEYEVENTF_KEYUP), *[_key(m, flags=KEYEVENTF_KEYUP) for m in reversed(mods)])
        time.sleep(delay)


def wait_modifiers_released(timeout=3.0):
    end = time.time() + timeout
    while time.time() < end and any(is_down(k) for k in (VK_CONTROL, VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN)):
        time.sleep(0.02)


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD), ("hwndActive", wt.HWND), ("hwndFocus", wt.HWND),
                ("hwndCapture", wt.HWND), ("hwndMenuOwner", wt.HWND), ("hwndMoveSize", wt.HWND),
                ("hwndCaret", wt.HWND), ("rcCaret", wt.RECT)]


user32.GetGUIThreadInfo.argtypes = [wt.DWORD, ctypes.POINTER(GUITHREADINFO)]
user32.SetFocus.argtypes = [wt.HWND]
user32.SetFocus.restype = wt.HWND


def focused_control(top_hwnd):
    """The exact box that has the cursor inside a window (e.g. Outlook's search box), or None."""
    if not top_hwnd:
        return None
    tid = user32.GetWindowThreadProcessId(top_hwnd, None)
    info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
    if tid and user32.GetGUIThreadInfo(tid, ctypes.byref(info)) and info.hwndFocus:
        return info.hwndFocus
    return None


def restore_focus(top_hwnd, control):
    """Bring the window back AND put the cursor back in the box you were in. Without this some
    apps (Outlook search, browser address bars...) lose the box and the paste goes nowhere."""
    force_foreground(top_hwnd)
    if not control or not user32.IsWindow(control):
        return
    cur = kernel32.GetCurrentThreadId()
    tid = user32.GetWindowThreadProcessId(control, None)
    attached = bool(tid and tid != cur and user32.AttachThreadInput(cur, tid, True))
    try:
        user32.SetFocus(control)
    finally:
        if attached:
            user32.AttachThreadInput(cur, tid, False)


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


user32.RegisterClipboardFormatW.argtypes = [wt.LPCWSTR]
user32.RegisterClipboardFormatW.restype = wt.UINT


def _global_dword(value):
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, 4)
    p = kernel32.GlobalLock(h)
    ctypes.memmove(p, ctypes.byref(wt.DWORD(value)), 4)
    kernel32.GlobalUnlock(h)
    return h


def clip_set(text, private=False):
    """Puts text on the clipboard. private=True keeps it out of Windows clipboard history (Win+V),
    cloud clipboard sync and clipboard monitors - used when a snippet is pasted."""
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
        if private:
            for name in ("ExcludeClipboardContentFromMonitorProcessing",
                         "CanIncludeInClipboardHistory", "CanUploadToCloudClipboard"):
                fmt = user32.RegisterClipboardFormatW(name)
                flag = _global_dword(0)
                if not fmt or not user32.SetClipboardData(fmt, flag):
                    kernel32.GlobalFree(flag)
        return True
    finally:
        user32.CloseClipboard()


# ----------------------------------------------------------------- picture -> text (Windows OCR)
CF_BITMAP, CF_DIB, CF_DIBV5 = 2, 8, 17


def clip_has_image():
    """True when the clipboard holds a picture (screenshot, Win+Shift+S, 'Copy image')."""
    if any(user32.IsClipboardFormatAvailable(f) for f in (CF_DIB, CF_BITMAP, CF_DIBV5)):
        return True
    fmt = user32.RegisterClipboardFormatW("PNG")
    return bool(fmt and user32.IsClipboardFormatAvailable(fmt))


# Runs in a hidden PowerShell: takes the clipboard picture, makes small screenshots 2x bigger
# (OCR reads small UI text much better), then asks the OCR engine built into Windows to read it.
# Nothing is installed and the picture never leaves this PC. A blank line is printed where the
# picture has a bigger gap between lines (paragraphs), so blocks stay together.
OCR_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
try {
  Add-Type -AssemblyName System.Windows.Forms, System.Drawing, System.Runtime.WindowsRuntime
  $img = [System.Windows.Forms.Clipboard]::GetImage()
  if (-not $img) {
    $data = [System.Windows.Forms.Clipboard]::GetDataObject()
    if ($data -and $data.GetDataPresent('PNG')) { $img = [System.Drawing.Image]::FromStream($data.GetData('PNG')) }
  }
  if (-not $img) { Write-Output '##NOIMAGE##'; exit 0 }
  $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
  $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
  $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
  $max = [Windows.Media.Ocr.OcrEngine]::MaxImageDimension
  $s = 1.0
  if ($img.Width -lt 1200 -and $img.Height -lt 1200) { $s = 2.0 }
  $s = [Math]::Min($s, [Math]::Min($max / $img.Width, $max / $img.Height))
  $w = [Math]::Max(1, [int]($img.Width * $s)); $h = [Math]::Max(1, [int]($img.Height * $s))
  $bmp = New-Object System.Drawing.Bitmap $w, $h
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.Clear([System.Drawing.Color]::White)
  $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
  $g.DrawImage($img, 0, 0, $w, $h)
  $g.Dispose(); $img.Dispose()
  $bmp.Save('__PNG__', [System.Drawing.Imaging.ImageFormat]::Png)
  $bmp.Dispose()

  $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
      $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
      $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
  function Await($op, [Type]$type) {
    $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    $null = $t.Wait(-1)
    $t.Result
  }
  $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
  if (-not $engine) { Write-Output '##NOLANG##'; exit 0 }
  $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync('__PNG__')) ([Windows.Storage.StorageFile])
  $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
  $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $soft = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $result = Await ($engine.RecognizeAsync($soft)) ([Windows.Media.Ocr.OcrResult])
  $stream.Dispose()
  $prevTop = $null; $prevBot = $null
  foreach ($line in $result.Lines) {
    $top = ($line.Words | ForEach-Object { $_.BoundingRect.Y } | Measure-Object -Minimum).Minimum
    $bot = ($line.Words | ForEach-Object { $_.BoundingRect.Y + $_.BoundingRect.Height } | Measure-Object -Maximum).Maximum
    if ($prevBot -ne $null -and ($top - $prevBot) -gt 0.9 * ($prevBot - $prevTop)) { Write-Output '' }
    Write-Output $line.Text
    $prevTop = $top; $prevBot = $bot
  }
} catch {
  Write-Output ('##ERROR## ' + $_.Exception.Message)
}
'''


def ocr_clipboard_image(timeout=40):
    """Reads the words in the copied picture. Returns (lines, error_message)."""
    fd, png = tempfile.mkstemp(prefix="clip_ocr_", suffix=".png")
    os.close(fd)
    script = OCR_SCRIPT.replace("__PNG__", png.replace("'", "''"))
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA",
             "-EncodedCommand", base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
            creationflags=0x08000000)          # CREATE_NO_WINDOW: no console flashes up
    except FileNotFoundError:
        return None, "PowerShell was not found on this PC, so Windows OCR can't be used."
    except subprocess.TimeoutExpired:
        return None, "Reading the picture took too long. Try a smaller picture."
    finally:
        try:
            os.remove(png)
        except OSError:
            pass
    text = out.stdout.decode("utf-8", errors="replace").lstrip("﻿")
    lines = [l.rstrip() for l in text.replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1].strip():
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    first = lines[0] if lines else ""
    if first == "##NOIMAGE##":
        return None, "There is no picture on the clipboard any more. Copy it again."
    if first == "##NOLANG##":
        return None, ("Windows has no OCR language installed. Settings > Time & language > "
                      "Language: add English (with 'Optical character recognition').")
    if first.startswith("##ERROR##"):
        return None, "Windows OCR failed: " + first[9:].strip()
    if not lines:
        err = out.stderr.decode("utf-8", errors="replace").strip()
        return None, ("No text was found in the picture." if not err or out.returncode == 0
                      else "Windows OCR failed: " + err[:200])
    return lines, ""


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

    def __init__(self, on_ctrl_v, on_escape, on_copy_twice=None, skip_apps=()):
        super().__init__(daemon=True)
        self.skip_apps = {a.lower() for a in skip_apps}   # apps where Ctrl+V is left alone
        self.enabled = True       # False = turned off: Ctrl+V / Ctrl+C left alone
        self.on_copy_off = None   # called on Ctrl+C twice while turned off (shows the small "turn on" box)
        self.on_toggle = None     # called on Ctrl+X twice (turn on / off, works in every app)
        self.on_ctrl_v, self.on_escape, self.on_copy_twice = on_ctrl_v, on_escape, on_copy_twice
        self.tid = None
        self._last_c = 0.0
        self._c_held = False
        self._last_x = 0.0
        self._x_held = False
        self._last_c_off = 0.0
        self.sending = False      # set by the popup while it is typing lines
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

    WATCHED = frozenset((VK_V, VK_C, VK_X, VK_Z, VK_ESCAPE))   # the only keys this tool cares about

    def _callback(self, n_code, w_param, l_param):
        # Windows waits for this on EVERY key press in EVERY app, so stay as quick as possible:
        # read just the key code (first field) and pass everything else straight on.
        if n_code != 0 or ctypes.c_uint32.from_address(l_param).value not in self.WATCHED:
            return user32.CallNextHookEx(None, n_code, w_param, l_param)
        if n_code == 0:
            kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if kb.dwExtraInfo != MAGIC:
                if w_param in (WM_KEYUP, WM_SYSKEYUP) and kb.vkCode == VK_C:
                    self._c_held = False
                elif w_param in (WM_KEYUP, WM_SYSKEYUP) and kb.vkCode == VK_X:
                    self._x_held = False
                elif w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    plain_ctrl = kb.vkCode != VK_ESCAPE and is_down(VK_CONTROL) and not any(
                        is_down(k) for k in (VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN))
                    if kb.vkCode == VK_ESCAPE:
                        self.on_escape()
                    elif kb.vkCode == VK_Z and plain_ctrl and self.sending:
                        self.on_escape()     # Ctrl+Z = stop sending
                        return 1             # swallowed, so the app doesn't undo what was typed
                    elif kb.vkCode == VK_X and plain_ctrl:
                        if not self._x_held and self.on_toggle:
                            self._x_held = True      # once per press, not on key-repeat
                            now = time.time()
                            if now - self._last_x <= TOGGLE_TWICE_WINDOW:
                                self._last_x = 0.0
                                self.on_toggle()
                            else:
                                self._last_x = now
                        # never swallowed: the app still cuts normally
                    elif kb.vkCode in (VK_V, VK_C) and plain_ctrl and self.skip_apps \
                            and foreground_exe() in self.skip_apps:
                        pass       # e.g. File Explorer: normal paste / copy, no popup
                    elif kb.vkCode in (VK_V, VK_C) and plain_ctrl and not self.enabled:
                        if kb.vkCode == VK_C and not self._c_held and self.on_copy_off:
                            self._c_held = True      # once per press, not on key-repeat
                            now = time.time()
                            if now - self._last_c_off <= COPY_TWICE_WINDOW:   # Ctrl+C twice
                                self._last_c_off = 0.0
                                self.on_copy_off()
                            else:
                                self._last_c_off = now
                        # turned off: never swallowed, the app pastes / copies normally
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
_TEXT_CACHE = {}   # path -> ((mtime, size), text): files are only re-read when they change


def read_text(path):
    try:
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None
    cached = _TEXT_CACHE.get(path)
    if stamp is not None and cached is not None and cached[0] == stamp:
        return cached[1]
    text = None
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = path.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = path.read_text(encoding="utf-8", errors="replace")
    if stamp is not None:
        _TEXT_CACHE[path] = (stamp, text)
    return text


def list_files():
    if not SNIPPET_DIR.is_dir():
        return []
    # freestyle.txt always first (so it is always "1"), then the rest A-Z
    return sorted(SNIPPET_DIR.glob("*.txt"), key=lambda p: (p.name.lower() != "freestyle.txt", p.stem.lower()))


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
MASK = "**********"      # what hidden text looks like in the popup ('name*:' sections)


def split_block_spans(pairs):
    """pairs = [(line_index, raw_text)] -> [(start, end, [lines])], blocks split by blank lines."""
    out, cur, first, last = [], [], 0, 0
    for idx, raw in pairs:
        line = raw.strip()
        if line:
            if not cur:
                first = idx
            cur.append(line)
            last = idx
        elif cur:
            out.append((first, last + 1, cur))
            cur = []
    if cur:
        out.append((first, last + 1, cur))
    return out


def parse_sections(path):
    """Headers are lines ending in colons:
         name:    send all the lines below it (each line = one message)
         name::   a menu - every line below it is its own choice
         name:::  a freestyle menu - blocks separated by blank lines, each block is one choice
       Every section / choice / block also gets a 'span' (its line range in the file) so it can be removed.
    """
    sections, current = [], None
    for idx, raw in enumerate(read_text(path).splitlines()):
        line = raw.strip()
        if line.endswith(":"):
            colons = len(line) - len(line.rstrip(":"))
            name = line.rstrip(":").strip()
            hidden = name.endswith("*")          # 'password*:' -> content shown as ********
            current = {"title": name.rstrip("*").strip() or line, "hidden": hidden, "header": line,
                       "line_no": idx, "kind": SECTION_KINDS.get(colons, "blocks"), "raw": []}
            sections.append(current)
            continue
        if current is None:
            if not line:
                continue
            current = {"title": "(no header)", "kind": "all", "hidden": False, "header": None,
                       "line_no": None, "raw": []}   # text before the first header
            sections.append(current)
        current["raw"].append((idx, raw))
    for sec in sections:
        raw = sec.pop("raw")
        content = [(i, r) for i, r in raw if r.strip()]
        sec["lines"] = [r.strip() for _, r in content]
        if sec["kind"] == "all" and content:
            # 'name:' keeps its layout: blank lines BETWEEN the lines stay (paragraphs)
            sec["lines"] = [r.strip() for i, r in raw if content[0][0] <= i <= content[-1][0]]
        first = sec["line_no"] if sec["line_no"] is not None else (content[0][0] if content else 0)
        sec["span"] = (first, content[-1][0] + 1 if content else first + 1)
        secret = {"hidden": sec["hidden"], "secret_title": sec["hidden"]}   # option titles ARE the text
        if sec["kind"] == "choices":
            sec["options"] = [{"title": r.strip(), "lines": [r.strip()], "span": (i, i + 1), **secret}
                              for i, r in content]
        elif sec["kind"] == "blocks":
            sec["options"] = [{"title": b[0], "lines": b, "span": (a, z), **secret}
                              for a, z, b in split_block_spans(raw)]
    return sections


def is_freestyle(path):
    return path.stem.lower().startswith("freestyle")


def parse_blocks(path):
    """Freestyle: blocks are separated by one or more blank lines; each block is one option."""
    pairs = list(enumerate(read_text(path).splitlines()))
    return [{"title": b[0], "lines": b, "span": (a, z)} for a, z, b in split_block_spans(pairs)]


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
    "all":     "name:    sends EVERY line below it, one message per line. Blank lines between them stay.",
    "choices": "name::   opens a menu; EACH LINE is its own choice (sends just that line).",
    "blocks":  "name:::  opens a searchable menu; BLANK LINES separate the snippets.",
    "none":    "No header: the whole file works like freestyle (blank lines separate snippets).",
}


def safe_filename(name):
    name = "".join(ch for ch in name if ch not in '<>:"/\\|?*').strip().rstrip(".")
    return name[:-4] if name.lower().endswith(".txt") else name


def clean_lines(text, kind):
    """Copied text -> lines for the file. ':' keeps the blank lines between lines (same layout);
    '::' drops blank lines; ':::'/freestyle keep one between blocks."""
    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if kind == "all":
        lines = [l.strip() for l in raw]
        while lines and not lines[0]:
            lines.pop(0)
        while lines and not lines[-1]:
            lines.pop()
        return lines
    if kind == "choices":
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
    name = s.rstrip(":").strip()
    return name.rstrip("*").strip() or s, SECTION_KINDS.get(colons, "blocks")


def header_hidden(line):
    """True for 'name*:' / 'name*::' / 'name*:::' headers (their text is hidden in the popup)."""
    s = line.strip()
    return s.endswith(":") and s.rstrip(":").strip().endswith("*")


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


def remove_span(path, start, end, expect):
    """Removes lines [start, end) from the file, after checking they are still what we showed."""
    lines = file_lines(path)
    if [l.strip() for l in lines[start:end] if l.strip()] != [e for e in expect if e.strip()]:
        raise ValueError("the file changed since the menu was opened - go back and try again")
    new = lines[:start] + lines[end:]
    i = max(start, 1)
    while i < len(new) and not new[i].strip() and not new[i - 1].strip():
        del new[i]                     # don't leave a double blank line where it was
    while new and not new[0].strip():
        new.pop(0)
    write_snippet_file(path, new)


def replace_span(path, start, end, expect, new_lines):
    """Replaces lines [start, end) with new_lines, after checking they are still what we showed."""
    lines = file_lines(path)
    if [l.strip() for l in lines[start:end] if l.strip()] != [e for e in expect if e.strip()]:
        raise ValueError("the file changed since the menu was opened - go back and try again")
    write_snippet_file(path, lines[:start] + list(new_lines) + lines[end:])


def set_header_hidden(path, line_no, header, hide):
    """Adds or removes the '*' before the colons of one header line ('name:' <-> 'name*:')."""
    lines = file_lines(path)
    if line_no is None or line_no >= len(lines) or lines[line_no].strip() != header:
        raise ValueError("the file changed since the menu was opened - go back and try again")
    stripped = lines[line_no].strip()
    name = stripped.rstrip(":")
    colons = stripped[len(name):]
    name = name.rstrip().rstrip("*").rstrip()
    lines[line_no] = (name + "*" if hide else name) + colons
    write_snippet_file(path, lines)


def write_snippet_file(path, lines):
    """Writes the file (CRLF, UTF-8) after keeping a backup in clipboard/.backup/.
    Creates the clipboard folder first if it doesn't exist yet (first ever save)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup_dir = SNIPPET_DIR / ".backup"
        backup_dir.mkdir(exist_ok=True)
        (backup_dir / f"{path.name}.bak").write_text(read_text(path), encoding="utf-8")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\r\n")


# ----------------------------------------------------------------- popup
BG, ROW, HOVER, BORDER = "#1e2230", "#1e2230", "#323a52", "#5b8def"
FG, SUB, BADGE_BG, DIM = "#eef1f8", "#8f98ad", "#2c3349", "#5c6479"
WARN = "#ff8a80"
# each file (except freestyle) gets its own colour in the main menu, so they are easy to tell apart
FILE_COLORS = ["#7ee2a8", "#ffcf6e", "#c3a6ff", "#6fd3ff", "#ff9e80", "#ff8fc7", "#b5e06b", "#f6a5ff"]
FONT = "Segoe UI"
ICON_FONT = "Segoe Fluent Icons"   # Windows 11 icon font (Office / Teams look); checked at start-up
ICON = {"back": "\uE72B", "close": "\uE8BB", "power": "\uE7E8", "help": "\uE897", "search": "\uE721",
        "view": "\uE890", "edit": "\uE70F", "delete": "\uE74D", "lock": "\uE72E", "unlock": "\uE785",
        "add": "\uE710", "save": "\uE74E", "keyboard": "\uE765", "paste": "\uE77F", "picture": "\uEB9F",
        "send": "\uE724", "copy": "\uE8C8", "file": "\uE8A5", "folder": "\uE8B7"}
ICON_FALLBACK = {"back": "‹", "close": "✕", "power": "⏻", "help": "?", "search": "\U0001F50D",
                 "view": "\U0001F441", "edit": "✎", "delete": "\U0001F5D1", "lock": "\U0001F512",
                 "unlock": "\U0001F513", "add": "＋", "save": "\U0001F4BE", "keyboard": "⌨",
                 "paste": "\U0001F4CB", "picture": "\U0001F5BC", "send": "➤", "copy": "⧉",
                 "file": "\U0001F4C4", "folder": "\U0001F4C1"}


def pick_fonts(root):
    """Modern Windows 11 fonts when installed, otherwise the classic ones."""
    global FONT, ICON_FONT
    fams = set(tkfont.families(root))
    if "Segoe UI Variable Text" in fams:
        FONT = "Segoe UI Variable Text"
    ICON_FONT = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in fams), None)
    if ICON_FONT is None:
        ICON.update(ICON_FALLBACK)
        ICON_FONT = FONT


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
        pick_fonts(self.root)
        self._tip_saved = None       # footer text to put back after a button's tooltip
        self._drag_off = None        # dragging the window by its title bar
        self.screen_keys = {}        # extra one-key shortcuts of the current screen ('+', '?')

        self.events = queue.Queue()
        self.visible = False
        self.busy = False
        self.abort = threading.Event()
        self.target = None
        self.target_focus = None     # the exact box (control) inside the target window
        self.items, self.rows, self.sel = [], [], 0
        self.back_action = None
        self.hwnd = None
        self.search_entry = None
        self.editor = None
        self._bs_was_empty = False
        self.width = POPUP_WIDTH
        self.undo_action = None
        self.viewer = None
        self.view_editing = False
        self.leave_guard = None
        self.on_leave = None
        self.initial_focus = None
        self.save = None
        self.drafts = {}             # unsaved View edits, per file
        self._row_cache, self._empty_lbl, self._more_lbl = {}, None, None
        self._screen, self._files_sig, self._files_hint = None, None, ""
        self.all_items = []
        self._filter_job = None
        self._applied_query = ""
        self._active_seen = False    # watchdog: has the popup been the active window yet?
        self._away_since = None
        self.current_stem = ""
        self._files_img = None       # was a picture on the clipboard when the main menu was built?
        self._ocr_token = 0          # which "read the picture" request is the current one
        self._ocr_waiting = False
        self.settings = load_settings()
        self.quit_event = kernel32.CreateEventW(None, True, False, QUIT_EVENT_NAME)

        outer = tk.Frame(self.root, bg=BG)
        outer.pack(fill="both", expand=True, padx=1, pady=1)

        head = tk.Frame(outer, bg=BG, cursor="fleur")
        head.pack(fill="x", padx=(6, 4), pady=(5, 2))
        self.back_lbl = self._icon_btn(head, "back", lambda: self.go_back(), "Back  (Backspace)", fg=FG)
        self.title_lbl = tk.Label(head, text="", bg=BG, fg=FG, font=(FONT, 11, "bold"), anchor="w",
                                  cursor="fleur")
        self.title_lbl.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self._icon_btn(head, "close", self.hide, "Close  (Esc)", hover_fg=WARN).pack(side="right")
        self._icon_btn(head, "power", self.turn_off,
                       "Turn off (or Ctrl+X twice): Ctrl+V pastes normally. Ctrl+X twice turns it back on").pack(side="right")
        self._icon_btn(head, "help", self.show_help, "How snippet files work  (?)").pack(side="right")
        self.mode_lbl = tk.Label(head, text="", bg=BADGE_BG, fg=FG, font=(FONT, 8, "bold"),
                                 cursor="hand2", padx=8, pady=2)
        self.mode_lbl.pack(side="right", padx=(0, 6))
        self.mode_lbl.bind("<Button-1>", lambda e: self.cycle_mode())
        self._update_mode_chip()
        for w in (head, self.title_lbl):          # drag the title bar to move the window
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
        tk.Frame(outer, bg=BADGE_BG, height=1).pack(fill="x", padx=8, pady=(0, 6))   # thin divider

        self.body = tk.Frame(outer, bg=BG)
        self.body.pack(fill="both", expand=True, padx=6)

        foot = tk.Frame(outer, bg=BG)
        foot.pack(fill="x", padx=10, pady=(4, 8))
        quit_lbl = tk.Label(foot, text="Quit", bg=BG, fg=DIM, font=(FONT, 8, "underline"), cursor="hand2")
        quit_lbl.pack(side="right")           # packed first so a long hint never covers it
        self.hint = tk.Label(foot, text="", bg=BG, fg=DIM, font=(FONT, 8), anchor="w")
        self.hint.pack(side="left", fill="x", expand=True)
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
        r.bind("<MouseWheel>", self._wheel)
        r.bind("<Control-z>", lambda e: self.undo_action() if self.undo_action else None)
        r.bind("<Control-Z>", lambda e: self.undo_action() if self.undo_action else None)
        r.bind("<FocusOut>", lambda e: r.after(250, self._check_focus))

        self.hook = KeyHook(self._from_hook_ctrl_v, self._from_hook_escape, self._from_hook_copy_twice,
                            skip_apps=self.settings.get("skip_apps", SKIP_APPS))
        if not self.settings.get("enabled", True):   # starting the tool always turns it back on
            self.settings["enabled"] = True
            save_settings(self.settings)
        self.hook.enabled = True
        self.hook.on_copy_off = lambda: self.events.put(("off_toast", None))
        self.hook.on_toggle = lambda: self.events.put(("toggle", None))
        self.toast = None            # the small "turned off - turn on?" box (built on first use)
        self._toast_job = None
        self.hook.start()
        r.after(30, self._poll)
        r.after(300, self._warm_up)

    def _icon_btn(self, parent, icon, cmd, tip="", size=10, fg=SUB, hover_fg=FG, bg=BG):
        """A flat icon button (Windows icon font). The tooltip shows in the footer while hovering."""
        b = tk.Label(parent, text=ICON[icon], bg=bg, fg=fg, font=(ICON_FONT, size), cursor="hand2",
                     padx=6, pady=3)
        b.bind("<Button-1>", lambda e: (cmd(), "break")[1])
        b.bind("<Enter>", lambda e: (b.config(fg=hover_fg), self._tip(tip)))
        b.bind("<Leave>", lambda e: (b.config(fg=fg), self._tip(None)))
        return b

    def _tip(self, text):
        if not hasattr(self, "hint"):
            return
        if text:
            if self._tip_saved is None:
                self._tip_saved = self.hint.cget("text")
            self.hint.config(text=text)
        elif self._tip_saved is not None:
            self.hint.config(text=self._tip_saved)
            self._tip_saved = None

    def _drag_start(self, e):
        self._drag_off = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag_move(self, e):
        if self._drag_off:
            self.root.geometry(f"+{e.x_root - self._drag_off[0]}+{e.y_root - self._drag_off[1]}")

    def _action_bar(self, actions):
        """A slim row of buttons under the list (instead of long rows): [(icon, text, tip, fn)]."""
        bar = tk.Frame(self.body, bg=BG)
        bar.pack(fill="x", padx=4, pady=(6, 0))
        for icon, text, tip, fn in actions:
            chip = tk.Frame(bar, bg=BADGE_BG, cursor="hand2")
            chip.pack(side="left", padx=(0, 6))
            ic = tk.Label(chip, text=ICON[icon], bg=BADGE_BG, fg=SUB, font=(ICON_FONT, 9), cursor="hand2")
            ic.pack(side="left", padx=(8, 5), pady=5)
            tx = tk.Label(chip, text=text, bg=BADGE_BG, fg=FG, font=(FONT, 8), cursor="hand2")
            tx.pack(side="left", padx=(0, 10), pady=5)
            parts = (chip, ic, tx)
            for w in parts:
                w.bind("<Button-1>", lambda e, f=fn: (f(), "break")[1])
                w.bind("<Enter>", lambda e, ps=parts, t=tip: ([x.config(bg=HOVER) for x in ps], self._tip(t)))
                w.bind("<Leave>", lambda e, ps=parts: ([x.config(bg=BADGE_BG) for x in ps], self._tip(None)))

    def _warm_up(self):
        """At startup, while hidden: load the fonts for the icons (Windows can take seconds to find
        an emoji font the first time) and build the main menu, so even the first Ctrl+V is instant."""
        icons = "\U0001F441\U0001F5D1\U0001F512\U0001F513✎⌨＋⏎•›←↑↓✓⚠"
        probe = tk.Label(self.root, text=icons, font=(FONT, 10))
        probe.pack()
        tk.Label(probe, text="".join(ICON.values()), font=(ICON_FONT, 10)).pack()
        self.root.update_idletasks()
        probe.destroy()
        self._prebuild_menu()

    # ---- thread-safe entry points (called from the hook thread)
    def _from_hook_ctrl_v(self, hwnd):
        self.events.put(("show", hwnd))

    def _from_hook_copy_twice(self, hwnd):
        if self.settings.get("copy_twice", True):      # can be switched off (save form / help)
            self.events.put(("save", hwnd))

    # ---- on / off
    def turn_off(self):
        """Ctrl+V goes back to normal paste. Ctrl+C twice shows a small box with 'Turn on'; Ctrl+X twice turns it on."""
        self.settings["enabled"] = False
        save_settings(self.settings)
        self.hook.enabled = False
        if self.visible:
            self._return_to_target()
        self.show_toast(message="Snippets OFF. Turn on: click here, or Ctrl+X twice")

    def turn_on(self):
        self.settings["enabled"] = True
        save_settings(self.settings)
        self.hook.enabled = True
        self.show_toast(message="✓ ON again: Ctrl+V opens the snippets  (Ctrl+X twice = off)", button=False, seconds=2.5)

    def _build_toast(self):
        t = tk.Toplevel(self.root)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        t.configure(bg=BORDER)
        inner = tk.Frame(t, bg=BG)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        self._toast_msg = tk.Label(inner, text="", bg=BG, fg=FG, font=(FONT, 9), anchor="w")
        self._toast_msg.pack(side="left", padx=(10, 8), pady=7)
        x = tk.Label(inner, text="✕", bg=BG, fg=SUB, font=(FONT, 9), cursor="hand2")
        x.pack(side="right", padx=(0, 8))
        x.bind("<Button-1>", lambda e: self.hide_toast())
        self._toast_btn = tk.Label(inner, text="Turn on", bg=BORDER, fg=FG, font=(FONT, 9, "bold"),
                                   cursor="hand2", padx=10, pady=2)
        self._toast_btn.pack(side="right", padx=(0, 6))
        self._toast_btn.bind("<Button-1>", lambda e: self.turn_on())
        t.geometry("+-3000+-3000")
        t.update_idletasks()
        hwnd = user32.GetAncestor(t.winfo_id(), GA_ROOT)
        # never take focus from the app you're working in (clicks still work)
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST)
        user32.ShowWindow(hwnd, SW_HIDE)
        self.toast, self._toast_hwnd = t, hwnd

    def show_toast(self, message="Clipboard snippets is off. Turn it on?", button=True, seconds=7.0):
        """Small box near the mouse. It does NOT take focus, so your copy / paste carries on normally."""
        if self.toast is None:
            self._build_toast()
        self._toast_msg.config(text=message)
        if button:
            self._toast_btn.pack(side="right", padx=(0, 6))
        else:
            self._toast_btn.pack_forget()
        self.toast.update_idletasks()
        w, h = self.toast.winfo_reqwidth(), self.toast.winfo_reqheight()
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        left, top, right, bottom = work_area_at(pt.x, pt.y)
        x = min(max(pt.x + 14, left), right - w)
        y = min(max(pt.y + 18, top), bottom - h)
        user32.SetWindowPos(self._toast_hwnd, HWND_TOPMOST, x, y, w, h,
                            SWP_NOACTIVATE | SWP_SHOWWINDOW)
        if self._toast_job is not None:
            self.root.after_cancel(self._toast_job)
        self._toast_job = self.root.after(int(seconds * 1000), self.hide_toast)

    def hide_toast(self):
        self._toast_job = None
        if self.toast is not None:
            user32.ShowWindow(self._toast_hwnd, SW_HIDE)

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
                elif kind == "off_toast":
                    self.show_toast()
                elif kind == "toggle":     # Ctrl+X twice
                    if self.hook.enabled:
                        self.turn_off()
                    else:
                        self.turn_on()
                elif kind == "ocr":
                    self._ocr_done(*arg)
                elif kind == "save":   # give the app a moment to put the copied text on the clipboard
                    self.root.after(300, lambda h=arg: self.open_save(h))
        except queue.Empty:
            pass
        self._watch_active()
        # quick while the popup is open or sending, relaxed while hidden (Ctrl+V still opens in < 0.1 s)
        self.root.after(30 if (self.visible or self.busy) else 60, self._poll)

    def _watch_active(self):
        """Safety net: if you're working in another window, close the popup (it must never linger
        on top). Once the popup has been active, 0.7 s elsewhere closes it; if Windows never gave it
        focus at all, it closes after 8 s. Unsaved View edits are kept as a draft by hide()."""
        if not (self.visible and self.hwnd):
            self._away_since = None
            return
        now = time.time()
        if user32.GetForegroundWindow() == self.hwnd:
            self._active_seen = True
            self._away_since = None
            return
        if self._away_since is None:
            self._away_since = now
        elif now - self._away_since > (0.7 if self._active_seen else 8.0):
            self._away_since = None
            self.hide()

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
        if self._screen == "files" and self._files_sig == self._files_signature() \
                and self._files_img == clip_has_image():   # picture row only shows when a picture is copied
            self._open(target_hwnd, self._refresh_files)   # nothing changed: reuse the menu (instant)
        else:
            self._open(target_hwnd, self.show_files)

    def _files_signature(self):
        """Changes when a snippet file is added, removed, renamed or edited."""
        sig = []
        for p in list_files():
            try:
                st = p.stat()
                sig.append((p.name, st.st_mtime_ns, st.st_size))
            except OSError:
                sig.append((p.name, 0, 0))
        return tuple(sig)

    def _refresh_files(self):
        """Re-show the main menu we already built: new clipboard preview, empty search, default hint."""
        clip_item = self.all_items[0] if self.all_items else None
        if clip_item is not None:
            clip_item["sub"] = self._clip_preview()
            built = self._row_cache.get(id(clip_item))
            if built is not None and len(built[1]) > 3:
                built[1][3].config(text=clip_item["sub"])
        self.undo_action = None
        self.hint.config(text=self._files_hint)
        if self.search_entry is not None and self.search_entry.get():
            self.search_entry.delete(0, "end")      # clearing it re-shows every row
        else:
            self._filter("")

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
        self.target_focus = focused_control(target_hwnd)   # the box you were typing in (before we take focus)
        self._active_seen = False
        self._away_since = None
        screen()
        self._place_at_cursor()
        self.root.deiconify()
        self.root.update_idletasks()
        self.hwnd = user32.GetAncestor(self.root.winfo_id(), GA_ROOT)
        force_foreground(self.hwnd)
        self.root.focus_force()
        self.visible = True
        self._focus_typing_box()
        # Windows sometimes hands focus over a moment later: make sure again
        self.root.after(60, self._focus_typing_box)
        self.root.after(200, self._focus_typing_box)

    def _focus_typing_box(self):
        """Put the cursor in the search box (or the screen's text box) so you can type right away."""
        if not self.visible:
            return
        target = self.search_entry if self.search_entry is not None else self.initial_focus
        if target is None or not target.winfo_exists():
            return
        current = self.root.focus_get()
        if current is not None and current is not self.root and current is not target:
            return                       # you already clicked into another box: leave it
        if user32.GetForegroundWindow() != self.hwnd:
            force_foreground(self.hwnd)
        target.focus_force()
        if isinstance(target, tk.Entry):
            target.icursor("end")

    def hide(self):
        if self.on_leave:
            self.on_leave()                # View: keep unsaved edits as a draft (restored next time)
            self.on_leave = None
        self.root.withdraw()
        self.visible = False
        if self._screen != "files":
            self.root.after(400, self._prebuild_menu)

    def _prebuild_menu(self):
        """While hidden, quietly build the main menu so the next Ctrl+V shows it instantly."""
        if not self.visible and self._screen != "files":
            self.show_files()

    def _check_focus(self):
        if self.visible and self.root.focus_get() is None:
            self.hide()                    # clicked somewhere else: always close (drafts are kept)

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
        if self.on_leave:
            self.on_leave()                # leaving the View: keep unsaved edits as a draft
            self.on_leave = None
        for child in self.body.winfo_children():
            child.destroy()
        self.title_lbl.config(text=title)
        self.width = POPUP_WIDTH
        self.undo_action = None
        self.viewer = None
        self.view_editing = False
        self.leave_guard = None
        self.on_leave = None
        self.initial_focus = None
        self._ocr_waiting = False
        self.screen_keys = {}
        self._tip_saved = None
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
            tk.Label(box, text=ICON["search"], bg=BADGE_BG, fg=SUB, font=(ICON_FONT, 9)).pack(
                side="left", padx=(8, 4))
            var = tk.StringVar()
            entry = tk.Entry(box, textvariable=var, bg=BADGE_BG, fg=FG, insertbackground=FG,
                             relief="flat", font=(FONT, 10))
            entry.pack(side="left", fill="x", expand=True, ipady=4, padx=(0, 6))
            var.trace_add("write", lambda *a: self._filter_soon())
            self._search_var = var        # keep it alive: if Python frees it, Tk stops reporting typing
            # runs before the Entry deletes the character: remember if it was already empty
            entry.bind("<BackSpace>", lambda e: setattr(self, "_bs_was_empty", not entry.get()))
            entry.bind("<Key>", self._search_shortcut)
            self.search_entry = entry
            entry.focus_set()
            tk.Label(box, text="type to search", bg=BADGE_BG, fg=DIM, font=(FONT, 7)).pack(side="right", padx=6)
        self.list_frame = tk.Frame(self.body, bg=BG)
        self.list_frame.pack(fill="both", expand=True)
        self._row_cache, self._empty_lbl, self._more_lbl = {}, None, None   # rows are built once per screen
        self._screen = None
        if self._filter_job is not None:           # a search waiting from the previous screen
            self.root.after_cancel(self._filter_job)
            self._filter_job = None
        self._applied_query = ""
        self._render_rows(items)

    def _search_shortcut(self, e):
        """With the search box still empty, 0-9 / + / ? pick items; once you type, it's all search."""
        if self.search_entry.get() or not e.char:
            return None
        for i, it in enumerate(self.items):
            if it.get("key") and it["key"] == e.char:
                self.activate(i)
                return "break"
        if e.char in self.screen_keys:
            self.screen_keys[e.char]()
            return "break"
        return None

    def _filter_soon(self):
        """Search re-filters once you pause typing (one layout pass instead of one per letter)."""
        if self._filter_job is not None:
            self.root.after_cancel(self._filter_job)
        self._filter_job = self.root.after(90, self._flush_filter)

    def _search_is_stale(self):
        """True if the search box text differs from the search the list currently shows."""
        if self.search_entry is None or not self.search_entry.winfo_exists():
            return False
        return self._filter_job is not None or self.search_entry.get() != self._applied_query

    def _flush_filter(self):
        """Apply a pending search now (Enter / arrows / clicks always act on the latest results)."""
        if self._filter_job is not None:
            self.root.after_cancel(self._filter_job)
            self._filter_job = None
        if self.search_entry is not None and self.search_entry.winfo_exists() \
                and self.search_entry.get() != self._applied_query:
            self._filter(self.search_entry.get())

    def _filter(self, query):
        self._applied_query = query
        words = query.lower().split()

        def haystack(it):
            return (it.get("text") or f"{it['label']} {it.get('sub', '')}").lower()
        shown = [it for it in self.all_items
                 if it.get("always") or all(w in haystack(it) for w in words)]
        if [id(it) for it in shown] == [id(it) for it in self.items] and not self._more_lbl:
            self.select(self._first_selectable())    # same rows as now: no re-layout needed
            return
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

    def _index_of(self, item):
        for i, it in enumerate(self.items):
            if it is item:
                return i
        return -1

    def _render_rows(self, items, limit=12):
        """Shows `items` in order. Each row is built ONCE per screen; searching only hides and
        shows rows again, because creating widgets is the slow part (about 1 ms each on some PCs)."""
        for w in self.list_frame.pack_slaves():
            w.pack_forget()
        normal = [it for it in items if not it.get("always")]
        hidden = max(0, len(normal) - limit)
        self.items, self.rows = normal[:limit] + [it for it in items if it.get("always")], []
        wrap = int((POPUP_WIDTH - 70) * self.scale)

        if not items:
            if self._empty_lbl is None:
                self._empty_lbl = tk.Label(self.list_frame, text="Nothing here.", bg=BG, fg=SUB, font=(FONT, 9))
            self._empty_lbl.pack(pady=10)
        for item in self.items:
            built = self._row_cache.get(id(item))
            if built is None:
                built = self._build_row(item, wrap)
                self._row_cache[id(item)] = built
            built[0].pack(fill="x", pady=1)
            self.rows.append(built[1])
        if hidden:
            if self._more_lbl is None:
                self._more_lbl = tk.Label(self.list_frame, text="", bg=BG, fg=DIM, font=(FONT, 8))
            self._more_lbl.config(text=f"+ {hidden} more, type to narrow down")
            self._more_lbl.pack(pady=(2, 0))
        self.sel = 0
        self.select(self._first_selectable())
        self._resize()

    def _build_row(self, item, wrap):
        """Creates one row's widgets. Returns (row frame, widgets that get the hover colour)."""
        row = tk.Frame(self.list_frame, bg=ROW, cursor="hand2")
        color = item.get("color")
        if item.get("icon"):
            badge = tk.Label(row, text=ICON[item["icon"]], width=2, bg=BADGE_BG, fg=BORDER,
                             font=(ICON_FONT, 10))
        else:
            badge = tk.Label(row, text=item.get("badge", item.get("key", "")), width=2, bg=color or BADGE_BG,
                             fg=BG if color else FG, font=(FONT, 9, "bold"))
        extras = []                # small buttons on the right (they follow the row's hover colour)
        badge.pack(side="left", padx=(6, 8), pady=6, anchor="n")
        def icon(name, fn, tip, hover_fg=FG, pad=(0, 0)):
            b = self._icon_btn(row, name, fn, tip, size=10, hover_fg=hover_fg, bg=ROW)
            b.pack(side="right", padx=pad, pady=5, anchor="n")
            extras.append(b)
        if item.get("remove"):   # bin on the far right: remove this file / entry
            icon("delete", item["remove"], "Remove (a backup is kept, Ctrl+Z undoes it)", hover_fg=WARN, pad=(0, 4))
        if item.get("change"):   # pen: edit the saved entry / rename the file
            icon("edit", item["change"], "Edit / rename")
        if item.get("toggle"):   # Hide / Show: adds or removes the * in the header
            t_text, t_action = item["toggle"]
            hiding = "Hide" in t_text
            icon("lock" if hiding else "unlock", t_action,
                 "Hide the text (shows **********)" if hiding else "Show the text again")
        if item.get("check"):    # a tick box on the right, e.g. "☑ Enter" (clicking it doesn't send)
            chk = item["check"]
            box = tk.Label(row, text="", bg=BADGE_BG, font=(FONT, 9, "bold"), cursor="hand2", padx=8, pady=2)
            box.pack(side="right", padx=(4, 6), pady=6, anchor="n")

            def show_state(chk=chk, box=box):
                box.config(text=("☑ " if chk["on"] else "☐ ") + chk["text"],
                           fg=FG if chk["on"] else DIM, bg=BORDER if chk["on"] else BADGE_BG)

            def flip(_e=None, chk=chk, show_state=show_state):
                chk["on"] = not chk["on"]
                show_state()
                return "break"
            box.bind("<Button-1>", flip)
            show_state()
        if item.get("view"):     # eye: view / edit the whole file
            icon("view", item["view"], "View / edit the whole file")
        text = tk.Frame(row, bg=ROW)
        text.pack(side="left", fill="x", expand=True, pady=4)
        main = tk.Label(text, text=item["label"], bg=ROW, fg=color or FG,
                        font=(FONT, 10, "bold") if color else (FONT, 10), anchor="w", justify="left")
        main.pack(fill="x")
        widgets = [row, text, main]
        if item.get("sub"):
            sub = tk.Label(text, text=item["sub"], bg=ROW, fg=SUB, font=(FONT, 8), anchor="w",
                           justify="left", wraplength=wrap)
            sub.pack(fill="x")
            widgets.append(sub)
        for w in widgets + [badge]:
            # look the row up when clicked: its position changes while you search
            w.bind("<Button-1>", lambda e, it=item: self.activate(self._index_of(it)))
            w.bind("<Enter>", lambda e, it=item: self.select(self._index_of(it)))
            if item.get("edit"):   # right-click = edit, whatever the mode
                w.bind("<Button-3>", lambda e, f=item["edit"]: f())
            elif item.get("context"):   # right-click on a file = view it
                w.bind("<Button-3>", lambda e, f=item["context"]: f())
        return row, widgets + extras

    def _first_selectable(self):
        """Which row Enter picks. Main menu with an empty search: '0 Paste clipboard normally', so
        Ctrl+V then Enter pastes as usual. Once you type a search: the first matching result."""
        if not self._applied_query.strip():
            return 0
        for i, it in enumerate(self.items):
            if not it.get("paste_row"):
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
        if self.viewer is not None and self.viewer.winfo_exists():
            if not self.view_editing:
                self.viewer.yview_scroll(d * 3, "units")     # arrows scroll the read-only view
            return
        self._flush_filter()
        self.select(self.sel + d)

    def _wheel(self, e):
        if self.viewer is not None and self.viewer.winfo_exists():
            self.viewer.yview_scroll(-int(e.delta / 120) * 3, "units")
            return "break"
        return None

    def activate(self, i):
        target = self.items[i] if 0 <= i < len(self.items) else None
        if self._search_is_stale():          # you acted right after typing: use the newest results
            was_selection = i == self.sel
            self._flush_filter()
            if was_selection:                # Enter -> the highlighted (top) new result
                target = self.items[self.sel] if self.items else None
            elif target is not None and self._index_of(target) < 0:
                return                       # the row you clicked is no longer in the results
        if target is not None:
            target["action"]()

    def on_key(self, e):
        if self._typing():             # typing goes into the text box, not the shortcuts
            return
        for i, it in enumerate(self.items):
            if e.char and it.get("key") == e.char:
                self.activate(i)
                return
        if e.char and e.char in self.screen_keys:
            self.screen_keys[e.char]()

    def go_back(self, key=None):
        focus = self.root.focus_get()
        if key and focus is self.viewer and (self.view_editing or key == "Left"):
            return                     # editing (or Left scrolling the read-only view)
        if key and focus is not self.viewer and (self._in_editor() or self._in_form_entry()
                                                 or isinstance(focus, tk.Text)):
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
        files_sig = self._files_signature()
        items = [{"key": "", "icon": "paste", "paste_row": True,   # Enter = paste normally (it's highlighted)
                  "label": "Paste clipboard normally", "sub": self._clip_preview(),
                  "action": self.normal_paste}]
        has_img = clip_has_image()
        if has_img:                       # a picture is copied: offer to read the text in it
            items.append({"key": "", "icon": "picture", "label": "Text from copied picture",
                          "sub": "reads the words in the picture (Windows OCR, stays on this PC), "
                                 "then Send / Type / Paste / Copy",
                          "text": "text from copied picture image screenshot ocr read scan",
                          "action": self.read_picture})
        colored = 0
        for n, path in enumerate(list_files(), start=1):
            try:
                secs, free = load_options(path)
                sub = (f"freestyle • {len(secs)} snippet(s), searchable" if free
                       else f"{len(secs)} section(s): " + ", ".join(s["title"] for s in secs))
            except OSError as exc:
                secs, sub = [], f"cannot read: {exc}"
            color = None
            if path.name.lower() != "freestyle.txt":
                color = FILE_COLORS[colored % len(FILE_COLORS)]
                colored += 1
            items.append({"key": str(n) if n < 10 else "", "label": path.stem, "color": color,
                          "sub": sub if len(sub) <= 46 else sub[:45] + "\u2026",   # one tidy line
                          "text": f"{path.stem} {sub}",
                          "action": lambda p=path: self.show_sections(p),
                          "view": lambda p=path: self.show_view(p, back=self.show_files),
                          "remove": None if color is None else (lambda p=path: self._remove_file(p)),
                          "change": None if color is None else (lambda p=path: self.show_rename(p)),
                          "context": lambda p=path: self.show_view(p, back=self.show_files)})
        hint = "Enter paste • type to search • 1-9 + ? • ↑↓ • Tab mode"
        if not any(it.get("view") for it in items):     # no snippet files at all yet
            items.insert(1, {"key": "", "label": "Welcome! You have no snippet files yet",
                             "sub": "Click New file below (or copy text and press Ctrl+C twice). "
                                    f"The folder {SNIPPET_DIR.name}\\ is created for you.",
                             "action": self.show_new_file})
        self.render("Clipboard snippets", items, hint=hint, search=True)
        save_copied = lambda: self.show_save(clip_get() or "")
        self._action_bar([
            ("add", "New file", "Create an empty snippet file (a new person / topic)", self.show_new_file),
            ("save", "Save copied", "Save what you copied as a snippet  (+, or Ctrl+C twice)", save_copied),
            ("keyboard", "Type clipboard", "Types what you copied key by key, for boxes that block paste",
             self.type_clipboard),
        ])
        self.screen_keys = {"+": save_copied, "?": self.show_help}
        self._screen, self._files_sig, self._files_hint = "files", files_sig, hint   # Ctrl+V can reuse it
        self._files_img = has_img

    # ---- picture -> text
    def read_picture(self):
        """Reads the text in the copied picture (in the background), then opens it in the editor."""
        self._ocr_token += 1
        token = self._ocr_token
        back = self.show_files
        self.render("Reading the picture…", [{"key": "", "label": "Cancel", "sub": "", "action": back}],
                    back=back, hint="usually 1-2 seconds (the first time can take longer) • Esc close")
        self._note(self.body, "Windows is reading the text in the picture you copied. "
                              "Nothing is sent anywhere: it all happens on this PC.")
        self._ocr_waiting = True
        self._resize()

        def job():
            try:
                lines, err = ocr_clipboard_image()
            except Exception as exc:          # never leave the screen waiting forever
                lines, err = None, f"Could not read the picture: {exc}"
            self.events.put(("ocr", (token, lines, err)))
        threading.Thread(target=job, daemon=True).start()

    def _ocr_done(self, token, lines, err):
        if token != self._ocr_token or not (self.visible and self._ocr_waiting):
            return                            # cancelled, closed, or an older request
        self._ocr_waiting = False
        if err:
            self.render("Text from picture", [
                {"key": "1", "label": "Try again", "sub": "copy the picture again first if it changed",
                 "action": self.read_picture},
                {"key": "2", "label": "Back", "sub": "", "action": self.show_files}],
                back=self.show_files, hint="1 try again • 2 back • Esc close")
            self._note(self.body, err, color=WARN)
            self._resize()
            return

        def copy_text(text):
            if clip_set(text.strip("\n").replace("\n", "\r\n")):
                self.hide()                   # now a normal paste (Ctrl+V, Enter) gives the text
            else:
                self.hint.config(text="Could not copy, try again")

        self.show_editor("Text from picture", lines, back=self.show_files, fill=False,
                         note="Check the text first: OCR can mix up look-alike characters (0/O, 1/l/I, rn/m).",
                         extra=[("Copy text", copy_text),
                                ("Save as snippet", lambda text: self.show_save(text))])

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

    def choose(self, title, lines, back, hidden=False):
        """What a click on a snippet does, depending on the mode chip."""
        mode = self.settings["mode"]
        if mode == "confirm" or (hidden and mode == "edit"):   # hidden text never opens the editor
            self.show_confirm(title, lines, back, hidden=hidden)
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

    def show_confirm(self, title, lines, back, hidden=False):
        filled = fill_placeholders(lines, self.current_stem)

        def summary():
            n = sum(1 for l in filled if l.strip())
            return f"1 message ({n} lines)" if self.settings["one_message"] and n > 1 else f"{n} message(s)"

        # each option has its own "Enter" tick box: on for Send, off for Type / Paste (fresh every time)
        enter_send, enter_type, enter_paste = ({"text": "Enter", "on": True}, {"text": "Enter", "on": False},
                                               {"text": "Enter", "on": False})
        items = [
            {"key": "1", "label": "Send", "sub": "types it fast", "check": enter_send,
             "action": lambda: self.send_lines(lines, enter=enter_send["on"], method="fast")},
            {"key": "2", "label": "Type", "sub": "types every key (for boxes that block paste)",
             "check": enter_type,
             "action": lambda: self.send_lines(lines, enter=enter_type["on"], method="keys")},
            {"key": "3", "label": "Paste", "sub": "pastes it with the clipboard", "check": enter_paste,
             "action": lambda: self.send_lines(lines, enter=enter_paste["on"], method="clipboard")},
        ]
        if not hidden:
            items.append({"key": "4", "label": "Edit", "sub": "change the text first",
                          "action": lambda: self.show_editor(title, lines, back)})
        items.append({"key": str(len(items) + 1), "label": "Cancel", "sub": "", "action": back})
        self.render(f"Send? \u2022 {title}", items, back=back,
                    hint="1 send \u2022 2 type \u2022 3 paste \u2022 \u2611 = Enter after each line" if hidden
                    else "1-3 send / type / paste \u2022 4 edit \u2022 \u2611 = Enter after each line")
        preview = tk.Frame(self.body, bg=BADGE_BG)
        preview.pack(fill="x", padx=4, pady=(0, 4), before=self.list_frame)
        tk.Label(preview, text=self._target_label(), bg=BADGE_BG, fg=BORDER, font=(FONT, 8, "bold"),
                 anchor="w").pack(fill="x", padx=8, pady=(6, 2))
        wrap = int((POPUP_WIDTH - 50) * self.scale)
        n = 0
        for line in filled:
            if not line.strip():               # a blank line in the snippet: shown as a gap
                tk.Frame(preview, bg=BADGE_BG, height=int(8 * self.scale)).pack(fill="x")
                continue
            n += 1
            tk.Label(preview, text=f"{n}.  {MASK if hidden else line}", bg=BADGE_BG, fg=FG, font=(FONT, 9),
                     anchor="w", justify="left", wraplength=wrap).pack(fill="x", padx=8)
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
                lock = "\U0001F512 " if sec.get("hidden") else ""
                items.append({"key": key, "label": f"{sec['title']}  \u203a",
                              "sub": f"menu \u2022 {len(opts)} {what}" + ("  \u2022 hidden" if lock else ""),
                              "text": sec["title"] if lock else " ".join([sec["title"]] + sec["lines"]),
                              "action": lambda s=sec: self.show_menu(path, s)})
            else:
                items.append(self._snippet_item(sec, key, freestyle, back))
            items[-1]["remove"] = self._remover_for(path, sec, back, section=not freestyle)
            if sec.get("span"):
                items[-1]["change"] = (lambda s_=sec: self.show_edit_entry(
                    path, s_, "block" if freestyle else "section", back))
            if not freestyle and sec.get("header"):
                hide = not sec.get("hidden")
                items[-1]["toggle"] = ("\U0001F512 Hide" if hide else "\U0001F513 Show",
                                       lambda s_=sec, h=hide: self._toggle_hidden(path, s_, h, back))
        add_title = "Add snippet" if freestyle else "Add entry"
        items.append({"key": "", "label": add_title, "icon": "add", "always": True,
                      "sub": ("type a new snippet and save it" if freestyle
                              else "type it, pick the type (:  ::  :::) and save"),
                      "action": lambda: self.show_save("", target=path, title=add_title, back=back)})
        items.append({"key": "", "label": "View whole file", "icon": "view", "always": True,
                      "sub": f"see and edit {path.name}",
                      "action": lambda: self.show_view(path, back=back)})
        hint = ("type to search • Enter pick • right-click edit • Tab mode" if freestyle
                else "type to search • 1-9 pick • right-click edit • Tab mode")
        self.render(path.stem, items, back=self.show_files, hint=hint, search=True)

    def show_menu(self, path, sec):
        """Submenu for 'name::' (one choice per line) and 'name:::' (blocks, searchable)."""
        blocks = sec["kind"] == "blocks"
        opts = sec["options"]
        title_key, kind_key = sec["title"], sec["kind"]
        back = lambda: self._reopen_menu(path, title_key, kind_key)   # re-reads the file (after add/remove)
        items = [self._snippet_item(o, "" if blocks or n > 9 else str(n), blocks, back)
                 for n, o in enumerate(opts, start=1)]
        for item, o in zip(items, opts):
            item["remove"] = self._remover_for(path, o, back)
            item["change"] = (lambda o_=o: self.show_edit_entry(path, o_, "block" if blocks else "choice", back))
        ranges = section_ranges(file_lines(path))
        sec_index = next((i for i, r in enumerate(ranges) if r[2] == sec.get("line_no")), 0)
        add_title = "Add snippet" if blocks else "Add choice"
        items.append({"key": "", "label": add_title, "icon": "add", "always": True,
                      "sub": f"adds to '{sec['title']}'",
                      "action": lambda: self.show_save("", target=path, how="add", sec=sec_index,
                                                       title=add_title, back=back)})
        hint = ("type to search • Enter pick • right-click edit • Backspace back" if blocks
                else "type to search • 1-9 pick • right-click edit • Backspace back")
        self.render(f"{path.stem} › {sec['title']}", items,
                    back=lambda: self.show_sections(path), hint=hint, search=True)

    def _snippet_item(self, opt, key, block_style, back):
        """A row that sends its lines (respecting the Auto/Confirm/Edit mode)."""
        lines = opt["lines"]
        if opt.get("hidden"):
            # hidden ('name*:'): never show the text, keep it out of search, no Edit
            secret_title = bool(opt.get("secret_title"))
            title = "hidden" if secret_title else opt["title"]
            return {"key": key, "label": MASK if secret_title else title,
                    "sub": f"hidden \u2022 {sum(1 for l in lines if l.strip())} line(s)   {MASK}",
                    "text": "\U0001F512" if secret_title else title,
                    "action": lambda: self.choose(title, lines, back=back, hidden=True)}
        if block_style:   # label = first line, sub = the rest
            rest = lines[1:4]
            sub = (f"{len(lines)} line(s) • " + " ↵ ".join(rest) + (" …" if len(lines) > 4 else "")
                   if rest else "1 line")
        elif len(lines) == 1 and lines[0] == opt["title"]:
            sub = ""      # a single-line choice: the label already is the message
        else:
            real = [l for l in lines if l.strip()]       # blank lines kept for sending, not counted
            preview = " ↵ ".join(real[:3]) + (" …" if len(real) > 3 else "")
            sub = f"{len(real)} line(s) • {preview}" if real else "(empty)"
        title = opt["title"]
        return {"key": key, "label": title, "sub": sub, "text": " ".join([title] + lines),
                "action": lambda: self.choose(title, lines, back=back),
                "edit": lambda: self.show_editor(title, lines, back=back)}

    def show_editor(self, title, lines, back, fill=True, note="", extra=()):
        """Edit the lines before sending. Each non-blank line is sent, then Enter.
        extra = [(label, fn(text))] adds more buttons (e.g. Copy / Save for text read from a picture)."""
        if fill:
            lines = fill_placeholders(lines, self.current_stem)
        self.render(f"Edit \u2022 {title}", [], back=back,
                    hint="Ctrl+Enter send \u2022 Ctrl+Shift+Enter paste \u2022 Esc close")
        self.width = SAVE_WIDTH            # room for Send / Type / Paste / Cancel
        for child in self.list_frame.winfo_children():
            child.destroy()
        tk.Label(self.list_frame, text=self._target_label(), bg=BG, fg=BORDER, font=(FONT, 8, "bold"),
                 anchor="w").pack(fill="x", padx=6, pady=(0, 2))
        if note:
            self._note(self.list_frame, note, color=WARN).pack_configure(pady=(0, 4))
        box = tk.Frame(self.list_frame, bg=BADGE_BG)
        box.pack(fill="both", expand=True, padx=4, pady=(0, 6))
        editor = tk.Text(box, bg=BADGE_BG, fg=FG, insertbackground=FG, relief="flat", wrap="word",
                         font=(FONT, 10), height=min(max(len(lines) + 1, 4), 12), undo=True,
                         padx=6, pady=6, selectbackground=HOVER)
        editor.pack(fill="both", expand=True)
        editor.insert("1.0", "\n".join(lines))
        editor.edit_reset()
        self.editor = editor

        def send(_e=None, enter=True, method="fast"):
            out = [l.rstrip() for l in editor.get("1.0", "end").splitlines()]   # blank lines kept
            if out:
                self.send_lines(out, enter=enter, method=method, fill=fill)
            return "break"

        self._one_message_toggle(self.list_frame)
        # "press Enter after each line" per button: on for Send, off for Type / Paste (fresh every time)
        enter_on = {"fast": True, "keys": False, "clipboard": False}
        ticks = tk.Frame(self.list_frame, bg=BG)
        ticks.pack(fill="x", padx=6, pady=(4, 0))
        tk.Label(ticks, text="Press Enter after each line:", bg=BG, fg=SUB, font=(FONT, 8)).pack(side="left")
        for method, name in (("clipboard", "Paste"), ("keys", "Type"), ("fast", "Send")):
            tick = tk.Label(ticks, text="", bg=BADGE_BG, font=(FONT, 8, "bold"), cursor="hand2", padx=6, pady=1)
            tick.pack(side="right", padx=(4, 0))

            def show_tick(m=method, n=name, t=tick):
                on = enter_on[m]
                t.config(text=("\u2611 " if on else "\u2610 ") + n, fg=FG if on else DIM, bg=BORDER if on else BADGE_BG)

            def flip_tick(_e=None, m=method, show=show_tick):
                enter_on[m] = not enter_on[m]
                show()
            tick.bind("<Button-1>", flip_tick)
            show_tick()
        buttons = tk.Frame(self.list_frame, bg=BG)
        buttons.pack(fill="x", padx=4, pady=(6, 2))
        for label, cmd, color in (("Send", lambda e=None: send(enter=enter_on["fast"], method="fast"), BORDER),
                                  ("Type", lambda e=None: send(enter=enter_on["keys"], method="keys"), BADGE_BG),
                                  ("Paste", lambda e=None: send(enter=enter_on["clipboard"], method="clipboard"),
                                   BADGE_BG),
                                  ("Cancel", lambda e=None: back(), BADGE_BG)):
            b = tk.Label(buttons, text=label, bg=color, fg=FG, font=(FONT, 9, "bold"),
                         cursor="hand2", padx=12, pady=4)
            b.pack(side="right", padx=(6, 0))
            b.bind("<Button-1>", lambda e, c=cmd: c())
        if extra:                          # e.g. Copy text / Save as snippet (text read from a picture)
            more = tk.Frame(self.list_frame, bg=BG)
            more.pack(fill="x", padx=4, pady=(4, 2))
            for label, fn in extra:
                b = tk.Label(more, text=label, bg=BADGE_BG, fg=FG, font=(FONT, 9, "bold"),
                             cursor="hand2", padx=10, pady=3)
                b.pack(side="right", padx=(6, 0))
                b.bind("<Button-1>", lambda e, f=fn: f(editor.get("1.0", "end-1c")))
                b.bind("<Enter>", lambda e, w=b: w.config(bg=BORDER))
                b.bind("<Leave>", lambda e, w=b: w.config(bg=BADGE_BG))
        editor.bind("<Control-Return>", lambda e: send(enter=enter_on["fast"], method="fast"))
        editor.bind("<Control-Shift-Return>", lambda e: send(enter=enter_on["clipboard"], method="clipboard"))
        editor.focus_set()
        self.initial_focus = editor
        editor.mark_set("insert", "end-1c")
        self._resize()

    # ---- add / remove files and entries
    def _reopen_menu(self, path, title, kind):
        """Re-read the file and show the '::' / ':::' menu again (it may have changed)."""
        for sec in parse_sections(path) if path.exists() else []:
            if sec["title"] == title and sec["kind"] == kind:
                self.show_menu(path, sec)
                return
        self.show_sections(path) if path.exists() else self.show_files()

    def _remover_for(self, path, opt, back, section=False):
        """The bin button's action for a section / choice / block."""
        span = opt.get("span")
        if not span:
            return None
        hidden = opt.get("hidden")
        body = [MASK] * len(opt["lines"]) if hidden else opt["lines"]
        if section and opt.get("header"):
            expect, preview, title = [opt["header"]] + opt["lines"], [opt["header"]] + body, opt["title"]
        else:
            expect, preview = opt["lines"], body
            title = "hidden entry" if opt.get("secret_title") else opt["title"]
        return lambda: self._confirm_remove(title, preview, lambda: self._cut(path, span, expect), back)

    @staticmethod
    def _cut(path, span, expect):
        before = read_text(path)
        remove_span(path, span[0], span[1], expect)
        return lambda: path.write_text(before, encoding="utf-8", newline="\r\n")

    def _after_change(self, path, before, back, message):
        back()
        self.undo_action = lambda: (path.write_text(before, encoding="utf-8", newline="\r\n"), back())
        self.hint.config(text=f"{message} \u2022 Ctrl+Z to undo")

    def show_rename(self, path):
        """Pen on a file: rename it (the name shown in the menu)."""
        self.render(f"Rename \u2022 {path.name}", [], back=self.show_files, hint="Enter rename \u2022 Esc close")
        lf = self.list_frame
        for c in lf.winfo_children():
            c.destroy()
        self._section(lf, "NEW NAME")
        var = tk.StringVar(value=path.stem)
        entry = self._entry(lf, var, "this is the name in the menu; the file becomes <name>.txt")
        msg = self._note(lf, "")

        def rename(_e=None):
            name = safe_filename(var.get())
            if not name:
                msg.config(text="Type a name.", fg=WARN)
                return "break"
            if name == path.stem:
                self.show_files()
                return "break"
            new = path.with_name(f"{name}.txt")
            if new.exists() and new.resolve() != path.resolve():
                msg.config(text=f"{new.name} already exists.", fg=WARN)
                return "break"
            if name.lower() == "freestyle":
                msg.config(text="'freestyle' is the special quick-save file - pick another name.", fg=WARN)
                return "break"
            try:
                path.rename(new)
            except OSError as exc:
                msg.config(text=f"Could not rename: {exc}", fg=WARN)
                return "break"
            if self.settings.get("last_save") == path.name:
                self.settings["last_save"] = new.name
                save_settings(self.settings)
            if path in self.drafts:
                self.drafts[new] = self.drafts.pop(path)
            self.show_files()
            self.undo_action = lambda: (new.rename(path) if not path.exists() else None, self.show_files())
            self.hint.config(text=f"Renamed to '{name}' \u2022 Ctrl+Z to undo")
            return "break"
        entry.bind("<Return>", rename)
        self._buttons(lf, [("Rename \u2713", rename, BORDER), ("Cancel", self.show_files, BADGE_BG)])
        entry.focus_set()
        entry.select_range(0, "end")
        self.initial_focus = entry
        self._resize()

    def show_edit_entry(self, path, opt, what, back):
        """Pen on an entry: change what is saved in the file.
        what = 'section' (name / type / hidden / text), 'choice' (one line), 'block' (a few lines)."""
        hidden = bool(opt.get("hidden"))
        name_for_title = "hidden entry" if opt.get("secret_title") else opt["title"]
        self.render(f"Edit saved {what} \u2022 {name_for_title}", [], back=back,
                    hint="Ctrl+S / Enter save \u2022 Esc close")
        self.width = SAVE_WIDTH
        lf = self.list_frame
        for c in lf.winfo_children():
            c.destroy()
        state = {"kind": opt.get("kind", "all"), "revealed": not hidden}
        if what == "section" and state["kind"] == "blocks":
            original_text = "\n\n".join("\n".join(o["lines"]) for o in opt.get("options", []))
        else:
            original_text = "\n".join(opt["lines"])
        name_var = tk.StringVar(value=opt["title"])
        hidden_var = tk.BooleanVar(value=hidden)
        widgets = {}

        if what == "section":
            self._section(lf, "NAME   (what you click in the menu)")
            name_entry = self._entry(lf, name_var, "")
            widgets["name"] = name_entry
            self._section(lf, "TYPE")
            holder = tk.Frame(lf, bg=BG)
            holder.pack(fill="x")

            def draw_kind():
                for c in holder.winfo_children():
                    c.destroy()
                self._chips(holder, [("all", ":   send all"), ("choices", "::   choices"), ("blocks", ":::   blocks")],
                            state["kind"], pick_kind)
                self._note(holder, KIND_HELP[state["kind"]], mono=True)

            def pick_kind(value):
                state["kind"] = value
                draw_kind()
                self._resize()
            draw_kind()
            tk.Checkbutton(lf, text="Hidden  (shows ********** in the menu; the real text is still sent)",
                           variable=hidden_var, bg=BG, fg=SUB, activebackground=BG, activeforeground=FG,
                           selectcolor=BADGE_BG, font=(FONT, 8), anchor="w", bd=0,
                           highlightthickness=0).pack(fill="x", padx=6, pady=(6, 0))

        self._section(lf, "TEXT" if what != "choice" else "THE LINE")
        area = tk.Frame(lf, bg=BG)
        area.pack(fill="x")

        def build_text():
            for c in area.winfo_children():
                c.destroy()
            if what == "choice":
                e = tk.Entry(area, bg=BADGE_BG, fg=FG, insertbackground=FG, relief="flat", font=(FONT, 10))
                e.pack(fill="x", padx=6, ipady=4)
                e.insert(0, original_text)
                e.bind("<Return>", save)
                widgets["text"] = e
            else:
                boxf = tk.Frame(area, bg=BADGE_BG)
                boxf.pack(fill="x", padx=6)
                t = tk.Text(boxf, bg=BADGE_BG, fg=FG, insertbackground=FG, relief="flat", wrap="word",
                            font=(FONT, 9), height=min(max(original_text.count("\n") + 2, 4), 12), undo=True,
                            padx=6, pady=4, selectbackground=HOVER)
                t.pack(fill="x")
                t.insert("1.0", original_text)
                t.edit_reset()
                widgets["text"] = t
            widgets["text"].bind("<Control-s>", save)
            widgets["text"].bind("<Control-S>", save)
            self._resize()

        def reveal(_e=None):
            state["revealed"] = True
            build_text()
            widgets["text"].focus_set()

        msg = self._note(lf, "")

        def current_text():
            if not state["revealed"]:
                return original_text
            w = widgets["text"]
            return w.get() if isinstance(w, tk.Entry) else w.get("1.0", "end-1c")

        def save(_e=None):
            span = opt["span"]
            try:
                before = read_text(path)
                if what == "section":
                    title = name_var.get().strip().rstrip(":").strip().rstrip("*").strip()
                    if not title:
                        msg.config(text="Type a name.", fg=WARN)
                        return "break"
                    for t_, k_, i_, j_ in section_ranges(file_lines(path)):
                        if i_ != opt.get("line_no") and t_.lower() == title.lower():
                            msg.config(text=f"'{t_}' already exists in this file - pick another name.", fg=WARN)
                            return "break"
                    kind = state["kind"]
                    header = title + ("*" if hidden_var.get() else "") + HEADER_COLONS[kind]
                    expect = ([opt["header"]] if opt.get("header") else []) + opt["lines"]
                    new = ([header] if opt.get("header") else []) + clean_lines(current_text(), kind)
                    label = title
                elif what == "choice":
                    line = current_text().strip()
                    if not line:
                        msg.config(text="The line is empty - use the bin to remove it instead.", fg=WARN)
                        return "break"
                    expect, new, label = opt["lines"], [line], "the choice"
                else:
                    new = clean_lines(current_text(), "blocks")
                    if not new:
                        msg.config(text="The text is empty - use the bin to remove it instead.", fg=WARN)
                        return "break"
                    expect, label = opt["lines"], "the snippet"
                lookalike = [l for l in new[1 if what == "section" and opt.get("header") else 0:] if header_info(l)]
                if lookalike and not msg.cget("text").startswith("\u26a0"):
                    msg.config(text=f"\u26a0 '{lookalike[0]}' ends with ':' so it will become a new header. "
                                    "Save again to keep it anyway.", fg=WARN)
                    return "break"
                replace_span(path, span[0], span[1], expect, new)
            except (OSError, ValueError) as exc:
                msg.config(text=f"Could not save: {exc}", fg=WARN)
                return "break"
            self._after_change(path, before, back, f"Saved {label}")
            return "break"

        if state["revealed"]:
            build_text()
        else:   # hidden: don't show the text until you ask for it
            cover = tk.Label(area, text="\U0001F512  " + MASK + "      click to show the text and edit it",
                             bg=BADGE_BG, fg=SUB, font=(FONT, 9), cursor="hand2", anchor="w", padx=8, pady=8)
            cover.pack(fill="x", padx=6)
            cover.bind("<Button-1>", reveal)
        if what == "section":
            widgets["name"].bind("<Return>", save)
        self._buttons(lf, [("Save \u2713", save, BORDER), ("Cancel", back, BADGE_BG)])
        first = widgets.get("name") or widgets.get("text")
        if first is not None:
            first.focus_set()
            self.initial_focus = first
        self._resize()

    def _toggle_hidden(self, path, sec, hide, back):
        try:
            before = read_text(path)
            set_header_hidden(path, sec["line_no"], sec["header"], hide)
        except (OSError, ValueError) as exc:
            self.hint.config(text=f"Could not change it: {exc}")
            return
        back()
        self.undo_action = lambda: (path.write_text(before, encoding="utf-8", newline="\r\n"), back())
        self.hint.config(text=(f"'{sec['title']}' is now hidden (shows **********)" if hide
                               else f"'{sec['title']}' is now shown") + " \u2022 Ctrl+Z to undo")

    def _remove_file(self, path):
        def move_away():
            dest_dir = SNIPPET_DIR / ".backup" / "deleted"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{path.stem} {datetime.now():%Y-%m-%d %H%M%S}.txt"
            path.replace(dest)
            return lambda: None if path.exists() else dest.replace(path)
        n = len(file_lines(path))
        self._confirm_remove(path.name, [f"{n} line(s) in this file",
                                         "it is moved to clipboard\\.backup\\deleted\\ (not lost)"],
                             move_away, self.show_files)

    def _confirm_remove(self, title, preview, do_remove, back):
        def run():
            try:
                undo = do_remove()
            except (OSError, ValueError) as exc:
                note.config(text=f"Could not remove: {exc}", fg=WARN)
                return
            back()
            self.undo_action = lambda: (undo(), back())
            self.hint.config(text=f"Removed '{title}' \u2022 Ctrl+Z to undo")
        items = [{"key": "1", "label": "Yes, remove it",
                  "sub": "a backup is kept \u2022 Ctrl+Z right after brings it back", "action": run},
                 {"key": "2", "label": "Cancel", "sub": "", "action": back}]
        self.render(f"Remove? \u2022 {title}", items, back=back, hint="1 remove \u2022 2 cancel \u2022 Esc close")
        box = tk.Frame(self.body, bg=BADGE_BG)
        box.pack(fill="x", padx=4, pady=(0, 4), before=self.list_frame)
        shown = preview[:8] + (["\u2026"] if len(preview) > 8 else [])
        tk.Label(box, text="\n".join(shown) or "(empty)", bg=BADGE_BG, fg=FG, font=("Consolas", 9), anchor="w",
                 justify="left", wraplength=int((POPUP_WIDTH - 40) * self.scale)).pack(fill="x", padx=8, pady=6)
        note = self._note(self.body, "")
        self.select(1)                 # Cancel is highlighted, so Enter never removes by accident
        self.root.focus_set()
        self._resize()

    def show_new_file(self):
        self.render("New text file", [], back=self.show_files, hint="Enter create \u2022 Esc close")
        lf = self.list_frame
        for c in lf.winfo_children():
            c.destroy()
        self._section(lf, "FILE NAME")
        var = tk.StringVar()
        entry = self._entry(lf, var, "e.g.  maria   \u2192  creates maria.txt, shown as 'maria' in the menu")
        msg = self._note(lf, "")

        def create(_e=None):
            name = safe_filename(var.get())
            if not name:
                msg.config(text="Type a file name.", fg=WARN)
                return "break"
            path = SNIPPET_DIR / f"{name}.txt"
            if path.exists():
                msg.config(text=f"{path.name} already exists.", fg=WARN)
                return "break"
            try:
                SNIPPET_DIR.mkdir(parents=True, exist_ok=True)   # first file ever: create the folder too
                path.write_text("", encoding="utf-8")
            except OSError as exc:
                msg.config(text=f"Could not create it: {exc}", fg=WARN)
                return "break"
            self.show_view(path, back=self.show_files,
                           flash=f"\u2713 Created {path.name}. Type your entries (e.g. 'greetings:' then the "
                                 "lines), then Ctrl+S. Or go back and use \uff0b Add entry.")
            return "break"
        entry.bind("<Return>", create)
        self._buttons(lf, [("Create \u2713", create, BORDER), ("Cancel", self.show_files, BADGE_BG)])
        entry.focus_set()
        self.initial_focus = entry
        self._resize()

    def _copy_twice_switch(self, parent):
        """On / Off for 'Ctrl+C twice opens Save copied text' (remembered)."""
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", padx=6, pady=(6, 0))
        tk.Label(row, text="Ctrl+C twice opens this save form:", bg=BG, fg=SUB, font=(FONT, 8)).pack(side="left")
        chips = {}

        def draw():
            on = self.settings.get("copy_twice", True)
            for value, lbl in chips.items():
                lbl.config(bg=BORDER if value == on else BADGE_BG)

        def pick(value):
            self.settings["copy_twice"] = value
            save_settings(self.settings)
            draw()
        for value, text in ((False, "Off"), (True, "On")):
            lbl = tk.Label(row, text=text, bg=BADGE_BG, fg=FG, font=(FONT, 8, "bold"), cursor="hand2", padx=8, pady=1)
            lbl.pack(side="right", padx=(4, 0))
            lbl.bind("<Button-1>", lambda e, v=value: pick(v))
            chips[value] = lbl
        draw()

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
    def show_save(self, text, target=None, how="new", sec=0, title="Save copied text", back=None):
        if target is None:
            last = SNIPPET_DIR / self.settings.get("last_save", FREESTYLE_FILE.name)
            target = last if last.exists() else FREESTYLE_FILE
        self.save = {"text": text.strip("\r\n"), "target": target, "how": how, "kind": "all", "sec": sec,
                     "name": tk.StringVar(), "filename": tk.StringVar(), "title": title,
                     "back": back or self.show_files}
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
        self.render(s["title"], [], back=s["back"], hint="Enter save \u2022 Esc close \u2022 ? help")
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
        self._buttons(lf, [("Save \u2713", self.do_save, BORDER), ("Cancel", s["back"], BADGE_BG)])
        self._copy_twice_switch(lf)
        self._save_preview()
        empty = [e for e in entries if not e.get()]
        self.initial_focus = empty[0] if empty else box
        self.initial_focus.focus_set()
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
            plan["before"] = read_text(plan["path"]) if plan["path"].exists() else None
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
            {"key": "4", "label": "Undo this save   (Ctrl+Z)",
             "sub": "removes the new file" if plan.get("before") is None else "puts the file back how it was",
             "action": lambda: self.undo_save(plan)},
        ]
        self.render("Saved \u2713", items, hint="Ctrl+Z undo \u2022 Esc close")
        self.undo_action = lambda: self.undo_save(plan)
        info = tk.Frame(self.body, bg=BADGE_BG)
        info.pack(fill="x", padx=4, pady=(0, 4), before=self.list_frame)
        tk.Label(info, text=plan["desc"], bg=BADGE_BG, fg=BORDER, font=(FONT, 8, "bold"), anchor="w",
                 justify="left", wraplength=int((POPUP_WIDTH - 40) * self.scale)).pack(fill="x", padx=8, pady=(6, 2))
        excerpt = plan["excerpt"]
        if excerpt and header_hidden(excerpt[0]):     # saved into a hidden section: don't show it
            excerpt = [excerpt[0]] + [MASK if l and not l.startswith("\u2026") else l for l in excerpt[1:]]
        shown = excerpt[:8] + (["\u2026"] if len(excerpt) > 8 else [])
        tk.Label(info, text="\n".join(l if l else " " for l in shown), bg=BADGE_BG, fg=FG, font=("Consolas", 8),
                 anchor="w", justify="left").pack(fill="x", padx=8, pady=(0, 6))
        self.root.focus_set()
        self._resize()

    def undo_save(self, plan):
        path, before = plan["path"], plan.get("before")
        try:
            if before is None:
                path.unlink(missing_ok=True)
                what = f"Removed {path.name}"
            else:
                path.write_text(before, encoding="utf-8", newline="\r\n")
                what = f"{path.name} is back to how it was"
        except OSError as exc:
            what = f"Could not undo: {exc}"
        self.render("Undone \u21b6", [{"key": "1", "label": "Done", "sub": what, "action": self.hide},
                                      {"key": "2", "label": "Back to the menu", "sub": "",
                                       "action": self.show_files}], hint="Esc close")
        self.root.focus_set()

    # ---- view / edit a whole file
    def show_view(self, path, back, editing=True, flash=""):
        """Whole file. Read-only by default (select + Ctrl+C copies only the file text);
        'Edit' makes it editable with Save (Ctrl+S). A backup is kept on every save."""
        lines = file_lines(path)
        original = "\n".join(lines)
        colors = {"all": BORDER, "choices": "#7ee2a8", "blocks": "#ffcf6e"}
        confirm = {"armed": False}

        state = {"baseline": original}

        def dirty():
            return editing and txt.winfo_exists() and txt.get("1.0", "end-1c").rstrip("\n") != state["baseline"]

        def guarded(action):
            """Run action, but warn once first if there are unsaved changes."""
            def run(*_):
                if dirty() and not confirm["armed"]:
                    confirm["armed"] = True
                    status.config(text="⚠ You have unsaved changes. Click Save, or do that again "
                                       "to discard them.", fg=WARN)
                    return
                action()
            return run

        def discard_and_back():
            self.drafts.pop(path, None)
            self.on_leave = None
            back()
        leave = guarded(discard_and_back)
        self.render(f"View \u2022 {path.name}", [], back=leave,
                    hint=("Ctrl+S save • Ctrl+Z undo • Esc close" if editing
                          else "scroll or ↑↓ • Backspace back • Esc close"))
        self.width = VIEW_WIDTH
        self.view_editing = editing
        lf = self.list_frame
        for c in lf.winfo_children():
            c.destroy()

        legend = tk.Frame(lf, bg=BG)
        legend.pack(fill="x", padx=6, pady=(0, 4))
        for kind, label in (("all", ":   send all lines"), ("choices", "::   menu, each line a choice"),
                            ("blocks", ":::   menu, blank lines split snippets")):
            tk.Label(legend, text=label, bg=BG, fg=colors[kind], font=("Consolas", 8, "bold")).pack(
                side="left", padx=(0, 14))

        # find bar: Ctrl+F, Enter = next, Shift+Enter = previous
        findbar = tk.Frame(lf, bg=BADGE_BG)
        findbar.pack(fill="x", padx=6, pady=(0, 4))
        tk.Label(findbar, text="\U0001F50D", bg=BADGE_BG, fg=SUB, font=(FONT, 9)).pack(side="left", padx=(6, 2))
        find_var = tk.StringVar()
        find_entry = tk.Entry(findbar, textvariable=find_var, bg=BADGE_BG, fg=FG, insertbackground=FG,
                              relief="flat", font=(FONT, 10))
        find_entry.pack(side="left", fill="x", expand=True, ipady=3)
        find_btns = {}
        for name, symbol in (("next", "▼"), ("prev", "▲")):
            b = tk.Label(findbar, text=symbol, bg=BADGE_BG, fg=FG, font=(FONT, 8), cursor="hand2", padx=6)
            b.pack(side="right", padx=(0, 2))
            find_btns[name] = b
        find_count = tk.Label(findbar, text="find in file  (Ctrl+F)", bg=BADGE_BG, fg=DIM, font=(FONT, 8))
        secret = {"show": False}
        if any(header_hidden(l) for l in lines):
            reveal = tk.Label(findbar, text="\U0001F512 show hidden", bg=BORDER, fg=FG, font=(FONT, 8, "bold"),
                              cursor="hand2", padx=8)
            reveal.pack(side="right", padx=(4, 2))

            def flip(_e=None):
                secret["show"] = not secret["show"]
                reveal.config(text="\U0001F513 hide again" if secret["show"] else "\U0001F512 show hidden")
                paint()
            reveal.bind("<Button-1>", flip)
        find_count.pack(side="right", padx=6)

        # as many lines as fit on this screen
        mono = ("Consolas", 10)
        line_px = tkfont.Font(root=self.root, family=mono[0], size=mono[1]).metrics("linespace")
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        _, top, _, bottom = work_area_at(pt.x, pt.y)
        fit = max(10, (bottom - top - int(270 * self.scale)) // max(line_px, 1))
        height = max(12, min(len(lines) + (4 if editing else 1), fit))

        # The text box holds ONLY the file's text, so copying gives exactly what's in the file.
        # Line numbers live in a separate column next to it (not selectable, not copied).
        frame = tk.Frame(lf, bg=BADGE_BG)
        frame.pack(fill="both", expand=True, padx=6)
        common = dict(bg=BADGE_BG, relief="flat", wrap="none", font=mono, height=height,
                      pady=6, highlightthickness=0, bd=0)
        gutter = tk.Text(frame, fg=DIM, width=4, padx=4, takefocus=0, cursor="arrow",
                         exportselection=False, **common)
        txt = tk.Text(frame, fg=FG, padx=8, selectbackground=HOVER, insertbackground=FG,
                      undo=True, cursor="xterm", **common)
        if editing:
            txt.config(bg="#262c3f", highlightthickness=1, highlightbackground=BORDER, highlightcolor=BORDER)
        ybar = tk.Scrollbar(frame, command=lambda *a: (txt.yview(*a), gutter.yview(*a)))
        xbar = tk.Scrollbar(lf, orient="horizontal", command=txt.xview)

        def on_scroll(first, last):
            ybar.set(first, last)
            gutter.yview_moveto(first)    # keep the numbers lined up with the text
        txt.config(yscrollcommand=on_scroll, xscrollcommand=xbar.set)
        ybar.pack(side="right", fill="y")
        gutter.pack(side="left", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        if editing or any(len(l) > 80 for l in lines):
            xbar.pack(fill="x", padx=6)
        for kind, color in colors.items():
            txt.tag_config(kind, foreground=color, font=(mono[0], mono[1], "bold"))
        txt.tag_config("secret", foreground="#3a4159", background="#3a4159")   # hidden lines = solid bars

        finder = {"hits": [], "i": -1, "refresh": None}
        painted = {"count": -1, "job": None}

        def paint(*_):
            """Colour the header lines and refresh the line numbers (one pass over the text)."""
            painted["job"] = None
            if not txt.winfo_exists():
                return
            for kind in list(colors) + ["secret"]:
                txt.tag_remove(kind, "1.0", "end")
            all_lines = txt.get("1.0", "end-1c").split("\n")
            count = len(all_lines)
            in_hidden = False
            for i, text_line in enumerate(all_lines, start=1):
                if text_line.rstrip().endswith(":"):
                    info = header_info(text_line)
                    if info:
                        txt.tag_add(info[1], f"{i}.0", f"{i}.end")
                        in_hidden = header_hidden(text_line)
                        continue
                if in_hidden and text_line.strip() and not secret["show"]:
                    txt.tag_add("secret", f"{i}.0", f"{i}.end")
            if count != painted["count"]:      # line numbers only change when the line count does
                painted["count"] = count
                gutter.config(state="normal", width=len(str(count)) + 1)
                gutter.delete("1.0", "end")
                gutter.insert("1.0", "\n".join(str(i) for i in range(1, count + 1)))
                gutter.config(state="disabled")
            gutter.yview_moveto(txt.yview()[0])
            if finder["refresh"]:
                finder["refresh"]()           # keep find highlights right while you edit
            if editing:
                changed = dirty()
                self.title_lbl.config(text=f"View • {path.name}" + ("  ● unsaved" if changed else ""))
                if not changed:
                    confirm["armed"] = False

        txt.insert("1.0", original)
        state["baseline"] = txt.get("1.0", "end-1c").rstrip("\n")   # compare with what the box really holds
        draft = self.drafts.get(path)
        if editing and draft is not None and draft != state["baseline"]:
            txt.delete("1.0", "end")
            txt.insert("1.0", draft)       # your unsaved changes from last time
            flash = flash or "\u21ba Restored the changes you hadn't saved yet. Save keeps them, Back throws them away."
        txt.edit_reset()
        paint()
        for ev in ("<Button-1>", "<B1-Motion>", "<Double-Button-1>"):
            gutter.bind(ev, lambda e: "break")        # numbers can't be selected
        select_all = lambda e: (txt.tag_add("sel", "1.0", "end-1c"), "break")[1]
        txt.bind("<Control-a>", select_all)
        txt.bind("<Control-A>", select_all)
        self.viewer = txt

        # ---- find in file
        txt.tag_config("hit", background="#5a4a1f")
        txt.tag_config("hit_now", background="#ffcf6e", foreground="#1e2230")
        txt.tag_raise("hit")
        txt.tag_raise("hit_now")
        txt.tag_raise("sel")

        def collect(jump):
            txt.tag_remove("hit", "1.0", "end")
            txt.tag_remove("hit_now", "1.0", "end")
            query, hits, start = find_var.get(), [], "1.0"
            while query:
                pos = txt.search(query, start, stopindex="end", nocase=True)
                if not pos:
                    break
                end = f"{pos}+{len(query)}c"
                if "secret" in txt.tag_names(pos):    # don't reveal hidden text through find
                    start = end
                    continue
                txt.tag_add("hit", pos, end)
                hits.append((pos, end))
                start = end
            finder["hits"] = hits
            if not query:
                finder["i"] = -1
                find_count.config(text="find in file  (Ctrl+F)", fg=DIM)
            elif not hits:
                finder["i"] = -1
                find_count.config(text="no matches", fg=WARN)
            elif jump:
                # first match at or after the cursor
                cursor = txt.index("insert")
                finder["i"] = next((n for n, (p, _) in enumerate(hits)
                                    if txt.compare(p, ">=", cursor)), 0)
                show_hit()
            else:
                finder["i"] = min(max(finder["i"], 0), len(hits) - 1)
                show_hit(scroll=False)

        def show_hit(scroll=True):
            pos, end = finder["hits"][finder["i"]]
            txt.tag_remove("hit_now", "1.0", "end")
            txt.tag_add("hit_now", pos, end)
            if scroll:
                txt.see(pos)
                txt.mark_set("insert", end)
            find_count.config(text=f"{finder['i'] + 1} of {len(finder['hits'])}", fg=SUB)

        def step(d):
            if finder["hits"]:
                finder["i"] = (finder["i"] + d) % len(finder["hits"])
                show_hit()
            return "break"

        def focus_find(_e=None):
            find_entry.focus_set()
            find_entry.select_range(0, "end")
            return "break"

        finder["refresh"] = lambda: collect(jump=False)
        find_var.trace_add("write", lambda *a: collect(jump=True))
        find_entry.bind("<Return>", lambda e: step(1))
        find_entry.bind("<Shift-Return>", lambda e: step(-1))
        find_entry.bind("<Down>", lambda e: step(1))
        find_entry.bind("<Up>", lambda e: step(-1))
        for w in (txt, find_entry):
            w.bind("<Control-f>", focus_find)
            w.bind("<Control-F>", focus_find)
            w.bind("<F3>", lambda e: step(1))
            w.bind("<Shift-F3>", lambda e: step(-1))
        find_btns["next"].bind("<Button-1>", lambda e: step(1))
        find_btns["prev"].bind("<Button-1>", lambda e: step(-1))

        def copy_all():
            ok = clip_set(txt.get("1.0", "end-1c").replace("\n", "\r\n"))
            status.config(text="✓ Copied the whole file" if ok else "Could not copy, try again", fg=BORDER)

        def save(*_):
            new = txt.get("1.0", "end-1c").replace("\r\n", "\n").split("\n")
            while new and not new[-1].strip():
                new.pop()
            try:
                write_snippet_file(path, new)
            except OSError as exc:
                status.config(text=f"Could not save: {exc}", fg=WARN)
                return "break"
            self.drafts.pop(path, None)
            self.on_leave = None
            self.show_view(path, back, flash="✓ Saved. The old version is in clipboard\\.backup\\")
            return "break"

        if editing:
            txt.config(state="normal")
            def paint_soon(*_):
                """While you type, repaint once you pause (not on every key) - big files stay smooth."""
                if painted["job"] is not None:
                    self.root.after_cancel(painted["job"])
                painted["job"] = self.root.after(150, paint)
            txt.bind("<KeyRelease>", paint_soon)
            txt.bind("<<Paste>>", lambda e: paint_soon())
            txt.bind("<Control-s>", save)
            txt.bind("<Control-S>", save)

            def keep_draft():
                if txt.winfo_exists():
                    if dirty():
                        self.drafts[path] = txt.get("1.0", "end-1c").rstrip("\n")
                    else:
                        self.drafts.pop(path, None)
            self.on_leave = keep_draft
        else:
            txt.config(state="disabled")      # read-only, but you can still select and copy

        sections = section_ranges(lines)
        if is_freestyle_file(path):
            summary = (f"{len(lines)} lines • freestyle file: blank lines separate "
                       f"{len(split_blocks(lines))} snippet(s)")
        else:
            summary = f"{len(lines)} lines • {len(sections)} section(s)"
        tip = ("type to edit • Ctrl+S save • Ctrl+F find • Ctrl+A select all" if editing
               else "select text + Ctrl+C to copy • Ctrl+A selects all • Edit to change it")
        self._note(lf, f"{summary}   •   {tip}")
        status = self._note(lf, flash, color=BORDER)
        if editing:
            self._buttons(lf, [("Save ✓", save, BORDER),
                               ("Copy all", copy_all, BADGE_BG),
                               ("Open in Notepad", guarded(lambda: (os.startfile(path), self.hide())), BADGE_BG),
                               ("Back", leave, BADGE_BG)])
        else:
            self._buttons(lf, [("Edit ✎", lambda: self.show_view(path, back, editing=True), BORDER),
                               ("Copy all", copy_all, BADGE_BG),
                               ("Open in Notepad", lambda: (os.startfile(path), self.hide()), BADGE_BG),
                               ("Back", back, BADGE_BG)])
        txt.focus_set()
        self.initial_focus = txt
        if editing:
            txt.mark_set("insert", "1.0")
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
        self._note(lf, "Ctrl+V          open this menu   (then Enter = normal paste)\n"
                       "Ctrl+C twice    save what you copied  (new file, section, freestyle...)\n"
                       "Tab             Auto send / Confirm / Edit first\n"
                       "Right-click     edit a snippet / view a file\n"
                       "Type            search the list you are in\n"
                       "\U0001F441 View         see the whole file; Edit \u270e to change + save\n"
                       "Esc / Ctrl+Z    close, or STOP while it is sending\n"
                       "Send            types it fast, then Enter after each line\n"
                       "Type            types every key, no Enter (boxes that block paste)\n"
                       "Paste           pastes with the clipboard, no Enter (kept out of Win+V history)\n"
                       "\U0001F524 Picture      copied a screenshot? 'Text from copied picture' reads it (OCR)\n"
                       "Ctrl+X twice    turn the tool on / off (anywhere); while off Ctrl+V pastes normally\n"
                       "\uff0b / \U0001F5D1          add or remove files and entries (Ctrl+Z undoes a remove)\n"
                       "name*:          * before the colons HIDES the text (shows **********)\n"
                       "Hide / Show     buttons on each section add / remove that * for you\n"
                       "\u270e (pen)         rename a file / change a saved entry (right-click = this send only)\n"
                       "{name} {greeting} {day} {date} {time} {clipboard}   filled in when sent", mono=True)
        self._copy_twice_switch(lf)
        self._buttons(lf, [("Back", self.show_files, BADGE_BG),
                           ("Open clipboard folder", lambda: (os.startfile(SNIPPET_DIR), self.hide()), BORDER)])
        self.root.focus_set()
        self._resize()

    @staticmethod
    def _clip_preview():
        text = clip_get()
        if text is None:
            return "(a picture: Enter pastes it as usual)" if clip_has_image() else "(clipboard is empty or not text)"
        one = " ".join(text.split())
        return one[:60] + ("…" if len(one) > 60 else "")

    # ---- actions
    def _return_to_target(self):
        self.hide()
        restore_focus(self.target, self.target_focus)

    def normal_paste(self):
        self._return_to_target()

        def job():
            time.sleep(0.12)
            wait_modifiers_released()
            press_ctrl_v()
        threading.Thread(target=job, daemon=True).start()

    def send_lines(self, lines, enter=True, fill=True, keep_blank=False, method=None):
        """enter=True: Send (Enter after each message). enter=False: Paste - put it in the box only."""
        src = list(lines)
        while src and not src[-1].strip():
            src.pop()
        if not keep_blank:
            while src and not src[0].strip():
                src.pop(0)
        lines = fill_placeholders(src, self.current_stem) if fill else src
        if not any(l.strip() for l in lines):
            return
        if enter:
            # one message = all lines in one chat message (Shift+Enter between), else one per line
            messages = [lines] if self.settings["one_message"] else [[l] for l in lines if l.strip()]
        else:
            messages = [lines]       # Paste: everything into the box, Shift+Enter between lines
        self._return_to_target()
        self.busy = True
        self.hook.sending = True
        self.abort.clear()
        threading.Thread(target=self._send_job, args=(messages, enter, method or "fast"),
                         daemon=True).start()

    def type_clipboard(self):
        """For boxes that block paste: type what you copied, key by key, without Enter."""
        text = clip_get()
        if not text:
            return
        self.send_lines(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"),
                        enter=False, fill=False, keep_blank=True, method="keys")

    def _send_job(self, messages, enter=True, method="fast"):
        original = clip_get() if (method == "clipboard" and RESTORE_CLIPBOARD) else None
        try:
            time.sleep(0.15)
            wait_modifiers_released()
            confirm = window_exe(self.target) in CHAT_APPS
            for msg in messages:
                # stop if Esc was pressed or you clicked into another window
                if self.abort.is_set() or user32.GetForegroundWindow() != self.target:
                    break
                if method == "clipboard":
                    clip_set("\r\n".join(msg), private=True)   # not kept in Win+V history
                    time.sleep(0.05)
                    press_ctrl_v()
                    time.sleep(PASTE_DELAY)       # let the app finish reading the clipboard
                else:
                    for i, line in enumerate(msg):
                        if i:
                            press_shift_enter()   # new line inside the same message
                            time.sleep(0.05)
                        if method == "keys":
                            type_keys(line, self.abort)
                        else:
                            type_text(line)
                        time.sleep(STEP_DELAY + len(line) * 0.002)   # long lines get a bit more time
                if not enter:
                    continue                      # Paste: leave it in the box, no Enter
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
            self.hook.sending = False

    def run(self):
        self.root.mainloop()


def main():
    if sys.platform != "win32":
        sys.exit("This tool only runs on Windows.")
    # The keyboard hook runs in its own thread and every key press on the PC waits for it. Let it
    # get Python's attention within ~1 ms (default 5 ms) even if the popup is busy drawing.
    sys.setswitchinterval(0.001)
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
