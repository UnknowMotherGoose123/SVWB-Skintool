"""Wwise .pck (AKPK) / .bnk helpers for the skin swap tool."""
import re
import struct


def fnv1(s: str) -> int:
    h = 2166136261
    for ch in s.lower().encode():
        h = (h * 16777619) & 0xFFFFFFFF
        h ^= ch
    return h


# ---------------------------------------------------------------- pck
class Pck:
    def __init__(self, data: bytes):
        assert data[:4] == b'AKPK', 'not an AKPK file'
        self.data = data
        hdr_size, self.ver, lang_sz, bank_sz, snd_sz, self.ext_sz = struct.unpack_from('<6I', data, 4)
        off = 28
        self.lang_blob = data[off:off + lang_sz]
        off += lang_sz
        self.banks = self._lut(off, 4); off += bank_sz
        self.sounds = self._lut(off, 4); off += snd_sz
        self.exts = self._lut(off, 8) if self.ext_sz else []

    def _lut(self, off, idsize):
        n = struct.unpack_from('<I', self.data, off)[0]
        out, p = [], off + 4
        for _ in range(n):
            if idsize == 8:
                fid, blk, sz, start, lang = struct.unpack_from('<QIIII', self.data, p); p += 24
            else:
                fid, blk, sz, start, lang = struct.unpack_from('<IIIII', self.data, p); p += 20
            o = start * blk if blk > 0 else start
            out.append({'id': fid, 'block': blk, 'lang': lang, 'payload': self.data[o:o + sz]})
        return out

    def build(self) -> bytes:
        lang = self.lang_blob

        def lut_size(entries, idsize):
            return 4 + len(entries) * (idsize + 16)

        bank_sz = lut_size(self.banks, 4)
        snd_sz = lut_size(self.sounds, 4)
        ext_sz = lut_size(self.exts, 8)
        has_ext = True
        head_len = 28 + len(lang) + bank_sz + snd_sz + ext_sz
        # lay out payloads
        cur = head_len
        layout = []
        for group in (self.banks, self.sounds, self.exts):
            g = []
            for e in group:
                blk = e['block'] or 1
                if cur % blk:
                    cur += blk - cur % blk
                g.append((cur // blk, cur))
                cur += len(e['payload'])
            layout.append(g)
        out = bytearray(cur)
        hdr_size = head_len - 8
        struct.pack_into('<4sI', out, 0, b'AKPK', hdr_size)
        struct.pack_into('<5I', out, 8, self.ver, len(lang), bank_sz, snd_sz, ext_sz)
        p = 28
        out[p:p + len(lang)] = lang; p += len(lang)
        for group, lay, idsize in ((self.banks, layout[0], 4), (self.sounds, layout[1], 4), (self.exts, layout[2], 8)):
            struct.pack_into('<I', out, p, len(group)); p += 4
            for e, (start, off) in zip(group, lay):
                blk = e['block'] or 1
                fmt = '<QIIII' if idsize == 8 else '<IIIII'
                struct.pack_into(fmt, out, p, e['id'], e['block'], len(e['payload']), start if e['block'] else off, e['lang'])
                p += idsize + 16
                out[off:off + len(e['payload'])] = e['payload']
        return bytes(out)


# ---------------------------------------------------------------- bnk
def bnk_sections(b: bytes):
    p, out = 0, []
    while p + 8 <= len(b):
        tag = b[p:p + 4]
        sz = struct.unpack_from('<I', b, p + 4)[0]
        out.append([tag, b[p + 8:p + 8 + sz]])
        p += 8 + sz
    return out


def hirc_items(hirc: bytes):
    n = struct.unpack_from('<I', hirc, 0)[0]
    p, items = 4, []
    for _ in range(n):
        t = hirc[p]
        ln = struct.unpack_from('<I', hirc, p + 1)[0]
        iid = struct.unpack_from('<I', hirc, p + 5)[0]
        items.append((t, iid, hirc[p:p + 5 + ln]))
        p += 5 + ln
    return items


def bank_event_ids(bnk: bytes):
    for tag, body in bnk_sections(bnk):
        if tag == b'HIRC':
            return [iid for t, iid, _ in hirc_items(body) if t == 4]
    return []


def rewrite_bank(bnk: bytes, new_bank_id: int, aliases: dict) -> bytes:
    """aliases: {new_event_id: existing_event_id}; adds copies of existing events under new ids."""
    secs = bnk_sections(bnk)
    out = bytearray()
    for tag, body in secs:
        body = bytearray(body)
        if tag == b'BKHD':
            struct.pack_into('<I', body, 4, new_bank_id)
        elif tag == b'HIRC' and aliases:
            items = hirc_items(bytes(body))
            existing = {iid for t, iid, _ in items}
            ev = {iid: raw for t, iid, raw in items if t == 4}
            extra = []
            for nid, src in aliases.items():
                if nid in existing or src not in ev:
                    continue
                raw = bytearray(ev[src])
                struct.pack_into('<I', raw, 5, nid)
                extra.append(bytes(raw))
                existing.add(nid)
            if extra:
                n = struct.unpack_from('<I', body, 0)[0]
                struct.pack_into('<I', body, 0, n + len(extra))
                body += b''.join(extra)
        out += tag + struct.pack('<I', len(body)) + body
    return bytes(out)


def _varint(b, p):
    v = 0
    while True:
        x = b[p]
        p += 1
        v = (v << 7) | (x & 0x7F)
        if not x & 0x80:
            return v, p


def event_media(bnk: bytes):
    """{事件ID: {媒体ID}}（事件 → 动作 → 声音 → 媒体，适用于语音包这种一对一结构）。"""
    hirc = next((b for t, b in bnk_sections(bnk) if t == b'HIRC'), None)
    if hirc is None:
        return {}
    sounds, actions, events = {}, {}, {}
    for t, iid, raw in hirc_items(hirc):
        body = raw[9:]
        try:
            if t == 2:
                sounds[iid] = struct.unpack_from('<IBI', body, 0)[2]
            elif t == 3:
                actions[iid] = struct.unpack_from('<HI', body, 0)[1]
            elif t == 4:
                n, p = _varint(body, 0)
                events[iid] = list(struct.unpack_from(f'<{n}I', body, p))
        except struct.error:
            continue
    out = {}
    for ev, acts in events.items():
        out[ev] = {sounds[actions[a]] for a in acts if a in actions and actions[a] in sounds}
    return out


def retarget_events(bnk: bytes, mapping: dict) -> bytes:
    """mapping: {事件ID: 改播的事件ID 或 None（静音）}。被改的事件内容换成目标事件的动作列表。"""
    out = bytearray()
    for tag, body in bnk_sections(bnk):
        if tag == b'HIRC' and mapping:
            items = hirc_items(bytes(body))
            ev = {iid: raw for t, iid, raw in items if t == 4}
            parts = []
            for t, iid, raw in items:
                if t == 4 and iid in mapping:
                    src = mapping[iid]
                    if src is not None and src in ev:
                        raw = bytearray(ev[src])
                        struct.pack_into('<I', raw, 5, iid)
                    else:
                        raw = bytes([4]) + struct.pack('<II', 5, iid) + b'\x00'   # 空事件：不播放
                parts.append(bytes(raw))
            body = struct.pack('<I', len(items)) + b''.join(parts)
        out += tag + struct.pack('<I', len(body)) + body
    return bytes(out)


# ---------------------------------------------------------------- event-name guessing
def _templates(bank_base: str):
    """Event-name templates for a bank named like 'dx_btl_{id}'. `{id}` is substituted later."""
    t = set()
    nums2 = [f'{i:02d}' for i in range(0, 41)]
    nums1 = [str(i) for i in range(0, 41)]
    for pre in ('Play_', 'Stop_'):
        t.add(pre + bank_base)
        for a in nums2 + nums1:
            t.add(f'{pre}{bank_base}_{a}')
            for k in range(0, 10):
                t.add(f'{pre}{bank_base}_{a}_{k}')
    # special code-side names seen in the game assemblies
    for pre in ('Play_', 'Stop_'):
        t.add(pre + 'fx_btl_{id}_evo')
        for a in nums2 + nums1:
            for side in ('pl', 'pc'):
                t.add(f'{pre}fx_mt_cl_{side}_{{id}}_{a}')
                t.add(f'{pre}fx_mt_fs_{side}_{{id}}_{a}')
            t.add(f'{pre}fx_evo_{{id}}_{a}')
        for side in ('pl', 'pc'):
            t.add(f'{pre}fx_mt_other_{side}_{{id}}')
    return t


def resolve_events(bank_name: str, sid: str, event_ids):
    """Return {event_id: template_with_{id}} for the events we can name."""
    base = re.sub(r'(?<!\d)' + re.escape(sid) + r'(?!\d)', '{id}', bank_name)
    want = set(event_ids)
    found = {}
    for tpl in _templates(base):
        h = fnv1(tpl.replace('{id}', sid))
        if h in want and h not in found:
            found[h] = tpl
    return found
