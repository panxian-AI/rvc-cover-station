# -*- coding: utf-8 -*-
"""任务栏通知区图标（托盘）—— 静默启动之后的「入口」。

为什么需要它
------------
v1.4.0 把启动黑窗口去掉了，这是好事，但也带走了一个东西：那个窗口曾经是
「服务在跑」的唯一视觉证据，也是用户唯一能点的地方。窗口没了，用户想停服务
就得去翻安装目录找 停止RVC.bat。

所以面板启动时在通知区放一个图标：
    左键单击  → 弹出菜单（打开控制台 / 打开安装目录 / 停服务 / 退出）
    左键双击  → 直接打开控制台页面
    右键单击  → 同一个菜单（Windows 的习惯）

为什么不用 pystray
------------------
多一个第三方依赖就多一处安装失败的可能，而托盘本身只是 Shell_NotifyIconW
加一个消息循环。这里用 ctypes 直接调 Win32，零依赖，离线也能用。
"""
import ctypes
import os
import struct
import sys
import threading

WM_APP = 0x8000
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_NULL = 0x0000
WM_COMMAND = 0x0111
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002

NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004

MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
MF_GRAYED = 0x00000001
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
TPM_NONOTIFY = 0x0080

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040
IDI_APPLICATION = 32512

ICON_ID = 1
CALLBACK_MSG = WM_APP + 1
ICO_NAME = "rvc_tray.ico"


# ------------------------------------------------------------------ 图标

def _pixel(x, y, size):
    """一个「唱片」形状的小图标：外圈亮色、盘面深色、中心一个亮点。

    纯颜色块在 16px 下最容易认出来，也不用带字体渲染。
    """
    cx = cy = (size - 1) / 2.0
    r = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
    edge = size / 2.0
    accent = (0x7c, 0x5c, 0xff, 0xff)      # 紫（和页面主色一致）
    dark = (0x16, 0x1c, 0x2c, 0xff)
    light = (0xf2, 0xf5, 0xff, 0xff)
    if r > edge - 0.6:
        return (0, 0, 0, 0)                 # 圆外透明
    if r > edge - 2.2:
        return accent                       # 外圈
    if r > size * 0.16:
        return dark                         # 盘面
    return light                            # 中心


def icon_frames(size=32):
    """返回 BGRA 像素（自下而上）与 1bpp AND 掩码。"""
    rows = []
    for y in range(size - 1, -1, -1):       # ICO 是自下而上存的
        row = bytearray()
        for x in range(size):
            b, g, r, a = _pixel(x, y, size)
            row += bytes((b, g, r, a))
        rows.append(bytes(row))
    xor = b"".join(rows)

    mask_row = ((size + 31) // 32) * 4      # 每行 4 字节对齐
    mask = bytearray()
    for y in range(size - 1, -1, -1):
        bits = bytearray(mask_row)
        for x in range(size):
            if _pixel(x, y, size)[3] == 0:
                bits[x // 8] |= 0x80 >> (x % 8)   # 透明像素在掩码里置 1
        mask += bytes(bits)
    return xor, bytes(mask)


def ico_bytes(sizes=(32, 16)):
    """拼一个多尺寸 .ico（PNG 都不用，直接 BMP 段，兼容性最好）。"""
    images = []
    for size in sizes:
        xor, mask = icon_frames(size)
        header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                             len(xor) + len(mask), 0, 0, 0, 0)
        images.append((size, header + xor + mask))
    head = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    for size, blob in images:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32,
                               len(blob), offset)
        offset += len(blob)
    return head + entries + b"".join(blob for _s, blob in images)


def ensure_icon(path=None):
    """把图标写到磁盘（已存在就复用）。返回路径或空串。"""
    path = path or os.path.join(os.path.dirname(os.path.realpath(__file__)), ICO_NAME)
    try:
        if os.path.isfile(path) and os.path.getsize(path) > 100:
            return path
        with open(path, "wb") as f:
            f.write(ico_bytes())
        return path
    except OSError:
        return ""


# ------------------------------------------------------------------ 是否该起

def should_start():
    """非 Windows、或者 RVC_NO_TRAY=1（自动化测试/无桌面环境）时不起。"""
    if os.name != "nt":
        return False
    if os.environ.get("RVC_NO_TRAY") == "1":
        return False
    return True


# ------------------------------------------------------------------ Win32 类型

