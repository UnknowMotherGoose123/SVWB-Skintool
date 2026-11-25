"""纹理相关的“放得下”处理：
  * repaint_bundle：把来源包的画面缩放后，按原格式/尺寸/mipmap 写进目标包自己的纹理（包结构完全不变）
  * shrink_bundle：按比例缩小包里的纹理（Spine 图集用归一化坐标，缩小纹理不影响动画定位）
  * embed_cab：把依赖包（如进化时间轴）的序列化文件嵌进主包，随主包一起加载
"""
import io
import lzma
import math
import os
import struct
import threading

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


def repaint_bundle(data_a: bytes, data_b: bytes, bits: int = 0) -> bytes:
    """来源画面写进目标包（目标包的名字、CAB、Sprite、尺寸、格式、mipmap 全不变）。失败抛异常。
    bits > 0 时对画面做色阶量化（用于放不进原文件的情况）。"""
    env_a = UnityPy.load(data_a)
    env_b = UnityPy.load(data_b)
    ta = sorted([o.read() for o in env_a.objects if o.type.name == 'Texture2D'], key=lambda t: t.m_Name)
    tb = sorted([o.read() for o in env_b.objects if o.type.name == 'Texture2D'], key=lambda t: t.m_Name)
    if not ta or not tb:
        raise ValueError('没有纹理')
    bufs = _resS_bufs(env_a)
    for t, s in _pair_textures(ta, tb):
        if s is None:
            continue
        img = s.image.convert('RGBA')
        pma = is_premultiplied(img)
        img = resize_rgba(img, (t.m_Width, t.m_Height), pma)
        if bits:
            img = posterize(img, bits, pma)
        mips = t.m_MipCount or 1
        new = _encode(img, t.m_TextureFormat, t.m_Width, t.m_Height, mips, pma)
        sd = t.m_StreamData
        if sd and sd.path and sd.size:
            node = _node_name(sd.path)
            if node not in bufs or len(new) != sd.size:
                raise ValueError(f'纹理数据长度不一致 {len(new)} != {sd.size}')
            bufs[node][sd.offset:sd.offset + sd.size] = new
        else:
            if len(new) != len(t.image_data or b''):
                raise ValueError('内嵌纹理长度不一致')
            t.image_data = new
            t.save()
    _put_resS(env_a, bufs)
    return env_a.file.save(packer=_flags(env_a))


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


def embed_cab(main: bytes, dep: bytes) -> bytes:
    """把依赖包里的序列化文件（及其 .resS）一起放进主包。CAB 名不变（调用方之后统一改名）。"""
    env = UnityPy.load(main)
    denv = UnityPy.load(dep)
    for name, f in denv.file.files.items():
        if name in env.file.files:
            continue
        if hasattr(f, 'objects'):
            # 去掉依赖包自己的 AssetBundle 对象，避免一个包里出现两个
            for pid, obj in list(f.objects.items()):
                if obj.type.name == 'AssetBundle':
                    del f.objects[pid]
            env.file.files[name] = _RawNode(f.save(), getattr(f, 'flags', 4))
        else:
            env.file.files[name] = _RawNode(f.bytes, getattr(f, 'flags', 0))
    return env.file.save(packer=_flags(env))
