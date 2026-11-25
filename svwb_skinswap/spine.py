"""Spine 4.2 二进制骨骼（.skel）的最小解析与改写。

用途：游戏在对战/进化里调用 LeaderSkinSpineObject.SetIsEnglish，按主战者 ID 写死皮肤名：
    'class_<id>' 含 "10000"，或等于 class_1105 / class_100012  → SetSkin("default")
    其余                                                       → SetSkin("JP")
    英文语音                                                    → SetSkin("US")
换皮后模型来自 B、ID 却是 A，皮肤名对不上就会丢东西（嘴只放在 JP/US 皮肤里）或直接抛异常
（骨骼里没有 JP 皮肤）。这里只改骨骼的皮肤段，骨骼/动画等其它数据原样保留。
"""
import struct


class SpineError(Exception):
    pass


class _R:
    def __init__(self, b, p=0):
        self.b, self.p = b, p

    def byte(self):
        v = self.b[self.p]
        self.p += 1
        return v

    def varint(self):
        r, shift = 0, 0
        while True:
            c = self.b[self.p]
            self.p += 1
            r |= (c & 0x7F) << shift
            if not c & 0x80:
                return r
            shift += 7
            if shift > 28:
                raise SpineError('varint 过长')

    def i32(self):
        v = struct.unpack_from('>i', self.b, self.p)[0]
        self.p += 4
        return v

    def f32(self):
        self.p += 4

    def string(self):
        n = self.varint()
        if n == 0:
            return None
        s = bytes(self.b[self.p:self.p + n - 1])
        if len(s) != n - 1:
            raise SpineError('字符串越界')
        self.p += n - 1
        return s.decode('utf-8', 'replace')

    def floats(self, n):
        self.p += 4 * n


def _uvarint(v):
    out = bytearray()
    while True:
        c = v & 0x7F
        v >>= 7
        if v:
            out.append(c | 0x80)
        else:
            out.append(c)
            return bytes(out)


def _skip_vertices(r, weighted):
    n = r.varint()
    if not weighted:
        r.floats(n * 2)
        return n * 2
    for _ in range(n):
        bones = r.varint()
        for _ in range(bones):
            r.varint()
            r.floats(3)
    return n * 2


def _skip_sequence(r):
    for _ in range(4):
        r.varint()


class Att:
    __slots__ = ('t', 'raw', 'flags', 'name', 'path', 'color', 'seq', 'wh')

    def __init__(self, t, raw, flags, name, path, color, seq, wh):
        self.t, self.raw, self.flags, self.name, self.path = t, raw, flags, name, path
        self.color, self.seq, self.wh = color, seq, wh


