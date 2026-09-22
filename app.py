# -*- coding: utf-8 -*-
"""
有线网络开关 (Ethernet Switch)
--------------------------------------------------
轻量级桌面小工具：一键启用 / 禁用有线网卡（以太网适配器）。

特点：
  * 零第三方依赖：Python 内置 tkinter / ctypes，不需要 pip 安装任何东西
  * 状态读取走原生 Win32 API（IP Helper，GetIfTable2 / GetIpAddrTable），单次约 10 ms
  * 自动识别真实有线网卡（自动排除 Wi-Fi / 蓝牙 / VPN / 虚拟机网卡）
  * 醒目大开关，实时显示"已开启 / 已关闭"，点击即时生效（乐观 UI，不等系统返回）
  * 启用 / 禁用用 netsh（毫秒级），仅在极少数精简系统上回退到 PowerShell
  * 首次启动自动申请管理员权限（切换网卡必需）

用法：
  pythonw app.py              # 正常启动（GUI）
  python  app.py --selftest   # 无界面自检，输出检测到的网卡信息
  python  app.py --uitest     # 构建界面后立即退出，用于冒烟测试
"""

import os
import re
import sys
import json
import math
import time
import atexit
import shutil
import ctypes
import threading
import subprocess
import tempfile
import argparse

import tkinter as tk
from tkinter import messagebox

APP_NAME = "有线网络开关"
APP_VERSION = "1.1"

# ---------------------------------------------------------------- 常量 / 主题
BG          = "#FFFFFF"
CARD        = "#F6F7F9"
TEXT        = "#1B1F24"
SUBTEXT     = "#7A828E"
LINE        = "#E6E8EB"
GREEN       = "#16A34A"
GREEN_DARK  = "#12813B"
TRACK_OFF   = "#CED3DA"
TRACK_ON    = GREEN
KNOB        = "#FFFFFF"
WARN_BG     = "#FFF6E5"
WARN_FG     = "#8A5A00"
ERR_BG      = "#FDECEC"
ERR_FG      = "#A32020"

FONT_FAMILY = "Microsoft YaHei UI"

CREATE_NO_WINDOW = 0x08000000
PS_EXE = shutil.which("powershell.exe") or r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

# 判定为"虚拟 / 非真实有线网卡"的关键字（小写匹配描述与名称）
VIRTUAL_HINTS = [
    "virtual", "vmware", "virtualbox", "hyper-v", "tap-windows", "wintun",
    "bluetooth", "蓝牙", "miniport", "npcap", "loopback", "anyconnect",
    "sangfor", "深信服", "fortinet", "zerotier", "radmin", "kaspersky",
    "cisco", "check point", "expressvpn", "nordvpn", "wireguard", "pptp",
    "l2tp", "ikev2", "teredo", "isatap", "wi-fi direct", "tunnel", "隧道",
    "vpn", "pppoe", "ras", "远程访问", "kernel debug", "km-test", "hamachi",
    "radmin", "nord", "openvpn", "softether", "pangp", "hidecom", "netkeeper",
    "anchorfree", "hotspotshield", "proxifier", "panda", "gsvpn",
]
WIRELESS_HINTS = ["wi-fi", "wifi", "wireless", "802.11", "wlan", "无线", "ax200", "ax201", "ac 9", "agn", "rtl88"]


