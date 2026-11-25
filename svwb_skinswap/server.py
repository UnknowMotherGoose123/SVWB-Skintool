"""本地界面服务：Python 标准库 HTTP 服务 + Edge 应用窗口（无边框浏览器窗口）。

    python -m svwb_skinswap [--game DIR] [--port N] [--web-dir DIR] [--data-dir DIR] [--demo] [--no-browser]

--demo：只读演示模式，替换/还原只模拟进度，不写任何游戏文件（界面开发用）。
"""
import argparse
import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import updates
from .core import SkinSwapper, detect_game_dirs, find_persistent, game_running, swap_list, swap_record, PART_KEYS

APP_ID = 'svwb-skinswap'
APP_NAME = '影之诗 WB 主战者换皮'
VERSION = '2.1.1'
PREFERRED_PORT = 47321
CLS_RANK = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7, 99: 8, 0: 9}
# 显式指定 MIME，避免 Windows 注册表把 .js/.css 映射错导致页面加载失败
STATIC_TYPES = {
    '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
    '.json': 'application/json; charset=utf-8', '.svg': 'image/svg+xml; charset=utf-8',
    '.png': 'image/png', '.webp': 'image/webp', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.gif': 'image/gif', '.ico': 'image/x-icon', '.woff2': 'font/woff2', '.woff': 'font/woff',
    '.ttf': 'font/ttf', '.otf': 'font/otf', '.mp3': 'audio/mpeg', '.ogg': 'audio/ogg',
}


# ======================================================================= paths
def resource_dir():
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, 'svwb_skinswap')
    return os.path.dirname(os.path.abspath(__file__))


def app_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _writable(d):
    try:
        os.makedirs(d, exist_ok=True)
        test = os.path.join(d, '.write_test')
        with open(test, 'w') as fp:
            fp.write('ok')
        os.remove(test)
        return True
    except OSError:
        return False


def pick_data_dir():
    """数据目录（设置、替换记录、图片缓存、窗口配置）。
    默认 %LOCALAPPDATA%\\SVWBSkinSwap：exe 换位置/放桌面都不会丢替换记录，也不会在桌面生成杂物；
    如果程序旁边已经有 data 文件夹，就用它（便携模式）。"""
    portable = os.path.join(app_dir(), 'data')
    if os.path.isdir(portable) and _writable(portable):
        return portable
    alt = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'SVWBSkinSwap')
    if _writable(alt):
        return alt
    _writable(portable)
    return portable


# ======================================================================= backend
class ApiError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


