"""生成 README 顶部的演示 GIF：一个"绿字成功、其实是假"的终端动画。

    python tools/make_gif.py                    # 写 docs/demo.gif
    python tools/make_gif.py --preview 5.10     # 只导出某个时刻的 PNG 用于验收

全部用 Pillow 自绘，不依赖浏览器（沙箱里截图不可用）。

render(t) 是 t 的纯函数，所以任何一帧都能单独复现、单独验收。
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "demo.gif"

W, H = 960, 590
BAR = 44
PAD_X, PAD_Y = 26, 22
LINE = 34

BG = (13, 15, 20)
PANEL = (18, 21, 28)
BAR_BG = (24, 27, 35)
INK = (226, 232, 240)
DIM = (139, 148, 167)
FAINT = (95, 103, 121)
GREEN = (74, 222, 128)
RED = (248, 113, 113)
YELLOW = (251, 191, 36)
CYAN = (94, 234, 212)

FONT_PATH = "C:/Windows/Fonts/msyh.ttc"
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"
_cache: dict = {}


def font(size: int, bold: bool = False):
    key = (size, bold)
    if key not in _cache:
        _cache[key] = ImageFont.truetype(FONT_BOLD if bold else FONT_PATH, size)
    return _cache[key]


def tw(d, s, size, bold=False):
    return d.textlength(s, font=font(size, bold))


def put(d, x, y, s, size=17, color=INK, bold=False):
    d.text((x, y), s, font=font(size, bold), fill=color)
    return tw(d, s, size, bold)


def chrome(img):
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W - 1, H - 1], fill=BG)
    d.rectangle([0, 0, W - 1, BAR], fill=BAR_BG)
    for i, c in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
        cx = 22 + i * 22
        d.ellipse([cx - 6, BAR // 2 - 6, cx + 6, BAR // 2 + 6], fill=c)
    put(d, 96, 12, "silent-audit", 15, DIM)
    put(d, 96 + tw(d, "silent-audit", 15) + 12, 12, "/  demo", 15, FAINT)
    d.line([0, BAR, W - 1, BAR], fill=(38, 42, 52))
    d.rectangle([0, BAR + 1, W - 1, H - 1], fill=PANEL)
    return img


def caret(d, x, y):
    d.rectangle([x, y + 3, x + 9, y + 21], fill=INK)


def typing(t, t0, t1):
    if t <= t0:
        return 0.0
    if t >= t1:
        return 1.0
    return (t - t0) / (t1 - t0)


def cmd_line(d, y, cmd, prog, t):
    put(d, PAD_X, y, "$", 17, CYAN, bold=True)
    n = int(round(len(cmd) * prog))
    put(d, PAD_X + 20, y, cmd[:n], 17, INK)
    if prog < 1.0 and (t * 2.4) % 1.0 < 0.6:
        caret(d, PAD_X + 22 + tw(d, cmd[:n], 17), y)


def render(t: float) -> Image.Image:
    img = chrome(Image.new("RGB", (W, H), BG))
    d = ImageDraw.Draw(img)
    y = BAR + PAD_Y

    # 1) 跑脚本
    cmd_line(d, y, "python migrate.py", typing(t, 0.15, 1.00), t)
    y += LINE
    if t < 1.00:
        return img
    put(d, PAD_X + 20, y, "正在迁移 users 表…", 17, DIM)
    y += LINE
    if t >= 1.35:
        put(d, PAD_X + 20, y, "迁移完成：处理 1000 行", 17, GREEN, bold=True)
        y += LINE
    if t < 1.60:
        return img

    # 2) 自己去数一行
    cmd_line(d, y, "wc -l out.csv", typing(t, 1.60, 2.40), t)
    y += LINE
    if t < 2.60:
        return img
    put(d, PAD_X + 20, y, "0 out.csv", 17, RED, bold=True)
    y += LINE
    if t < 2.90:
        return img

    # 3) 让它自己说
    t3 = 3.30
    cmd_line(d, y, "silent-audit run migrate.py", typing(t, t3 - 0.9, t3), t)
    y += LINE
    if t < t3:
        return img

    # 4) 判决面板
    q = 0.0 if t < t3 + 0.2 else min(1.0, (t - t3 - 0.2) / 0.45)
    a = q

    def fade(c):
        return tuple(int(v * a + PANEL[i] * (1 - a)) for i, v in enumerate(c))

    top = y + 6
    h = 268
    d.rectangle([PAD_X, top, W - PAD_X, top + h], fill=(22, 25, 33),
                outline=(46, 52, 66))
    iy = top + 20
    put(d, PAD_X + 20, iy, "不可信：它说的话和事实对不上", 18, fade(RED), bold=True)
    iy += 38

    put(d, PAD_X + 20, iy + 2, "可信度", 15, fade(FAINT))
    bx = PAD_X + 94
    for i in range(20):
        x = bx + i * 13
        on = i < 2
        d.rectangle([x, iy + 2, x + 9, iy + 21],
                    fill=fade(RED) if on else fade((40, 45, 57)))
    put(d, bx + 20 * 13 + 8, iy + 2, "1.0 / 10", 15, fade(RED), bold=True)
    iy += 38

    for label, val, col in (
        ("它声称", "处理 0 项", YELLOW),
        ("系统调用", "1 次写入 / 2 次读取", INK),
        ("物证", "1 个文件被写 · 其中 0 个真的变了", INK),
        ("退出码", "0", GREEN),
    ):
        put(d, PAD_X + 20, iy, label, 15, fade(FAINT))
        put(d, PAD_X + 118, iy, val, 15, fade(col))
        iy += 27

    iy += 10
    # ✗ 在 msyh.ttc 里是豆腐块，自己画
    cx0, cy0 = PAD_X + 28, iy + 8
    d.line([cx0 - 5, cy0 - 5, cx0 + 5, cy0 + 5], fill=fade(RED), width=2)
    d.line([cx0 - 5, cy0 + 5, cx0 + 5, cy0 - 5], fill=fade(RED), width=2)
    put(d, PAD_X + 44, iy, "退出码 0，报告成功，但磁盘上没有任何文件发生变化", 15, fade(RED))
    iy += 26
    put(d, PAD_X + 44, iy, "写入位置：migrate.py:6", 14, fade(FAINT))

    if q >= 1.0 and (t * 2.0) % 1.0 < 0.55:
        caret(d, PAD_X + 44 + tw(d, "写入位置：migrate.py:6", 14) + 10, iy)
    return img


# t 值 → 停留毫秒
KEYS = [
    (0.60, 240), (0.80, 200),
    (1.00, 260), (1.18, 160), (1.35, 820),
    (1.60, 180), (1.80, 90), (2.00, 90), (2.20, 90), (2.40, 140),
    (2.60, 300), (2.90, 880),
    (3.30, 180), (3.45, 90), (3.60, 90), (3.75, 90),
    (4.10, 140), (4.35, 120), (4.60, 120), (4.85, 120), (5.10, 120),
    (5.60, 900),
]


def build():
    frames = [(render(t), ms) for t, ms in KEYS]
    for i in range(10):
        frames.append((render(5.60 + i * 0.11), 118))
    return frames


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", metavar="T", help="只画某一时刻（如 5.10），导出 PNG")
    ap.add_argument("-o", "--out", default=str(OUT))
    a = ap.parse_args(argv)

    if a.preview is not None:
        outdir = ROOT / "out" / "gif"
        outdir.mkdir(parents=True, exist_ok=True)
        p = outdir / f"t{float(a.preview):.2f}.png"
        render(float(a.preview)).save(p)
        print(f"wrote {p}")
        return 0

    frames = build()
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # GIF 只有 256 色：用中间帧定一张全局调色板，避免每帧各自抖动
    pal = frames[len(frames) // 2][0].quantize(colors=96, method=Image.MEDIANCUT)
    imgs = [f.quantize(palette=pal, dither=Image.NONE) for f, _ in frames]
    imgs[0].save(out, save_all=True, append_images=imgs[1:],
                 duration=[ms for _, ms in frames], loop=0, optimize=False)
    print(f"wrote {out}  {out.stat().st_size / 1024:.0f} KB  {len(frames)} 帧  "
          f"{sum(ms for _, ms in frames) / 1000:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