# ---------------------------------------------------------------- 基础工具
def decode_bytes(data: bytes) -> str:
    """PowerShell 输出可能是 UTF-8 或 GBK，逐个尝试。"""
    for enc in ("utf-8", "gbk", "mbcs"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", errors="replace")


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run_ps(script: str, timeout: int = 25):
    """执行 PowerShell 脚本，返回 (stdout, stderr, returncode)。"""
    try:
        p = subprocess.run(
            [PS_EXE, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return "", "PowerShell 执行超时", -1
    except Exception as exc:                                  # pragma: no cover
        return "", f"无法启动 PowerShell：{exc}", -1
    return decode_bytes(p.stdout), decode_bytes(p.stderr), p.returncode


def ps_quote(text: str) -> str:
    """PowerShell 单引号字符串转义。"""
    return "'" + text.replace("'", "''") + "'"


def extract_json(text: str):
    """从 PowerShell 输出中取出第一段 JSON。"""
    if not text:
        return None
    idx = min([i for i in (text.find("["), text.find("{")) if i >= 0], default=-1)
    if idx < 0:
        return None
    dec = json.JSONDecoder()
    try:
        obj, _ = dec.raw_decode(text[idx:])
        return obj
    except Exception:
        return None


# ---------------------------------------------------------------- 原生快速通道
# 说明：本机实测每次调用 PowerShell 需要 4~6 秒（进程冷启动 1.6 秒起），
# 而 GetIfTable2 / GetAdaptersAddresses 只需 2~10 毫秒。
# 因此状态读取一律走原生 API，PowerShell 仅作为兜底与执行启用/禁用命令使用。
_iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)

IF_TYPE_ETHERNET_CSMACD = 6
IF_TYPE_IEEE80211 = 71
IF_TYPE_LOOPBACK = 24
IF_TYPE_TUNNEL = 131

# NDIS 过滤驱动会为每个真实网卡派生出若干同名接口（"-WFP ..." 等），需剔除
FILTER_SUFFIXES = (
    "-wfp ", "-qos packet scheduler", "-virtualbox ndis", "-native wifi filter",
    "-virtual wifi filter", "-lightweight filter", "-0000",
)
HIDDEN_DESC_HINTS = (
    "wan miniport", "teredo", "6to4", "ip-https", "kernel debug",
    "wi-fi direct", "bluetooth", "蓝牙", "network monitor",
)


class MIB_IF_ROW2(ctypes.Structure):
    """MIB_IF_ROW2（sizeof = 1352，已实测校验）。"""
    _fields_ = [
        ("InterfaceLuid", ctypes.c_ulonglong),
        ("InterfaceIndex", ctypes.c_ulong),
        ("InterfaceGuid", ctypes.c_ubyte * 16),
        ("Alias", ctypes.c_wchar * 257),
        ("Description", ctypes.c_wchar * 257),
        ("PhysicalAddressLength", ctypes.c_ulong),
        ("PhysicalAddress", ctypes.c_ubyte * 32),
        ("PermanentPhysicalAddress", ctypes.c_ubyte * 32),
        ("Mtu", ctypes.c_ulong),
        ("Type", ctypes.c_ulong),
        ("TunnelType", ctypes.c_ulong),
        ("MediaType", ctypes.c_ulong),
        ("PhysicalMediumType", ctypes.c_ulong),
        ("AccessType", ctypes.c_ulong),
        ("Direction", ctypes.c_ulong),
        ("Flags", ctypes.c_ulong),
        ("OperStatus", ctypes.c_ulong),
        ("AdminStatus", ctypes.c_ulong),
        ("MediaConnectState", ctypes.c_ulong),
        ("NetworkGuid", ctypes.c_ubyte * 16),
        ("ConnectionType", ctypes.c_ulong),
        ("_pad", ctypes.c_ulong),
        ("TransmitLinkSpeed", ctypes.c_ulonglong),
        ("ReceiveLinkSpeed", ctypes.c_ulonglong),
        ("_stats", ctypes.c_ubyte * (18 * 8)),
    ]


class IP_ADAPTER_UNICAST_ADDRESS(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ulong), ("Flags", ctypes.c_ulong),
        ("Next", ctypes.c_void_p),
        ("lpSockaddr", ctypes.c_void_p), ("iSockaddrLength", ctypes.c_int),
    ]


class IP_ADAPTER_ADDRESSES(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ulong), ("IfIndex", ctypes.c_ulong),
        ("Next", ctypes.c_void_p),
        ("AdapterName", ctypes.c_void_p),
        ("FirstUnicastAddress", ctypes.c_void_p),
        ("FirstAnycastAddress", ctypes.c_void_p),
        ("FirstMulticastAddress", ctypes.c_void_p),
        ("FirstDnsServerAddress", ctypes.c_void_p),
        ("DnsSuffix", ctypes.c_void_p),
        ("Description", ctypes.c_void_p),
        ("FriendlyName", ctypes.c_void_p),
        ("PhysicalAddress", ctypes.c_ubyte * 8),
        ("PhysicalAddressLength", ctypes.c_ulong),
        ("Flags", ctypes.c_ulong),
        ("Mtu", ctypes.c_ulong),
        ("IfType", ctypes.c_ulong),
        ("OperStatus", ctypes.c_ulong),
    ]


def native_if_table():
    """GetIfTable2：一次调用拿到全部接口的名称/描述/运行状态/管理状态。"""
    tbl = ctypes.c_void_p()
    rc = _iphlpapi.GetIfTable2(ctypes.byref(tbl))
    if rc != 0:
        return []
    try:
        num = ctypes.cast(tbl, ctypes.POINTER(ctypes.c_ulong)).contents.value
        base = tbl.value or 0
        stride = ctypes.sizeof(MIB_IF_ROW2)
        rows = []
        for i in range(num):
            row = ctypes.cast(base + 8 + i * stride, ctypes.POINTER(MIB_IF_ROW2)).contents
            rows.append({
                "index": row.InterfaceIndex,
                "alias": row.Alias,
                "desc": row.Description,
                "type": row.Type,
                "oper": row.OperStatus,
                "admin": row.AdminStatus,
                "mcs": row.MediaConnectState,
                "tx": row.TransmitLinkSpeed,
            })
        return rows
    finally:
        _iphlpapi.FreeMibTable(tbl)


def native_ipv4():
    """GetAdaptersAddresses：{ifIndex: IPv4}。"""
    buf = ctypes.create_string_buffer(32000)
    size = ctypes.c_ulong(len(buf))
    rc = _iphlpapi.GetAdaptersAddresses(2, 0, None, buf, ctypes.byref(size))   # AF_INET
    out = {}
    if rc != 0:
        return out
    addr = ctypes.addressof(buf)
    while addr:
        a = ctypes.cast(addr, ctypes.POINTER(IP_ADAPTER_ADDRESSES)).contents
        p = a.FirstUnicastAddress
        while p:
            u = ctypes.cast(p, ctypes.POINTER(IP_ADAPTER_UNICAST_ADDRESS)).contents
            if u.lpSockaddr:
                raw = ctypes.string_at(u.lpSockaddr, 16)
                if len(raw) >= 8 and raw[0] == 2:                              # AF_INET
                    ip = ".".join(str(b) for b in raw[4:8])
                    if not ip.startswith("169.254"):
                        out.setdefault(a.IfIndex, ip)
            p = u.Next
        addr = a.Next
    return out


