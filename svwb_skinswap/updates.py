"""检查 GitHub 上有没有新版本。

联网用 Windows 自带的 WinHTTP（ctypes 调用）：不需要把 OpenSSL 打进 exe，
走系统的证书和代理设置。只发一个 GET，请求里不带任何个人信息。
"""
import ctypes
import json
import re
from ctypes import wintypes

REPO = 'UnknowMotherGoose123/SVWB-Skintool'
REPO_URL = f'https://github.com/{REPO}'
RELEASES_URL = f'{REPO_URL}/releases'

_API_LATEST = f'https://api.github.com/repos/{REPO}/releases/latest'
_RAW_SERVER = f'https://raw.githubusercontent.com/{REPO}/main/svwb_skinswap/server.py'

WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY = 4     # Win 8.1+：自动用系统代理
WINHTTP_ACCESS_TYPE_DEFAULT_PROXY = 0
WINHTTP_FLAG_SECURE = 0x00800000
WINHTTP_QUERY_STATUS_CODE = 19
WINHTTP_QUERY_FLAG_NUMBER = 0x20000000


class UpdateError(Exception):
    pass


def _winhttp():
    w = ctypes.WinDLL('winhttp', use_last_error=True)
    H = ctypes.c_void_p
    w.WinHttpOpen.restype = H
    w.WinHttpOpen.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    w.WinHttpConnect.restype = H
    w.WinHttpConnect.argtypes = [H, wintypes.LPCWSTR, wintypes.WORD, wintypes.DWORD]
    w.WinHttpOpenRequest.restype = H
    w.WinHttpOpenRequest.argtypes = [H, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                     ctypes.c_void_p, wintypes.DWORD]
    w.WinHttpSetTimeouts.argtypes = [H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    w.WinHttpSendRequest.argtypes = [H, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                     wintypes.DWORD, ctypes.c_size_t]
    w.WinHttpReceiveResponse.argtypes = [H, ctypes.c_void_p]
    w.WinHttpQueryHeaders.argtypes = [H, wintypes.DWORD, wintypes.LPCWSTR, ctypes.c_void_p,
                                      ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
    w.WinHttpReadData.argtypes = [H, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    w.WinHttpCloseHandle.argtypes = [H]
    return w


def http_get(url, user_agent, accept='*/*', timeout_ms=8000, limit=2 << 20):
    """返回 (状态码, 内容 bytes)。网络错误抛 UpdateError。"""
    m = re.match(r'^https://([^/]+)(/.*)?$', url)
    if not m:
        raise UpdateError('只支持 https 地址')
    host, path = m.group(1), m.group(2) or '/'
    w = _winhttp()
    s = w.WinHttpOpen(user_agent, WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY, None, None, 0) \
        or w.WinHttpOpen(user_agent, WINHTTP_ACCESS_TYPE_DEFAULT_PROXY, None, None, 0)
    if not s:
        raise UpdateError(f'WinHTTP 初始化失败（{ctypes.get_last_error()}）')
    c = r = None
    try:
        w.WinHttpSetTimeouts(s, timeout_ms, timeout_ms, timeout_ms, timeout_ms)
        c = w.WinHttpConnect(s, host, 443, 0)
        r = c and w.WinHttpOpenRequest(c, 'GET', path, None, None, None, WINHTTP_FLAG_SECURE)
        if not r:
            raise UpdateError(f'连接失败（{ctypes.get_last_error()}）')
        headers = f'Accept: {accept}\r\n'
        if not w.WinHttpSendRequest(r, headers, len(headers), None, 0, 0, 0) or not w.WinHttpReceiveResponse(r, None):
            err = ctypes.get_last_error()
            hint = '，连不上 GitHub（可能需要代理）' if err in (12002, 12007, 12029, 12030) else ''
            raise UpdateError(f'网络请求失败（错误 {err}{hint}）')
        status = wintypes.DWORD(0)
        size = wintypes.DWORD(4)
        w.WinHttpQueryHeaders(r, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER, None,
                              ctypes.byref(status), ctypes.byref(size), None)
        body = bytearray()
        buf = ctypes.create_string_buffer(65536)
        got = wintypes.DWORD(0)
        while len(body) < limit:
            if not w.WinHttpReadData(r, buf, len(buf), ctypes.byref(got)):
                raise UpdateError(f'读取失败（{ctypes.get_last_error()}）')
            if got.value == 0:
                break
            body += buf.raw[:got.value]
        return status.value, bytes(body)
    finally:
        for h in (r, c, s):
            if h:
                w.WinHttpCloseHandle(h)


def parse_version(v):
    nums = re.findall(r'\d+', str(v or ''))
    return tuple(int(x) for x in nums[:4]) if nums else ()


def check(current, user_agent):
    """对比 GitHub 上的最新版本。
    优先看 Releases（有发布说明和 exe 下载）；还没发过 Release 时，读 main 分支代码里的版本号。"""
    status, body = http_get(_API_LATEST, user_agent, accept='application/vnd.github+json')
    if status == 200:
        d = json.loads(body.decode('utf-8'))
        latest = str(d.get('tag_name') or '').lstrip('vV')
        asset = next((a.get('browser_download_url') for a in d.get('assets') or []
                      if str(a.get('name', '')).lower().endswith('.exe')), None)
        info = {'latest': latest, 'url': d.get('html_url') or RELEASES_URL, 'download': asset,
                'notes': (d.get('body') or '').strip()[:2000], 'source': 'release',
                'published': d.get('published_at')}
    elif status == 404:
        status2, body2 = http_get(_RAW_SERVER, user_agent)
        if status2 != 200:
            raise UpdateError(f'GitHub 返回 {status2}')
        m = re.search(r"^VERSION = '([^']+)'", body2.decode('utf-8', 'replace'), re.M)
        if not m:
            raise UpdateError('没在仓库代码里找到版本号')
        info = {'latest': m.group(1), 'url': REPO_URL, 'download': None, 'notes': '', 'source': 'main',
                'published': None}
    elif status == 403:
        raise UpdateError('GitHub 访问太频繁，稍后再试')
    else:
        raise UpdateError(f'GitHub 返回 {status}')
    info['current'] = current
    info['newer'] = parse_version(info['latest']) > parse_version(current)
    info['repo'] = REPO_URL
    return info
