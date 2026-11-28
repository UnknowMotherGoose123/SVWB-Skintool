"""卡面异画用的游戏代码补丁（HybridCLR 热更新程序集 View.bytes）。

游戏显示高级异画的人物动画时，按卡牌 ID 拼动画路径 Prefabs/Foil/{卡ID}{0|1}。卡 ID 先经过
CardUtils.ConvertStarterFullArtToNormalFullArt：原逻辑只有初始卡组的卡（卡包 81001/81002）才查 StarterCardIdMapper
换成异画卡 ID（手牌走的 ConvertStarterToCardIdInt 则不看卡包）。补丁把前者改成直接调用后者：查得到映射就换。
原版映射表里全是初始卡组的卡，所以主数据不加映射行时，行为和原版完全一样（补丁留着也无害）；
主数据给普通卡加一行“普通卡 → 异画卡”后，手牌、场上、收藏、卡牌详情都会加载异画的人物动画。

热更新有多份副本（安装包自带 / 游戏下载后拷出的补丁目录），游戏按版本号选一份加载，所以每份都改。
文件前 10 字节异或 0x5A，没有校验；只原地改一个方法体，长度不变，和帧率工具的补丁互不影响。
"""
import os
import struct

KEY = 0x5A
NKEY = 10
TYPE_NS, TYPE_NAME = 'Wizard2.View', 'CardUtils'
TARGET = 'ConvertStarterFullArtToNormalFullArt'
CALLEE = 'ConvertStarterToCardIdInt'
ORIG_FIRST_CALL = 'IsStarterCard'


class HotfixError(Exception):
    pass


def _plain(raw):
    b = bytearray(raw)
    if b[:2] != b'MZ':
        for i in range(min(NKEY, len(b))):
            b[i] ^= KEY
    return b


class _Asm:
    """只读出补丁要用的部分：节表、元数据流、TypeDef/MethodDef 表。"""

    def __init__(self, plain):
        r = self.raw = plain
        if r[:2] != b'MZ':
            raise HotfixError('文件头不对（不是程序集）')
        pe = struct.unpack_from('<I', r, 0x3C)[0]
        if r[pe:pe + 4] != b'PE\0\0':
            raise HotfixError('PE 头不对')
        nsec = struct.unpack_from('<H', r, pe + 6)[0]
        opt_size = struct.unpack_from('<H', r, pe + 20)[0]
        opt = pe + 24
        dd = opt + (96 if struct.unpack_from('<H', r, opt)[0] == 0x10B else 112)
        self.sections = []
        for i in range(nsec):
            vsz, va, rsz, rptr = struct.unpack_from('<IIII', r, opt + opt_size + 40 * i + 8)
            self.sections.append((va, max(vsz, rsz), rptr))
        cli_rva = struct.unpack_from('<I', r, dd + 14 * 8)[0]
        if not cli_rva:
            raise HotfixError('不是 .NET 程序集')
        cli = self.off(cli_rva)
        self.md = self.off(struct.unpack_from('<I', r, cli + 8)[0])
        if r[self.md:self.md + 4] != b'BSJB':
            raise HotfixError('元数据签名不对')
        vlen = struct.unpack_from('<I', r, self.md + 12)[0]
        p = self.md + 16 + vlen + 2
        n = struct.unpack_from('<H', r, p)[0]
        p += 2
        self.streams = {}
        for _ in range(n):
            o, size = struct.unpack_from('<II', r, p)
            e = r.index(b'\0', p + 8)
            self.streams[r[p + 8:e].decode()] = o
            p = (e + 1 + 3) // 4 * 4
        if '#~' not in self.streams or '#Strings' not in self.streams:
            raise HotfixError('缺少元数据流')
        self._tables()

    def off(self, rva):
        for va, size, rptr in self.sections:
            if va <= rva < va + size:
                return rptr + rva - va
        raise HotfixError('RVA %x 不在任何节里' % rva)

    def string(self, idx):
        base = self.md + self.streams['#Strings']
        e = self.raw.index(b'\0', base + idx)
        return self.raw[base + idx:e].decode('utf-8', 'replace')

    def _tables(self):
        r = self.raw
        t = self.md + self.streams['#~']
        heap = r[t + 6]
        valid = struct.unpack_from('<Q', r, t + 8)[0]
        rows = [0] * 64
        p = t + 24
        for i in range(64):
            if valid >> i & 1:
                rows[i] = struct.unpack_from('<I', r, p)[0]
                p += 4
        if heap & 0x40:
            p += 4
        sidx = 4 if heap & 1 else 2
        gidx = 4 if heap & 2 else 2
        bidx = 4 if heap & 4 else 2

        def simple(tb):
            return 2 if rows[tb] < 0x10000 else 4

        def coded(bits, tbs):
            return 2 if max(rows[x] for x in tbs) < (1 << (16 - bits)) else 4
        tdor = coded(2, (0x02, 0x01, 0x1B))
        sizes = {0x00: 2 + sidx + 3 * gidx, 0x01: coded(2, (0x00, 0x1A, 0x23, 0x01)) + 2 * sidx,
                 0x02: 4 + 2 * sidx + tdor + simple(0x04) + simple(0x06), 0x03: simple(0x04),
                 0x04: 2 + sidx + bidx, 0x05: simple(0x06), 0x06: 8 + sidx + bidx + simple(0x08)}
        if rows[0x03] or rows[0x05]:
            raise HotfixError('元数据是未优化格式，不支持')
        start = {}
        for tb in range(7):
            start[tb] = p
            p += rows[tb] * sizes[tb]
        rd = lambda pos, n: struct.unpack_from('<H' if n == 2 else '<I', r, pos)[0]
        self.types = []
        for i in range(rows[0x02]):
            q = start[0x02] + i * sizes[0x02] + 4
            name, ns = rd(q, sidx), rd(q + sidx, sidx)
            ml = rd(q + 2 * sidx + tdor + simple(0x04), simple(0x06))
            self.types.append((self.string(ns), self.string(name), ml))
        self.n_methods = rows[0x06]
        self.m_start, self.m_size, self.sidx = start[0x06], sizes[0x06], sidx

    def method(self, row):
        """MethodDef 第 row 行：(名字, RVA)。"""
        q = self.m_start + (row - 1) * self.m_size
        nm = struct.unpack_from('<H' if self.sidx == 2 else '<I', self.raw, q + 8)[0]
        return self.string(nm), struct.unpack_from('<I', self.raw, q)[0]

    def methods_of(self, ns, name):
        for i, (tns, tn, ml) in enumerate(self.types):
            if (tns, tn) == (ns, name):
                hi = self.types[i + 1][2] if i + 1 < len(self.types) else self.n_methods + 1
                return {row: self.method(row) for row in range(ml, hi)}
        return {}

    def code(self, rva):
        """方法体 IL 的 (文件偏移, 长度)。"""
        o = self.off(rva)
        h = self.raw[o]
        if h & 3 == 2:
            return o + 1, h >> 2
        if h & 3 != 3:
            raise HotfixError('方法头格式不认识')
        hs = (struct.unpack_from('<H', self.raw, o)[0] >> 12) * 4
        return o + hs, struct.unpack_from('<I', self.raw, o + 4)[0]