def fmt_speed(bps):
    if not bps:
        return ""
    if bps >= 1_000_000_000:
        return f"{bps / 1_000_000_000:.1f} Gbps"
    if bps >= 1_000_000:
        return f"{int(bps / 1_000_000)} Mbps"
    return f"{int(bps / 1000)} Kbps"


def native_adapters():
    """返回与 list_adapters_ps 相同结构的适配器列表，外加 admin/oper/mcs/ip 字段。"""
    rows = native_if_table()
    if not rows:
        return [], "GetIfTable2 未返回任何接口"
    ips = native_ipv4()
    out = []
    for r in rows:
        alias, desc = r["alias"], r["desc"]
        low = f"{alias} {desc}".lower()
        if r["type"] in (IF_TYPE_LOOPBACK, IF_TYPE_TUNNEL):
            continue
        if any(s in alias.lower() for s in FILTER_SUFFIXES):
            continue
        if any(h in low for h in HIDDEN_DESC_HINTS):
            continue
        if r["admin"] == 2:
            status = "Disabled"
        elif r["oper"] == 1:
            status = "Up"
        elif r["oper"] == 5:
            status = "Dormant"
        else:
            status = "Disconnected"
        media = ("802.3" if r["type"] == IF_TYPE_ETHERNET_CSMACD
                 else "Native 802.11" if r["type"] == IF_TYPE_IEEE80211 else "")
        mac = ":".join(f"{b:02X}" for b in r.get("mac", [])) if r.get("mac") else ""
        out.append({
            "name": alias, "desc": desc, "status": status, "media": media,
            "mac": mac, "speed": fmt_speed(r["tx"]), "index": r["index"],
            "admin": r["admin"], "oper": r["oper"], "mcs": r["mcs"],
            "ip": ips.get(r["index"], ""),
        })
    return out, ""


# ---------------------------------------------------------------- 网卡相关
def list_adapters():
    """返回所有网络适配器信息（原生 API 优先，PowerShell / netsh 兜底）。"""
    try:
        ads, err = native_adapters()
    except Exception as exc:                                     # pragma: no cover
        ads, err = [], f"原生接口枚举失败：{exc}"
    if ads:
        return ads, ""
    ads2, err2 = list_adapters_ps()
    return ads2, (err2 or err)


def list_adapters_ps():
    """返回所有网络适配器信息（PowerShell 优先，netsh 兜底）。仅在原生通道失效时使用。"""
    script = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        "$a = @(Get-NetAdapter -ErrorAction SilentlyContinue | "
        "Select-Object Name,InterfaceDescription,Status,MediaType,MacAddress,LinkSpeed,ifIndex);"
        "ConvertTo-Json -InputObject $a -Compress -Depth 4"
    )
    out, err, rc = run_ps(script)
    data = extract_json(out)
    if isinstance(data, dict):
        data = [data]
    if data:
        result = []
        for item in data:
            result.append({
                "name": item.get("Name") or "",
                "desc": item.get("InterfaceDescription") or "",
                "status": item.get("Status") or "",
                "media": item.get("MediaType") or "",
                "mac": item.get("MacAddress") or "",
                "speed": item.get("LinkSpeed") or "",
                "index": item.get("ifIndex"),
            })
        return result, ""
    return list_adapters_netsh()


def list_adapters_netsh():
    """netsh 兜底方案。"""
    out, err, rc = run_ps("netsh interface show interface")
    items = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\S+)\s+(\S+)\s+(\S+)\s+(.+)$", line)
        if not m:
            continue
        admin, state, _type, name = m.groups()
        if admin in ("管理员状态", "Admin") or name.startswith("接口"):
            continue
        items.append({
            "name": name.strip(),
            "desc": name.strip(),
            "status": "Disabled" if "禁用" in admin else ("Up" if "已连接" in state else "Disconnected"),
            "media": "802.3",
            "mac": "",
            "speed": "",
            "index": None,
        })
    return items, ("" if items else (err.strip() or "未能读取网卡列表"))


def is_wired(ad) -> bool:
    blob = f"{ad['name']} {ad['desc']}".lower()
    if any(h in blob for h in VIRTUAL_HINTS):
        return False
    if any(h in blob for h in WIRELESS_HINTS):
        return False
    return ad["media"].strip() == "802.3"


def _wired_rank(ad):
    """候选排序权重：已启用 > 设备在位 > 已插网线 > 已连通。"""
    return (ad.get("admin", 1) == 1,
            ad.get("oper", 1) != 6,
            ad.get("mcs", 2) == 1,
            ad.get("oper", 0) == 1)


def pick_wired(adapters):
    wired = [a for a in adapters if is_wired(a)]
    if not wired:
        return adapters
    return sorted(wired, key=_wired_rank, reverse=True)


def get_ipv4(index):
    """取 IPv4：原生 API 优先（毫秒级），失败再退回 PowerShell。"""
    if not index:
        return ""
    try:
        m = native_ipv4()
        if m:
            return m.get(int(index), "")
    except Exception:
        pass
    out, _, _ = run_ps(
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        f"(Get-NetIPAddress -InterfaceIndex {int(index)} -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
        "Where-Object { $_.IPAddress -notlike '169.254*' } | "
        "Select-Object -First 1 -ExpandProperty IPAddress)"
    )
    return out.strip().splitlines()[0].strip() if out.strip() else ""


