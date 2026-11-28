"""纹理相关的“放得下”处理：
  * repaint_bundle：把来源包的画面缩放后按原格式写进目标包自己的纹理，Sprite 区域/网格换成来源的；
    放不下时降分辨率（Sprite 一起换算，显示大小不变）
  * shrink_bundle：按比例缩小包里的纹理（Spine 图集用归一化坐标，缩小纹理不影响动画定位）
  * merge_cabs：把依赖包（进化时间轴、时间轴里生成的特效）的对象并进主包的主文件，随主包一起加载
"""
import copy
import io
import lzma
import math
import os
import struct
import threading

import attrs
import UnityPy
from PIL import Image, ImageChops
from UnityPy.export.Texture2DConverter import image_to_texture2d

UnityPy.config.FALLBACK_UNITY_VERSION = "2022.3.0f1"   # 游戏包里的版本号被抹掉了


def _compress_lzma_fast(data, write_decompressed_size=False):
    """UnityPy 默认的 LZMA 参数（normal/bt4）很慢；换成 fast/hc4：快 2~3 倍，体积只大约 7%。
    参数写在流头里，Unity 解压不受影响。"""
    dict_size = 0x800000
    co = lzma.LZMACompressor(format=lzma.FORMAT_RAW, filters=[{
        'id': lzma.FILTER_LZMA1, 'dict_size': dict_size, 'lc': 3, 'lp': 0, 'pb': 2,
        'mode': lzma.MODE_FAST, 'mf': lzma.MF_HC4, 'nice_len': 32}])
    cd = co.compress(data) + co.flush()
    if write_decompressed_size:
        return struct.pack('<BIQ', 0x5D, dict_size, len(data)) + cd
    return struct.pack('<BI', 0x5D, dict_size) + cd