HAND_TYPE_PREFIX = '<SetSpineObjectAsync>d__'      # 手牌 HandCardView.SetSpineObjectAsync 的状态机


def _points(a):
    """所有补丁点：[(名字, 偏移, 原版字节或 None, 补丁字节, 当前字节)]；认不出抛 HotfixError。
    1. CardUtils.ConvertStarterFullArtToNormalFullArt（场上、收藏、卡牌详情）：整个函数体改成调用 ConvertStarterToCardIdInt；
    2. 手牌 SetSpineObjectAsync 里 `if (IsStarterCard(id)) id = ConvertStarterToCardIdInt(id)`：
       把 brfalse.s 改成 pop; nop，不管是不是初始卡组的卡都查映射表（查不到原样返回）。"""
    ms = a.methods_of(TYPE_NS, TYPE_NAME)
    tgt = [rva for row, (nm, rva) in ms.items() if nm == TARGET]
    cal = [row for row, (nm, rva) in ms.items() if nm == CALLEE]
    starter = {0x06000000 | row for row, (nm, rva) in ms.items() if nm == ORIG_FIRST_CALL}
    if len(tgt) != 1 or len(cal) != 1 or not tgt[0] or not starter:
        raise HotfixError('找不到要改的函数（游戏代码可能更新了）')
    callee = 0x06000000 | cal[0]
    out = []
    off, n = a.code(tgt[0])
    cur = bytes(a.raw[off:off + n])
    new = b'\x00' * (n - 7) + b'\x02\x28' + struct.pack('<I', callee) + b'\x2a'
    if n < 7:
        raise HotfixError('函数太短')
    tok = struct.unpack_from('<I', cur, 2)[0] if cur[:2] == b'\x02\x28' else 0
    if cur != new and tok not in starter:
        raise HotfixError('函数内容和预期不一样（游戏代码可能更新了）')
    out.append(('全画幅卡转换', off, None if cur == new else cur, new, cur))

    # 手牌：在各个 <SetSpineObjectAsync>d__N.MoveNext 里找  call IsStarterCard; brfalse.s X; ldarg.0; ldarg.0; ldfld _; call ConvertStarterToCardIdInt
    found = []
    for i, (tns, tn, ml) in enumerate(a.types):
        if not tn.startswith(HAND_TYPE_PREFIX):
            continue
        hi = a.types[i + 1][2] if i + 1 < len(a.types) else a.n_methods + 1
        for row in range(ml, hi):
            nm, rva = a.method(row)
            if nm != 'MoveNext' or not rva:
                continue
            off, n = a.code(rva)
            body = bytes(a.raw[off:off + n])
            for k in range(n - 21):
                if body[k] != 0x28 or struct.unpack_from('<I', body, k + 1)[0] not in starter:
                    continue
                if body[k + 7:k + 10] != b'\x02\x02\x7b' or body[k + 14] != 0x28 or \
                        struct.unpack_from('<I', body, k + 15)[0] != callee:
                    continue
                br = body[k + 5:k + 7]
                if br[0] == 0x2C or br == b'\x26\x00':
                    found.append((off + k + 5, None if br == b'\x26\x00' else br, br))
    if len(found) != 1:
        raise HotfixError(f'手牌那段代码找到 {len(found)} 处，和预期不一样（游戏代码可能更新了）')
    off, orig, cur = found[0]
    out.append(('手牌人物动画', off, orig, b'\x26\x00', cur))
    return out


