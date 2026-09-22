# -*- coding: utf-8 -*-
"""
生成 README 用的界面预览图：docs/preview.png

做法是「真刀真枪」，不是照着代码推算布局：
  1. 用 tkinter 真的把 App 的界面跑起来，并注入一组示例网卡数据；
  2. 让窗口在屏幕右下角显示约 1 秒，依次摆成「已开启」「已关闭」两态；
  3. 各抓一次屏幕像素，用标准库 zlib / struct 编成 PNG 再横向拼接。

为什么不是用 PrintWindow 抓窗口：Tk 的 Canvas 不响应 WM_PRINT，
开关与指示灯会抓成空白。所以要真的让窗口显示出来。

全程零第三方依赖，不请求管理员权限、不读取或改动任何真实网卡。

用法：
    python make_screenshot.py [输出路径]
"""

import os
import sys
import zlib
import time
import ctypes
import struct
from ctypes import wintypes

import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import app as appmod                                        # noqa: E402

# ---------------------------------------------------------------- 示例数据
ADAPTER_UP = {
    "name": "以太网 3",
    "desc": "Realtek USB FE Family Controller",
    "status": "Up",
    "media": "802.3",
    "speed": "100 Mbps",
    "index": 12,
    "ip": "192.168.1.23",
}
ADAPTER_OFF = dict(ADAPTER_UP, status="Disabled", speed="", ip="")

CANVAS_PAD = 24
GAP = 24
PAGE_BG = (244, 246, 248)          # #F4F6F8

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

SRCCOPY = 0x00CC0020
GA_ROOT = 2
DWMWA_EXTENDED_FRAME_BOUNDS = 9
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
RDW_INVALIDATE = 0x0001
RDW_ERASE = 0x0004
RDW_ALLCHILDREN = 0x0080
RDW_UPDATENOW = 0x0100


# ---------------------------------------------------------------- 最小 PNG 画布
class Canvas:
    """RGB 位图画布（行优先），够用就行。"""

    def __init__(self, w, h, rgb=PAGE_BG):
        self.w, self.h = w, h
        self.rows = [bytearray(bytes(rgb) * w) for _ in range(h)]

    def paste(self, rows, x, y):
        if not rows:
            return 0
        rw = len(rows[0]) // 3
        for i, src in enumerate(rows):
            if y + i >= self.h:
                break
            self.rows[y + i][x * 3:x * 3 + rw * 3] = src
        return rw

    def size(self):
        return max((len(r) // 3 for r in self.rows), default=0), len(self.rows)


def png_bytes(canvas):
    w, h = canvas.size()
    raw = b"".join(b"\x00" + bytes(r) for r in canvas.rows)
    comp = zlib.compress(raw, 9)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)   # 8 位、真彩 RGB
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", comp) + chunk(b"IEND", b"")


# ---------------------------------------------------------------- GDI 抓像素
class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def _setup_gdi_signatures():
    """句柄在 64 位下必须显式标注类型，否则默认的 c_int 会截断或溢出。"""
    HDC, HGDIOBJ, HWND, UINT = wintypes.HDC, wintypes.HGDIOBJ, wintypes.HWND, wintypes.UINT
    user32.GetDC.restype, user32.GetDC.argtypes = HDC, [HWND]
    user32.ReleaseDC.restype, user32.ReleaseDC.argtypes = ctypes.c_int, [HWND, HDC]
    user32.GetWindowDC.restype, user32.GetWindowDC.argtypes = HDC, [HWND]
    user32.GetAncestor.restype, user32.GetAncestor.argtypes = HWND, [HWND, UINT]
    user32.SetWindowPos.restype = ctypes.c_int
    user32.SetWindowPos.argtypes = [HWND, HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, UINT]
    user32.RedrawWindow.restype = ctypes.c_int
    user32.RedrawWindow.argtypes = [HWND, ctypes.c_void_p, ctypes.c_void_p, UINT]
    user32.GetClientRect.argtypes = [HWND, ctypes.POINTER(wintypes.RECT)]
    user32.ClientToScreen.argtypes = [HWND, ctypes.POINTER(wintypes.POINT)]
    gdi32.CreateCompatibleDC.restype, gdi32.CreateCompatibleDC.argtypes = HDC, [HDC]
    gdi32.CreateCompatibleBitmap.restype = HGDIOBJ
    gdi32.CreateCompatibleBitmap.argtypes = [HDC, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype, gdi32.SelectObject.argtypes = HGDIOBJ, [HDC, HGDIOBJ]
    gdi32.DeleteObject.restype, gdi32.DeleteObject.argtypes = ctypes.c_int, [HGDIOBJ]
    gdi32.DeleteDC.restype, gdi32.DeleteDC.argtypes = ctypes.c_int, [HDC]
    gdi32.BitBlt.restype = ctypes.c_int
    gdi32.BitBlt.argtypes = [HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.GetDIBits.argtypes = [HDC, HGDIOBJ, UINT, UINT, ctypes.c_void_p,
                                ctypes.POINTER(BITMAPINFO), UINT]


_setup_gdi_signatures()


def window_rect(root):
    """窗口外框边界（优先 EXTENDED_FRAME_BOUNDS，避开 DWM 阴影）。"""
    hwnd = user32.GetAncestor(root.winfo_id(), GA_ROOT) or root.winfo_id()
    r = wintypes.RECT()
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)) != 0:
        user32.GetWindowRect(hwnd, ctypes.byref(r))
    return hwnd, r.left, r.top, r.right - r.left, r.bottom - r.top


def client_rect(hwnd, inset=2):
    """窗口客户区在屏幕上的位置与尺寸（向内收 inset 像素，避开边框与圆角）。

    只抓客户区有两个好处：内容全部由 Tk 绘制、不会把标题栏外的桌面像素
    （或 DWM 合成残影）带进来；四角也是直角的界面本体，不需要额外裁圆角。
    """
    cr = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(cr))
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x + inset, pt.y + inset, cr.right - inset * 2, cr.bottom - inset * 2