class Backend:
    def __init__(self, data_dir, demo=False):
        self.data_dir = data_dir
        self.cfg_path = os.path.join(data_dir, 'config.json')
        self.cache_dir = os.path.join(data_dir, 'cache')
        self.demo = demo
        self.lock = threading.RLock()
        self.cfg = self._load_cfg()
        self.sw = None
        self.game_error = None
        self.skins = []
        self.skins_ver = 0
        self.loading = False
        self.task = None
        self.result = None
        self.result_seq = 0
        self.logs = deque(maxlen=600)
        self.log_seq = 0
        self.running = None
        self.img_locks = {}
        self.prewarm = {'done': 0, 'total': 0}
        self.demo_applied = []
        self.health = None          # 最近一次检查：{'need': bool, 'reason': str, 'time': ts}
        self.meta_info = None       # 资源索引是否被游戏重置：{'broken','needGb','canRestore'}
        self.last_seen = time.time()
        self.bye_at = None
        self.quit_event = threading.Event()
        self.app_update = None      # 工具新版本检查结果
        self.app_update_checking = False
        threading.Thread(target=self._watch_game, daemon=True).start()
        if not demo:
            t = threading.Timer(3.0, lambda: self.check_app_update(auto=True))
            t.daemon = True
            t.start()

    # ------------------------------------------------------------ config / log
    def _load_cfg(self):
        cfg = {'game_dir': '', 'auto_rescan': True, 'swaps': []}
        try:
            with open(self.cfg_path, encoding='utf-8') as fp:
                cfg.update(json.load(fp))
        except Exception:
            pass
        cfg['swaps'] = [swap_record(*x) for x in swap_list(cfg.get('swaps', []))]
        return cfg

    def _save_cfg(self):
        os.makedirs(self.data_dir, exist_ok=True)
        tmp = self.cfg_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fp:
            json.dump(self.cfg, fp, ensure_ascii=False, indent=1)
        os.replace(tmp, self.cfg_path)

    def log(self, text, level='info'):
        with self.lock:
            self.log_seq += 1
            self.logs.append({'seq': self.log_seq, 't': time.strftime('%H:%M:%S'), 'level': level, 'text': text})
        try:
            print(f'[{level}] {text}', flush=True)
        except Exception:
            pass

    def _watch_game(self):
        while not self.quit_event.is_set():
            name = None if self.demo else game_running()
            with self.lock:
                exited = False
                if name != self.running:
                    exited = bool(self.running) and not name
                    self.running = name
                    if name:
                        self.log(f'检测到游戏正在运行（{name}），替换/还原前请先退出游戏', 'warn')
                    elif self.sw:
                        self.log('游戏已退出', 'info')
            if exited:
                self.refresh_meta()
            self.quit_event.wait(4)

    # ------------------------------------------------------------ state
    def _applied(self):
        if self.demo:
            return list(self.demo_applied)
        if not self.sw:
            return []
        return [list(x) for x in self.sw.state.get('applied', [])]

    def state(self):
        with self.lock:
            applied = self._applied()
            plan = self.cfg.get('swaps', [])
            return {
                'app': {'id': APP_ID, 'name': APP_NAME, 'version': VERSION, 'demo': self.demo,
                        'dataDir': self.data_dir},
                'game': {'dir': self.cfg.get('game_dir', ''), 'ok': self.sw is not None,
                         'persist': self.sw.persist if self.sw else None,
                         'error': self.game_error, 'running': self.running},
                'options': {'autoRescan': bool(self.cfg.get('auto_rescan', True))},
                'plan': [self._plan_item(x) for x in swap_list(plan)],
                'applied': [self._plan_item(x) for x in swap_list(applied)],
                'parts': PART_KEYS,
                'dirty': (sorted(swap_list(plan), key=repr) != sorted(swap_list(applied), key=repr)
                          or bool(self.sw and not self.demo and self.sw.logic_stale())),
                'skins': {'ver': self.skins_ver, 'loading': self.loading, 'count': len(self.skins)},
                'task': dict(self.task) if self.task else None,
                'result': self.result,
                'logSeq': self.log_seq,
                'prewarm': dict(self.prewarm),
                'health': dict(self.health) if self.health else None,
                'meta': dict(self.meta_info) if self.meta_info else None,
                'appUpdate': dict(self.app_update or {}, checking=self.app_update_checking, current=VERSION),
            }

    @staticmethod
    def _plan_item(x):
        a, b, parts = x
        d = {'target': a, 'source': b}
        if parts is not None:
            d['parts'] = list(parts)          # 只替换这些部分；没有这个字段 = 全部替换
        return d

    def refresh_meta(self):
        sw = self.sw
        if not sw or self.demo:
            return
        try:
            info = sw.meta_status()
        except Exception as ex:
            info = {'broken': False, 'error': str(ex)}
        with self.lock:
            changed = bool(info.get('broken')) != bool(self.meta_info and self.meta_info.get('broken'))
            self.meta_info = info
        if changed and info.get('broken'):
            self.log(f'检测到游戏的资源索引被重置（游戏会要求重新下载约 {info.get("needGb", 0)} GB）。'
                     + ('缓存文件都还在，点顶部的“一键恢复”即可。' if info.get('canRestore') else '没有找到可用的备份。'), 'error')

    def restore_meta(self):
        self._require_game()
        self._require_idle()

        def fn():
            r = self.sw.restore_meta()
            self.refresh_meta()
            summary = f'已恢复资源索引：{r["done"]}/{r["total"]} 个资源可用，剩余待下载 {r["needGb"]} GB'
            self.log(summary, 'ok')
            return summary, {}
        return self._start_task('restore', '恢复索引', fn)

    def logs_after(self, after):
        with self.lock:
            return {'seq': self.log_seq, 'lines': [x for x in self.logs if x['seq'] > after]}

    # ------------------------------------------------------------ game dir
    def set_game_dir(self, path, auto_check=False, quiet=False):
        path = os.path.normpath(str(path).strip().strip('"'))
        if not path or not os.path.isdir(path):
            raise ApiError('目录不存在：' + path)
        if not find_persistent(path):
            raise ApiError('这个目录里没有找到影之诗的资源（应包含 *_Data\\Persistent\\meta）。'
                           '请选择游戏安装目录，例如 E:\\shadowverse')
        if self.task:
            raise ApiError('正在执行：' + self.task['label'] + '，完成后再更换目录', 409)
        sw = SkinSwapper(path, self.data_dir, log=lambda m: self.log(m))
        with self.lock:
            self.sw = sw
            self.game_error = None
            self.health = None
            self.cfg['game_dir'] = path
            self._save_cfg()
        if not quiet:
            self.log(f'已载入游戏目录：{path}', 'ok')
        self._start_catalogue(auto_check)
        return self.state()

    def _start_catalogue(self, auto_check):
        sw = self.sw
        with self.lock:
            self.loading = True

        def run():
            try:
                skins = sw.catalogue()
                skins.sort(key=lambda s: (CLS_RANK.get(s['cls'], 10), s['order'], int(s['id'])))
                with self.lock:
                    if sw is not self.sw:
                        return
                    self.skins = skins
                    self.skins_ver += 1
                n_ok = sum(1 for s in skins if s['downloaded'])
                self.log(f'共 {len(skins)} 个主战者皮肤，其中 {n_ok} 个已下载到本地', 'info')
            except Exception as ex:
                self.log(f'读取皮肤列表失败：{ex}', 'error')
                self.log(traceback.format_exc(), 'debug')
                with self.lock:
                    self.game_error = f'读取皮肤列表失败：{ex}'
            finally:
                with self.lock:
                    self.loading = False
            self.refresh_meta()
            if sw is self.sw and (self.cfg.get('swaps') or self._applied()) \
                    and not (self.meta_info and self.meta_info.get('broken')):
                self.auto_check(allow_apply=auto_check and self.cfg.get('auto_rescan', True))
            self._prewarm(sw)

        threading.Thread(target=run, daemon=True).start()

    def _prewarm(self, sw):
        """后台预先导出缩略图，首屏不用等。"""
        from concurrent.futures import ThreadPoolExecutor
        ids = [s['id'] for s in self.skins]
        with self.lock:
            self.prewarm = {'done': 0, 'total': len(ids)}

        def one(sid):
            if self.quit_event.is_set() or sw is not self.sw:
                return
            for kind in ('thumb', 'icon'):
                try:
                    self.image(kind, sid)
                except Exception:
                    pass
            with self.lock:
                self.prewarm['done'] += 1

        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(one, ids))

    def detect(self):
        return [{'dir': d, 'persist': find_persistent(d)} for d in detect_game_dirs()]

    def browse(self):
        """弹出系统的“选择文件夹”对话框。"""
        result = {}

        def dlg():
            try:
                from .winpick import pick_folder
                init = self.cfg.get('game_dir')
                init = os.path.normpath(init) if init and os.path.isdir(init) else None
                result['path'] = pick_folder('选择影之诗安装目录（包含 *_Data 文件夹的那一层）', init)
            except Exception as ex:
                result['error'] = str(ex)

        t = threading.Thread(target=dlg, daemon=True)
        t.start()
        t.join()
        if result.get('error'):
            raise ApiError('无法打开文件夹选择框：' + result['error'], 500)
        path = result.get('path')
        if not path:
            return {'path': None, 'state': self.state()}
        return {'path': os.path.normpath(path), 'state': self.set_game_dir(path, auto_check=False)}

    # ------------------------------------------------------------ catalogue / preview / images
    def skins_payload(self):
        with self.lock:
            return {'ver': self.skins_ver, 'loading': self.loading, 'skins': self.skins}

    def preview(self, a, b):
        self._require_game()
        if not a or not b or str(a) == str(b):
            raise ApiError('请选择两个不同的皮肤')
        return self.sw.preview(a, b)

    def image(self, kind, sid):
        sw = self.sw
        if not sw or not re.fullmatch(r'\d{1,8}', sid or ''):
            return None
        key = (kind, sid)
        with self.lock:
            lk = self.img_locks.setdefault(key, threading.Lock())
        with lk:
            return sw.export_image(kind, sid, os.path.join(self.cache_dir, 'img'))

    # ------------------------------------------------------------ plan / options
    def set_plan(self, plan):
        swaps, seen = [], set()
        for item in plan or []:
            a, b = str(item.get('target', '')), str(item.get('source', ''))
            if not a or not b or a == b or a in seen:
                continue
            parts = item.get('parts')
            if parts is not None and not isinstance(parts, list):
                raise ApiError('parts 必须是列表')
            seen.add(a)
            swaps.append(swap_record(*swap_list([(a, b, parts)])[0]))
        with self.lock:
            self.cfg['swaps'] = swaps
            self._save_cfg()
        self._kick_precompute(swaps)
        return self.state()

    def _kick_precompute(self, swaps):
        """选好方案就在后台预先算好（结果进缓存），点“一键替换”时只剩写文件。"""
        if self.demo or not self.sw or not swaps:
            return

        def done(sec, sw_list):
            if sec >= 3:
                self.log(f'已在后台预先处理好当前方案（用时 {sec:.0f} 秒），点“一键替换”会很快完成', 'ok')

        try:
            self.sw.precompute([tuple(s) for s in swaps], on_done=done)
        except Exception as ex:
            self.log(f'后台预处理没能启动：{ex}', 'debug')

    def set_options(self, opts):
        with self.lock:
            if 'autoRescan' in opts:
                self.cfg['auto_rescan'] = bool(opts['autoRescan'])
            self._save_cfg()
        return self.state()

    # ------------------------------------------------------------ tasks
    def _require_game(self):
        if not self.sw:
            raise ApiError('请先选择影之诗的安装目录')

    def _require_idle(self):
        if self.running and not self.demo:
            raise ApiError(f'游戏正在运行（{self.running}），请先完全退出游戏再操作', 409)
        if self.task:
            raise ApiError('正在执行：' + self.task['label'] + '，请稍候', 409)

    def _start_task(self, kind, label, fn):
        with self.lock:
            if self.task:
                raise ApiError('正在执行：' + self.task['label'] + '，请稍候', 409)
            self.task = {'kind': kind, 'label': label, 'done': 0, 'total': 0, 'text': '准备中…',
                         'started': time.time()}

        def run():
            ok, summary, extra = True, '', {}
            try:
                summary, extra = fn()
                self._set_health(False, '替换已是最新' if kind == 'apply' else '已还原')
            except Exception as ex:
                ok, summary = False, str(ex)
                self.log(f'{label}失败：{ex}', 'error')
                self.log(traceback.format_exc(), 'debug')
            if kind != 'restore':
                self.refresh_meta()
            with self.lock:
                self.task = None
                self.result_seq += 1
                self.result = {'seq': self.result_seq, 'kind': kind, 'ok': ok, 'summary': summary,
                               'time': time.time(), **(extra or {})}

        threading.Thread(target=run, daemon=True).start()
        return self.state()

    def _progress(self, k, n, text):
        with self.lock:
            if self.task:
                self.task.update(done=k, total=n, text=text)

    def _do_apply(self, swaps):
        if self.demo:
            return self._demo_apply(swaps)
        changed, report = self.sw.apply(swaps, progress=self._progress)
        for line in report:
            self.log(line.strip(), 'info')
        st = getattr(self.sw, 'last_stats', {}) or {}
        summary = f'替换完成：{changed} 个资源 · {st.get("labels", 0)} 条字幕'
        if st.get('evolve') or st.get('native'):
            summary += ' · 豪华进化演出已启用'
        if st.get('too_big'):
            summary += f' · {st["too_big"]} 个因体积放不下跳过'
        if st.get('no_target'):
            summary += f' · {st["no_target"]} 个原皮肤没有对应资源'
        if st.get('skipped'):
            summary += f' · {st["skipped"]} 个未下载已跳过'
        if st.get('failed'):
            summary += f' · {st["failed"]} 个失败'
        self.log(summary + '。启动游戏即可看到效果', 'ok')
        return summary, {k: st.get(k, 0) for k in ('changed', 'labels', 'skipped', 'failed', 'too_big', 'no_target')}

    def _do_revert(self):
        if self.demo:
            n = len(self.demo_applied)
            for k in range(20):
                self._progress(k, 20, '还原中…')
                time.sleep(0.03)
            self.demo_applied = []
            summary = f'已全部还原（演示模式，{n} 组方案）'
        else:
            reverted, skipped = self.sw.revert_all()
            summary = f'已全部还原：{reverted} 个条目' + (f'（{skipped} 个已被游戏更新，无需还原）' if skipped else '')
        self.log(summary, 'ok')
        return summary, {}

    def apply(self):
        self._require_game()
        self._require_idle()
        swaps = [tuple(s) for s in self.cfg.get('swaps', [])]
        if not swaps:
            return self._start_task('revert', '还原', self._do_revert)
        self.log('开始替换：' + '，'.join(f'{self._name(a)} → {self._name(b)}' + ('' if parts is None else f'（只替换 {len(parts)} 部分）')
                                         for a, b, parts in swap_list(swaps)), 'info')
        return self._start_task('apply', '替换', lambda: self._do_apply(swaps))

    def revert(self, clear_plan=True):
        self._require_game()
        self._require_idle()
        if clear_plan:
            with self.lock:
                self.cfg['swaps'] = []
                self._save_cfg()
        return self._start_task('revert', '还原', self._do_revert)

    def _needs_update(self, swaps):
        if self.demo:
            need = sorted(map(list, swaps)) != sorted(self.demo_applied)
            return need, ('替换方案有变化' if need else '没有变化')
        if not swaps and not self._applied():
            return False, '没有设置替换'
        return self.sw.needs_update(swaps)

    def _set_health(self, need, reason):
        with self.lock:
            self.health = {'need': bool(need), 'reason': reason, 'time': time.time()}

    def check(self):
        self._require_game()
        swaps = [tuple(s) for s in self.cfg.get('swaps', [])]
        need, why = self._needs_update(swaps)
        self._set_health(need, why)
        self.log(('需要更新：' if need else '检查完成：') + why, 'warn' if need else 'ok')
        return {'need': need, 'reason': why, 'state': self.state()}

    def auto_check(self, allow_apply=True):
        """载入后自动检查替换是否仍然有效；开启了自动扫描时，需要就自动重新替换。"""
        swaps = [tuple(s) for s in self.cfg.get('swaps', [])]
        if not self.sw:
            return
        try:
            need, why = self._needs_update(swaps)
        except Exception as ex:
            self.log(f'自动扫描失败：{ex}', 'error')
            return
        self._set_health(need, why)
        if not need:
            if swaps:
                self.log('启动检查：替换仍然有效，没有新增资源', 'ok')
            return
        self.log(f'启动检查：{why}', 'warn')
        if not allow_apply or not swaps:
            self.log('点“一键替换”即可重新应用', 'warn')
            return
        if self.running and not self.demo:
            self.log('游戏正在运行，暂不自动更新；退出游戏后点“一键替换”', 'warn')
            return
        try:
            self._start_task('apply', '自动更新', lambda: self._do_apply(swaps))
        except ApiError as ex:
            self.log(str(ex), 'warn')

    def _name(self, sid):
        for s in self.skins:
            if s['id'] == str(sid):
                return f'{s["name"]}({sid})'
        return str(sid)

    def _demo_apply(self, swaps):
        total = 0
        plans = []
        for a, b, _ in swap_list(swaps):
            p = self.sw.preview(a, b)
            plans.append(p)
            total += p['total']
        k = 0
        for p in plans:
            for c in p['categories']:
                for it in c['items']:
                    k += 1
                    self._progress(k, total, it['name'])
                    time.sleep(0.035)
        self.demo_applied = [list(s) for s in swaps]
        summary = f'替换完成（演示模式，未写入游戏）：{total} 项'
        self.log(summary, 'ok')
        return summary, {'changed': total, 'labels': 0, 'skipped': 0, 'failed': 0}

    # ------------------------------------------------------------ 工具新版本
    def check_app_update(self, auto=False):
        """后台对比 GitHub 上的版本（启动时自动查一次；界面上点版本号可以再查）。"""
        with self.lock:
            if self.app_update_checking:
                return self.state()
            self.app_update_checking = True

        def run():
            try:
                info = updates.check(VERSION, f'SVWBSkinSwap/{VERSION}')
                info['time'] = time.time()
                if info['newer']:
                    self.log(f'发现新版本 v{info["latest"]}（当前 v{VERSION}），点左上角的版本号可以打开下载页', 'warn')
                elif not auto:
                    self.log(f'已是最新版本（v{VERSION}）', 'ok')
            except Exception as ex:
                info = {'error': str(ex), 'time': time.time()}
                if not auto:
                    self.log(f'检查新版本失败：{ex}', 'warn')
            with self.lock:
                self.app_update = info
                self.app_update_checking = False

        threading.Thread(target=run, daemon=True).start()
        return self.state()

    def open_path(self, what):
        if what == 'release':
            # 只打开本仓库的页面（地址来自版本检查结果）
            info = self.app_update or {}
            url = info.get('url') or updates.RELEASES_URL
            if not str(url).startswith(updates.REPO_URL):
                url = updates.RELEASES_URL
            os.startfile(url)
            return {'ok': True}
        path = {'game': self.cfg.get('game_dir'), 'data': self.data_dir}.get(what)
        if not path or not os.path.isdir(path):
            raise ApiError('目录不存在')
        os.startfile(path)
        return {'ok': True}