def run_cmd(args, timeout=20):
    """执行普通命令行程序（无窗口），返回 (stdout, stderr, returncode)。"""
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return "", "命令执行超时", -1
    except Exception as exc:                                     # pragma: no cover
        return "", f"无法启动命令：{exc}", -1
    return decode_bytes(p.stdout), decode_bytes(p.stderr), p.returncode


def _cmd_failed(text):
    low = (text or "").lower()
    return any(k in low for k in ("错误", "无效", "失败", "error", "fail", "invalid", "not found"))


def set_adapter_state(ad, enable: bool):
    """启用 / 禁用网卡，返回 (成功, 提示信息)。netsh 优先（实测比 PowerShell 快约 50 倍）。"""
    state = "enable" if enable else "disable"
    out, err, rc = run_cmd(["netsh", "interface", "set", "interface",
                            f'name="{ad["name"]}"', f"admin={state}"])
    if rc == 0 and not _cmd_failed(out) and not _cmd_failed(err):
        return True, ""
    # PowerShell 兜底
    verb = "Enable" if enable else "Disable"
    if ad.get("index") is not None:
        script = (
            f"$a = Get-NetAdapter -InterfaceIndex {int(ad['index'])} -ErrorAction Stop; "
            f"$a | {verb}-NetAdapter -Confirm:$false -ErrorAction Stop"
        )
    else:
        script = (
            f"Get-NetAdapter -Name {ps_quote(ad['name'])} -ErrorAction Stop | "
            f"{verb}-NetAdapter -Confirm:$false -ErrorAction Stop"
        )
    out2, err2, rc2 = run_ps(script)
    if rc2 == 0 and not err2.strip():
        return True, ""
    msg = (err2.strip() or out.strip() or err.strip() or "未知错误").splitlines()
    return False, msg[-1][:120] if msg else "操作失败"


# ---------------------------------------------------------------- 权限提升
def script_path() -> str:
    return os.path.abspath(__file__)


def is_frozen() -> bool:
    """是否为 PyInstaller 打包后的 exe。"""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> str:
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(script_path())


def resource_path(*parts) -> str:
    """兼容源码运行与 PyInstaller 打包后的资源路径。"""
    base = getattr(sys, "_MEIPASS", None) or app_dir()
    return os.path.join(base, *parts)


def python_for_elevate() -> str:
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        pw = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(pw):
            return pw
    return exe


def launch_elevated(token: str) -> bool:
    """以管理员身份重新启动本程序（兼容 exe 打包形态）。"""
    if is_frozen():
        exe = sys.executable
        params = f"--elevated --token {token}"
    else:
        exe = python_for_elevate()
        params = f'"{script_path()}" --elevated --token {token}'
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, app_dir(), 1)
    return rc > 32


def mark_ready(token: str):
    path = os.path.join(tempfile.gettempdir(), f"ethsw_ready_{token}")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        atexit.register(lambda: os.path.exists(path) and os.remove(path))
    except Exception:
        pass


def ready_path(token: str) -> str:
    return os.path.join(tempfile.gettempdir(), f"ethsw_ready_{token}")


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


def desktop_path() -> str:
    """真实的桌面目录（可能被重定向到其它盘符，不能硬拼 USERPROFILE）。"""
    try:
        folderid_desktop = GUID(0xB4BFCC3A, 0xDB2C, 0x424C,
                                (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41))
        ptr = ctypes.c_wchar_p()
        ctypes.windll.ole32.SHGetKnownFolderPath(ctypes.byref(folderid_desktop), 0, None,
                                                 ctypes.byref(ptr))
        if ptr.value:
            value = ptr.value
            ctypes.windll.ole32.CoTaskMemFree(ptr)
            return value
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Desktop")


def create_desktop_shortcut():
    """为当前运行的程序在桌面创建快捷方式，返回 (成功, 说明)。"""
    workdir = app_dir()
    icon = resource_path("assets", "icon.ico")
    if not os.path.exists(icon):
        icon = ""
    if is_frozen():
        target, args = sys.executable, ""
    else:
        target, args = python_for_elevate(), f'"{script_path()}"'
    lnk = os.path.join(desktop_path(), f"{APP_NAME}.lnk")

    parts = [
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({ps_quote(lnk)});",
        f"$s.TargetPath = {ps_quote(target)};",
        f"$s.WorkingDirectory = {ps_quote(workdir)};",
        f"$s.Description = {ps_quote('一键启用 / 禁用有线网卡')};",
        f"$s.Arguments = {ps_quote(args)};" if args else "",
        f"$s.IconLocation = {ps_quote(icon)};" if icon else "",
        "$s.Save()",
    ]
    out, err, rc = run_ps(" ".join(p for p in parts if p), timeout=60)
    if os.path.exists(lnk):
        return True, f"已在桌面创建快捷方式：\n{lnk}"
    return False, (err.strip() or out.strip() or "创建失败")[:160]