def grab_screen(x, y, w, h):
    """从屏幕 DC 抓一块区域，返回 RGB 行列表。"""
    hdc = user32.GetDC(None)
    memdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(memdc, bmp)
    try:
        gdi32.BitBlt(memdc, 0, 0, w, h, hdc, x, y, SRCCOPY)
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h                    # 负号 = 自上而下
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0                # BI_RGB
        buf = (ctypes.c_char * (w * h * 4))()
        if not gdi32.GetDIBits(memdc, bmp, 0, h, ctypes.cast(buf, ctypes.c_void_p),
                               ctypes.byref(bi), 0):
            return None
        raw = bytes(buf)
        rows = []
        for yy in range(h):
            line = raw[yy * w * 4:(yy + 1) * w * 4]
            rgb = bytearray(w * 3)
            for xx in range(w):
                rgb[xx * 3] = line[xx * 4 + 2]        # BGR(A) -> RGB
                rgb[xx * 3 + 1] = line[xx * 4 + 1]
                rgb[xx * 3 + 2] = line[xx * 4]
            rows.append(bytes(rgb))
        return rows
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(memdc)
        user32.ReleaseDC(None, hdc)


def count_color(rows, rgb, tol=12):
    """统计接近某颜色的像素数，用来判断抓到的到底是不是我们的窗口。"""
    tr, tg, tb = rgb
    n = 0
    for row in rows:
        for i in range(0, len(row), 3):
            if abs(row[i] - tr) <= tol and abs(row[i + 1] - tg) <= tol and abs(row[i + 2] - tb) <= tol:
                n += 1
                if n > 4000:
                    return n
    return n


# ---------------------------------------------------------------- 抓一张界面
def _quiet_bgerror(root):
    """窗口销毁后残留的 after 回调会往 stderr 喷错，重定义 bgerror 静默掉。"""
    try:
        root.tk.eval("proc bgerror {args} {}")
    except Exception:
        pass