def _attachment(r, nonessential):
    """跳过一个附件，返回 Att（原始字节 + 网格转链接网格要用的字段）。"""
    start = r.p
    flags = r.byte()
    name = b''
    if flags & 8:
        p0 = r.p
        r.varint()
        name = r.b[p0:r.p]
    t = flags & 7
    path = color = seq = wh = b''
    if t == 0:  # region
        if flags & 16: r.varint()
        if flags & 32: r.i32()
        if flags & 64: _skip_sequence(r)
        if flags & 128: r.f32()
        r.floats(6)
    elif t == 1:  # bounding box
        _skip_vertices(r, bool(flags & 16))
        if nonessential: r.i32()
    elif t == 2:  # mesh
        if flags & 16:
            p0 = r.p; r.varint(); path = r.b[p0:r.p]
        if flags & 32:
            p0 = r.p; r.i32(); color = r.b[p0:r.p]
        if flags & 64:
            p0 = r.p; _skip_sequence(r); seq = r.b[p0:r.p]
        hull = r.varint()
        vlen = _skip_vertices(r, bool(flags & 128))
        r.floats(vlen)
        tri = (vlen - hull - 2) * 3
        if tri < 0:
            raise SpineError('网格三角形数为负')
        for _ in range(tri):
            r.varint()
        if nonessential:
            for _ in range(r.varint()):
                r.varint()
            p0 = r.p; r.floats(2); wh = r.b[p0:r.p]
    elif t == 3:  # linked mesh
        if flags & 16: r.varint()
        if flags & 32: r.i32()
        if flags & 64: _skip_sequence(r)
        r.varint()  # skin index
        r.varint()  # parent
        if nonessential: r.floats(2)
    elif t == 4:  # path
        vlen = _skip_vertices(r, bool(flags & 64))
        r.floats(vlen // 6)
        if nonessential: r.i32()
    elif t == 5:  # point
        r.floats(3)
        if nonessential: r.i32()
    elif t == 6:  # clipping
        r.varint()
        _skip_vertices(r, bool(flags & 16))
        if nonessential: r.i32()
    else:
        raise SpineError(f'未知附件类型 {t}')
    return Att(t, bytes(r.b[start:r.p]), flags, bytes(name or b''), bytes(path), bytes(color), bytes(seq), bytes(wh))


class Skel:
    """解析到皮肤段为止，并校验后面的事件段能正常读出。"""

    def __init__(self, data):
        self.data = bytes(data)
        r = _R(self.data)
        r.p = 8
        self.version = r.string() or ''
        if not self.version.startswith('4.2'):
            raise SpineError(f'不支持的 Spine 版本 {self.version!r}')
        r.floats(5)
        self.nonessential = bool(r.byte())
        ne = self.nonessential
        if ne:
            r.f32()
            r.string()
            r.string()
        self.strings_pos = r.p
        self.strings = [r.string() for _ in range(r.varint())]
        self.strings_end = r.p
        # 骨骼
        self.bones = []          # (名字, skinRequired 字节偏移)
        for i in range(r.varint()):
            name = r.string()
            if i:
                r.varint()
            r.floats(8)
            r.varint()
            self.bones.append((name, r.p))
            r.byte()
            if ne:
                r.i32()
                r.string()
                r.byte()
        # 插槽
        self.slots = []
        for _ in range(r.varint()):
            name = r.string()
            r.varint()
            r.i32()
            r.i32()
            r.varint()
            r.varint()
            if ne:
                r.byte()
            self.slots.append(name)
        # 约束：记录每个约束 skinRequired 所在字节和位
        self.ik, self.tc, self.pc, self.phys = [], [], [], []
        for _ in range(r.varint()):
            r.string(); r.varint()
            for _ in range(r.varint()): r.varint()
            r.varint()
            pos = r.p
            flags = r.byte()
            self.ik.append(pos)
            if flags & 32 and flags & 64: r.f32()
            if flags & 128: r.f32()
        for _ in range(r.varint()):
            r.string(); r.varint()
            for _ in range(r.varint()): r.varint()
            r.varint()
            pos = r.p
            flags = r.byte()
            self.tc.append(pos)
            for bit in (8, 16, 32, 64, 128):
                if flags & bit: r.f32()
            flags = r.byte()
            for bit in (1, 2, 4, 8, 16, 32, 64):
                if flags & bit: r.f32()
        for _ in range(r.varint()):
            r.string(); r.varint()
            self.pc.append(r.p)
            r.byte()
            for _ in range(r.varint()): r.varint()
            r.varint()
            flags = r.byte()
            if flags & 128: r.f32()
            r.floats(5)
        for _ in range(r.varint()):
            r.string(); r.varint(); r.varint()
            pos = r.p
            flags = r.byte()
            self.phys.append(pos)
            for bit in (2, 4, 8, 16, 32, 64):
                if flags & bit: r.f32()
            r.byte()
            r.floats(3)
            if flags & 128: r.f32()
            r.floats(2)
            flags = r.byte()
            if flags & 128: r.f32()
        # 皮肤
        self.skins_pos = r.p
        self.skins = []
        default = self._skin(r, True)
        self.has_default = default is not None
        if default:
            self.skins.append(default)
        self.named_count_pos = r.p
        for _ in range(r.varint()):
            self.skins.append(self._skin(r, False))
        self.skins_end = r.p
        # 事件段
        self.events, self.event_audio = [], []
        for _ in range(r.varint()):
            name = r.string()
            r.varint(); r.f32(); r.string()
            audio = r.string()
            if audio is not None:
                r.floats(2)
            self.events.append(name)
            self.event_audio.append(audio is not None)
        # 动画段：完整读一遍，必须正好读到文件末尾
        self.anim_pos = r.p
        self.animations = parse_animations(self)
        self.first_anim = self.animations[0][0] if self.animations else None

    def animation_names(self):
        return [a[0] for a in self.animations]

    def _skin(self, r, default):
        start = r.p
        sk = {'name': 'default' if default else None, 'start': start, 'bones': [], 'ik': [], 'tc': [], 'pc': [],
              'phys': [], 'slots': {}}
        if default:
            n = r.varint()
            if n == 0:
                return None
            sk['slots_pos'] = start
        else:
            sk['name'] = r.string()
            if self.nonessential:
                r.i32()
            sk['bones'] = [r.varint() for _ in range(r.varint())]
            for key in ('ik', 'tc', 'pc', 'phys'):
                sk[key] = [r.varint() for _ in range(r.varint())]
            sk['slots_pos'] = r.p
            n = r.varint()
        for _ in range(n):
            slot = r.varint()
            atts = sk['slots'].setdefault(slot, {})
            for _ in range(r.varint()):
                name_ref = r.varint()
                atts[name_ref] = _attachment(r, self.nonessential)
        sk['end'] = r.p
        return sk

    # ---- 查询 ----
    def skin(self, name):
        for s in self.skins:
            if s['name'] == name:
                return s
        return None

    def skin_index(self, name):
        for i, s in enumerate(self.skins):
            if s['name'] == name:
                return i
        return -1

    def skin_names(self):
        return [s['name'] for s in self.skins]

    # ---- 改写 ----
    @staticmethod
    def _slots_bytes(slots):
        out = bytearray(_uvarint(len(slots)))
        for slot, atts in slots.items():   # 保持原顺序，未改动的皮肤能逐字节还原
            out += _uvarint(slot) + _uvarint(len(atts))
            for ref, att in atts.items():
                out += _uvarint(ref) + att.raw
        return bytes(out)

    def _emit(self, prefix, default_slots, named, extra=b'', extra_count=0):
        """prefix=皮肤段之前的数据；default_slots=None 表示没有 default 皮肤；named=[(皮肤, 插槽表)]。"""
        out = bytearray(prefix)
        out += self._slots_bytes(default_slots) if default_slots is not None else b'\x00'
        out += _uvarint(len(named) + extra_count)
        for sk, slots in named:
            out += self.data[sk['start']:sk['slots_pos']] + self._slots_bytes(slots)
        out += extra
        out += self.data[self.skins_end:]
        return bytes(out)

    def _parts(self):
        named = self.skins[1:] if self.has_default else self.skins
        default_slots = {k: dict(v) for k, v in self.skins[0]['slots'].items()} if self.has_default else None
        return default_slots, [(sk, {k: dict(v) for k, v in sk['slots'].items()}) for sk in named]

    def _linked_copy(self, att, skin_index, parent_ref):
        """把网格附件改成指向原件的链接网格（继承动画），这样嘴部的网格变形动画照样作用到副本上。"""
        flags = 3 | 128
        out = bytearray()
        if att.name: flags |= 8; out += att.name
        if att.path: flags |= 16; out += att.path
        if att.color: flags |= 32; out += att.color
        if att.seq: flags |= 64; out += att.seq
        out += _uvarint(skin_index) + _uvarint(parent_ref)
        if self.nonessential:
            out += att.wh or b'\x00' * 8
        return Att(3, bytes([flags]) + bytes(out), flags, att.name, att.path, att.color, att.seq, att.wh)

    def _point(self):
        raw = b'\x05' + b'\x00' * 12 + (b'\xff\xff\xff\xff' if self.nonessential else b'')
        return Att(5, raw, 5, b'', b'', b'', b'', b'')

    def merge_into_default(self, name='JP', hide_in='US'):
        """把命名皮肤 name 的附件并入 default 皮肤，并让它依赖的骨骼/约束常驻。
        hide_in 皮肤里给这些新增附件放上不渲染的点附件，避免切到那个皮肤时两套嘴叠在一起。"""
        si = self.skin_index(name)
        if si < 0:
            return None
        if not self.has_default:
            # 没有 default 皮肤时新建会让命名皮肤下标整体后移，动画/链接网格按下标引用皮肤，不能这么做
            raise SpineError('骨骼没有 default 皮肤，无法合并')
        src = self.skins[si]
        prefix = bytearray(self.data[:self.skins_pos])
        for b in src['bones']:
            prefix[self.bones[b][1]] = 0
        for key, table in (('ik', self.ik), ('tc', self.tc), ('phys', self.phys)):
            for i in src[key]:
                prefix[table[i]] &= 0xFE
        for i in src['pc']:
            prefix[self.pc[i]] = 0
        default_slots, named = self._parts()
        original = {k: set(v) for k, v in default_slots.items()}
        added = []
        for slot, atts in src['slots'].items():
            dst = default_slots.setdefault(slot, {})
            for ref, att in atts.items():
                dst[ref] = self._linked_copy(att, si, ref) if att.t == 2 else att
                if ref not in original.get(slot, ()):
                    added.append((slot, ref))
        for sk, slots in named:
            if sk['name'] == hide_in:
                for slot, ref in added:
                    slots.setdefault(slot, {}).setdefault(ref, self._point())
        return self._emit(prefix, default_slots, named)

    def add_empty_skins(self, names):
        """在末尾追加空的命名皮肤（只为让 SetSkin 不抛异常；空皮肤会回落到 default）。"""
        names = [n for n in names if self.skin(n) is None]
        if not names:
            return None
        extra = bytearray()
        for n in names:
            raw = n.encode('utf-8')
            extra += _uvarint(len(raw) + 1) + raw
            if self.nonessential:
                extra += b'\xff\xff\xff\xff'
            extra += b'\x00' * 6   # bones, ik, transform, path, physics, slots 都是 0
        default_slots, named = self._parts()
        return self._emit(self.data[:self.skins_pos], default_slots, named, bytes(extra), len(names))


def leader_skin_mode(skin_id):
    """复刻 LeaderSkinSpineObject.SetIsEnglish 对日语/中文语音选的皮肤名。"""
    s = f'class_{skin_id}'
    return 'default' if ('10000' in s or s in ('class_1105', 'class_100012')) else 'JP'


def fit_language_skins(skel, a_id):
    """让 B 的骨骼在 A 的 ID 下也能正常显示。返回 (新数据或 None, 说明)。"""
    sk = Skel(skel)
    mode = leader_skin_mode(a_id)
    out, note = None, ''
    if mode == 'default':
        if sk.skin('JP') is not None:
            out, note = sk.merge_into_default('JP', 'US'), 'JP→default'
    elif sk.skin('JP') is None:
        out = sk.add_empty_skins(['JP', 'US'])
        note = '+JP/US'
    if out is not None:
        Skel(out)   # 改完再完整解析一遍，确认结构没坏
    return out, note

# ---------------- 动画段 ----------------
def _bezier(r, n):
    r.floats(4 * n)


def _curve_timeline(r, values):
    """CurveTimeline1/2/…：首帧 time+values，之后每帧 time2+values2+曲线类型(+贝塞尔)。"""
    frames = r.varint()
    r.varint()                     # bezierCount
    r.floats(1 + values)
    for _ in range(frames - 1):
        r.floats(1 + values)
        c = r.byte()
        if c == 2:
            _bezier(r, values)


def _color_timeline(r, frames, color_bytes, curves):
    r.varint()                     # bezierCount
    r.f32()
    r.p += color_bytes
    for _ in range(frames - 1):
        r.f32()
        r.p += color_bytes
        if r.byte() == 2:
            _bezier(r, curves)


def _skip_animation(r):
    r.varint()                                             # timeline 总数
    for _ in range(r.varint()):                            # 插槽
        r.varint()
        for _ in range(r.varint()):
            t = r.byte()
            frames = r.varint()
            if t == 0:
                for _ in range(frames):
                    r.f32(); r.varint()
            elif t == 1: _color_timeline(r, frames, 4, 4)
            elif t == 2: _color_timeline(r, frames, 3, 3)
            elif t == 3: _color_timeline(r, frames, 7, 7)
            elif t == 4: _color_timeline(r, frames, 6, 6)
            elif t == 5: _color_timeline(r, frames, 1, 1)
            else:
                raise SpineError(f'未知插槽时间轴 {t}')
    for _ in range(r.varint()):                            # 骨骼
        r.varint()
        for _ in range(r.varint()):
            t = r.byte()
            if t == 10:
                for _ in range(r.varint()):
                    r.f32(); r.byte()
                continue
            if t > 10:
                raise SpineError(f'未知骨骼时间轴 {t}')
            _curve_timeline(r, 2 if t in (1, 4, 7) else 1)   # 平移/缩放/斜切是双值
    for _ in range(r.varint()):                            # IK
        r.varint()
        frames = r.varint()
        r.varint()
        flags = r.byte()
        r.f32()
        if flags & 1 and flags & 2: r.f32()
        if flags & 4: r.f32()
        for _ in range(frames - 1):
            flags = r.byte()
            r.f32()
            if flags & 1 and flags & 2: r.f32()
            if flags & 4: r.f32()
            if not flags & 64 and flags & 128:
                _bezier(r, 2)
    for _ in range(r.varint()):                            # 变换约束
        r.varint()
        frames = r.varint()
        r.varint()
        r.floats(7)
        for _ in range(frames - 1):
            r.floats(7)
            if r.byte() == 2:
                _bezier(r, 6)
    for _ in range(r.varint()):                            # 路径约束
        r.varint()
        for _ in range(r.varint()):
            t = r.byte()
            if t in (0, 1):
                _curve_timeline(r, 1)
            elif t == 2:
                frames = r.varint()
                r.varint()
                r.floats(4)
                for _ in range(frames - 1):
                    r.floats(4)
                    if r.byte() == 2:
                        _bezier(r, 3)
            else:
                raise SpineError(f'未知路径时间轴 {t}')
    for _ in range(r.varint()):                            # 物理约束
        r.varint()
        for _ in range(r.varint()):
            t = r.byte()
            if t == 8:
                r.floats(r.varint())
            else:
                _curve_timeline(r, 1)
    for _ in range(r.varint()):                            # 附件（变形/序列）
        r.varint()                                         # 皮肤下标
        for _ in range(r.varint()):
            r.varint()                                     # 插槽
            for _ in range(r.varint()):
                r.varint()                                 # 附件名
                t = r.byte()
                frames = r.varint()
                if t == 0:
                    r.varint()
                    r.f32()
                    for f in range(frames):
                        end = r.varint()
                        if end:
                            r.varint()
                            r.floats(end)
                        if f == frames - 1:
                            break
                        r.f32()
                        if r.byte() == 2:
                            _bezier(r, 1)
                elif t == 1:
                    r.floats(frames * 3)
                else:
                    raise SpineError(f'未知附件时间轴 {t}')
    n = r.varint()                                         # 绘制顺序
    for _ in range(n):
        r.f32()
        for _ in range(r.varint()):
            r.varint(); r.varint()
    return r.varint()                                      # 事件帧数（由调用方处理）


def parse_animations(sk):
    """返回 [(名字, 起点, 终点)]，起点是名字字符串的位置。要求正好读到文件末尾。"""
    r = _R(sk.data, sk.anim_pos)
    out = []
    audio = sk.event_audio
    for _ in range(r.varint()):
        start = r.p
        name = r.string()
        events = _skip_animation(r)
        for _ in range(events):
            r.f32()
            ei = r.varint()
            r.varint(); r.f32(); r.string()
            if audio[ei]:
                r.floats(2)
        out.append((name, start, r.p))
    if r.p != len(sk.data):
        raise SpineError(f'动画段长度不符 {r.p} != {len(sk.data)}')
    return out