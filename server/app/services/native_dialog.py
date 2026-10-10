"""本机原生「选择文件夹」窗口（plan-20261009-ui U1）。

桌面版（Electron）由主进程弹系统选择器；浏览器里的页面既弹不出系统选择器，也拿不到所选文件夹的绝对路径，
于是由与浏览器同机的后端代为弹出，把选中的绝对路径交还给页面：
  · Windows —— IFileOpenDialog + FOS_PICKFOLDERS（与 Electron dialog.showOpenDialog 同款的现代文件夹选择窗口），
               ctypes 直接调用 COM，不依赖 PowerShell / .NET，也没有编译开销；
  · macOS   —— osascript 的 choose folder；
  · Linux   —— zenity / kdialog（没有桌面会话或没装时视为不可用）。
弹不出来时抛 NativeDialogUnavailable，前端回落到自绘的目录浏览器。
"""
import asyncio
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path


class NativeDialogUnavailable(RuntimeError):
    """当前环境弹不出系统选择器（无桌面会话 / 缺少系统组件）。"""


class NativeDialogBusy(RuntimeError):
    """已有一个选择窗口在显示（避免连点叠出多个窗口）。"""


_busy = threading.Lock()
_DIALOG_TIMEOUT_S = 600


async def pick_directory(title: str = "选择文件夹", initial: str | None = None) -> str | None:
    """弹出系统自带的文件夹选择窗口并等待用户操作；返回绝对路径，用户取消返回 None。"""
    if not _busy.acquire(blocking=False):
        raise NativeDialogBusy()
    try:
        start = initial if initial and os.path.isdir(initial) else None
        return await asyncio.to_thread(_pick_blocking, title, start)
    finally:
        _busy.release()


def _pick_blocking(title: str, initial: str | None) -> str | None:
    if sys.platform == "win32":
        return _pick_windows(title, initial)
    if sys.platform == "darwin":
        return _pick_macos(title, initial)
    return _pick_linux(title, initial)


# ── Windows：IFileOpenDialog（COM vtable 手工调用） ─────────────────────────

_ERROR_CANCELLED_HR = 0x800704C7  # HRESULT_FROM_WIN32(ERROR_CANCELLED)


