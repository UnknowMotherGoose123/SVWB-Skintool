"""MasterMemory (Cysharp) container used for Master/mastermemory.bytes."""
import struct

import lz4.block
import msgpack


def _ext_hook(code, data):
    if code == 99:  # MessagePack-CSharp Lz4Block
        u = msgpack.Unpacker()
        u.feed(data)
        n = u.unpack()
        off = u.tell()
        return msgpack.unpackb(lz4.block.decompress(data[off:], uncompressed_size=n),
                               raw=False, strict_map_key=False, ext_hook=_ext_hook)
    return msgpack.ExtType(code, data)


def _pack_lz4block(obj, high=False) -> bytes:
    raw = msgpack.packb(obj, use_bin_type=True)
    if high:
        comp = lz4.block.compress(raw, store_size=False, mode='high_compression', compression=12)
    else:
        comp = lz4.block.compress(raw, store_size=False)
    payload = b'\xd2' + struct.pack('>i', len(raw)) + comp
    return b'\xc9' + struct.pack('>I', len(payload)) + b'\x63' + payload


class MasterMemory:
    def __init__(self, data: bytes):
        self.data = data
        up = msgpack.Unpacker(raw=False, strict_map_key=False)
        up.feed(data)
        self.header = up.unpack()          # {table: [offset, length]} (ordered)
        self.base = up.tell()
        self.replaced = {}

    def raw_table(self, name):
        off, ln = self.header[name]
        return self.data[self.base + off:self.base + off + ln]

    def table(self, name):
        return msgpack.unpackb(self.raw_table(name), raw=False, strict_map_key=False, ext_hook=_ext_hook)

    def set_table(self, name, rows, high=False):
        self.replaced[name] = _pack_lz4block(rows, high=high)

    def build(self) -> bytes:
        names = list(self.header.keys())
        order = sorted(names, key=lambda n: self.header[n][0])
        blobs, pos, newhdr = [], 0, {}
        for n in order:
            b = self.replaced.get(n) or self.raw_table(n)
            newhdr[n] = (pos, len(b))
            blobs.append(b)
            pos += len(b)
        h = bytearray()
        cnt = len(names)
        h += (bytes([0x80 | cnt]) if cnt < 16 else b'\xde' + struct.pack('>H', cnt))
        for n in names:
            h += msgpack.packb(n)
            o, l = newhdr[n]
            h += b'\x92\xd2' + struct.pack('>i', o) + b'\xd2' + struct.pack('>i', l)
        return bytes(h) + b''.join(blobs)
