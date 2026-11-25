"""多进程 BC7 编码的子进程入口：只导入 etcpak，启动快。"""


def encode(raw: bytes, width: int, height: int) -> bytes:
    import etcpak
    return etcpak.compress_bc7(raw, width, height, None)