def estimate_lzma_size(raw: bytes, parts: int = 8) -> int:
    """估算整块 LZMA 压缩后的大小：切成几段多线程并行压（lzma 会释放 GIL），
    比整块压快好几倍；段与段之间没有上下文，结果会略偏大（偏保守），用来估算纹理缩小比例。"""
    from concurrent.futures import ThreadPoolExecutor
    n = max(1, min(parts, len(raw) // (1 << 20)))
    step = -(-len(raw) // n)
    chunks = [raw[i:i + step] for i in range(0, len(raw), step)]
    with ThreadPoolExecutor(max_workers=len(chunks)) as ex:
        sizes = list(ex.map(lambda c: len(_compress_lzma_fast(c)), chunks))
    return sum(sizes)


def _install_fast_lzma():
    try:
        from UnityPy.helpers import CompressionHelper as C
        from UnityPy.enums.BundleFile import CompressionFlags
        C.compress_lzma = _compress_lzma_fast
        C.COMPRESSION_MAP[CompressionFlags.LZMA] = _compress_lzma_fast
    except Exception:
        pass


_install_fast_lzma()

# ---------------------------------------------------------------- 多进程 BC7 编码
# etcpak 编码时不释放 GIL，多线程没用；大纹理切成横条交给子进程并行编码（结果与单进程逐字节一致）
BC7 = 25
_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            from concurrent.futures import ProcessPoolExecutor
            _pool = ProcessPoolExecutor(max_workers=max(1, min(8, (os.cpu_count() or 2) - 1)))
        return _pool


def shutdown_pool():
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.shutdown(wait=False, cancel_futures=True)
            _pool = None


def _bc7_parallel(img: Image.Image) -> bytes:
    from . import _bc7worker
    img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)      # 与 image_to_texture2d 一致：Unity 纹理自下而上
    w, h = img.size
    raw = img.tobytes('raw', 'RGBA')
    pool = _get_pool()
    parts = max(2, pool._max_workers * 2)
    step = max(4, (h // parts + 3) // 4 * 4)
    jobs = [pool.submit(_bc7worker.encode, raw[y * w * 4:min(h, y + step) * w * 4], w, min(step, h - y))
            for y in range(0, h, step)]
    return b''.join(j.result() for j in jobs)


def encode_image(img: Image.Image, fmt) -> bytes:
    if int(fmt) == BC7 and img.width % 4 == 0 and img.height % 4 == 0 and img.width * img.height >= 2048 * 2048:
        try:
            return _bc7_parallel(img.convert('RGBA'))
        except Exception:
            shutdown_pool()
    data, _ = image_to_texture2d(img, fmt)
    return data

TEXTURE_ONLY = {'AssetBundle', 'Texture2D', 'Sprite'}


class _RawNode:
    """BundleFile.files 里的原始节点（.resS 等）。save() 时原样写出。"""
    def __init__(self, data: bytes, flags: int):
        self.bytes = bytes(data)
        self.flags = flags

    def save(self):
        return self.bytes


def _flags(env):
    f = env.file
    return int(f.dataflags), int(getattr(f, '_block_info_flags', 3))


def types_of(data: bytes):
    env = UnityPy.load(data)
    return {o.type.name for o in env.objects}


def is_texture_only(data: bytes):
    try:
        t = types_of(data)
    except Exception:
        return False
    return 'Texture2D' in t and t <= TEXTURE_ONLY


def has_textures(data: bytes):
    try:
        return 'Texture2D' in types_of(data)
    except Exception:
        return False


def is_premultiplied(img: Image.Image) -> bool:
    """判断 RGBA 画面是不是预乘透明度（Spine 图集 pma:true 那种）：几乎所有像素都满足 RGB ≤ A。
    直通透明度的图里，半透明的亮色像素（RGB > A）会大量出现。"""
    r, g, b, a = img.split()
    hist_a = a.histogram()
    if hist_a[255] == img.width * img.height:
        return True                      # 完全不透明：两种处理结果一样
    semi = sum(hist_a[1:255])            # 半透明像素：直通图的抗锯齿边缘大量 RGB > A，预乘图几乎没有
    over = sum(ImageChops.subtract(ImageChops.lighter(ImageChops.lighter(r, g), b), a).histogram()[9:])
    return over <= max(64, semi * 0.01)


def clamp_premultiplied(img: Image.Image) -> Image.Image:
    """预乘图里 RGB 不能超过 A，否则按预乘混合会叠加出发白的亮点。"""
    r, g, b, a = img.split()
    return Image.merge('RGBA', (ImageChops.darker(r, a), ImageChops.darker(g, a), ImageChops.darker(b, a), a))


def resize_rgba(img: Image.Image, size, pma: bool) -> Image.Image:
    """缩放 RGBA 画面。
    Pillow 缩放 RGBA 时会自己先预乘再还原，对直通透明度的图是对的；
    但预乘图再预乘一次，边缘会被不透明像素的颜色“撑亮”（RGB > A），游戏里就是一圈白点。
    所以预乘图逐通道缩放（缩小用面积平均，不产生振铃），最后再把 RGB 限制在 A 以内。"""
    size = tuple(size)
    if img.size == size:
        return img
    if not pma:
        return img.resize(size, Image.LANCZOS)
    shrink = size[0] <= img.width and size[1] <= img.height
    flt = Image.BOX if shrink else Image.BILINEAR
    return clamp_premultiplied(Image.merge('RGBA', [band.resize(size, flt) for band in img.split()]))


def _encode(img: Image.Image, fmt, width, height, mips, pma=False):
    out = []
    for lv in range(max(1, mips)):
        w, h = max(1, width >> lv), max(1, height >> lv)
        im = resize_rgba(img, (w, h), pma)
        out.append(encode_image(im, fmt))
    return b''.join(out)


def _node_name(path):
    return path.split('/')[-1] if path else ''


def _resS_bufs(env):
    bufs = {}
    for name, f in env.file.files.items():
        if name.lower().endswith(('.ress', '.resource')):
            raw = f.bytes if hasattr(f, 'bytes') else f.save()
            bufs[name] = bytearray(raw)
    return bufs


def _put_resS(env, bufs):
    for name, buf in bufs.items():
        old = env.file.files[name]
        env.file.files[name] = _RawNode(buf, getattr(old, 'flags', 0))


def _pair_textures(ta, tb):
    """按名字（去掉数字）配对，配不上就按顺序。"""
    import re
    key = lambda t: re.sub(r'\d+', '#', t.m_Name)
    mb = {}
    for t in tb:
        mb.setdefault(key(t), []).append(t)
    pairs = []
    rest_b = list(tb)
    for t in ta:
        cand = mb.get(key(t))
        if cand:
            s = cand.pop(0)
            rest_b.remove(s)
            pairs.append((t, s))
        else:
            pairs.append((t, None))
    it = iter(rest_b)
    return [(a, b if b is not None else next(it, None)) for a, b in pairs]


def posterize(img: Image.Image, bits: int, pma: bool = False) -> Image.Image:
    """每通道只保留 bits 位（色阶量化）。压缩纹理块更规整，压缩后明显更小；5 位以上几乎看不出差别。"""
    from PIL import ImageOps
    r, g, b, a = img.split()
    rgb = ImageOps.posterize(Image.merge('RGB', (r, g, b)), bits)
    shift = 8 - bits
    a = a.point(lambda v: 0 if v < 16 else (255 if v > 240 else ((v >> shift) << shift)))
    out = Image.merge('RGBA', (*rgb.split(), a))
    return clamp_premultiplied(out) if pma else out


_VFMT_SIZE = {0: 4, 1: 2, 2: 1, 3: 1, 4: 2, 5: 2, 6: 1, 7: 1, 8: 2, 9: 2, 10: 4, 11: 4}


def _sprites_by_texture(env):
    """{纹理 path_id: [Sprite 对象]}（只看文件内引用）。"""
    out = {}
    for o in env.objects:
        if o.type.name != 'Sprite':
            continue
        tex = o.read_typetree()['m_RD']['texture']
        if tex['m_FileID'] == 0:
            out.setdefault(tex['m_PathID'], []).append(o)
    return out


def _scale_positions(vd, kx, ky):
    """按比例缩放 Sprite 网格顶点坐标（位置通道必须是 float32），返回新的顶点数据；看不懂布局返回 None。"""
    chans = vd['m_Channels']
    n = vd['m_VertexCount']
    raw = bytearray(vd['m_DataSize'])
    if not chans or chans[0]['dimension'] < 2 or chans[0]['format'] != 0:
        return None
    strides = {}
    for c in chans:
        if c['dimension']:
            size = _VFMT_SIZE.get(c['format'])
            if size is None:
                return None
            strides[c['stream']] = max(strides.get(c['stream'], 0), c['offset'] + size * c['dimension'])
    starts, pos = {}, 0
    for st in sorted(strides):
        pos = (pos + 15) // 16 * 16
        starts[st] = pos
        pos += strides[st] * n
    if pos > len(raw) or len(raw) - pos >= 16:
        return None
    p = chans[0]
    for i in range(n):
        off = starts[p['stream']] + i * strides[p['stream']] + p['offset']
        x, y = struct.unpack_from('<2f', raw, off)
        struct.pack_into('<2f', raw, off, x * kx, y * ky)
    return bytes(raw)


def _transplant_sprite(tt_a, tt_b, src_wh, new_wh, old_wh):
    """把来源 Sprite 的区域/网格（来源纹理尺寸 src_wh）换算到目标纹理的新尺寸 new_wh 上，
    写进目标 Sprite（名字、纹理引用、渲染键等身份信息不动；显示的单位尺寸保持目标原来的）。不支持返回 None。"""
    rd_a, rd_b = tt_a['m_RD'], tt_b['m_RD']
    if tt_a['m_SpriteAtlas']['m_PathID'] or tt_b['m_SpriteAtlas']['m_PathID'] or \
            rd_a['alphaTexture']['m_PathID'] or rd_b['alphaTexture']['m_PathID'] or \
            rd_a.get('secondaryTextures') or rd_b.get('secondaryTextures') or rd_b.get('m_Bindpose'):
        return None
    sx, sy = new_wh[0] / src_wh[0], new_wh[1] / src_wh[1]
    ppu = tt_a['m_PixelsToUnits'] * new_wh[0] / old_wh[0]
    kx, ky = tt_b['m_PixelsToUnits'] * sx / ppu, tt_b['m_PixelsToUnits'] * sy / ppu
    vd = copy.deepcopy(rd_b['m_VertexData'])
    if (kx, ky) != (1.0, 1.0):
        data = _scale_positions(vd, kx, ky)
        if data is None:
            return None
        vd['m_DataSize'] = data if isinstance(vd['m_DataSize'], (bytes, bytearray)) else list(data)

    def rect(r):
        return {**r, 'x': r['x'] * sx, 'y': r['y'] * sy, 'width': r['width'] * sx, 'height': r['height'] * sy}

    out = copy.deepcopy(tt_a)
    for k in ('m_Pivot', 'm_Extrude', 'm_IsPolygon'):
        if k in tt_b:
            out[k] = copy.deepcopy(tt_b[k])
    out['m_Rect'] = rect(tt_b['m_Rect'])
    out['m_Offset'] = {'x': tt_b['m_Offset']['x'] * sx, 'y': tt_b['m_Offset']['y'] * sy}
    b = tt_b['m_Border']
    out['m_Border'] = {'x': b['x'] * sx, 'y': b['y'] * sy, 'z': b['z'] * sx, 'w': b['w'] * sy}
    out['m_PixelsToUnits'] = ppu
    out['m_PhysicsShape'] = [[{'x': v['x'] * kx, 'y': v['y'] * ky} for v in shape] for shape in tt_b.get('m_PhysicsShape', [])]
    ra = out['m_RD']
    for k in ('m_SubMeshes', 'm_IndexBuffer', 'settingsRaw', 'atlasRectOffset', 'downscaleMultiplier'):
        if k in rd_b:
            ra[k] = copy.deepcopy(rd_b[k])
    ra['m_VertexData'] = vd
    ra['textureRect'] = rect(rd_b['textureRect'])
    ra['textureRectOffset'] = {'x': rd_b['textureRectOffset']['x'] * sx, 'y': rd_b['textureRectOffset']['y'] * sy}
    ut = rd_b['uvTransform']
    ra['uvTransform'] = {'x': ppu, 'y': ut['y'] * sx, 'z': ppu, 'w': ut['w'] * sy}
    return out


def _sprite_pairs(env_a, env_b, t, s):
    """目标纹理 t 和来源纹理 s 各自唯一的 Sprite；不是一对一返回 None。"""
    sa = _sprites_by_texture(env_a).get(t.object_reader.path_id, [])
    sb = _sprites_by_texture(env_b).get(s.object_reader.path_id, [])
    return (sa[0], sb[0]) if len(sa) == 1 and len(sb) == 1 else None


def can_resize(data_a: bytes, data_b: bytes) -> bool:
    """目标包里每张要重绘的纹理都只被一个 Sprite 使用、并且能换算 Sprite：可以降分辨率放进原文件。"""
    try:
        env_a, env_b = UnityPy.load(data_a), UnityPy.load(data_b)
        ta = sorted([o.read() for o in env_a.objects if o.type.name == 'Texture2D'], key=lambda t: t.m_Name)
        tb = sorted([o.read() for o in env_b.objects if o.type.name == 'Texture2D'], key=lambda t: t.m_Name)
        ok = False
        for t, s in _pair_textures(ta, tb):
            if s is None:
                continue
            pr = _sprite_pairs(env_a, env_b, t, s)
            if not pr or _transplant_sprite(pr[0].read_typetree(), pr[1].read_typetree(), (s.m_Width, s.m_Height),
                                            (t.m_Width // 2, t.m_Height // 2), (t.m_Width, t.m_Height)) is None:
                return False
            ok = True
        return ok
    except Exception:
        return False


def repaint_bundle(data_a: bytes, data_b: bytes, bits: int = 0, scale: float = 1.0) -> bytes:
    """来源画面写进目标包（目标包的名字、CAB、格式不变）。失败抛异常。
    纹理只被一个 Sprite 使用时，Sprite 的区域/网格换成来源的（换算到目标纹理上），来源人物不会被原皮肤的轮廓裁掉；
    scale < 1 时纹理按比例降分辨率（Sprite 一起换算，游戏里显示大小不变），用于放不进原文件的情况；
    bits > 0 时对画面做色阶量化（最后的手段，会出现色块）。"""
    env_a = UnityPy.load(data_a)
    env_b = UnityPy.load(data_b)
    ta = sorted([o.read() for o in env_a.objects if o.type.name == 'Texture2D'], key=lambda t: t.m_Name)
    tb = sorted([o.read() for o in env_b.objects if o.type.name == 'Texture2D'], key=lambda t: t.m_Name)
    if not ta or not tb:
        raise ValueError('没有纹理')
    bufs = _resS_bufs(env_a)
    resized = False
    for t, s in _pair_textures(ta, tb):
        if s is None:
            continue
        w, h = t.m_Width, t.m_Height
        pr = _sprite_pairs(env_a, env_b, t, s)
        if scale < 1:
            w = max(4, int(round(t.m_Width * scale / 4)) * 4)
            h = max(4, int(round(t.m_Height * scale / 4)) * 4)
        sp = None
        if pr:
            sp = _transplant_sprite(pr[0].read_typetree(), pr[1].read_typetree(), (s.m_Width, s.m_Height),
                                    (w, h), (t.m_Width, t.m_Height))
        if sp is None:
            w, h = t.m_Width, t.m_Height      # Sprite 换算不了：尺寸保持原样
        img = s.image.convert('RGBA')
        pma = is_premultiplied(img)
        img = resize_rgba(img, (w, h), pma)
        if bits:
            img = posterize(img, bits, pma)
        mips = t.m_MipCount or 1
        if mips > 1 and (w, h) != (t.m_Width, t.m_Height):
            mips = min(mips, int(math.log2(max(w, h))) + 1)     # Unity 的完整 mip 链按长边算
        new = _encode(img, t.m_TextureFormat, w, h, mips, pma)
        if sp is not None:
            pr[0].save_typetree(sp)
        sd = t.m_StreamData
        if (w, h) != (t.m_Width, t.m_Height):
            resized = True
            t.m_Width, t.m_Height = w, h
            if t.m_MipCount is not None:
                t.m_MipCount = mips
            t.m_CompleteImageSize = len(new)
            if sd and sd.path and sd.size:
                node = _node_name(sd.path)
                if node not in bufs:
                    raise ValueError(f'找不到纹理数据 {sd.path}')
                bufs[node] += b'\x00' * (-len(bufs[node]) % 16)
                sd.offset, sd.size = len(bufs[node]), len(new)
                bufs[node] += new
            else:
                t.image_data = new
            t.save()
        elif sd and sd.path and sd.size:
            node = _node_name(sd.path)
            if node not in bufs or len(new) != sd.size:
                raise ValueError(f'纹理数据长度不一致 {len(new)} != {sd.size}')
            bufs[node][sd.offset:sd.offset + sd.size] = new
        else:
            if len(new) != len(t.image_data or b''):
                raise ValueError('内嵌纹理长度不一致')
            t.image_data = new
            t.save()
    if resized:
        bufs = _compact_resS(env_a, bufs, ta)
    _put_resS(env_a, bufs)
    return env_a.file.save(packer=_flags(env_a))


def _compact_resS(env, bufs, texs):
    """去掉 .resS 里没人引用的旧数据（降分辨率后旧纹理数据留在原处），所有引用按新位置更新。
    texs：包里全部纹理（已改过的对象；UnityPy 保存后再读出来还是旧内容，所以不能重新读）。"""
    new_bufs = {name: bytearray() for name in bufs}
    for t in texs:
        sd = t.m_StreamData
        if sd and sd.path and sd.size:
            node = _node_name(sd.path)
            buf = new_bufs[node]
            buf += b'\x00' * (-len(buf) % 16)
            blob = bytes(bufs[node][sd.offset:sd.offset + sd.size])
            sd.offset = len(buf)
            buf += blob
            t.save()
    for o in env.objects:
        if o.type.name in ('AssetBundle', 'Texture2D', 'Sprite'):
            continue
        try:
            tt = o.read_typetree()
        except Exception:
            continue
        sd = tt.get('m_StreamData') if isinstance(tt, dict) else None
        if not isinstance(sd, dict) or not sd.get('size'):
            continue
        node = _node_name(sd.get('path', ''))
        if node not in bufs:
            raise ValueError(f'{o.type.name} 引用了未知的外部数据 {node}')
        buf = new_bufs[node]
        buf += b'\x00' * (-len(buf) % 16)
        blob = bytes(bufs[node][sd['offset']:sd['offset'] + sd['size']])
        sd['offset'] = len(buf)
        buf += blob
        o.save_typetree(tt)
    return new_bufs


def shrink_bundle(data: bytes, scale: float) -> bytes:
    """把包里所有纹理按 scale 缩小（宽高同比，按 4 对齐），重建 .resS。"""
    env = UnityPy.load(data)
    bufs = _resS_bufs(env)
    new_bufs = {name: bytearray() for name in bufs}

    def append(node, blob):
        buf = new_bufs.setdefault(node, bytearray())
        off = len(buf)
        buf += blob
        while len(buf) % 16:
            buf += b'\x00'
        return off

    # 其它对象（Mesh 等）放在 .resS 里的数据原样搬到新 .resS，只更新偏移
    for o in env.objects:
        if o.type.name in ('Texture2D', 'AssetBundle'):
            continue
        try:
            tt = o.read_typetree()
        except Exception:
            continue
        sd = tt.get('m_StreamData') if isinstance(tt, dict) else None
        if not sd or not sd.get('size'):
            continue
        node = _node_name(sd.get('path', ''))
        if node not in bufs:
            raise ValueError(f'{o.type.name} 引用了未知的外部数据 {node}')
        blob = bytes(bufs[node][sd['offset']:sd['offset'] + sd['size']])
        sd['offset'] = append(node, blob)
        o.save_typetree(tt)

    texs = [o for o in env.objects if o.type.name == 'Texture2D']
    for o in texs:
        t = o.read()
        sd = t.m_StreamData
        if max(t.m_Width, t.m_Height) < 1024:
            # 小纹理（粒子特效等）原样保留，只搬数据
            if sd and sd.path and sd.size:
                node = _node_name(sd.path)
                blob = bytes(bufs[node][sd.offset:sd.offset + sd.size])
                sd.offset = append(node, blob)
                t.save()
            continue
        img = t.image.convert('RGBA')
        w = max(4, int(round(t.m_Width * scale / 4)) * 4)
        h = max(4, int(round(t.m_Height * scale / 4)) * 4)
        mips = t.m_MipCount or 1
        if mips > 1:
            mips = min(mips, int(math.log2(max(1, min(w, h)))) + 1)
        pma = is_premultiplied(img)
        img = resize_rgba(img, (w, h), pma)
        new = _encode(img, t.m_TextureFormat, w, h, mips, pma)
        t.m_Width, t.m_Height = w, h
        if t.m_MipCount is not None:
            t.m_MipCount = mips
        t.m_CompleteImageSize = len(new)
        if sd and sd.path and sd.size:
            node = _node_name(sd.path)
            sd.offset = append(node, new)
            sd.size = len(new)
        else:
            t.image_data = new
        t.save()
    _put_resS(env, {k: v for k, v in new_bufs.items() if k in bufs})
    return env.file.save(packer=_flags(env))


# ---------------------------------------------------------------- 合并依赖包
# 游戏只按索引里的依赖关系加载包；原皮肤没有位置放的来源依赖包（进化时间轴、时间轴里生成的特效等）
# 只能放进会被加载的包里。包里的所有文件都挂在“包名”目录下（archive:/<主 CAB>/<文件>），
# 单独塞一个序列化文件进去，别的文件按 archive:/<它的 CAB>/<它的 CAB> 是找不到的——
# 所以把它的对象直接并进主文件，引用全部改成文件内引用，纹理数据也写进对象本身。

# 不会引用其它资源的大对象：扫描引用时跳过（省时间）
_LEAF_TYPES = {'Texture2D', 'Texture2DArray', 'Cubemap', 'TextAsset', 'Mesh', 'AudioClip', 'VideoClip', 'Shader', 'Font'}


def _main_file(bundle):
    """包里的主序列化文件（名字不带扩展名的那个）。"""
    for name, f in bundle.files.items():
        if hasattr(f, 'objects') and '.' not in name:
            return f
    raise ValueError('包里没有序列化文件')


def _cab_of(path):
    return str(path).rsplit('/', 1)[-1]


def _remap_pptrs(node, fmap):
    """把 typetree 里所有 PPtr 的 m_FileID 按 fmap（旧 → 新）改掉，返回是否有改动。"""
    changed = False
    if isinstance(node, dict):
        if 'm_FileID' in node and 'm_PathID' in node:
            new = fmap.get(node['m_FileID'], node['m_FileID'])
            if new != node['m_FileID']:
                node['m_FileID'] = new
                return True
            return False
        for v in node.values():
            if isinstance(v, (dict, list, tuple)):
                changed |= _remap_pptrs(v, fmap)
    elif isinstance(node, (list, tuple)):
        for v in node:
            if isinstance(v, (dict, list, tuple)):
                changed |= _remap_pptrs(v, fmap)
    return changed


def _ext_index(sf, ext):
    """sf 里指向 ext.path 的外部引用序号（从 1 开始），没有就加一条。"""
    for i, e in enumerate(sf.externals):
        if e.path == ext.path:
            return i + 1
    sf.externals.append(copy.copy(ext))
    return len(sf.externals)


def _type_index(dst, src, st, fmap):
    """把来源文件的类型 st 对应到目标文件的类型表（相同的直接复用，没有就加一条），返回序号。"""
    for i, t in enumerate(dst.types):
        if t.class_id == st.class_id and t.old_type_hash == st.old_type_hash and \
                (st.class_id != 114 or t.script_id == st.script_id):
            return i
    nt = copy.copy(st)
    if st.class_id == 114 and st.script_type_index is not None and st.script_type_index >= 0:
        ls = src.script_types[st.script_type_index]
        fi = fmap.get(ls.local_serialized_file_index, ls.local_serialized_file_index) \
            if ls.local_serialized_file_index else 0
        for j, s2 in enumerate(dst.script_types):
            if s2.local_serialized_file_index == fi and s2.local_identifier_in_file == ls.local_identifier_in_file:
                nt.script_type_index = j
                break
        else:
            ns = copy.copy(ls)
            ns.local_serialized_file_index = fi
            dst.script_types.append(ns)
            nt.script_type_index = len(dst.script_types) - 1
    dst.types.append(nt)
    return len(dst.types) - 1


def merge_cabs(main: bytes, deps) -> bytes:
    """把依赖包 deps 的对象并进 main 包的主文件（去掉它们自己的 AssetBundle 对象）。
    main 和 deps 之间、deps 彼此之间的引用都变成文件内引用；其它外部引用（着色器等）按路径对上。
    依赖包放在 .resS 里的纹理数据直接写进纹理对象。调用前各包应已改好 CAB 名。"""
    env = UnityPy.load(main)
    m = _main_file(env.file)
    srcs = []
    for dep in deps:
        denv = UnityPy.load(dep)
        s = _main_file(denv.file)
        bufs = {name: (f.bytes if hasattr(f, 'bytes') else f.save())
                for name, f in denv.file.files.items() if not hasattr(f, 'objects')}
        srcs.append((s, bufs))
    merged = {m.name} | {s.name for s, _ in srcs}
    ab = next(o for o in m.objects.values() if o.type.name == 'AssetBundle')

    # 1. 主文件里原来指向这些依赖包的引用 → 文件内（AssetBundle 对象最后单独处理）
    mmap = {i + 1: 0 for i, e in enumerate(m.externals) if _cab_of(e.path) in merged}
    if mmap:
        for obj in list(m.objects.values()):
            if obj.type.name in _LEAF_TYPES or obj is ab:
                continue
            tt = obj.read_typetree()
            if _remap_pptrs(tt, mmap):
                obj.save_typetree(tt)
        for ls in m.script_types:
            if ls.local_serialized_file_index in mmap:
                ls.local_serialized_file_index = 0

    # 2. 依赖包的对象搬进主文件
    extra_preload = []
    for s, bufs in srcs:
        fmap = {i + 1: (0 if _cab_of(e.path) in merged else _ext_index(m, e)) for i, e in enumerate(s.externals)}
        tmap = {}
        for pid, obj in s.objects.items():
            if obj.type.name == 'AssetBundle':
                sab = obj.read_typetree()
                _remap_pptrs(sab['m_PreloadTable'], fmap)
                extra_preload += sab['m_PreloadTable']
                continue
            if pid in m.objects:
                if m.objects[pid].class_id == obj.class_id:
                    continue        # 同一个资源（Unity 的对象编号由资源本身决定，例如同一个脚本）
                raise ValueError(f'对象编号冲突：{pid}')
            tt = obj.read_typetree()
            changed = _remap_pptrs(tt, fmap)
            if isinstance(tt, dict):
                for key in ('m_StreamData', 'm_Resource'):
                    sd = tt.get(key)
                    if not isinstance(sd, dict):
                        continue
                    path = sd.get('path') or sd.get('m_Source') or ''
                    size = sd.get('size', sd.get('m_Size', 0))
                    if not path or not size:
                        continue
                    if obj.type.name != 'Texture2D' or key != 'm_StreamData':
                        raise ValueError(f'{obj.type.name} 的外部数据无法合并')
                    blob = bufs.get(_cab_of(path))
                    if blob is None:
                        raise ValueError(f'找不到外部数据 {path}')
                    tt['image data'] = bytes(blob[sd['offset']:sd['offset'] + size])
                    tt['m_StreamData'] = {'offset': 0, 'size': 0, 'path': ''}
                    changed = True
            data = obj.save_typetree(tt) if changed else obj.get_raw_data()
            ti = s.types.index(obj.serialized_type)
            if ti not in tmap:
                tmap[ti] = _type_index(m, s, obj.serialized_type, fmap)
            nt = tmap[ti]
            m.objects[pid] = attrs.evolve(obj, assets_file=m, type_id=nt, serialized_type=m.types[nt], data=data)
            extra_preload.append({'m_FileID': 0, 'm_PathID': pid})

    # 3. 主包的预加载表：游戏加载模型时只读表里的对象。原版包里每个对象都在表里，
    #    并进来的时间轴、特效（连同它们引用的着色器等）也要加进去，覆盖整张表的条目跟着变长
    t = ab.read_typetree()
    _remap_pptrs(t, mmap)
    pre = t['m_PreloadTable']
    old_len = len(pre)
    seen = {(p['m_FileID'], p['m_PathID']) for p in pre}
    for p in extra_preload:
        k = (p['m_FileID'], p['m_PathID'])
        if k not in seen and k[1]:
            seen.add(k)
            pre.append(dict(p))
    for _, info in t['m_Container']:
        if info['preloadIndex'] + info['preloadSize'] == old_len:
            info['preloadSize'] += len(pre) - old_len
    gone = {c.lower() for c in merged if c != m.name}
    t['m_Dependencies'] = [d for d in t.get('m_Dependencies', []) if d.lower() not in gone]
    ab.save_typetree(t)
    m.mark_changed()
    return env.file.save(packer=_flags(env))
