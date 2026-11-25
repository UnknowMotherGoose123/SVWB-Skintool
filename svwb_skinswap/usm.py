"""CRI USM 视频（Sofdec2）：解析、改写，并精确封装到指定字节数。

游戏不允许改资源索引（meta），所以替换进去的视频必须和原文件一样大：
  * 来源更小：把差额放进各个块自带的 padding 字段（USM 本来就靠 padding 对齐，播放器读到会跳过）；
  * 来源更大：把来源画面逐帧重新编码成 VP9（游戏里绝大多数视频本来就是 VP9），
    码率按目标大小计算，编码后再用 padding 补齐到正好的大小。
画面内容、帧数、帧率、分辨率都和来源一致，只是换了编码/码率。
"""
import io
import struct

SIGS = {b'CRID', b'@SFV', b'@SFA', b'@ALP', b'@CUE', b'@SBT', b'@USR'}
ALIGN = 64            # 新版 CRI 编码器的块对齐
MAX_PAD = 0xFFFF      # 块头里的 padding 字段是 16 位


class UsmError(Exception):
    pass


class Cancelled(Exception):
    """后台预计算被取消（方案变了，或用户开始替换/还原）。"""


def _check(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled()


# ----------------------------------------------------------------------------- @UTF 表
_FMT = {0: '>B', 1: '>b', 2: '>H', 3: '>h', 4: '>I', 5: '>i', 6: '>Q', 7: '>q', 8: '>f', 9: '>d'}


class UtfTable:
    """CRI @UTF 表。列：(名字, 标志, 常量值)；标志高 4 位 0x10 空 / 0x30 常量 / 0x50 逐行，低 4 位类型。"""

    def __init__(self, raw: bytes):
        if raw[:4] != b'@UTF':
            raise UsmError('不是 @UTF 表')
        size = struct.unpack('>I', raw[4:8])[0]
        t = raw[8:8 + size]
        self.unk = t[0:2]
        rows_off = struct.unpack('>H', t[2:4])[0]
        str_off, data_off, name_off = struct.unpack('>III', t[4:16])
        ncol, rowlen = struct.unpack('>HH', t[16:20])
        nrow = struct.unpack('>I', t[20:24])[0]
        if data_off != size:
            raise UsmError('暂不支持带数据区的 @UTF 表')

        def s(o):
            e = t.index(b'\0', str_off + o)
            return t[str_off + o:e]

        def val(typ, p):
            if typ in _FMT:
                n = struct.calcsize(_FMT[typ])
                return struct.unpack(_FMT[typ], t[p:p + n])[0], n
            if typ == 0xA:
                return s(struct.unpack('>I', t[p:p + 4])[0]), 4
            raise UsmError(f'不支持的 @UTF 列类型 {typ}')

        self.name = s(name_off)
        self.cols = []
        p = 24
        for _ in range(ncol):
            flag = t[p]
            cname = s(struct.unpack('>I', t[p + 1:p + 5])[0])
            p += 5
            const = None
            if flag & 0xF0 == 0x30:
                const, n = val(flag & 0x0F, p)
                p += n
            self.cols.append([cname, flag, const])
        self.rows = []
        for r in range(nrow):
            q = rows_off + r * rowlen
            row = {}
            for cname, flag, const in self.cols:
                if flag & 0xF0 == 0x50:
                    row[cname], n = val(flag & 0x0F, q)
                    q += n
            self.rows.append(row)

    def get(self, key, row=0):
        key = key.encode() if isinstance(key, str) else key
        for cname, flag, const in self.cols:
            if cname == key:
                return self.rows[row][key] if flag & 0xF0 == 0x50 else const
        raise KeyError(key)

    def set(self, key, value, row=0):
        key = key.encode() if isinstance(key, str) else key
        for col in self.cols:
            if col[0] == key:
                if col[1] & 0xF0 == 0x50:
                    self.rows[row][key] = value
                else:
                    col[2] = value
                return
        raise KeyError(key)

    def build(self) -> bytes:
        pool = bytearray(b'<NULL>\0')
        index = {b'<NULL>': 0}

        def sref(b):
            if b not in index:
                index[b] = len(pool)
                pool.extend(b + b'\0')
            return index[b]

        name_ref = sref(self.name)
        for c in self.cols:
            sref(c[0])

        def enc(typ, v):
            if typ in _FMT:
                return struct.pack(_FMT[typ], v)
            return struct.pack('>I', sref(v))

        coldefs = bytearray()
        for cname, flag, const in self.cols:
            coldefs += bytes([flag]) + struct.pack('>I', index[cname])
            if flag & 0xF0 == 0x30:
                coldefs += enc(flag & 0x0F, const)
        rowdata = bytearray()
        rowlen = 0
        for i, row in enumerate(self.rows):
            one = bytearray()
            for cname, flag, const in self.cols:
                if flag & 0xF0 == 0x50:
                    one += enc(flag & 0x0F, row[cname])
            rowlen = len(one)
            rowdata += one
        rows_off = 24 + len(coldefs)
        str_off = rows_off + len(rowdata)
        size = str_off + len(pool)
        size += (-size) % 8
        t = (self.unk + struct.pack('>H', rows_off) + struct.pack('>III', str_off, size, name_ref)
             + struct.pack('>HHI', len(self.cols), rowlen, len(self.rows)) + coldefs + rowdata + pool)
        t += b'\0' * (size - len(t))
        return b'@UTF' + struct.pack('>I', size) + t


# ----------------------------------------------------------------------------- 块
class Chunk:
    """sig + size(4) + 块头 hdr（含 payload 偏移、padding、通道、类型、帧时间等）+ body + padding。"""
    __slots__ = ('sig', 'hdr', 'body', 'pad')

    def __init__(self, sig, hdr, body, pad):
        self.sig, self.hdr, self.body, self.pad = sig, bytearray(hdr), bytes(body), pad

    @property
    def chno(self):
        return self.hdr[4]

    @property
    def dtype(self):
        return self.hdr[7]

    def total(self):
        return 8 + len(self.hdr) + len(self.body) + self.pad

    def natural_pad(self):
        return (-(8 + len(self.hdr) + len(self.body))) % ALIGN

    def to_bytes(self):
        if not 0 <= self.pad <= MAX_PAD:
            raise UsmError('padding 超出范围')
        hdr = bytearray(self.hdr)
        hdr[2:4] = struct.pack('>H', self.pad)
        size = len(hdr) + len(self.body) + self.pad
        return self.sig + struct.pack('>I', size) + bytes(hdr) + self.body + b'\0' * self.pad


def parse(data: bytes):
    chunks = []
    pos = 0
    while pos < len(data):
        sig = data[pos:pos + 4]
        if sig not in SIGS:
            if not data[pos:].strip(b'\0'):
                break                                   # 末尾补零
            raise UsmError(f'未知的块 {sig!r} @ {pos}')
        size = struct.unpack('>I', data[pos + 4:pos + 8])[0]
        off = data[pos + 9]
        pad = struct.unpack('>H', data[pos + 10:pos + 12])[0]
        if off < 8 or 8 + size > len(data) - pos or off + pad > size:
            raise UsmError('块结构不对')
        hdr = data[pos + 8:pos + 8 + off]
        body = data[pos + 8 + off:pos + 8 + size - pad]
        chunks.append(Chunk(sig, hdr, body, pad))
        pos += 8 + size
    if not chunks or chunks[0].sig != b'CRID':
        raise UsmError('不是 USM 文件')
    return chunks


def _section(chunks, sig, dtype, name=None):
    for c in chunks:
        if c.sig == sig and c.dtype == dtype and c.body[:4] == b'@UTF':
            if name is None or UtfTable(c.body).name == name:
                return c
    return None


def _layout(chunks):
    """每个块的文件偏移。"""
    out, pos = [], 0
    for c in chunks:
        out.append(pos)
        pos += c.total()
    return out, pos


def _refresh_tables(chunks, extra_buf=0):
    """块布局变化后，更新 CRID 的文件大小、seek 表的字节偏移、视频头的块大小上限。"""
    crid = UtfTable(chunks[0].body)
    vids = [c for c in chunks if c.sig == b'@SFV' and c.dtype == 0]
    offs, total = _layout(chunks)
    # seek 表：ofs_frmid 第几帧 → 那一帧所在块的偏移（每块一帧）
    seek_c = _section(chunks, b'@SFV', 3, b'VIDEO_SEEKINFO')
    if seek_c is not None:
        seek = UtfTable(seek_c.body)
        vid_offs = [offs[i] for i, c in enumerate(chunks) if c.sig == b'@SFV' and c.dtype == 0]
        for r in range(len(seek.rows)):
            fid = seek.get('ofs_frmid', r)
            if 0 <= fid < len(vid_offs):
                seek.set('ofs_byte', vid_offs[fid], r)
        body = seek.build()
        if len(body) != len(seek_c.body):
            raise UsmError('seek 表大小变化')
        seek_c.body = body
    hdr_c = _section(chunks, b'@SFV', 1, b'VIDEO_HDRINFO')
    if hdr_c is not None and vids:
        h = UtfTable(hdr_c.body)
        need = max(c.total() for c in vids)
        try:
            if h.get('ixsize') < need:
                h.set('ixsize', need)
                hdr_c.body = h.build()
        except KeyError:
            pass
    crid.set('filesize', total, 0)
    if extra_buf:
        for r in range(len(crid.rows)):
            try:
                crid.set('minbuf', crid.get('minbuf', r) + extra_buf, r)
            except KeyError:
                pass
    body = crid.build()
    if len(body) > len(chunks[0].body) + chunks[0].pad:
        raise UsmError('CRID 表变大了')
    room = len(chunks[0].body) + chunks[0].pad
    chunks[0].body = body
    chunks[0].pad = room - len(body)
    return total


def pad_to(chunks, size):
    """把多出来的字节分散进各数据块的 padding（每块 64 字节对齐），零头放进最后一个块。放不下返回 None。"""
    _, total = _layout(chunks)
    extra = size - total
    if extra < 0:
        return None
    if extra == 0:
        return b''.join(c.to_bytes() for c in chunks)
    data_idx = [i for i, c in enumerate(chunks) if c.dtype == 0]
    last = chunks[-1]
    tail = extra % ALIGN
    units = extra // ALIGN
    if last.pad + tail > MAX_PAD:
        return None
    per = units // max(1, len(data_idx))
    rest = units - per * len(data_idx)
    grow = 0
    for k, i in enumerate(data_idx):
        add = (per + (1 if k >= len(data_idx) - rest else 0)) * ALIGN
        if chunks[i].pad + add > MAX_PAD:
            return None
        chunks[i].pad += add
        grow = max(grow, add)
    last.pad += tail
    total = _refresh_tables(chunks, extra_buf=2 * grow)
    if total != size:
        return None
    out = b''.join(c.to_bytes() for c in chunks)
    return out if len(out) == size else None


# ----------------------------------------------------------------------------- 重新编码
_DECODE_FMT = {1: 'mpegvideo', 2: 'mpegvideo', 5: 'h264', 9: 'ivf'}

# VP9 视频头（列名/类型照游戏里的 VP9 视频）
_VP9_HDR_COLS = [
    ('width', 0x54), ('height', 0x54), ('mat_width', 0x54), ('mat_height', 0x54),
    ('disp_width', 0x54), ('disp_height', 0x54), ('scrn_width', 0x54), ('mpeg_dcprec', 0x50),
    ('mpeg_codec', 0x50), ('mpeg_profile', 0x50), ('mpeg_level', 0x54), ('alpha_type', 0x54),
    ('total_frames', 0x54), ('framerate_n', 0x54), ('framerate_d', 0x54), ('metadata_count', 0x54),
    ('metadata_size', 0x54), ('ixsize', 0x54), ('pre_padding', 0x54), ('max_picture_size', 0x54),
    ('color_space', 0x54), ('picture_type', 0x54),
]


def _vp9_hdr_table(src: UtfTable, frames, ixsize, meta_size):
    t = UtfTable.__new__(UtfTable)
    t.unk, t.name = src.unk, b'VIDEO_HDRINFO'
    t.cols = [[n.encode(), f, None] for n, f in _VP9_HDR_COLS]
    row = {}
    for n, f in _VP9_HDR_COLS:
        try:
            row[n.encode()] = src.get(n.encode())
        except KeyError:
            row[n.encode()] = 0
    row.update({b'mpeg_dcprec': 0, b'mpeg_codec': 9, b'mpeg_profile': 0, b'mpeg_level': 0,
                b'total_frames': frames, b'ixsize': ixsize, b'metadata_size': meta_size,
                b'max_picture_size': 0})
    t.rows = [row]
    return t


def _decode_frames(chunks, info):
    import av
    codec = info.get(b'mpeg_codec')
    fmt = _DECODE_FMT.get(codec)
    if fmt is None:
        raise UsmError(f'不认识的视频编码 {codec}')
    es = b''.join(c.body for c in chunks if c.sig == b'@SFV' and c.dtype == 0 and c.chno == 0)
    ctn = av.open(io.BytesIO(es), format=fmt)
    try:
        for f in ctn.decode(ctn.streams.video[0]):
            yield f
    finally:
        ctn.close()


VP9_SPEED = '4'      # libvpx cpu-used：越大越快；4 比 2 快约 1.5 倍，高码率下画质几乎一样


def _encode_vp9(chunks, info, bitrate, keyint, opts=None, cancel=None):
    import av
    from av.video.frame import PictureType
    from fractions import Fraction
    w, h = info[b'width'], info[b'height']
    fn, fd = info[b'framerate_n'], info[b'framerate_d']
    cc = av.CodecContext.create('libvpx-vp9', 'w')
    cc.width, cc.height, cc.pix_fmt = w, h, 'yuv420p'
    cc.time_base = Fraction(fd, fn)
    cc.framerate = Fraction(fn, fd)
    cc.bit_rate = int(bitrate)
    cc.gop_size = keyint
    cc.thread_count = 0
    cc.options = {'deadline': 'good', 'cpu-used': VP9_SPEED, 'row-mt': '1', 'auto-alt-ref': '0',
                  'lag-in-frames': '16', 'undershoot-pct': '0', 'overshoot-pct': '0',
                  'tile-columns': '2', 'frame-parallel': '0', **(opts or {})}
    pkts = []
    n = 0
    for f in _decode_frames(chunks, info):
        _check(cancel)
        if (f.width, f.height) != (w, h) or f.format.name != 'yuv420p':
            f = f.reformat(width=w, height=h, format='yuv420p')
        f.pts = n
        f.time_base = cc.time_base
        f.pict_type = PictureType.NONE     # 不然来源的 I 帧会被强制编成关键帧，白白浪费码率
        n += 1
        pkts += [(bytes(p), p.is_keyframe) for p in cc.encode(f)]
    pkts += [(bytes(p), p.is_keyframe) for p in cc.encode(None)]
    if len(pkts) != n:
        raise UsmError(f'VP9 编码帧数不一致 {len(pkts)} != {n}')
    return pkts


def _build_vp9(chunks, info, pkts):
    """用来源的控制块 + 新的 VP9 帧组装 USM。"""
    w, h = info[b'width'], info[b'height']
    fn, fd = info[b'framerate_n'], info[b'framerate_d']
    n = len(pkts)
    ivf_head = (b'DKIF' + struct.pack('<HH', 0, 32) + b'VP90' + struct.pack('<HHIII', w, h, fn, fd, n) + b'\0' * 4)
    bodies = []
    for i, (p, _) in enumerate(pkts):
        rec = struct.pack('<IQ', len(p), i) + p
        bodies.append((ivf_head + rec) if i == 0 else rec)

    old_vid = [c for c in chunks if c.sig == b'@SFV' and c.dtype == 0 and c.chno == 0]
    if any(c.sig != b'@SFV' and c.dtype == 0 for c in chunks):
        raise UsmError('带音轨/透明通道的视频暂不支持重新编码')
    rate_field = old_vid[0].hdr[12:16] if old_vid else struct.pack('>I', fn * 100 // fd)
    rate = struct.unpack('>I', rate_field)[0] or 1
    vids = []
    for i, body in enumerate(bodies):
        if i < len(old_vid):
            hdr = bytearray(old_vid[i].hdr)
        else:
            hdr = bytearray(old_vid[-1].hdr)
            hdr[8:12] = struct.pack('>I', i * 299700 // rate)
        c = Chunk(b'@SFV', hdr, body, 0)
        c.pad = c.natural_pad()
        vids.append(c)
    ixsize = max(c.total() for c in vids)

    hdr_c = _section(chunks, b'@SFV', 1, b'VIDEO_HDRINFO')
    seek_c = _section(chunks, b'@SFV', 3, b'VIDEO_SEEKINFO')
    if hdr_c is None:
        raise UsmError('缺少视频头')
    keys = [i for i, (_, k) in enumerate(pkts) if k] or [0]
    if seek_c is not None:
        seek = UtfTable(seek_c.body)
        tmpl = dict(seek.rows[0]) if seek.rows else {}
        seek.rows = []
        for k in keys:
            r = dict(tmpl)
            for key in r:
                r[key] = 0
            r[b'ofs_frmid'] = k
            seek.rows.append(r)
        seek_c = Chunk(seek_c.sig, seek_c.hdr, seek.build(), 0)
        seek_c.pad = seek_c.natural_pad()
    meta_size = seek_c.total() if seek_c is not None else 0
    hdr_t = _vp9_hdr_table(UtfTable(hdr_c.body), n, ixsize, meta_size)
    new_hdr = Chunk(hdr_c.sig, hdr_c.hdr, hdr_t.build(), 0)
    new_hdr.pad = new_hdr.natural_pad()

    out = []
    vid_done = False
    for c in chunks:
        if c is hdr_c:
            out.append(new_hdr)
        elif seek_c is not None and c.sig == b'@SFV' and c.dtype == 3 and c.body[:4] == b'@UTF':
            out.append(seek_c)
        elif c.sig == b'@SFV' and c.dtype == 0:
            if not vid_done:
                out += vids
                vid_done = True
        else:
            out.append(Chunk(c.sig, c.hdr, c.body, c.pad))
    # CRID：视频流的大小、码率、最小缓冲
    crid = UtfTable(out[0].body)
    stream_len = sum(len(b) for b in bodies)
    for r in range(len(crid.rows)):
        try:
            if crid.get('stmid', r) == 0x40534656:          # '@SFV'
                fn_ = crid.get('filename', r)
                if isinstance(fn_, bytes) and fn_[-4:].lower() in (b'.avi', b'.m2v', b'.264', b'.m1v'):
                    crid.set('filename', fn_[:-4] + b'.ivf', r)
                crid.set('filesize', stream_len, r)
                crid.set('minbuf', max(len(b) for b in bodies), r)
        except KeyError:
            pass
    try:
        crid.set('avbps', int(stream_len * 8 * fn / (fd * max(1, n))))
    except KeyError:
        pass
    body = crid.build()
    room = len(out[0].body) + out[0].pad
    if len(body) > room:
        raise UsmError('CRID 表变大了')
    out[0].body, out[0].pad = body, room - len(body)
    _refresh_tables(out)
    return out


def reencode_to(data: bytes, size: int, progress=None, cancel=None):
    """把视频重新编码成 VP9，封装成正好 size 字节。"""
    chunks = parse(data)
    hdr_c = _section(chunks, b'@SFV', 1, b'VIDEO_HDRINFO')
    if hdr_c is None:
        raise UsmError('缺少视频头')
    ht = UtfTable(hdr_c.body)
    info = {c[0]: ht.get(c[0]) for c in ht.cols}
    frames = info.get(b'total_frames') or 1
    seconds = frames * info[b'framerate_d'] / max(1, info[b'framerate_n'])
    keyint = max(1, round(2 * info[b'framerate_n'] / max(1, info[b'framerate_d'])))     # 约 2 秒一个关键帧
    overhead = sum(c.total() for c in chunks if c.dtype != 0) + 4096 + frames * (8 + 24 + 12 + ALIGN)
    budget = size - overhead
    if budget <= frames * 64:
        raise UsmError('目标太小')
    # libvpx 实际输出一般只有设定码率的 80% 左右，所以起始码率设得比预算略高，争取一次编完
    bitrate = budget * 8 / seconds * 1.05
    best = None                         # 放得下的结果里体积最大（画质最好）的那个
    for attempt in range(5):
        if progress:
            progress(f'重新编码视频（VP9，{bitrate / 1e6:.1f} Mbps' + (f'，第 {attempt + 1} 次）' if attempt else '）'))
        pkts = _encode_vp9(chunks, info, bitrate, keyint, cancel=cancel)
        built = _build_vp9(chunks, info, pkts)
        _, total = _layout(built)
        if total <= size:
            out = pad_to(built, size)
            if out is not None and (best is None or total > best[0]):
                best = (total, out)
            if total >= size * 0.72:
                break                   # 已经用上了原文件的大部分空间，不再为了一点画质多编一遍
        # 编码器的实际码率和设定值有偏差：按这次的实际大小修正，目标是原文件大小的 97%
        bitrate *= max(0.5, min(1.8, size * 0.97 / total))
    if best is None:
        raise UsmError('重新编码后仍放不进原文件')
    return best[1]


def fit(data: bytes, size: int, progress=None, cancel=None):
    """把 USM 视频调整成正好 size 字节；返回 (新数据, 说明)。做不到时抛 UsmError。"""
    if len(data) == size:
        return data, ''
    if len(data) < size:
        out = pad_to(parse(data), size)
        if out is not None:
            return out, f'比原文件小 {(size - len(data)) // 1024 + 1} KB，已用 USM 自带的填充补齐'
    out = reencode_to(data, size, progress, cancel)
    return out, '比原文件大，已把画面重新编码成 VP9 放进原文件（帧数/分辨率不变）'
