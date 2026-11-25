"""Per-file XOR obfuscation used by the game's asset cache (first 256 bytes are left plain)."""
import struct

import xxhash

BASE_KEY = bytes.fromhex("532B4631E4A7B9473E7CFB")


def _keystream(e: int) -> bytes:
    k = struct.pack('<q', e)
    return bytes(b ^ k[j] for b in BASE_KEY for j in range(8))


def xor_file(data: bytes, e: int) -> bytes:
    """Encrypt/decrypt (symmetric)."""
    if not e or len(data) <= 256:
        return bytes(data)
    ks = _keystream(e)
    n = len(ks)
    body = memoryview(data)[256:]
    # vectorised XOR via int.from_bytes on a repeated keystream (start phase = 256 % n)
    phase = 256 % n
    rep = (ks[phase:] + ks[:phase])
    full = (rep * (len(body) // n + 1))[:len(body)]
    x = int.from_bytes(body, 'little') ^ int.from_bytes(full, 'little')
    return bytes(data[:256]) + x.to_bytes(len(body), 'little')


def checksum(data: bytes) -> int:
    """meta.a.c column: signed xxh64 of the stored (encrypted) file."""
    v = xxhash.xxh64_intdigest(data)
    return v - (1 << 64) if v >= (1 << 63) else v