def _dump(rows, path):
    """调试用：把一次抓取结果单独写成 PNG。"""
    if not rows:
        return
    c = Canvas(len(rows[0]) // 3, len(rows))
    c.paste(rows, 0, 0)
    with open(path, "wb") as fh:
        fh.write(png_bytes(c))


def shoot(fake, marker_rgb, attempts=4):
    """渲染指定状态、让窗口短暂显示、抓屏。marker_rgb 用于校验抓取结果。"""
    appmod.list_adapters = lambda: ([fake], "")
    appmod.get_ipv4 = lambda index: fake.get("ip", "")
    # 截图是手工驱动 UI（不跑 mainloop），后台轮询线程会与 Tk 打架，这里关掉它
    appmod.App._worker_refresh = lambda self: None
    dbg = os.environ.get("ETHSW_SHOT_DEBUG")

    for attempt in range(attempts):
        root = tk.Tk()
        root.withdraw()                                  # 先不显示，避免在默认位置闪一下
        _quiet_bgerror(root)
        App = appmod.App(root, privileged=True)          # privileged=True 只用于藏起提权提示条
        App.adapters = [fake]
        App.wired = [fake]
        App.current = fake
        App._sync(ip=fake.get("ip", ""))
        root.update_idletasks()

        sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        PH = os.environ.get("ETHSW_SHOT_POS")
        if PH:
            x, y = (int(v) for v in PH.split(","))
        else:
            # 用请求尺寸预估落点，让窗口"首帧就在屏幕内"（先用屏幕外映射再挪进来
            # 会拿到没真正绘制过的表面，出现残影）
            rw, rh = root.winfo_reqwidth(), root.winfo_reqheight()
            x, y = max(sw - rw - 60, 0), max(sh - rh - 130, 0)
        root.geometry("+%d+%d" % (x, y))
        root.deiconify()
        root.update()
        time.sleep(0.25)
        root.update()

        hwnd, _, _, w, h = window_rect(root)              # 拿到真实尺寸
        if w < 100 or h < 100:
            App._closing = True
            root.destroy()
            continue

        if not PH:
            # 按真实外框尺寸精确贴到右下角（只挪几十像素，不会留残影）
            x, y = max(sw - w - 40, 0), max(sh - h - 96, 0)
        user32.SetWindowPos(hwnd, 0, x, y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
        root.attributes("-topmost", True)                 # 置顶，保证抓到的是它
        root.update()
        time.sleep(0.25)
        root.update()
        user32.RedrawWindow(hwnd, None, None,             # 保险起见再强制整窗重绘一次
                            RDW_INVALIDATE | RDW_ERASE | RDW_UPDATENOW | RDW_ALLCHILDREN)
        root.update()
        time.sleep(0.4)                                  # 等 DWM 完成合成
        root.update()

        x, y, w, h = client_rect(hwnd)
        rows = grab_screen(x, y, w, h)
        hits = count_color(rows, marker_rgb) if rows else -1
        if dbg:
            os.makedirs(dbg, exist_ok=True)
            _dump(rows, os.path.join(dbg, "try%d.png" % attempt))
            print("[debug] try%d screen=%dx%d client=(%d,%d,%d,%d) marker_hits=%d"
                  % (attempt, sw, sh, x, y, w, h, hits))

        if rows and hits > 3000:                           # 确认抓到的确实是本窗口
            time.sleep(0.3)                              # 再等一帧，躲开 DWM 的合成残影
            root.update()
            cx, cy, cw, ch = client_rect(hwnd)
            final = grab_screen(cx, cy, cw, ch) or rows
            App._closing = True
            root.destroy()
            return final

        App._closing = True
        root.destroy()

    return None


def render(out_path):
    appmod.enable_dpi_awareness()

    green = (0x16, 0xA3, 0x4A)        # 开启态的开关轨道
    grey = (0xCE, 0xD3, 0xDA)         # 关闭态的开关轨道
    shots = []
    for fake, marker in ((ADAPTER_UP, green), (ADAPTER_OFF, grey)):
        shots.append(shoot(fake, marker))
        time.sleep(0.6)               # 让桌面恢复，避免第二次抓图带上上一次的残影
    frames = [f for f in shots if f]
    if not frames:
        raise RuntimeError("未能抓到窗口内容，请确认在可交互的桌面会话中运行")

    heights = [len(f) for f in frames]
    widths = [len(f[0]) // 3 for f in frames]
    w = CANVAS_PAD * 2 + sum(widths) + GAP * (len(frames) - 1)
    h = CANVAS_PAD * 2 + max(heights)

    canvas = Canvas(w, h)
    x = CANVAS_PAD
    for rows in frames:
        canvas.paste(rows, x, CANVAS_PAD)          # 顶部对齐，标题栏在同一条线上
        x += len(rows[0]) // 3 + GAP

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    data = png_bytes(canvas)
    with open(out_path, "wb") as fh:
        fh.write(data)
    return out_path, canvas.size(), len(data)


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join("docs", "preview.png")
    path, size, length = render(target)
    print(f"已生成 {path}  {size[0]}x{size[1]}  {length / 1024:.0f} KB")