# ======================================================================= HTTP
class Handler(BaseHTTPRequestHandler):
    backend: Backend = None
    web_dir: str = None
    server_version = 'SVWBSkinSwap/' + VERSION
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        pass

    # ---------------------------------------------------------------- helpers
    def _send(self, status, body: bytes, ctype, headers=None):
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self._send(status, body, 'application/json; charset=utf-8', {'Cache-Control': 'no-store'})

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode('utf-8') or '{}')
        except Exception:
            raise ApiError('请求格式错误')

    def _touch(self):
        self.backend.last_seen = time.time()

    # ---------------------------------------------------------------- GET
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(url.path)
        qs = urllib.parse.parse_qs(url.query)
        b = self.backend
        try:
            if path.startswith('/api/'):
                self._touch()
                if path == '/api/ping':
                    return self._json({'app': APP_ID, 'version': VERSION})
                if path == '/api/state':
                    return self._json(b.state())
                if path == '/api/skins':
                    return self._json(b.skins_payload())
                if path == '/api/log':
                    return self._json(b.logs_after(int((qs.get('after') or ['0'])[0] or 0)))
                if path == '/api/preview':
                    return self._json(b.preview((qs.get('target') or [''])[0], (qs.get('source') or [''])[0]))
                if path == '/api/detect':
                    return self._json({'dirs': b.detect()})
                raise ApiError('未知接口', 404)
            m = re.fullmatch(r'/img/([a-z]+)/(\d{1,8})(?:\.webp)?', path)
            if m:
                return self._image(m.group(1), m.group(2))
            return self._static(path)
        except ApiError as ex:
            self._json({'error': str(ex)}, ex.status)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as ex:
            traceback.print_exc()
            try:
                self._json({'error': f'内部错误：{ex}'}, 500)
            except Exception:
                pass

    def _image(self, kind, sid):
        p = self.backend.image(kind, sid)
        if not p:
            return self._send(404, b'', 'image/webp', {'Cache-Control': 'max-age=60'})
        etag = '"' + os.path.basename(p) + '"'
        if self.headers.get('If-None-Match') == etag:
            self.send_response(304)
            self.send_header('ETag', etag)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        with open(p, 'rb') as fp:
            data = fp.read()
        self._send(200, data, 'image/webp', {'Cache-Control': 'max-age=86400', 'ETag': etag})

    def _static(self, path):
        if path in ('', '/'):
            path = '/index.html'
        root = os.path.abspath(self.web_dir)
        full = os.path.abspath(os.path.join(root, path.lstrip('/').replace('/', os.sep)))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            return self._send(404, '找不到页面'.encode('utf-8'), 'text/plain; charset=utf-8')
        ext = os.path.splitext(full)[1].lower()
        ctype = STATIC_TYPES.get(ext) or mimetypes.guess_type(full)[0] or 'application/octet-stream'
        with open(full, 'rb') as fp:
            data = fp.read()
        self._send(200, data, ctype, {'Cache-Control': 'no-cache'})

    # ---------------------------------------------------------------- POST
    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        b = self.backend
        self._touch()
        try:
            if path == '/api/bye':
                b.bye_at = time.time()
                return self._json({'ok': True})
            body = self._body()
            if path == '/api/dir':
                return self._json(b.set_game_dir(body.get('path', ''), auto_check=False))
            if path == '/api/browse':
                return self._json(b.browse())
            if path == '/api/plan':
                return self._json(b.set_plan(body.get('plan')))
            if path == '/api/options':
                return self._json(b.set_options(body))
            if path == '/api/apply':
                return self._json(b.apply())
            if path == '/api/revert':
                return self._json(b.revert(clear_plan=body.get('clearPlan', True)))
            if path == '/api/check':
                return self._json(b.check())
            if path == '/api/app_update':
                return self._json(b.check_app_update())
            if path == '/api/restore_meta':
                return self._json(b.restore_meta())
            if path == '/api/open':
                return self._json(b.open_path(body.get('what')))
            if path == '/api/quit':
                b.quit_event.set()
                return self._json({'ok': True})
            raise ApiError('未知接口', 404)
        except ApiError as ex:
            self._json({'error': str(ex)}, ex.status)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as ex:
            traceback.print_exc()
            try:
                self._json({'error': f'内部错误：{ex}'}, 500)
            except Exception:
                pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