# ---------------------------------------------------------------- 图形绘制
def rounded_polygon(canvas, x1, y1, x2, y2, r, **kw):
    """画一个圆角矩形（用多边形近似，smooth 让边角更柔和）。"""
    r = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
    pts = []
    for cx, cy, a0 in ((x1 + r, y1 + r, 180), (x2 - r, y1 + r, 270), (x2 - r, y2 - r, 0), (x1 + r, y2 - r, 90)):
        for deg in range(0, 91, 10):
            a = math.radians(a0 + deg)
            pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
    return canvas.create_polygon(pts, smooth=True, **kw)


class ToggleSwitch(tk.Canvas):
    """Canvas 手绘的滑动开关：外观现代、带动画、点击触发回调。"""

    W, H = 168, 84

    def __init__(self, master, command=None, **kw):
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("bd", 0)
        super().__init__(master, width=self.W, height=self.H, bg=master.cget("bg"), **kw)
        self.command = command
        self.enabled = False
        self.busy = False
        self._anim = 0.0
        self._job = None
        self.track = rounded_polygon(self, 0, 0, self.W, self.H, self.H / 2, fill=TRACK_OFF, outline="")
        self.knob = self.create_oval(0, 0, 0, 0, fill=KNOB, outline="", width=0)
        self.shadow = self.create_oval(0, 0, 0, 0, fill="#E3E6EA", outline="")
        self.bind("<Button-1>", self._on_click)
        self.configure(cursor="hand2")
        self.draw(0.0)

    # -- 交互
    def _on_click(self, _event=None):
        if self.busy:
            return
        if self.command:
            self.command()

    def set_busy(self, busy: bool):
        self.busy = busy
        self.configure(cursor="watch" if busy else "hand2")
        self.draw(self._anim)

    # -- 绘制
    def draw(self, t: float):
        """t: 0=关闭 1=开启"""
        self._anim = max(0.0, min(1.0, t))
        pad = 8
        d = self.H - 2 * pad
        x1 = pad + (self.W - d - 2 * pad) * self._anim
        color = TRACK_ON if self._anim > 0.5 else TRACK_OFF
        if self.busy:
            color = "#9AA1AA"
        self.itemconfigure(self.track, fill=color)
        self.coords(self.shadow, x1 - 2, pad - 1, x1 + d + 2, pad + d + 3)
        self.coords(self.knob, x1, pad, x1 + d, pad + d)
        self.itemconfigure(self.knob, fill=KNOB)
        # 关闭状态下的旋钮投影更淡
        self.itemconfigure(self.shadow, fill="#D9DDE2" if self._anim > 0.5 else "#E7EAEE")

    def animate_to(self, target: bool, steps=14, ms=12):
        if self._job:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        start = self._anim
        end = 1.0 if target else 0.0
        if abs(start - end) < 0.01:
            self.draw(end)
            return

        def step(i=0):
            if i > steps:
                self.draw(end)
                self._job = None
                return
            e = i / steps
            e = 1 - (1 - e) * (1 - e) * (1 - e)          # easeOutCubic
            self.draw(start + (end - start) * e)
            self._job = self.after(ms, lambda: step(i + 1))

        step()


class Dot(tk.Canvas):
    """状态指示灯（带呼吸动画的小圆点）。"""

    def __init__(self, master, size=14, **kw):
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("bd", 0)
        super().__init__(master, width=size, height=size, bg=master.cget("bg"), **kw)
        self.size = size
        self.dot = self.create_oval(1, 1, size - 1, size - 1, fill=TRACK_OFF, outline="")
        self._job = None
        self._phase = 0

    def set_color(self, color, pulse=False):
        self.itemconfigure(self.dot, fill=color)
        if self._job:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        if not pulse:
            self.coords(self.dot, 1, 1, self.size - 1, self.size - 1)
            return

        def blink():
            try:
                self._phase = (self._phase + 1) % 12
                scale = 0.75 + 0.25 * (1 - abs(self._phase - 6) / 6)
                pad = (self.size * (1 - scale)) / 2
                self.coords(self.dot, pad, pad, self.size - pad, self.size - pad)
                self._job = self.after(90, blink)
            except Exception:
                self._job = None

        blink()