def _pick_windows(title: str, initial: str | None) -> str | None:
    import ctypes
    from ctypes import POINTER, byref, c_int, c_long, c_uint, c_void_p, c_wchar_p, wintypes

    try:
        ole32 = ctypes.OleDLL("ole32")
        shell32 = ctypes.OleDLL("shell32")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError as exc:
        raise NativeDialogUnavailable(str(exc)) from exc

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    def guid(text: str) -> GUID:
        g = GUID()
        ole32.CLSIDFromString(text, byref(g))
        return g

    def method(obj: c_void_p, index: int, *argtypes):
        """取 COM 对象 vtable 里第 index 个方法（0-2 为 IUnknown，其后依接口声明顺序）。"""
        vtbl = ctypes.cast(obj, POINTER(c_void_p))[0]
        addr = ctypes.cast(vtbl, POINTER(c_void_p))[index]
        return ctypes.WINFUNCTYPE(c_long, c_void_p, *argtypes)(addr)

    def release(obj: c_void_p) -> None:
        if obj:
            method(obj, 2)(obj)

    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        c_int, c_int, c_int, c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.GetWindowRect.argtypes = [wintypes.HWND, POINTER(wintypes.RECT)]
    user32.GetSystemMetrics.argtypes = [c_int]
    user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, c_int, c_int, c_int, c_int, wintypes.UINT]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.MonitorFromWindow.restype = c_void_p
    user32.GetMonitorInfoW.argtypes = [c_void_p, POINTER(MONITORINFO)]
    enum_proc_t = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumThreadWindows.argtypes = [wintypes.DWORD, enum_proc_t, wintypes.LPARAM]
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    ole32.CoInitializeEx.argtypes = [c_void_p, wintypes.DWORD]
    ole32.CoTaskMemFree.argtypes = [c_void_p]

    inited = False
    owner = None
    stop = threading.Event()
    dlg = c_void_p()
    item = c_void_p()
    folder_item = c_void_p()
    try:
        ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED：UI 对话框要求单线程套间
        inited = True
        ole32.CoCreateInstance(
            byref(guid("{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}")),  # CLSID_FileOpenDialog
            None,
            1,  # CLSCTX_INPROC_SERVER
            byref(guid("{D57C7288-D4AD-4768-BE02-9D969532D960}")),  # IID_IFileOpenDialog
            byref(dlg),
        )

        opts = c_uint()
        method(dlg, 10, POINTER(c_uint))(dlg, byref(opts))  # GetOptions
        # FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST
        method(dlg, 9, c_uint)(dlg, opts.value | 0x20 | 0x40 | 0x800)  # SetOptions
        method(dlg, 17, c_wchar_p)(dlg, title)  # SetTitle

        if initial:
            try:
                shell32.SHCreateItemFromParsingName(
                    initial, None, byref(guid("{43826D1E-E718-42EE-BC55-A1E261C37BFE}")), byref(folder_item),
                )
                method(dlg, 12, c_void_p)(dlg, folder_item)  # SetFolder
            except OSError:
                pass  # 初始目录只是便利，失败就用系统默认位置

        # 属主窗口：透明、点击穿透、置顶的 1×1 小窗，放在用户正在看的前台窗口中心——
        # 系统文件夹窗口会居中于属主，于是出现在浏览器窗口正中；没有它，后台进程弹出的窗口会落在屏幕左上角或被浏览器盖住。
        fg = user32.GetForegroundWindow()
        rect = wintypes.RECT()
        if fg and user32.GetWindowRect(fg, byref(rect)):
            cx, cy = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
        else:
            cx, cy = user32.GetSystemMetrics(0) // 2, user32.GetSystemMetrics(1) // 2
        # WS_EX_TOPMOST | TOOLWINDOW | LAYERED | TRANSPARENT | NOACTIVATE；WS_POPUP | WS_VISIBLE
        owner = user32.CreateWindowExW(0x8 | 0x80 | 0x80000 | 0x20 | 0x08000000, "STATIC", "chatcoder-picker-owner", 0x80000000 | 0x10000000, cx, cy, 1, 1, None, None, None, None)
        if owner:
            user32.SetLayeredWindowAttributes(owner, 0, 0, 2)  # LWA_ALPHA，透明度 0

        # 监视线程：对话框窗口一出现就置顶并尽力抢到前台（后台进程默认没有前台权，窗口会被浏览器盖住）
        ui_tid = kernel32.GetCurrentThreadId()
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0

        def find_dialog() -> int:
            found: list[int] = []

            @enum_proc_t
            def cb(hwnd, _lparam):
                buf = ctypes.create_unicode_buffer(32)
                user32.GetClassNameW(hwnd, buf, 32)
                if buf.value == "#32770" and user32.IsWindowVisible(hwnd):
                    found.append(hwnd)
                return True

            user32.EnumThreadWindows(ui_tid, cb, 0)
            return found[0] if found else 0

        def raise_dialog() -> None:
            deadline = time.monotonic() + 6
            while not stop.is_set() and time.monotonic() < deadline:
                hwnd = find_dialog()
                if hwnd:
                    # 居中到前台窗口：系统窗口默认把左上角贴在属主的左上角，这里按实际尺寸挪到正中并限制在该屏工作区内
                    rc = wintypes.RECT()
                    user32.GetWindowRect(hwnd, byref(rc))
                    w, h = rc.right - rc.left, rc.bottom - rc.top
                    x, y = cx - w // 2, cy - h // 2
                    mi = MONITORINFO()
                    mi.cbSize = ctypes.sizeof(MONITORINFO)
                    mon = user32.MonitorFromWindow(fg, 2)  # MONITOR_DEFAULTTONEAREST
                    if mon and user32.GetMonitorInfoW(mon, byref(mi)):
                        x = max(mi.rcWork.left, min(x, mi.rcWork.right - w))
                        y = max(mi.rcWork.top, min(y, mi.rcWork.bottom - h))
                    user32.SetWindowPos(hwnd, -1, x, y, 0, 0, 0x1 | 0x40)  # HWND_TOPMOST + SWP_NOSIZE | SWP_SHOWWINDOW
                    watcher_tid = kernel32.GetCurrentThreadId()
                    attached = bool(fg_tid) and bool(user32.AttachThreadInput(watcher_tid, fg_tid, True))
                    user32.SetForegroundWindow(hwnd)
                    if attached:
                        user32.AttachThreadInput(watcher_tid, fg_tid, False)
                    return
                time.sleep(0.01)

        watcher = threading.Thread(target=raise_dialog, daemon=True)
        watcher.start()

        hr = method(dlg, 3, c_void_p)(dlg, owner)  # IModalWindow::Show
        if (hr & 0xFFFFFFFF) == _ERROR_CANCELLED_HR:
            return None
        if hr < 0:
            raise NativeDialogUnavailable(f"HRESULT 0x{hr & 0xFFFFFFFF:08X}")

        method(dlg, 20, POINTER(c_void_p))(dlg, byref(item))  # GetResult → IShellItem
        name = c_wchar_p()
        method(item, 5, c_uint, POINTER(c_wchar_p))(item, 0x80058000, byref(name))  # GetDisplayName(SIGDN_FILESYSPATH)
        try:
            return name.value or None
        finally:
            ole32.CoTaskMemFree(ctypes.cast(name, c_void_p))
    except NativeDialogUnavailable:
        raise
    except (OSError, AttributeError) as exc:
        raise NativeDialogUnavailable(str(exc)) from exc
    finally:
        stop.set()
        release(item)
        release(folder_item)
        release(dlg)
        if owner:
            user32.DestroyWindow(owner)
        if inited:
            ole32.CoUninitialize()


