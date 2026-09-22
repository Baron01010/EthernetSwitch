# -*- coding: utf-8 -*-
"""性能基准：对比 PowerShell 调用与原生 Windows API 调用的耗时与结果一致性。"""

import os
import re
import sys
import json
import time
import ctypes
import subprocess
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as A                                       # 复用现有实现做对照

IPHLPAPI = ctypes.WinDLL("iphlpapi", use_last_error=True)


# ------------------------------------------------------------ 原生 API 原型
class MIB_IF_ROW2(ctypes.Structure):
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
    tbl = ctypes.c_void_p()
    rc = IPHLPAPI.GetIfTable2(ctypes.byref(tbl))
    if rc != 0:
        return None
    try:
        num = ctypes.cast(tbl, ctypes.POINTER(ctypes.c_ulong)).contents.value
        base = tbl.value
        stride = ctypes.sizeof(MIB_IF_ROW2)
        rows = []
        for i in range(num):
            row = ctypes.cast(base + 8 + i * stride, ctypes.POINTER(MIB_IF_ROW2)).contents
            rows.append({
                "index": row.InterfaceIndex,
                "alias": row.Alias,
                "desc": row.Description,
                "type": row.Type,
                "phys": row.PhysicalMediumType,
                "oper": row.OperStatus,
                "admin": row.AdminStatus,
                "mcs": row.MediaConnectState,
                "tx": row.TransmitLinkSpeed,
            })
        return rows
    finally:
        IPHLPAPI.FreeMibTable(tbl)


def native_ipv4():
    buf = ctypes.create_string_buffer(30000)
    size = ctypes.c_ulong(len(buf))
    rc = IPHLPAPI.GetAdaptersAddresses(2, 0x0080, None, buf, ctypes.byref(size))  # AF_INET, GAA_FLAG_SKIP_ANYCAST...
    out = {}
    if rc != 0:
        return out
    addr = ctypes.addressof(buf)
    while addr:
        a = ctypes.cast(addr, ctypes.POINTER(IP_ADAPTER_ADDRESSES)).contents
        p = a.FirstUnicastAddress
        while p:
            u = ctypes.cast(p, ctypes.POINTER(IP_ADAPTER_UNICAST_ADDRESS)).contents
            sa = u.lpSockaddr
            if sa:
                raw = ctypes.string_at(sa, 16)
                if len(raw) >= 8 and raw[0] == 2:                 # AF_INET
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
    return f"{int(bps / 1_000_000)} Mbps"


def timeit(fn, n=3):
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000)
    return ts


def run(cmd):
    subprocess.run(cmd, capture_output=True, creationflags=0x08000000)


def main():
    print("=" * 72)
    print("1) 原生 API 结果（GetIfTable2）")
    rows = native_if_table()
    if not rows:
        print("  GetIfTable2 调用失败")
    else:
        ips = native_ipv4()
        print(f'  {"idx":>4} {"type":>5} {"oper":>4} {"admin":>5} {"mcs":>3}  {"alias":<14} {"ip":<15} desc')
        for r in rows:
            print(f'  {r["index"]:>4} {r["type"]:>5} {r["oper"]:>4} {r["admin"]:>5} '
                  f'{r["mcs"]:>3}  {r["alias"]:<14} {ips.get(r["index"], "-"):<15} {r["desc"]}')
        print(f"\n  sizeof(MIB_IF_ROW2) = {ctypes.sizeof(MIB_IF_ROW2)}")

    print("\n" + "=" * 72)
    print("2) PowerShell 结果（Get-NetAdapter，对照用）")
    ads, err = A.list_adapters()
    for a in ads:
        print(f'  {a["index"] if a["index"] else "-":>4} {"":>5}      -      -   -  '
              f'{a["name"]:<14} {"":<15} {a["desc"]}  [{a["status"]}/{a["media"]}/{a["speed"]}]')

    print("\n" + "=" * 72)
    print("3) 耗时对比（毫秒，各 3 次）")

    def ps_all():
        A.list_adapters()
        A.get_ipv4(ads[0]["index"]) if ads and ads[0]["index"] else None

    def nat_all():
        native_if_table()
        native_ipv4()

    print(f"  PowerShell  Get-NetAdapter        : {[round(x) for x in timeit(lambda: A.list_adapters())]}")
    print(f"  PowerShell  Get-NetIPAddress      : {[round(x) for x in timeit(lambda: A.get_ipv4(ads[0]['index']))]}")
    print(f"  原生        GetIfTable2           : {[round(x, 1) for x in timeit(native_if_table)]}")
    print(f"  原生        GetAdaptersAddresses  : {[round(x, 1) for x in timeit(native_ipv4)]}")
    print(f"  一轮完整查询 PS / 原生            : "
          f"{[round(x) for x in timeit(ps_all)]}  vs  {[round(x, 1) for x in timeit(nat_all)]}")

    print("\n" + "=" * 72)
    print("4) 命令进程启动开销（只读命令，不改网卡状态）")
    print(f"  powershell -Command Get-NetAdapter: "
          f"{[round(x) for x in timeit(lambda: run(['powershell.exe', '-NoProfile', '-Command', 'Get-NetAdapter']))]}")
    print(f"  netsh interface show interface    : "
          f"{[round(x) for x in timeit(lambda: run(['netsh', 'interface', 'show', 'interface']))]}")
    print(f"  powershell 冷启动（空命令）       : "
          f"{[round(x) for x in timeit(lambda: run(['powershell.exe', '-NoProfile', '-Command', '1']))]}")


if __name__ == "__main__":
    main()