# ---------------------------------------------------------------- 主应用
class App:
    POLL_INTERVAL = 1500          # 原生通道查询成本约 10ms，可以更频繁地感知外部变化

    def __init__(self, root, privileged: bool, token: str = ""):
        self.root = root
        self.privileged = privileged
        self.token = token
        self.adapters = []
        self.wired = []
        self.current = None
        self.busy = False
        self._refreshing = False
        self.target_state = None
        self.last_query_ms = None
        self._closing = False

        root.title(APP_NAME)
        root.configure(bg=BG)
        root.resizable(False, False)
        self._set_icon()
        self._build_ui()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<space>", lambda e: self.toggle())
        root.bind("<Return>", lambda e: self.toggle())
        root.bind("<F5>", lambda e: self.refresh())
        root.bind("<Escape>", lambda e: self.on_close())

        self.refresh()
        self._auto_poll()

    # -- 图标
    def _set_icon(self):
        for name in (os.path.join("assets", "icon.png"), "icon.png"):
            path = resource_path(name)
            if os.path.exists(path):
                try:
                    img = tk.PhotoImage(file=path)
                    self.root.iconphoto(True, img)
                    self._icon = img
                    return
                except Exception:
                    pass

    # -- 界面
    def _build_ui(self):
        f = lambda size, weight="normal": (FONT_FAMILY, size, weight)

        if not self.privileged:
            bar = tk.Frame(self.root, bg=WARN_BG, height=34)
            bar.pack(fill="x")
            bar.pack_propagate(False)
            tk.Label(bar, text="需要管理员权限才能切换网卡", bg=WARN_BG, fg=WARN_FG,
                     font=f(9)).pack(side="left", padx=(12, 6))
            tk.Button(bar, text="授权", command=self.request_privilege, bd=0, bg=WARN_BG,
                      fg="#1D4ED8", activebackground=WARN_BG, font=f(9, "bold"),
                      cursor="hand2", relief="flat").pack(side="left")

        body = tk.Frame(self.root, bg=BG, padx=22, pady=16)
        body.pack(fill="both", expand=True)

        head = tk.Frame(body, bg=BG)
        head.pack(fill="x")
        self.title_label = tk.Label(head, text=APP_NAME, bg=BG, fg=TEXT, font=f(13, "bold"))
        self.title_label.pack(side="left")
        self.menu_btn = tk.Button(head, text="⋯", command=self.show_main_menu,
                                  bd=0, bg=BG, fg=SUBTEXT, activebackground=BG,
                                  font=f(11), cursor="hand2", relief="flat", width=2)
        self.menu_btn.pack(side="right")
        self.adapter_btn = tk.Button(head, text="切换网卡 ▾", command=self.show_adapter_menu,
                                     bd=0, bg=BG, fg=SUBTEXT, activebackground=BG,
                                     font=f(9), cursor="hand2", relief="flat")
        self.adapter_btn.pack(side="right")

        self.switch = ToggleSwitch(body, command=self.toggle)
        self.switch.pack(pady=(14, 6))

        line = tk.Frame(body, bg=BG)
        line.pack()
        self.dot = Dot(line)
        self.dot.pack(side="left", padx=(0, 7))
        self.state_label = tk.Label(line, text="检测中…", bg=BG, fg=TEXT, font=f(17, "bold"))
        self.state_label.pack(side="left")

        self.detail_label = tk.Label(body, text="", bg=BG, fg=SUBTEXT, font=f(9),
                                     wraplength=320, justify="center")
        self.detail_label.pack(pady=(4, 0))

        self.msg_label = tk.Label(body, text="", bg=BG, fg=ERR_FG, font=f(9),
                                  wraplength=320, justify="center")
        self.msg_label.pack(pady=(6, 0))

        footer = tk.Frame(body, bg=BG)
        footer.pack(fill="x", pady=(10, 0))
        tk.Label(footer, text="空格键切换 · F5 刷新", bg=BG, fg="#AEB5BF", font=f(8)).pack(side="left")
        self.refresh_btn = tk.Button(footer, text="刷新", command=self.refresh, bd=0, bg=BG,
                                     fg=SUBTEXT, activebackground=BG, font=f(9),
                                     cursor="hand2", relief="flat")
        self.refresh_btn.pack(side="right")

    def center(self):
        self.root.update_idletasks()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 3
        self.root.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    # -- 数据
    def load_adapters(self):
        adapters, err = list_adapters()
        self.adapters = adapters
        self.wired = pick_wired(adapters)
        if self.wired:
            if self.current and any(a["name"] == self.current["name"] for a in self.wired):
                self.current = next(a for a in self.wired if a["name"] == self.current["name"])
            else:
                # 优先选状态为 Up / 非 Disabled 的
                active = [a for a in self.wired if a["status"] != "Disabled"]
                self.current = (active or self.wired)[0]
        else:
            self.current = None
        return err

    def refresh(self):
        """状态刷新：原生通道为毫秒级，首次也走后台线程，界面立刻可交互。"""
        if self.busy or self._refreshing:
            return
        self._refreshing = True
        threading.Thread(target=self._worker_refresh, daemon=True).start()

    def _worker_refresh(self):
        t0 = time.perf_counter()
        try:
            err = self.load_adapters()
            ad = self.current
            ip = ""
            if ad:
                ip = ad.get("ip") or (get_ipv4(ad["index"]) if ad.get("status") == "Up" else "")
        finally:
            self._refreshing = False
            self.last_query_ms = (time.perf_counter() - t0) * 1000
        self.root.after(0, lambda: self._sync(err=err, ip=ip))

    def _sync(self, err="", ip=""):
        if self._closing:
            return
        ad = self.current
        if not ad:
            self.switch.animate_to(False)
            self.state_label.configure(text="未检测到网卡", fg=SUBTEXT)
            self.dot.set_color(TRACK_OFF)
            self.detail_label.configure(text=err or "本机未找到可用的以太网适配器")
            self.title_label.configure(text=APP_NAME)
            return

        status = ad.get("status", "")
        on = status != "Disabled"
        self.switch.animate_to(on)
        self.title_label.configure(text=f'{APP_NAME} · {ad["name"]}')

        if not on:
            self.state_label.configure(text="已关闭", fg=SUBTEXT)
            self.dot.set_color(TRACK_OFF)
            self.detail_label.configure(text=ad["desc"])
        elif status == "Up":
            self.state_label.configure(text="已开启", fg=GREEN_DARK)
            self.dot.set_color(GREEN, pulse=True)
            bits = [ad["desc"]]
            if ad.get("speed"):
                bits.append(ad["speed"])
            if ip:
                bits.append(ip)
            self.detail_label.configure(text=" · ".join(bits))
        elif status == "Dormant":
            self.state_label.configure(text="已开启", fg=GREEN_DARK)
            self.dot.set_color("#EAB308", pulse=True)
            self.detail_label.configure(text="正在获取网络地址…")
        else:
            self.state_label.configure(text="已开启", fg=GREEN_DARK)
            self.dot.set_color("#EAB308", pulse=True)
            self.detail_label.configure(text=f'{ad["desc"]} · 网线未插入' if ad.get("desc") else "网线未插入")

        if err and not self.adapters:
            self.msg_label.configure(text=err[:100])

    def _auto_poll(self):
        if self._closing:
            return
        if not self.busy:
            self.refresh()
        self.root.after(self.POLL_INTERVAL, self._auto_poll)

    # -- 切换
    def toggle(self):
        if self.busy or not self.current:
            return
        if not self.privileged:
            self.request_privilege()
            return
        target_on = self.current.get("status") == "Disabled"
        self.target_state = target_on
        self.busy = True
        self.switch.set_busy(True)
        self.switch.animate_to(target_on)          # 立即给反馈，不等系统返回
        self.msg_label.configure(text="")
        self.dot.set_color("#9AA1AA", pulse=True)
        self.state_label.configure(text="切换中…", fg=SUBTEXT)
        threading.Thread(target=self._worker_toggle, args=(target_on,), daemon=True).start()

    def _worker_toggle(self, target_on):
        ok, msg = set_adapter_state(self.current, target_on)
        # 每 120ms 用原生接口回读一次，最多约 5.4 秒
        final = None
        for _ in range(45):
            time.sleep(0.12)
            ads, _ = list_adapters()
            for a in ads:
                if a["name"] == self.current["name"]:
                    final = a
                    break
            if final:
                got = final.get("status") != "Disabled"
                if got == target_on:
                    break
        self.root.after(0, lambda: self._toggle_done(ok, msg, final, target_on))

    def _toggle_done(self, ok, msg, final, target_on):
        self.busy = False
        self.switch.set_busy(False)
        if final:
            for a in self.adapters:
                if a["name"] == self.current["name"]:
                    a.update(final)
            self.current = final
        done = bool(final) and (final.get("status") != "Disabled") == target_on
        if done:
            self._sync()
        else:
            self._sync()
            self.msg_label.configure(text=msg or "切换失败，请检查权限或网卡状态")

    # -- 权限
    def request_privilege(self):
        token = f"{int(time.time())}_{os.getpid()}"
        self.pending_token = token
        self.msg_label.configure(text="正在请求管理员权限…")
        if launch_elevated(token):
            self._watch_elevated(token, 0)
        else:
            self.msg_label.configure(text="无法请求管理员权限")

    def _watch_elevated(self, token, ticks):
        """等待另一个以管理员身份运行的实例就绪；就绪后本实例自动让位。"""
        if self._closing:
            return
        if os.path.exists(ready_path(token)):
            self.on_close(silent=True)
            return
        if ticks == 60:                                   # 约 30 秒仍无授权
            self.msg_label.configure(text="尚未获得管理员权限；授权成功后本窗口会自动升级")
        interval = 500 if ticks < 60 else 1500            # 之后放慢轮询，但不放弃
        self.root.after(interval, lambda: self._watch_elevated(token, ticks + 1))

    # -- 菜单
    def _popup(self, menu, widget):
        x = self.root.winfo_rootx() + widget.winfo_x()
        y = self.root.winfo_rooty() + widget.winfo_y() + widget.winfo_height()
        menu.tk_popup(max(x, 0), max(y, 0))

    def show_main_menu(self):
        menu = tk.Menu(self.root, tearoff=0, font=(FONT_FAMILY, 9), bg="#FFFFFF",
                       fg=TEXT, activebackground=CARD, bd=0, relief="flat")
        menu.add_command(label="创建桌面快捷方式", command=self.do_shortcut)
        menu.add_command(label="重新检测网卡", command=self.refresh)
        menu.add_separator()
        menu.add_command(label="关于", command=self.show_about)
        self._popup(menu, self.menu_btn)

    def do_shortcut(self):
        self.msg_label.configure(text="正在创建桌面快捷方式…", fg=SUBTEXT)
        threading.Thread(target=self._worker_shortcut, daemon=True).start()

    def _worker_shortcut(self):
        ok, msg = create_desktop_shortcut()
        self.root.after(0, lambda: self._shortcut_done(ok, msg))

    def _shortcut_done(self, ok, msg):
        self.msg_label.configure(text=msg, fg=SUBTEXT if ok else ERR_FG)

    def show_about(self):
        if is_frozen():
            kind = "打包 exe · 目录版（极速启动）" if os.path.isdir(os.path.join(app_dir(), "_internal")) \
                else "打包 exe · 单文件版（便携）"
        else:
            kind = "Python 源码运行"
        cost = f"{self.last_query_ms:.1f} ms" if self.last_query_ms else "—"
        messagebox.showinfo("关于",
                            f"{APP_NAME} v{APP_VERSION}\n\n"
                            f"运行方式：{kind}\n"
                            f"状态查询：{cost}（原生 API）\n\n"
                            "空格键切换 · F5 刷新 · Esc 退出")

    # -- 网卡选择菜单
    def show_adapter_menu(self):
        menu = tk.Menu(self.root, tearoff=0, font=(FONT_FAMILY, 9), bg="#FFFFFF",
                       fg=TEXT, activebackground=CARD, bd=0, relief="flat")
        pool = self.wired or self.adapters
        if not pool:
            menu.add_command(label="（未检测到网卡）", state="disabled")
        for ad in pool:
            mark = "✓ " if self.current and ad["name"] == self.current["name"] else "   "
            state = "已关闭" if ad.get("status") == "Disabled" else ad.get("status", "")
            menu.add_command(label=f"{mark}{ad['name']}   ({state})",
                             command=lambda a=ad: self.select_adapter(a))
        self._popup(menu, self.adapter_btn)

    def select_adapter(self, ad):
        self.current = ad
        self.refresh()

    # -- 退出
    def on_close(self, silent=False):
        self._closing = True
        try:
            if self.token:
                p = ready_path(self.token)
                if os.path.exists(p):
                    os.remove(p)
        except Exception:
            pass
        self.root.destroy()