# ── macOS / Linux ───────────────────────────────────────────────────────────

def _pick_macos(title: str, initial: str | None) -> str | None:
    safe_title = title.replace('"', "'")
    script = f'POSIX path of (choose folder with prompt "{safe_title}"'
    if initial:
        script += f' default location POSIX file "{initial}"'
    script += ")"
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=_DIALOG_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        raise NativeDialogUnavailable(str(exc)) from exc
    if r.returncode != 0:
        if "-128" in r.stderr or "canceled" in r.stderr.lower():
            return None
        raise NativeDialogUnavailable(r.stderr.strip() or "osascript 失败")
    out = r.stdout.strip()
    return (out.rstrip("/") or "/") if out else None


def _pick_linux(title: str, initial: str | None) -> str | None:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise NativeDialogUnavailable("没有图形桌面会话")
    start = initial or str(Path.home())
    candidates = [
        ("zenity", ["zenity", "--file-selection", "--directory", "--title", title, "--filename", start.rstrip("/") + "/"]),
        ("kdialog", ["kdialog", "--getexistingdirectory", start, "--title", title]),
    ]
    for exe, cmd in candidates:
        if not shutil.which(exe):
            continue
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=_DIALOG_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as exc:
            raise NativeDialogUnavailable(str(exc)) from exc
        if r.returncode == 0:
            return r.stdout.strip() or None
        if r.returncode == 1 and not r.stderr.strip():
            return None  # 用户取消（zenity / kdialog 约定退出码 1，且无报错输出）
        raise NativeDialogUnavailable(r.stderr.strip() or f"{exe} 退出码 {r.returncode}")
    raise NativeDialogUnavailable("未安装 zenity 或 kdialog")
