# -*- coding: utf-8 -*-
"""
生成应用图标：圆角渐变方块 + 白色 RJ45 水晶头/网线图形。
输出 assets/icon.png（256×256 透明底）与 assets/icon.ico（Windows 图标）。
纯标准库实现，带解析式抗锯齿。
"""

import os
import struct
import zlib

SIZE = 256
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

BG_TOP = (0x3B, 0x82, 0xF6)      # 渐变顶色
BG_BOTTOM = (0x1D, 0x4E, 0xD8)   # 渐变底色
GLYPH = (0xFF, 0xFF, 0xFF)


# ---------------------------------------------------------------- SDF 工具
def sd_round_rect(px, py, x1, y1, x2, y2, r):
    """到圆角矩形的有符号距离（负数表示在内部）。"""
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    hx, hy = (x2 - x1) / 2.0, (y2 - y1) / 2.0
    r = min(r, hx, hy)
    dx = abs(px - cx) - (hx - r)
    dy = abs(py - cy) - (hy - r)
    ax, ay = max(dx, 0.0), max(dy, 0.0)
    outside = (ax * ax + ay * ay) ** 0.5
    inside = min(max(dx, dy), 0.0)
    return outside + inside - r


def cover(d):
    """距离 -> 覆盖率（约 1 像素抗锯齿过渡带）。"""
    c = 0.5 - d
    if c <= 0.0:
        return 0.0
    if c >= 1.0:
        return 1.0
    return c


def over(dst, src_rgb, src_a):
    """src over dst 合成，dst = (r, g, b, a)，颜色与 alpha 均为 0..1 区间外的 0..255。"""
    dr, dg, db, da = dst
    sr, sg, sb = src_rgb
    sa = src_a
    oa = sa + da * (1.0 - sa)
    if oa <= 0:
        return (0.0, 0.0, 0.0, 0.0)
    or_ = (sr * sa + dr * da * (1.0 - sa)) / oa
    og = (sg * sa + dg * da * (1.0 - sa)) / oa
    ob = (sb * sa + db * da * (1.0 - sa)) / oa
    return (or_, og, ob, oa)


def gradient_at(y):
    t = (y - 0.0) / (SIZE - 1.0)
    t = max(0.0, min(1.0, t))
    return tuple(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t for i in range(3))


# ---------------------------------------------------------------- 图形定义
SQUARE = dict(x1=10.0, y1=10.0, x2=SIZE - 10.0, y2=SIZE - 10.0, r=58.0)
CABLE = dict(x1=110.0, y1=50.0, x2=146.0, y2=118.0, r=13.0)
BODY = dict(x1=88.0, y1=104.0, x2=168.0, y2=186.0, r=18.0)
# 4 个卡槽：宽 8、间距 6，整体恰好居中于水晶头内部（103~153）
PINS = [dict(x1=103.0 + i * 14.0, y1=150.0, x2=111.0 + i * 14.0, y2=178.0, r=3.5) for i in range(4)]


def render():
    rows = []
    for y in range(SIZE):
        row = bytearray()
        py = y + 0.5
        grad = gradient_at(py)
        for x in range(SIZE):
            px = x + 0.5
            # 背景圆角方块
            a = cover(sd_round_rect(px, py, SQUARE["x1"], SQUARE["y1"], SQUARE["x2"], SQUARE["y2"], SQUARE["r"]))
            r, g, b, al = over((0.0, 0.0, 0.0, 0.0), grad, a)
            # 白色图形：网线 + 水晶头
            for shape in (CABLE, BODY):
                c = cover(sd_round_rect(px, py, shape["x1"], shape["y1"], shape["x2"], shape["y2"], shape["r"]))
                if c > 0:
                    r, g, b, al = over((r, g, b, al), GLYPH, c)
            # 卡槽（透出背景色）
            for pin in PINS:
                c = cover(sd_round_rect(px, py, pin["x1"], pin["y1"], pin["x2"], pin["y2"], pin["r"]))
                if c > 0:
                    r, g, b, al = over((r, g, b, al), grad, c)
            row += bytes((int(round(r)), int(round(g)), int(round(b)), int(round(al * 255))))
        rows.append(bytes(row))
    return rows


# ---------------------------------------------------------------- 文件写出
def png_bytes(rows):
    raw = b"".join(b"\x00" + r for r in rows)
    comp = zlib.compress(raw, 9)

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", comp) + chunk(b"IEND", b"")


def ico_bytes(png):
    """把 PNG 数据直接嵌进 ICO（Vista+ 支持 PNG 格式图标）。"""
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", SIZE if SIZE < 256 else 0, SIZE if SIZE < 256 else 0,
                        0, 0, 1, 32, len(png), 6 + 16)
    return header + entry + png


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    rows = render()
    data = png_bytes(rows)
    with open(os.path.join(OUT_DIR, "icon.png"), "wb") as fh:
        fh.write(data)
    with open(os.path.join(OUT_DIR, "icon.ico"), "wb") as fh:
        fh.write(ico_bytes(data))
    print(f"已生成图标：{os.path.join(OUT_DIR, 'icon.png')} ({len(data)} 字节)")


if __name__ == "__main__":
    main()