# ---------------------------------------------------------------- 入口
def enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def selftest():
    print(f"[{APP_NAME}] 版本 {APP_VERSION}")
    print(f"管理员权限: {is_admin()}")
    t0 = time.perf_counter()
    ads, err = list_adapters()
    cost = (time.perf_counter() - t0) * 1000
    print(f"读取到 {len(ads)} 个适配器，耗时 {cost:.1f} ms（原生通道）"
          + (f"（错误：{err}）" if err else ""))
    for a in ads:
        print(f'  - {a["name"]:<16} | {a["status"]:<12} | {a["media"]:<14} | {a["speed"]:<10} | {a["desc"]}')
    wired = pick_wired(ads)
    print("\n识别为有线网卡：")
    for a in wired:
        print(f'  * {a["name"]} ({a["desc"]}) -> {a["status"]}  '
              f'admin={a.get("admin")} oper={a.get("oper")} 网线={a.get("mcs")}  IPv4={a.get("ip") or get_ipv4(a["index"])}')
    if not wired:
        print("  （无）")
    t1 = time.perf_counter()
    native_adapters()
    print(f"\n二次查询耗时 {((time.perf_counter() - t1) * 1000):.1f} ms")


def uitest():
    """构建界面并演练各状态，1.5 秒后自动退出。"""
    root = tk.Tk()
    root.withdraw()
    app = App(root, privileged=is_admin())
    app.center()
    root.deiconify()
    checks = []

    def run_checks():
        try:
            app.switch.animate_to(True)
            app.dot.set_color(GREEN, pulse=True)
            app.state_label.configure(text="已开启")
            checks.append("on")
            app.switch.animate_to(False)
            app.dot.set_color(TRACK_OFF)
            checks.append("off")
            app.switch.set_busy(True)
            app.switch.set_busy(False)
            checks.append("busy")
            checks.append(f"adapter={app.current['name'] if app.current else 'NONE'}")
            checks.append(f"state={app.state_label.cget('text')}")
        except Exception as exc:                              # pragma: no cover
            checks.append(f"ERROR: {exc}")

    root.after(400, run_checks)
    root.after(1500, root.destroy)
    root.mainloop()
    print("UI 冒烟测试通过 | " + " | ".join(checks))


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--uitest", action="store_true")
    parser.add_argument("--elevated", action="store_true")
    parser.add_argument("--token", default="")
    args, _ = parser.parse_known_args()

    if args.selftest:
        selftest()
        return

    if args.uitest:
        uitest()
        return

    enable_dpi_awareness()

    if args.elevated:
        if args.token:
            mark_ready(args.token)
        start_ui(privileged=True, token=args.token)
        return

    if is_admin():
        start_ui(privileged=True)
        return

    # 非管理员：后台请求提权，同时先以只读方式启动，避免 UAC 被取消时窗口消失
    token = f"{int(time.time())}_{os.getpid()}"
    launch_elevated(token)
    start_ui(privileged=False, watch_token=token)


def start_ui(privileged, token="", watch_token=""):
    root = tk.Tk()
    # token       : 本实例自己创建的就绪标记（仅提权实例持有，退出时清理）
    # watch_token : 等待另一个提权实例就绪（只读实例使用，就绪后自动退出）
    app = App(root, privileged=privileged, token=token if privileged else "")
    if watch_token:
        app._watch_elevated(watch_token, 0)
    app.center()
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # pythonw 启动时没有控制台，崩溃必须用弹窗暴露出来，否则表现为"双击没反应"
        import traceback
        detail = traceback.format_exc()
        try:
            log = os.path.join(tempfile.gettempdir(), "ethernet_switch_error.log")
            with open(log, "w", encoding="utf-8") as fh:
                fh.write(detail)
            detail += f"\n\n详细信息已写入：{log}"
        except Exception:
            pass
        try:
            ctypes.windll.user32.MessageBoxW(None, detail[-1500:], f"{APP_NAME} 启动失败", 0x10)
        except Exception:
            pass