# ======================================================================= window
def find_browser():
    cands = []
    for env in ('ProgramFiles(x86)', 'ProgramFiles', 'LOCALAPPDATA'):
        base = os.environ.get(env)
        if base:
            cands.append(os.path.join(base, 'Microsoft', 'Edge', 'Application', 'msedge.exe'))
    for env in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA'):
        base = os.environ.get(env)
        if base:
            cands.append(os.path.join(base, 'Google', 'Chrome', 'Application', 'chrome.exe'))
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def open_window(url, data_dir):
    """用 Edge/Chrome 的应用模式打开（像独立程序一样的窗口）；没有就用默认浏览器。返回进程或 None。"""
    exe = find_browser()
    if exe:
        profile = os.path.join(data_dir, 'window')
        args = [exe, f'--app={url}', f'--user-data-dir={profile}', '--no-first-run', '--no-default-browser-check',
                '--disable-features=Translate,msEdgeSidebarV2,msHubApps', '--disable-background-mode',
                '--window-size=1400,900']
        try:
            return subprocess.Popen(args, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except OSError:
            pass
    import webbrowser
    webbrowser.open(url)
    return None


def _ping(port):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/ping', timeout=1.5) as r:
            return json.loads(r.read().decode('utf-8')).get('app') == APP_ID
    except Exception:
        return False


def selftest(args):
    """不开窗口：载入游戏、读皮肤列表、导出一张图并记录完整报错，用于排查打包/环境问题。"""
    data_dir = args.data_dir or pick_data_dir()
    os.makedirs(data_dir, exist_ok=True)
    out = os.path.join(data_dir, 'selftest.txt')
    lines = [f'{APP_NAME} v{VERSION} 自检 {time.strftime("%Y-%m-%d %H:%M:%S")}', f'frozen={getattr(sys, "frozen", False)}',
             f'python={sys.version}']
    try:
        game = args.game or Backend(data_dir)._load_cfg().get('game_dir') or (detect_game_dirs() or [''])[0]
        lines.append(f'game={game}')
        sw = SkinSwapper(game, data_dir)
        skins = sw.catalogue()
        lines.append(f'skins={len(skins)}')
        sid = next(s['id'] for s in skins if s['downloaded'])
        from PIL import features
        lines.append(f'webp={features.check("webp")}')
        from .core import IMAGE_KINDS
        rows = sw._orig_rows()
        tpl, _ = IMAGE_KINDS['thumb'][0]
        row = rows[tpl.format(id=sid)]
        import UnityPy
        env = UnityPy.load(sw._read_row_file(row))
        for obj in env.objects:
            if obj.type.name == 'Texture2D':
                tex = obj.read()
                lines.append(f'texture={tex.m_Name} {tex.m_Width}x{tex.m_Height} fmt={tex.m_TextureFormat}')
                img = tex.image
                lines.append(f'decoded={img.size} {img.mode}')
                p = os.path.join(data_dir, 'selftest.webp')
                img.save(p, 'WEBP')
                lines.append(f'saved={p}')
                break
        # Spine 骨骼解析（嘴部修正要用）
        from . import spine
        from .core import _split_textasset
        for s in skins:
            row = rows.get(f'Prefabs/LeaderSkin/class_{s["id"]}')
            if not s['downloaded'] or not row:
                continue
            env = UnityPy.load(sw._read_row_file(row))
            for obj in env.objects:
                if obj.type.name == 'TextAsset':
                    name, script, _ = _split_textasset(obj.get_raw_data())
                    if name == f'class_{s["id"]}.skel':
                        sk = spine.Skel(script)
                        lines.append(f'spine={name} {sk.version} skins={sk.skin_names()} anims={len(sk.animations)}')
            break
        # 视频重编码：游戏里找一个 USM，解码几帧再编码成 VP9
        from . import usm
        import av
        from av.video.frame import PictureType
        lines.append(f'av={av.__version__}')
        for n, r in sorted(rows.items()):
            if n.endswith('.usm') and '/Low/BattleDungeon/' in n:
                try:
                    ch = usm.parse(sw._read_row_file(r))
                except Exception:
                    continue
                ht = usm.UtfTable(usm._section(ch, b'@SFV', 1, b'VIDEO_HDRINFO').body)
                info = {c[0]: ht.get(c[0]) for c in ht.cols}
                cc = av.CodecContext.create('libvpx-vp9', 'w')
                cc.width, cc.height, cc.pix_fmt = info[b'width'], info[b'height'], 'yuv420p'
                from fractions import Fraction
                cc.time_base = Fraction(1, 30)
                got = 0
                for i, f in enumerate(usm._decode_frames(ch, info)):
                    if i >= 3:
                        break
                    f.pts, f.pict_type = i, PictureType.NONE
                    got += len(cc.encode(f))
                got += len(cc.encode(None))
                lines.append(f'usm={n} codec={info[b"mpeg_codec"]} {info[b"width"]}x{info[b"height"]} vp9_packets={got}')
                break
        # 多进程 BC7 编码：结果必须和单进程逐字节一致
        from . import texfit
        from UnityPy.export.Texture2DConverter import image_to_texture2d
        from PIL import Image
        probe = Image.frombytes('RGBA', (2048, 2048), bytes((i * 37) & 0xFF for i in range(2048 * 2048 * 4)))
        t0 = time.time()
        par = texfit.encode_image(probe, 25)
        t1 = time.time()
        single, _ = image_to_texture2d(probe, 25)
        texfit.shutdown_pool()
        lines.append(f'bc7_parallel={"identical" if par == single else "MISMATCH"} {t1 - t0:.1f}s')
        lines.append('OK')
    except Exception:
        lines.append(traceback.format_exc())
    with open(out, 'w', encoding='utf-8') as fp:
        fp.write('\n'.join(lines) + '\n')


def main(argv=None):
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument('--game', help='影之诗安装目录')
    ap.add_argument('--port', type=int, default=0)
    ap.add_argument('--web-dir')
    ap.add_argument('--data-dir')
    ap.add_argument('--demo', action='store_true')
    ap.add_argument('--no-browser', action='store_true')
    ap.add_argument('--selftest', action='store_true', help='自检：读取皮肤并导出图片，结果写入 data/selftest.txt')
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest(args)

    data_dir = args.data_dir or pick_data_dir()
    os.makedirs(data_dir, exist_ok=True)
    web_dir = args.web_dir or os.path.join(resource_dir(), 'web')
    if sys.stdout is None or sys.stderr is None:
        # 无控制台的 exe：把输出写进日志文件，方便排查
        try:
            fp = open(os.path.join(data_dir, 'app.log'), 'a', encoding='utf-8', buffering=1)
            fp.write(f'\n===== {time.strftime("%Y-%m-%d %H:%M:%S")} 启动 =====\n')
            sys.stdout = sys.stdout or fp
            sys.stderr = sys.stderr or fp
        except OSError:
            pass

    port = args.port or PREFERRED_PORT
    if not args.port and _ping(port):
        # 已经开着一个了：直接再打开一个窗口
        open_window(f'http://127.0.0.1:{port}/', data_dir)
        return
    try:
        httpd = Server(('127.0.0.1', port), Handler)
    except OSError:
        if args.port:
            raise
        httpd = Server(('127.0.0.1', 0), Handler)
    port = httpd.server_address[1]
    backend = Backend(data_dir, demo=args.demo)
    Handler.backend = backend
    Handler.web_dir = web_dir
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{port}/'
    backend.log(f'{APP_NAME} v{VERSION} 已启动' + ('（演示模式：不会写入游戏文件）' if args.demo else ''), 'info')

    # 载入游戏目录：命令行 > 上次使用 > 自动检测
    try:
        if args.game:
            backend.set_game_dir(args.game, auto_check=True)
        elif backend.cfg.get('game_dir') and find_persistent(backend.cfg['game_dir']):
            backend.set_game_dir(backend.cfg['game_dir'], auto_check=True)
        else:
            dirs = detect_game_dirs()
            if len(dirs) == 1:
                backend.log('自动检测到游戏目录：' + dirs[0], 'ok')
                backend.set_game_dir(dirs[0], auto_check=True, quiet=True)
            elif dirs:
                backend.log('检测到多个影之诗目录，请在界面里选择要用的那个', 'warn')
            else:
                backend.log('请先选择影之诗的安装目录', 'warn')
    except Exception as ex:
        backend.game_error = str(ex)
        backend.log(str(ex), 'error')

    if args.no_browser:
        print(url, flush=True)
        backend.quit_event.wait()
    else:
        proc = open_window(url, data_dir)
        started = time.time()
        while not backend.quit_event.wait(1.0):
            idle = time.time() - backend.last_seen
            if proc is not None and proc.poll() is None:
                continue                      # 窗口还开着
            if time.time() - started < 20:
                continue                      # 给窗口启动/握手留时间
            if backend.task:
                continue                      # 正在替换时不退出
            if backend.bye_at and idle > 6:
                break                         # 页面关闭了且没有新的心跳
            if proc is not None and idle > 10:
                break                         # 窗口进程已退出
            if proc is None and idle > 600:
                break                         # 默认浏览器模式：10 分钟没有心跳
    backend.quit_event.set()
    httpd.shutdown()


if __name__ == '__main__':
    main()
