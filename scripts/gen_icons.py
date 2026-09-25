#!/usr/bin/env python3
"""生成 PWA 图标(server/static/icons/): icon-192 / icon-512 / apple-touch-icon-180。
   风格对齐 iOS 26: 大圆角 + 蓝紫对角渐变底 + 白色媒体(播放+胶片)符号 + 柔和高光 + 玻璃内描边。
   用隔离环境 Pillow 运行:
     python3 scripts/gen_icons.py
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "server" / "static" / "icons"
OUT.mkdir(parents=True, exist_ok=True)

RADIUS = 0.2237   # iOS squircle 近似圆角比例


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def build_gradient(S):
    """在 SxS 画布上画蓝紫对角渐变(逐像素, S 取小值保证快)"""
    c1 = (10, 132, 255)    # iOS blue
    c2 = (142, 60, 222)    # iOS purple
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    px = img.load()
    for y in range(S):
        for x in range(S):
            t = (x + y) / (2 * S)
            px[x, y] = (*lerp(c1, c2, t), 255)
    return img


def build(size):
    # 渐变小画布(64) → 放大到 size(抗锯齿), 再叠加符号/高光
    g = build_gradient(64).resize((size, size), Image.LANCZOS)
    img = g.convert("RGBA")
    d = ImageDraw.Draw(img)
    rad = int(size * RADIUS)
    # 顶部柔和高光(液态玻璃)
    hi = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hi)
    hd.rounded_rectangle([0, 0, size, int(size * 0.6)], radius=rad, fill=(255, 255, 255, 52))
    hi = hi.filter(ImageFilter.GaussianBlur(size * 0.035))
    # 只保留圆角内的高光
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=rad, fill=255)
    hi.putalpha(Image.composite(hi.getchannel("A"), Image.new("L", hi.size, 0), mask))
    img = Image.alpha_composite(img, hi)
    d = ImageDraw.Draw(img)
    # 玻璃内描边(亮边)
    d.rounded_rectangle([size * 0.014, size * 0.014, size * 0.986, size * 0.986],
                        radius=int(rad * 0.96), outline=(255, 255, 255, 110),
                        width=max(2, size // 130))
    # 媒体符号: 白色播放三角 + 右侧两条胶片竖线
    cx, cy = size * 0.44, size * 0.5
    bw = size * 0.34
    bx0, by0 = cx - bw * 0.5, cy - bw * 0.5
    tri = [(bx0, by0), (bx0, cy + bw * 0.5), (bx0 + bw * 0.86, cy)]
    d.polygon(tri, fill=(255, 255, 255, 250))
    for i, ox in enumerate((0.56, 0.68)):
        lx = cx + bw * ox
        lw = size * 0.05
        d.rounded_rectangle([lx, cy - bw * 0.36, lx + lw, cy + bw * 0.36],
                            radius=lw // 2, fill=(255, 255, 255, 210 - i * 50))
    return img


def finalize(img, size, opaque=False):
    """套 iOS 圆角透明蒙版; apple-touch-icon 需不透明背景(系统自裁圆角, 透明会被填黑)"""
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * RADIUS), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    if opaque:
        bg = Image.new("RGBA", (size, size), (238, 242, 249, 255))
        bg.paste(img, (0, 0), mask)
        return bg
    out.paste(img, (0, 0), mask)
    return out


for name, sz, opaque in (("icon-512", 512, False), ("icon-192", 192, False), ("apple-touch-icon", 180, True)):
    final = finalize(build(sz), sz, opaque)
    final.save(OUT / f"{name}.png")
    print(f"  生成 {name}.png ({sz}x{sz})")

print(f"✓ 图标已写入 {OUT}")