if os.name == "nt":
    from ctypes import wintypes

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT),
                    ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE),
                    ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR)]

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD),
                    ("hWnd", wintypes.HWND),
                    ("uID", wintypes.UINT),
                    ("uFlags", wintypes.UINT),
                    ("uCallbackMessage", wintypes.UINT),
                    ("hIcon", wintypes.HICON),
                    ("szTip", wintypes.WCHAR * 128),
                    ("dwState", wintypes.DWORD),
                    ("dwStateMask", wintypes.DWORD),
                    ("szInfo", wintypes.WCHAR * 256),
                    ("uVersion", wintypes.UINT),
                    ("szInfoTitle", wintypes.WCHAR * 64),
                    ("dwInfoFlags", wintypes.DWORD),
                    ("guidItem", GUID),
                    ("hBalloonIcon", wintypes.HICON)]

    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


# ------------------------------------------------------------------ Win32 原型

_PROTO_DONE = False


def _proto():
    """给用到的 Win32 函数补上原型。

    不补的后果在 64 位上是真实的：ctypes 默认按 C int 传参/取返回值，
    HWND/LPARAM 一旦超出 32 位就被截断（DefWindowProcW 会直接
    OverflowError），托盘图标看着在，点下去就崩。
    """
    global _PROTO_DONE
    if _PROTO_DONE or os.name != "nt":
        return
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    sz = ctypes.c_size_t
    u32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                   wintypes.WPARAM, wintypes.LPARAM]
    u32.DefWindowProcW.restype = LRESULT
    u32.CreateWindowExW.restype = wintypes.HWND
    u32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                    wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                    wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    u32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    u32.CreatePopupMenu.restype = wintypes.HMENU
    u32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, sz, wintypes.LPCWSTR]
    u32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                   wintypes.LPVOID]
    u32.TrackPopupMenu.restype = ctypes.c_int
    u32.DestroyMenu.argtypes = [wintypes.HMENU]
    u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM]
    u32.DestroyWindow.argtypes = [wintypes.HWND]
    u32.SetForegroundWindow.argtypes = [wintypes.HWND]
    u32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
    u32.LoadImageW.restype = wintypes.HANDLE
    u32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                               ctypes.c_int, ctypes.c_int, wintypes.UINT]
    u32.LoadIconW.restype = wintypes.HICON
    u32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
    u32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                wintypes.UINT, wintypes.UINT]
    u32.GetMessageW.restype = ctypes.c_int
    u32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    u32.DispatchMessageW.restype = LRESULT
    k32.GetModuleHandleW.restype = wintypes.HMODULE
    k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    ctypes.windll.shell32.Shell_NotifyIconW.argtypes = [
        wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    ctypes.windll.shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    _PROTO_DONE = True


# ------------------------------------------------------------------ 托盘本体

class Tray(object):
    """一个图标 + 一个弹出菜单。菜单项是 (标题, 回调) 或 None（分隔线）。"""

    def __init__(self, tooltip, items, on_double=None, icon_path=""):
        self.tooltip = tooltip[:127]
        self.items = list(items)
        self.on_double = on_double
        self.icon_path = icon_path
        self.hwnd = None
        self.ready = threading.Event()
        self.error = ""
        self._thread = None
        self._proc = None          # 必须持有，否则回调会被 GC 掉
        self._nid = None
        self._ids = {}             # 菜单 id -> 回调

    # -------------------------------------------------- 生命周期
    def start(self, timeout=5.0):
        if os.name != "nt":
            self.error = "非 Windows"
            return False
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread.start()
        self.ready.wait(timeout)
        return self.hwnd is not None and not self.error

    def stop(self):
        if self.hwnd:
            try:
                ctypes.windll.user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass

    # -------------------------------------------------- 内部
    def _load_icon(self):
        u32 = ctypes.windll.user32
        path = self.icon_path or ensure_icon()
        if path:
            h = u32.LoadImageW(None, path, IMAGE_ICON, 0, 0,
                               LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if h:
                return h
        # MAKEINTRESOURCE(IDI_APPLICATION)：把整数当指针传，不能直接丢 int
        return u32.LoadIconW(
            None, ctypes.cast(ctypes.c_void_p(IDI_APPLICATION), wintypes.LPCWSTR))

    def _menu(self):
        u32 = ctypes.windll.user32
        hmenu = u32.CreatePopupMenu()
        self._ids.clear()
        n = 0
        for item in self.items:
            if not item:
                u32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
                continue
            label, cb = item
            n += 1
            self._ids[n] = cb
            u32.AppendMenuW(hmenu, MF_STRING, n, label)
        return hmenu

    def _popup(self):
        u32 = ctypes.windll.user32
        hmenu = self._menu()
        pt = POINT()
        u32.GetCursorPos(ctypes.byref(pt))
        # 不先把窗口提到前台，点菜单外面菜单不会自己收起来
        u32.SetForegroundWindow(self.hwnd)
        cmd = u32.TrackPopupMenu(hmenu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                                 pt.x, pt.y, 0, self.hwnd, None)
        u32.DestroyMenu(hmenu)
        u32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        cb = self._ids.get(cmd)
        if cb:
            # 回调里会去停服务（可能等十几秒），不能挡住消息循环
            threading.Thread(target=self._safe_call, args=(cb,), daemon=True).start()

    @staticmethod
    def _safe_call(cb):
        try:
            cb()
        except Exception:
            pass

    def _add_icon(self, hwnd):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = ICON_ID
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = CALLBACK_MSG
        nid.hIcon = self._load_icon()
        nid.szTip = self.tooltip
        self._nid = nid
        return bool(ctypes.windll.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)))

    def _del_icon(self):
        if self._nid is not None:
            try:
                ctypes.windll.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            except Exception:
                pass
            self._nid = None

    def _wndproc(self, hwnd, msg, wparam, lparam):
        u32 = ctypes.windll.user32
        if msg == CALLBACK_MSG:
            low = lparam & 0xFFFF
            if low in (WM_LBUTTONUP, WM_RBUTTONUP, WM_CONTEXTMENU):
                self._popup()
            elif low == WM_LBUTTONDBLCLK and self.on_double:
                threading.Thread(target=self._safe_call, args=(self.on_double,),
                                 daemon=True).start()
            return 0
        if msg == WM_CLOSE:
            self._del_icon()
            u32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            u32.PostQuitMessage(0)
            return 0
        return u32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _run(self):
        try:
            _proto()
            k32 = ctypes.windll.kernel32
            u32 = ctypes.windll.user32
            hinst = k32.GetModuleHandleW(None)
            self._proc = WNDPROC(self._wndproc)
            cls = WNDCLASSW()
            cls.style = 0
            cls.lpfnWndProc = self._proc
            cls.hInstance = hinst
            cls.lpszClassName = "RVCTrayWnd"
            if not u32.RegisterClassW(ctypes.byref(cls)):
                err = k32.GetLastError()
                if err not in (1410,):        # 类已注册（重复启动）可以接着用
                    self.error = "RegisterClass 失败 err=%s" % err
                    self.ready.set()
                    return
            # 普通窗口但不显示：消息框/菜单都挂得上，比 message-only 窗口稳
            hwnd = u32.CreateWindowExW(0, "RVCTrayWnd", "RVC", 0, 0, 0, 0, 0,
                                       None, None, hinst, None)
            if not hwnd:
                self.error = "CreateWindow 失败 err=%s" % k32.GetLastError()
                self.ready.set()
                return
            self.hwnd = hwnd
            if not self._add_icon(hwnd):
                self.error = "Shell_NotifyIcon 失败 err=%s" % k32.GetLastError()
            self.ready.set()
            msg = wintypes.MSG()
            while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                u32.TranslateMessage(ctypes.byref(msg))
                u32.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:                 # 托盘起不来也不能拖垮面板
            self.error = "%s: %s" % (type(e).__name__, e)
            self.ready.set()
        finally:
            self.hwnd = None


def start(tooltip, items, on_double=None, icon_path=""):
    """便捷入口：返回 Tray 对象（成功与否看 .hwnd / .error）。"""
    t = Tray(tooltip, items, on_double=on_double, icon_path=icon_path)
    t.start()
    return t


if __name__ == "__main__":
    # 单独跑一下能看到图标（调试用）：python tray.py
    def _hello():
        print("clicked")

    tr = start("RVC 翻唱站 · 托盘自检", [("测试项（点我没反应是正常的）", _hello),
                                        None, ("退出", lambda: tr.stop())])
    print("tray:", "ok" if tr.hwnd else ("失败: " + tr.error))
    if tr.hwnd:
        threading.Event().wait()