def analyze(raw):
    """返回 {'state': 'orig'|'patched'|'partial'|'unknown', 'points': [(偏移, 原版字节, 补丁字节)], 'why'}。"""
    a = _Asm(_plain(raw))
    try:
        pts = _points(a)
    except HotfixError as ex:
        return {'state': 'unknown', 'why': str(ex)}
    done = [cur == new for _, _, _, new, cur in pts]
    state = 'patched' if all(done) else ('orig' if not any(done) else 'partial')
    return {'state': state, 'points': [(off, orig, new) for _, off, orig, new, _ in pts]}


def copies(data_dir):
    """所有 View.bytes 副本：安装包自带、StreamingAssets 补丁目录、LocalLow 补丁目录（老版本）。"""
    cands = [os.path.join(data_dir, 'StreamingAssets', 'Hotfix', 'Release'),
             os.path.join(data_dir, 'StreamingAssets', 'Hotfix', 'Windows', 'Release')]
    try:
        with open(os.path.join(data_dir, 'app.info'), encoding='utf-8') as fp:
            company, product = [x.strip() for x in fp.read().split('\n')[:2]]
        if company and product:
            cands.append(os.path.join(os.path.expanduser('~'), 'AppData', 'LocalLow', company, product,
                                      'Hotfix', 'Windows', 'Release'))
    except Exception:
        pass
    return [os.path.normpath(os.path.join(p, 'View.bytes')) for p in cands if os.path.isfile(os.path.join(p, 'View.bytes'))]


def _write_at(path, edits):
    """edits：[(偏移, 字节)]，一次写回。"""
    with open(path, 'rb') as fp:
        raw = bytearray(fp.read())
    for off, data in edits:
        raw[off:off + len(data)] = data
    tmp = path + '.svwbtmp'
    with open(tmp, 'wb') as fp:
        fp.write(raw)
    os.replace(tmp, path)


def status(data_dir):
    out = []
    for p in copies(data_dir):
        try:
            with open(p, 'rb') as fp:
                info = analyze(fp.read())
        except Exception as ex:
            info = {'state': 'unknown', 'why': str(ex)}
        out.append({'path': p, **info})
    return out


def apply(data_dir):
    """给所有副本打补丁（已打过的点跳过）。
    返回 (记录 {路径: [{'off', 'orig', 'new'}]}, 认不出的副本 [(路径, 原因)])。"""
    rec, bad = {}, []
    for st in status(data_dir):
        p = st['path']
        if st['state'] == 'unknown':
            bad.append((p, st.get('why', '')))
            continue
        todo = [(off, orig, new) for off, orig, new in st['points'] if orig is not None]
        if todo:
            _write_at(p, [(off, new) for off, _, new in todo])
            rec[p] = [{'off': off, 'orig': orig.hex(), 'new': new.hex()} for off, orig, new in todo]
    return rec, bad


def revert(rec):
    """按记录把补丁改回原样（文件已被游戏更新覆盖的跳过）。返回还原的份数。"""
    n = 0
    for p, items in (rec or {}).items():
        if isinstance(items, dict):            # v2.1.5 早期的记录：一份一个补丁点
            items = [items]
        try:
            with open(p, 'rb') as fp:
                raw = fp.read()
            edits = [(r['off'], bytes.fromhex(r['orig'])) for r in items
                     if raw[r['off']:r['off'] + len(bytes.fromhex(r['new']))] == bytes.fromhex(r['new'])]
            if edits:
                _write_at(p, edits)
                n += 1
        except OSError:
            continue
    return n
