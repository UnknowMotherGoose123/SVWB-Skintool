"""生成程序图标 app_icon.ico（深色圆角方块 + 金紫渐变的双向交换箭头）。"""
import math
import os
import sys

from PIL import Image, ImageDraw, ImageFilter

S = 1024


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(len(a)))


def gradient(size, c1, c2, angle=135):
    w, h = size
    g = Image.new('RGBA', size)
    px = g.load()
    rad = math.radians(angle)
    dx, dy = math.cos(rad), math.sin(rad)
    span = abs(dx) * w + abs(dy) * h
    for y in range(h):
        for x in range(w):
            t = ((x * dx + y * dy) - min(0, dx * w) - min(0, dy * h)) / span
            px[x, y] = lerp(c1, c2, max(0.0, min(1.0, t)))
    return g


def rounded_mask(size, r):
    m = Image.new('L', size, 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], r, fill=255)
    return m


def arrow_layer(flip):
    """一条弧形箭头的蒙版。"""
    m = Image.new('L', (S, S), 0)
    d = ImageDraw.Draw(m)
    box = [S * 0.20, S * 0.20, S * 0.80, S * 0.80]
    width = int(S * 0.085)
    if not flip:
        d.arc(box, start=200, end=330, fill=255, width=width)
        tip = (S * 0.5 + S * 0.30 * math.cos(math.radians(335)), S * 0.5 + S * 0.30 * math.sin(math.radians(335)))
        ang = math.radians(335 + 90)
    else:
        d.arc(box, start=20, end=150, fill=255, width=width)
        tip = (S * 0.5 + S * 0.30 * math.cos(math.radians(155)), S * 0.5 + S * 0.30 * math.sin(math.radians(155)))
        ang = math.radians(155 + 90)
    L = S * 0.16
    side = math.radians(90)
    p1 = (tip[0] + L * math.cos(ang), tip[1] + L * math.sin(ang))
    p2 = (tip[0] + L * 0.62 * math.cos(ang - side) - L * 0.35 * math.cos(ang), tip[1] + L * 0.62 * math.sin(ang - side) - L * 0.35 * math.sin(ang))
    p3 = (tip[0] + L * 0.62 * math.cos(ang + side) - L * 0.35 * math.cos(ang), tip[1] + L * 0.62 * math.sin(ang + side) - L * 0.35 * math.sin(ang))
    d.polygon([p1, p2, p3], fill=255)
    return m


def main(out):
    small = 256
    bg = gradient((small, small), (24, 20, 48, 255), (10, 12, 26, 255), 120).resize((S, S), Image.BICUBIC)
    icon = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    icon.paste(bg, (0, 0), rounded_mask((S, S), int(S * 0.22)))
    # 内描边
    ring = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(ring).rounded_rectangle([S * 0.03, S * 0.03, S * 0.97, S * 0.97], int(S * 0.2),
                                           outline=(226, 180, 60, 120), width=int(S * 0.012))
    icon.alpha_composite(ring)
    gold = gradient((small, small), (255, 226, 140, 255), (214, 150, 40, 255), 90).resize((S, S), Image.BICUBIC)
    violet = gradient((small, small), (196, 150, 255, 255), (110, 90, 255, 255), 90).resize((S, S), Image.BICUBIC)
    for layer, mask in ((gold, arrow_layer(False)), (violet, arrow_layer(True))):
        glow = Image.new('RGBA', (S, S), (0, 0, 0, 0))
        glow.paste(layer, (0, 0), mask.filter(ImageFilter.GaussianBlur(S * 0.03)))
        glow.putalpha(glow.getchannel('A').point(lambda v: int(v * 0.55)))
        icon.alpha_composite(glow)
        part = Image.new('RGBA', (S, S), (0, 0, 0, 0))
        part.paste(layer, (0, 0), mask)
        icon.alpha_composite(part)
    # 中心菱形星
    star = Image.new('L', (S, S), 0)
    c, r = S / 2, S * 0.07
    ImageDraw.Draw(star).polygon([(c, c - r * 1.6), (c + r * 0.55, c), (c, c + r * 1.6), (c - r * 0.55, c)], fill=255)
    ImageDraw.Draw(star).polygon([(c - r * 1.6, c), (c, c - r * 0.55), (c + r * 1.6, c), (c, c + r * 0.55)], fill=255)
    white = Image.new('RGBA', (S, S), (255, 246, 220, 255))
    sl = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    sl.paste(white, (0, 0), star)
    icon.alpha_composite(sl)
    icon.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    icon.resize((256, 256), Image.LANCZOS).save(os.path.splitext(out)[0] + '.png')
    print('saved', out)


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'app_icon.ico')
