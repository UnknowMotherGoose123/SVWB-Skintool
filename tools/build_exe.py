"""打包成单个 exe：在项目根目录运行
    pip install -r requirements.txt pyinstaller
    python tools\\build_exe.py
输出：dist\\影之诗WB换皮工具.exe
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build(name, tmp_name, script, extra):
    icon = os.path.join(ROOT, 'tools', 'app_icon.ico')
    if not os.path.exists(icon):
        subprocess.check_call([sys.executable, os.path.join(ROOT, 'tools', 'make_icon.py'), icon])
    dist = os.path.join(ROOT, 'dist')
    build_dir = os.path.join(ROOT, 'build', tmp_name)
    cmd = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--windowed',
           '--name', tmp_name, '--icon', icon,
           '--distpath', dist, '--workpath', build_dir, '--specpath', build_dir] + extra + [os.path.join(ROOT, script)]
    subprocess.check_call(cmd, cwd=ROOT)
    src = os.path.join(dist, tmp_name + '.exe')
    dst = os.path.join(dist, name + '.exe')
    if os.path.exists(dst):
        os.remove(dst)
    shutil.move(src, dst)
    print('完成：', dst, round(os.path.getsize(dst) / 1048576, 1), 'MB')


def main():
    # GitHub Actions 的 Windows 机器上输出被重定向时默认是 cp1252，打印中文会直接报错退出
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    build('影之诗WB换皮工具', 'SVWBSkinSwap', 'run_skinswap.py', [
        '--add-data', os.path.join(ROOT, 'svwb_skinswap', 'web') + os.pathsep + 'svwb_skinswap/web',
        '--collect-all', 'UnityPy',
        '--collect-all', 'texture2ddecoder',
        # etcpak / astc_encoder 只要模块本身；collect-all 会把 .iobj/.ipdb 调试文件（约 11MB）也打进去
        '--collect-submodules', 'etcpak',
        '--collect-submodules', 'astc_encoder',
        '--collect-all', 'fmod_toolkit',      # UnityPy.export 会顺带加载 fmod.dll
        '--collect-all', 'pyfmodex',
        '--collect-all', 'archspec',          # fmod_toolkit 用它判断 CPU 架构（需要其 json 数据）
        '--collect-all', 'av',                # 视频放不进原文件时重新编码成 VP9（含 FFmpeg/libvpx）
        '--collect-submodules', 'PIL',
        '--collect-submodules', 'svwb_skinswap',
        '--exclude-module', 'numpy',
        # 用不到的大块：选文件夹改用系统原生对话框（不要 tkinter）；不联网（不要 ssl/requests/加密库）；
        # 图片只用 PNG/WebP（不要 AVIF/ICC）；哈希不用 OpenSSL；fsspec 只用本地文件
        '--exclude-module', 'tkinter', '--exclude-module', '_tkinter', '--exclude-module', 'PIL.ImageTk',
        '--exclude-module', 'ssl', '--exclude-module', '_ssl',
        '--exclude-module', 'requests', '--exclude-module', 'urllib3', '--exclude-module', 'certifi',
        '--exclude-module', 'chardet', '--exclude-module', 'charset_normalizer', '--exclude-module', 'idna',
        '--exclude-module', 'cryptography', '--exclude-module', 'Crypto', '--exclude-module', 'OpenSSL',
        '--exclude-module', 'PIL.AvifImagePlugin', '--exclude-module', 'PIL._avif',
        '--exclude-module', '_hashlib',           # 哈希用 Python 内置实现，不带 OpenSSL（libcrypto 5.7MB）
        '--exclude-module', 'PIL.ImageCms', '--exclude-module', 'PIL._imagingcms',
        '--exclude-module', 'PIL.ImageQt', '--exclude-module', 'PIL.ImageShow',
        '--exclude-module', 'setuptools', '--exclude-module', 'pkg_resources', '--exclude-module', 'distutils',
        '--exclude-module', 'xmlrpc',
        '--exclude-module', 'fsspec.implementations.http', '--exclude-module', 'fsspec.implementations.http_sync',
        '--exclude-module', 'aiohttp',
    ])


if __name__ == '__main__':
    main()
