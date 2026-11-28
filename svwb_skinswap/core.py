"""影之诗 Worlds Beyond 主战者皮肤替换 —— 核心逻辑。

思路：游戏的资源索引是 Persistent/meta（SQLite），按资源名找到 dat/ 下的缓存文件。
把「来源皮肤 B」的每个资源改写成「目标皮肤 A」的名字（包内容器路径、Wwise 银行/事件 ID、
口型数据名、CAB 名等），然后**原地**写进 A 自己的缓存文件（大小保持完全一致），meta 一字不改。
被改的原文件先备份，还原时原样拷回。字幕在 Master/mastermemory.bytes 中同理处理。
"""
import base64
import glob
import hashlib
import json
import os
import re
import shutil
import sqlite3
import struct
import threading
import time
import warnings

warnings.filterwarnings('ignore')

import UnityPy  # noqa: E402

import math  # noqa: E402

from .crypto import xor_file, checksum  # noqa: E402
from .texfit import repaint_bundle, shrink_bundle, merge_cabs, has_textures, can_resize, shutdown_pool  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
import threading  # noqa: E402
from .texfit import estimate_lzma_size  # noqa: E402
from .master import MasterMemory  # noqa: E402
from . import wwise  # noqa: E402
from . import spine  # noqa: E402
from . import usm  # noqa: E402
from . import hotfix  # noqa: E402

UnityPy.config.FALLBACK_UNITY_VERSION = "2022.3.0f1"

MASTER_NAME = 'Master/mastermemory.bytes'

# 皮肤相关资源（名字里必须同时带有皮肤 ID）
ASSET_PATTERNS = [re.compile(p) for p in (
    r'^Prefabs/LeaderSkin/',
    r'^Battle/Textures/LeaderSkin',
    r'^Battle/Effect/Prefab/(cmn_high_evo|cmn_high_evo_wipe|skn_class)_\d+(_|$)',
    r'^Battle/Timeline/(Evolve_\d+$|Excite_\d+$|skin_class_\d+_)',
    r'^Battle/BattleDungeon/',
    r'^Card/Header/log_leader_\d+$',
    r'^UI/(ClassChar|Deck|LeaderSkinThumbnail|LeaderSkinFrame|BattleLeaderSkinBG|LeaderSkinHologram|'
    r'Shop/ShopLeaderSkinThumbnail|ArtCollection/Textures/LeaderConfront|BattleDungeon|BattlePass)/',
    r'^movie/[^/]+/BattleDungeon/BossSkinMovie_\d+\.usm$',
    r'^Sv2Warchess/(SkillIcon/HeroSkill_|UI/HeroEmblem/)',
    r'^Prefabs/UI/HomeIllustration/hi_\d+$',
    r'^UI/Home/utx_pict_Illustration_\d+$',
    r'^UI/ArtCollection/Textures/BG/bg_artcollection_home_illustration_\d+$',
    r'^sound/Windows/d/[^/]+/(dx_btl|dx_fesbattle_button|dx_fesbattle_npc|dx_home)_\d+\.pck$',
    r'^sound/Windows/f/(fx_evo|fx_mt_cl|bg_oneshot_home|bg_oneshot_home02|bg_loop_home)_\d+\.pck$',
)]

# 字幕/名字等文本标签
LABEL_PATTERN = r'^(ET_[A-Za-z0-9]+_|ET_[A-Za-z0-9]+_[A-Za-z0-9]+_|LSPT_[NCI]|HomeIllustMessageText_dx_home_|HomeIllustrationName_){id}(_ENG|_\d+)?$'


CLASS_NAMES = {0: '中立', 1: '精灵', 2: '皇家护卫', 3: '巫师', 4: '龙族', 5: '梦魇', 6: '主教', 7: '超越者', 99: '特殊'}

# 资源分类（预览和界面展示用），按顺序匹配
CATEGORIES = [
    ('model', '主战者模型', re.compile(r'^Prefabs/LeaderSkin/class_')),
    ('vs', '对战立绘', re.compile(r'^(Prefabs/LeaderSkin/vs_|Battle/Textures/LeaderSkinVs)')),
    ('evolve', '进化演出', re.compile(r'^Battle/(Effect/Prefab|Timeline)/')),
    ('evo_point', '进化点插图', re.compile(r'^UI/LeaderSkinFrame/utx_base_btl_evo_frame_')),
    ('battle_ui', '战斗界面', re.compile(r'^(UI/LeaderSkinFrame/utx_base_btl_leader_|UI/BattleLeaderSkinBG/|Battle/Textures/LeaderSkin(Low|/Mask))')),
    ('portrait', '头像与卡组', re.compile(r'^(UI/(ClassChar|Deck|LeaderSkinThumbnail|LeaderSkinHologram|Shop|ArtCollection)/|Card/Header/)')),
    ('voice', '语音', re.compile(r'^sound/Windows/d/')),
    ('sfx', '音效', re.compile(r'^sound/Windows/f/')),
    ('other', '其他', re.compile(r'.')),
]

# 可以单独选择是否替换的“部分”：资源类别 + 字幕与名字（主数据）
PART_KEYS = [k for k, _, _ in CATEGORIES] + ['subtitle']


def norm_parts(parts):
    """None / 全选 → None（全部替换）；否则排好序的元组（只保留认识的部分）。"""
    if parts is None:
        return None
    sel = {str(x) for x in parts} & set(PART_KEYS)
    return None if sel == set(PART_KEYS) else tuple(sorted(sel))


def wants(parts, key):
    return parts is None or key in parts


def swap_list(swaps):
    """[(A, B) 或 (A, B, parts)] → [(A, B, parts)]，去掉 A==B。"""
    out = []
    for s in swaps:
        a, b = str(s[0]), str(s[1])
        if a != b:
            out.append((a, b, norm_parts(s[2] if len(s) > 2 else None)))
    return out


def swap_record(a, b, parts):
    """写进配置/状态文件的形式：全部替换时仍是 [A, B]（兼容旧版）。"""
    return [a, b] if parts is None else [a, b, list(parts)]


# 界面用图片：kind -> [(资源名模板, 最大尺寸或 None)]，按顺序找第一个存在的
IMAGE_KINDS = {
    'thumb': [('UI/LeaderSkinThumbnail/utx_base_leaderskin_thumbnail_{id}', (342, 370)),
              ('UI/ClassChar/utx_pict_leader_{id}_00', (370, 370))],
    'art': [('UI/ClassChar/utx_pict_leader_{id}_00', None),
            ('Battle/Textures/LeaderSkinVs/vs_{id}_left', None)],
    'icon': [('UI/ClassChar/utx_btn_class_leader_{id}', None),
             ('UI/ClassChar/utx_btn_leader_skin_{id}', (200, 200))],
    'medal': [('UI/ClassChar/utx_btn_leader_skin_{id}', (290, 290))],
    'vs': [('Battle/Textures/LeaderSkinVs/vs_{id}_left', None)],
    'evo': [('UI/LeaderSkinFrame/utx_base_btl_evo_frame_{id}_active', None)],
    'evosuper': [('UI/LeaderSkinFrame/utx_base_btl_evo_frame_{id}_super_active', None)],
    'deck': [('UI/Deck/utx_tex_deck_set_leader_{id}_char', None)],
    'header': [('Card/Header/log_leader_{id}', None)],
    'card': [('Card/Header/log_{id}0', None)],                 # 卡牌在卡组列表里的横条图（普通卡 ID）
}


# 主数据里按皮肤 ID 记录立绘摆放参数的表：表名 -> (模式, 跟随的部分)
#   RandomMatchLeaderSkinTransform：匹配/练习/大奖赛等界面摆放的是“对战立绘”（vs_ 图），参数跟着它走
#   HomeIllustrationMaster：主界面看板（hi_ 预制体，归在“其他”）
#   mirror：来源没有专门参数时，原皮肤也去掉（用默认）；copy：只在来源有参数时照抄
TRANSFORM_TABLES = {'RandomMatchLeaderSkinTransform': ('mirror', 'vs'), 'HomeIllustrationMaster': ('copy', 'other')}

# LeaderSkinMaster[6] SkinType：1=豪华进化（带专属边框） 2=只有专属边框 其它=普通。
# 专属边框时，战斗边框/遮罩/进化点框/进化音效库都按 BaseSkinId（为 0 时用自身 ID）加载（反编译 View.dll 确认）
SPECIAL_FRAME_TYPES = (1, 2)
FRAME_TYPE_ONLY = 2
FRAME_ASSET = re.compile(r'^(UI/LeaderSkinFrame/utx_base_btl_(leader|evo_frame)_\d+(_|$)|'
                         r'Battle/Textures/LeaderSkin/Mask/utx_encampment_mask_\d+$)')
FRAME_PARTS = ('battle_ui', 'evo_point')

# 替换逻辑的版本：改法变了就加一，已替换过的方案会被提示/自动重新替换
#   2：战斗边框/立绘摆放（只比主数据）
#   3：模型自带的进化时间轴/特效改为并进模型主文件（旧版单独塞进包里，游戏找不到）
#   4：并进来的对象补进模型包的预加载表（v2.1.3 漏了，游戏里时间轴建不起来）
#   5：图片 Sprite 区域/网格跟着来源换；放不下先降分辨率（旧版降颜色精度，卡面出色块）
APPLY_LOGIC = 5
MERGE_LOGIC = 4
IMAGE_LOGIC = 5


def is_special(row):
    return bool(row) and len(row) > 9 and row[6] in SPECIAL_FRAME_TYPES


def frame_target(ra, rb, parts):
    """原皮肤 ra 按来源 rb 显示时，主数据里 (SkinType, BaseSkinId) 应该是什么；不需要管返回 None。
    - 选了进化演出：照搬来源的类型，并指向来源（豪华进化会连带专属边框）
    - 只选了战斗界面/进化点插图：原皮肤自己没有专属边框而来源有 → 改成“只有专属边框”并指向来源，
      这样边框、遮罩、进化点框都直接用来源的原生资源，进化演出保持原样
      （原皮肤本来就有专属边框时，直接把它自己的边框图片换掉即可，不用改主数据）"""
    if not ra or not rb or len(ra) < 10 or len(rb) < 10:
        return None
    base_b = rb[9] or rb[0]
    base = base_b if base_b != ra[0] else 0
    if wants(parts, 'evolve'):
        return rb[6], base
    if any(wants(parts, k) for k in FRAME_PARTS) and is_special(rb) and not is_special(ra):
        return FRAME_TYPE_ONLY, base
    return None


def frame_native(ra, rb, parts):
    """来源的专属边框类资源（战斗边框/遮罩/进化点框）是否会经 BaseSkinId 直接被原皮肤用上。"""
    t = frame_target(ra, rb, parts)
    return bool(t) and t[0] in SPECIAL_FRAME_TYPES and bool(t[1])

# 图片类资源（用“重绘”写进原皮肤自己的纹理）
IMAGE_PREFIXES = ('UI/', 'Card/', 'Battle/Textures/', 'Battle/BattleDungeon/', 'Sv2Warchess/')

# 对战时按 BaseSkinId 加载的进化特效：改了主数据的 BaseSkinId 后，游戏直接用来源皮肤自己的这些资源
BASE_ID_ASSET = re.compile(r'^(Battle/Effect/Prefab/cmn_high_evo_\d+_\d+|sound/Windows/f/fx_evo_\d+\.pck)$')


def model_bound(rows, deps, b):
    """来源模型引用的皮肤资源（进化时间轴 Evolve_B、时间轴里生成的特效 skn_class_B_* 等）。
    游戏不按名字加载它们，而是模型里的导演组件直接引用，所以它们算模型的一部分：
    跟着“主战者模型”一起换，并进模型的主文件（原皮肤多半没有位置放，有也常常放不下）。
    返回 (资源名集合, 来源模型名)。"""
    mb = f'Prefabs/LeaderSkin/class_{b}'
    return {d for d in deps.get(mb, ()) if d in rows and d != mb and is_skin_asset(d, b)}, mb


def short(name):
    """资源名的最后两段，日志里用（避免同名资源混淆）。"""
    return '/'.join(str(name).split('/')[-2:])


def categorize(name):
    for key, _, pat in CATEGORIES:
        if pat.search(name):
            return key
    return 'other'


def tok(sid):
    return re.compile(r'(?<!\d)' + re.escape(str(sid)) + r'(?!\d)')


def rep(s, b, a):
    return tok(b).sub(str(a), s)


def is_skin_asset(name, sid):
    return bool(tok(sid).search(name)) and any(p.search(name) for p in ASSET_PATTERNS)


# ======================================================================= game paths
def find_persistent(game_dir):
    """接受游戏根目录 / *_Data 目录 / Persistent 目录，返回 Persistent 路径。"""
    game_dir = os.path.abspath(game_dir)
    cands = [game_dir, os.path.join(game_dir, 'Persistent')]
    cands += glob.glob(os.path.join(game_dir, '*_Data', 'Persistent'))
    for c in cands:
        if os.path.isfile(os.path.join(c, 'meta')) and os.path.isdir(os.path.join(c, 'dat')):
            return c
    return None


def game_running():
    """返回正在运行的游戏进程名，没有则 None。"""
    try:
        import subprocess
        import sys
        own = os.path.basename(sys.executable).lower()
        out = subprocess.run(['tasklist', '/fo', 'csv', '/nh'], capture_output=True, text=True,
                             encoding='mbcs', errors='ignore', timeout=10,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout
        for line in out.splitlines():
            name = line.split('","')[0].strip('"')
            low = name.lower()
            if low == own or '换皮' in name or 'skinswap' in low:
                continue          # 本工具自己
            if '影之诗' in name or 'shadowverse' in low:
                return name
    except Exception:
        pass
    return None


def detect_game_dirs():
    """在各个盘的前两层目录里找影之诗（含 *_Data/Persistent/meta 的目录）。"""
    import string
    found = []
    keys = ('shadowverse', '影之诗', 'svwb')
    for letter in string.ascii_uppercase[2:]:
        root = f'{letter}:\\'
        if not os.path.isdir(root):
            continue
        try:
            level1 = [os.path.join(root, d) for d in os.listdir(root)]
        except OSError:
            continue
        cands = []
        for d in level1:
            low = os.path.basename(d).lower()
            if any(k in low for k in keys):
                cands.append(d)
            elif os.path.isdir(d) and low in ('games', 'game', 'program files', 'program files (x86)', 'netease', 'mumu', '游戏'):
                try:
                    cands += [os.path.join(d, x) for x in os.listdir(d) if any(k in x.lower() for k in keys)]
                except OSError:
                    pass
        for c in cands:
            if os.path.isdir(c) and find_persistent(c):
                found.append(os.path.normpath(c))
    return found


# ======================================================================= helpers
def new_hash(content: bytes, salt: str) -> str:
    d = hashlib.sha1(salt.encode() + content).digest()
    return base64.b32encode(d).decode()[:26]


def _rename_raw_name(raw: bytes, new_name: str) -> bytes:
    """Object whose first field is m_Name (string, 4-aligned)."""
    n = struct.unpack_from('<i', raw, 0)[0]
    end = 4 + n
    end += (-end) % 4
    nb = new_name.encode('utf-8')
    head = struct.pack('<i', len(nb)) + nb
    head += b'\x00' * ((-len(head)) % 4)
    return head + raw[end:]


def _split_textasset(raw: bytes):
    """TextAsset 原始数据 → (名字, 内容, 内容之后的剩余字节)。"""
    n = struct.unpack_from('<i', raw, 0)[0]
    p = 4 + n
    p += (-p) % 4
    m = struct.unpack_from('<i', raw, p)[0]
    script = raw[p + 4:p + 4 + m]
    q = p + 4 + m
    q += (-q) % 4
    return raw[4:4 + n].decode('utf-8', 'ignore'), script, raw[q:]


def _join_textasset(name: str, script: bytes, rest: bytes) -> bytes:
    nb = name.encode('utf-8')
    out = struct.pack('<i', len(nb)) + nb
    out += b'\x00' * ((-len(out)) % 4)
    out += struct.pack('<i', len(script)) + script
    out += b'\x00' * ((-len(out)) % 4)
    return out + rest


def rename_spine_strings(skel: bytes, b, a) -> bytes:
    """Spine 二进制骨骼里按皮肤 ID 命名的口型动画（dx_btl_{id}_xx_x_jpn 等）改成目标 ID。
    Spine 字符串是“长度+1（变长整数）+ UTF-8”，改名会改变长度，所以按长度前缀精确改写。"""
    t = tok(b)
    printable = re.compile(rb'[\x21-\x7e]+')
    out = bytearray()
    i = 0
    while True:
        p = skel.find(b'dx_', i)
        if p < 0:
            break
        done = False
        # 字符串可能带前缀（如 “1_JP/dx_btl_1242_01_1_jpn”），向前找长度字节
        for k in range(1, 48):
            q = p - k
            if q < i:
                break
            L = skel[q] - 1
            end = q + 1 + L
            if L <= 0 or L >= 127 or end <= p + 3 or end > len(skel):
                continue
            s = skel[q + 1:end]
            if not printable.fullmatch(s):
                continue
            text = s.decode('ascii')
            if not t.search(text):
                break
            new = rep(text, b, a).encode('ascii')
            if len(new) >= 127:
                break
            out += skel[i:q] + bytes([len(new) + 1]) + new
            i = end
            done = True
            break
        if not done:
            out += skel[i:p + 3]
            i = p + 3
    out += skel[i:]
    return bytes(out)


def bundle_cabs(data: bytes):
    env = UnityPy.load(data)
    return [k for k in env.file.files.keys() if k.upper().startswith('CAB-') and '.' not in k]


def rewrite_bundle(data: bytes, b, a, cab_map) -> bytes:
    env = UnityPy.load(data)
    f = env.file
    flags = (int(f.dataflags), int(getattr(f, '_block_info_flags', 3)))
    t = tok(b)
    for obj in env.objects:
        tn = obj.type.name
        if tn == 'AssetBundle':
            tt = obj.read_typetree()
            tt['m_Name'] = rep(tt['m_Name'], b, a)
            if 'm_AssetBundleName' in tt:
                tt['m_AssetBundleName'] = rep(tt['m_AssetBundleName'], b, a)
            tt['m_Container'] = [(rep(k, b, a), v) for k, v in tt['m_Container']]
            obj.save_typetree(tt)
        elif tn == 'TextAsset':
            raw = obj.get_raw_data()
            name, script, rest = _split_textasset(raw)
            if name.startswith('dx_') and t.search(name):   # 口型/台词数据，代码按台词名查找
                obj.set_raw_data(_join_textasset(rep(name, b, a), script, rest))
            elif name.endswith('.skel'):
                new_script = script
                if b'dx_' in new_script:
                    # Spine 骨骼里的口型事件名（游戏按“当前皮肤 ID”拼台词名）
                    new_script = rename_spine_strings(new_script, b, a)
                if name == f'class_{b}.skel':
                    # 对战/进化里游戏按 A 的 ID 写死 Spine 皮肤名（default 或 JP），
                    # B 的嘴只在 JP/US 皮肤里时会整张嘴消失，这里把皮肤结构调成 A 的规则能用的样子
                    try:
                        fitted, _note = spine.fit_language_skins(new_script, a)
                        if fitted is not None:
                            new_script = fitted
                    except Exception:
                        pass
                if new_script != script:
                    obj.set_raw_data(_join_textasset(name, new_script, rest))
    raw = f.save(packer='none')
    for old, new in cab_map.items():
        raw = raw.replace(old.encode(), new.encode())
        raw = raw.replace(old.lower().encode(), new.lower().encode())
    env2 = UnityPy.load(raw)
    try:
        return env2.file.save(packer=flags)
    except Exception:
        return env2.file.save(packer='lz4')


def _bank_name(asset_name):
    return os.path.basename(asset_name)[:-4]  # strip .pck


def rewrite_pck(data_b: bytes, name_b, name_a, b, a, data_a=None):
    """把 B 的 Wwise 包改写成 A 的银行 ID，并为 A 的事件名添加别名。返回 (bytes, 统计)。"""
    pk = wwise.Pck(data_b)
    bank_b, bank_a = _bank_name(name_b), _bank_name(name_a)
    new_bank_id = wwise.fnv1(bank_a)
    old_bank_id = wwise.fnv1(bank_b)
    a_events = []
    if data_a:
        try:
            pa = wwise.Pck(data_a)
            for bk in pa.banks:
                a_events += wwise.bank_event_ids(bk['payload'])
        except Exception:
            a_events = []
    stats = {'events': 0, 'mapped': 0, 'fallback': 0}
    for bk in pk.banks:
        ev_b = wwise.bank_event_ids(bk['payload'])
        res_b = wwise.resolve_events(bank_b, str(b), ev_b)          # id -> template
        res_a = wwise.resolve_events(bank_a, str(a), a_events)      # id -> template
        tpl_to_b = {tpl: i for i, tpl in res_b.items()}
        aliases = {}
        for i, tpl in res_b.items():
            aliases[wwise.fnv1(tpl.replace('{id}', str(a)))] = i
        # A 有而 B 没有的台词：用 B 同一句的第 1 个变体顶上
        for i, tpl in res_a.items():
            nid = i
            if nid in aliases:
                continue
            m = re.match(r'(.*_\d+)_\d+$', tpl)
            src = None
            if m:
                for k in range(1, 10):
                    src = tpl_to_b.get(f'{m.group(1)}_{k}')
                    if src:
                        break
            if src:
                aliases[nid] = src
                stats['fallback'] += 1
        # 无法解析名字的事件：数量一致时按顺序一一对应
        un_b = sorted(set(ev_b) - set(res_b))
        un_a = sorted(set(a_events) - set(res_a))
        if un_b and len(un_a) == len(un_b):
            for x, y in zip(un_a, un_b):
                aliases.setdefault(x, y)
        stats['events'] += len(ev_b)
        stats['mapped'] += len(res_b)
        if bk['id'] == old_bank_id or len(pk.banks) == 1:
            bk['id'] = new_bank_id
        bk['payload'] = wwise.rewrite_bank(bk['payload'], bk['id'], aliases)
    # 按用户要求：语音一句都不删。放不下时由调用方完整写入（允许比原文件大）。
    return pk.build(), stats


# ======================================================================= 原地替换（不改 meta）
# 教训：直接改 meta（资源索引）会被游戏发现——游戏拿 meta 条目和服务器清单的数据哈希比对，
# 对不上就整库重建并要求重新下载全部资源。所以这里绝不写 meta：
#   * 只改「目标皮肤 A」已经存在的缓存文件的内容；
#   * 新内容必须和原文件一样大（短了补零，长了改用 LZMA 重压，仍放不下就跳过）；
#   * 每个被改的原文件先完整备份，还原时原样拷回。
BACKUP_DIRNAME = 'backup'


from .usm import Cancelled, _check  # noqa: E402,F401  后台预计算的取消信号


FIT_CACHE_VER = 'fit-2026.11.28-1'      # 处理算法有变化时改这里，让旧缓存失效
FIT_CACHE_LIMIT = 3 * 1024 ** 3          # 结果缓存最多占 3GB，超出删最久没用的


def fit_size(kind, plain: bytes, size: int):
    """把内容调整到正好 size 字节；放不下返回 None。"""
    if len(plain) > size and kind == 'bundle':
        try:
            env = UnityPy.load(plain)
            plain = env.file.save(packer='lzma')
        except Exception:
            return None
    if len(plain) > size:
        return None
    if kind == 'raw' and len(plain) != size:
        return None                        # 不认识格式的文件（如 CRI 视频）只在大小完全一致时替换
    return plain + b'\x00' * (size - len(plain))


def sha1(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


class SkinSwapper:
    def __init__(self, game_dir, state_dir, log=print):
        self.persist = find_persistent(game_dir)
        if not self.persist:
            raise FileNotFoundError('在该目录下没有找到 Persistent/meta，请选择影之诗的安装目录')
        self.data_dir = os.path.dirname(self.persist)     # …_Data（热更新代码在它下面的 StreamingAssets）
        self.meta = os.path.join(self.persist, 'meta')
        self.dat = os.path.join(self.persist, 'dat')
        self.log = log
        os.makedirs(state_dir, exist_ok=True)
        key = hashlib.md5(self.persist.lower().encode()).hexdigest()[:12]
        self.state_path = os.path.join(state_dir, f'state_{key}.json')
        self.backup_dir = os.path.join(state_dir, BACKUP_DIRNAME, key)
        self.cache_dir = os.path.join(state_dir, 'fitcache')
        self._io_lock = threading.RLock()      # 读写游戏文件互斥：替换/还原 与 后台预计算 不能同时进行
        self._pre_lock = threading.Lock()
        self._pre = None
        self.card_art = []                      # 卡面换成动态异画的卡（普通卡 ID），见 _rewrite_cards
        self.state = self._load_state()

    # ------------------------------------------------------------ state
    def _load_state(self):
        try:
            with open(self.state_path, encoding='utf-8') as fp:
                st = json.load(fp)
        except Exception:
            st = {}
        if st.get('engine') != 'inplace':
            # 旧版（改 meta 的）记录：不再使用，只保留已应用列表为空
            st = {'engine': 'inplace', 'files': {}, 'applied': []}
        return st

    def _save_state(self):
        tmp = self.state_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fp:
            json.dump(self.state, fp, ensure_ascii=False, indent=1)
        os.replace(tmp, self.state_path)

    # ------------------------------------------------------------ io（meta 只读）
    def _path(self, h):
        return os.path.join(self.dat, h[:2], h)

    def _connect(self):
        from pathlib import Path
        con = sqlite3.connect(Path(self.meta).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def _rows(self, con=None):
        """meta 的资源表（只读）。按 meta 文件的修改时间/大小缓存，游戏改了 meta 才重新读。"""
        try:
            st = os.stat(self.meta)
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            key = None
        cached = getattr(self, '_rows_cache', None)
        if con is None and key and cached and cached[0] == key:
            return cached[1]
        own = con is None
        con = con or self._connect()
        try:
            rows = {r['n']: dict(r) for r in con.execute('select i,n,g,l,c,h,k,s,e from a')}
        finally:
            if own:
                con.close()
        if key:
            self._rows_cache = (key, rows)
        return rows

    def _file_ok(self, row):
        p = self._path(row['h'])
        return os.path.exists(p) and os.path.getsize(p) == row['l']

    def _backup_path(self, h):
        return os.path.join(self.backup_dir, h)

    def _read_orig(self, row):
        """读取资源的原始内容（被本工具改过的文件从备份读）。返回解密后的数据或 None。"""
        h = row['h']
        bp = self._backup_path(h)
        p = bp if h in self.state.get('files', {}) and os.path.exists(bp) else self._path(h)
        if not os.path.exists(p) or os.path.getsize(p) != row['l']:
            return None
        return xor_file(open(p, 'rb').read(), row['e'])

    # 兼容旧接口
    def _orig_rows(self):
        return self._rows()

    def _read_row_file(self, row):
        return self._read_orig(row)

    def _lsm(self, rows=None):
        """原始主数据里的 LeaderSkinMaster：{皮肤 ID: 行}（按主数据文件缓存）。"""
        rows = rows or self._rows()
        mrow = rows.get(MASTER_NAME)
        if not mrow:
            return {}
        c = getattr(self, '_lsm_cache', None)
        if c and c[0] == mrow['h']:
            return c[1]
        data = self._read_orig(mrow)
        lsm = {str(r[0]): r for r in MasterMemory(data).table('LeaderSkinMaster')} if data else {}
        self._lsm_cache = (mrow['h'], lsm)
        return lsm

    def logic_stale(self):
        """已应用的替换是旧版替换逻辑做的、而且新逻辑做出来会不一样（需要重新替换一次）：
        来源模型带进化时间轴/特效的组合一定要重做；其它组合只有主数据的改法变了，只比主数据。
        结果按状态缓存（界面会频繁查询）。"""
        applied = self.state.get('applied') or []
        if not applied or self.state.get('logic') == APPLY_LOGIC:
            return False
        rows = self._rows()
        mrow = rows.get(MASTER_NAME)
        if not mrow:
            return False
        key = (json.dumps(applied), self.state.get('time'), mrow['h'])
        c = getattr(self, '_stale_cache', None)
        if c and c[0] == key:
            return c[1]
        stale = True
        try:
            swaps = swap_list(applied)
            deps = self._deps()
            if (self.state.get('logic') or 0) < IMAGE_LOGIC:
                pass                       # 图片（卡面、立绘等）的写法变了，所有方案都要重做
            elif (self.state.get('logic') or 0) < MERGE_LOGIC and any(
                    wants(parts, 'model') and model_bound(rows, deps, b)[0]
                    and rep(f'Prefabs/LeaderSkin/class_{b}', b, a) in rows for a, b, parts in swaps):
                pass                       # 模型带进化时间轴/特效的组合：旧版做法游戏加载不到，要重做
            elif (mdata := self._read_orig(mrow)):
                out, n, m = self._rewrite_master(mdata, swaps)
                rec = self.state.get('files', {}).get(mrow['h'])
                if not n and not m:
                    stale = rec is not None
                else:
                    fitted = fit_size('master', out, mrow['l'])
                    stale = fitted is None or not rec or rec.get('new') != sha1(xor_file(fitted, mrow['e']))
        except Exception:
            stale = True
        self._stale_cache = (key, stale)
        return stale

    # ------------------------------------------------------------ catalogue
    def catalogue(self):
        """全部主战者皮肤的信息（取原始资源，不受当前替换影响）。"""
        rows = self._rows()
        mm = MasterMemory(self._read_orig(rows[MASTER_NAME]))
        labels = {r[0]: r[1] for r in mm.table('MasterTextLabel')}
        lsm = mm.table('LeaderSkinMaster')
        ids = {str(r[0]) for r in lsm}
        by_id = {}
        for n in rows:
            for m in re.finditer(r'(?<!\d)(\d{4,6})(?!\d)', n):
                sid = m.group(1)
                if sid in ids and is_skin_asset(n, sid):
                    by_id.setdefault(sid, []).append(n)
        label_keys = list(labels.keys())
        out = []
        for r in lsm:
            sid = str(r[0])
            names = by_id.get(sid, [])
            cats, missing = {}, 0
            for n in names:
                c = categorize(n)
                cats[c] = cats.get(c, 0) + 1
                if not self._file_ok(rows[n]):
                    missing += 1
            pat = re.compile(LABEL_PATTERN.replace('{id}', re.escape(sid)))
            n_labels = sum(1 for k in label_keys if pat.match(k))
            model = rows.get(f'Prefabs/LeaderSkin/class_{sid}')
            cv = (labels.get(f'LSPT_C{sid}') or '').replace('声优：', '').strip()
            cls = int(r[3]) if isinstance(r[3], int) else 0
            out.append({
                'id': sid, 'name': labels.get(f'LSPT_N{sid}') or r[2], 'nameJp': r[2], 'order': r[1],
                'cls': cls, 'clsName': CLASS_NAMES.get(cls) or labels.get(f'ClassName_{cls:04d}') or '特殊',
                'cv': '' if cv in ('——', '-', '—') else cv, 'cvEn': labels.get(f'LSPT_C{sid}_ENG') or '',
                'downloaded': bool(model) and self._file_ok(model),
                'assets': cats, 'assetTotal': len(names), 'missing': missing, 'labels': n_labels,
                'hasVoice': cats.get('voice', 0) > 0, 'hasEvolve': cats.get('evolve', 0) > 0,
                'hasEvoPoint': cats.get('evo_point', 0) > 0,
                'skinType': r[6] if len(r) > 6 else 0, 'deluxe': len(r) > 6 and r[6] == 1,
            })
        return out

    def preview(self, a, b):
        """预览把 a 显示为 b 时会替换哪些资源。原皮肤没有对应资源的项目无法替换（不能往索引里加东西）。"""
        a, b = str(a), str(b)
        rows = self._rows()
        lsm = self._lsm(rows)
        ra, rb = lsm.get(a), lsm.get(b)
        frames = frame_native(ra, rb, None)
        bound, mb = model_bound(rows, self._deps(), b)
        items = []
        for nb in sorted(n for n in rows if is_skin_asset(n, b)):
            na = rep(nb, b, a)
            cat = categorize(nb)
            if BASE_ID_ASSET.search(nb):
                status = 'native'                  # 进化特效：改 BaseSkinId 后直接用来源原生资源
            elif nb in bound:
                cat = 'model'                      # 模型自己的进化时间轴/特效：并进模型主文件
                status = ('skip' if rep(mb, b, a) not in rows else
                          'missing' if not self._file_ok(rows[nb]) else 'merge')
            elif na not in rows and frames and FRAME_ASSET.search(nb):
                status = 'native'                  # 战斗边框/遮罩/进化点框：同样按 BaseSkinId 加载来源的
            elif na not in rows:
                status = 'skip'                    # A 没有这个资源，无法替换
            elif not self._file_ok(rows[nb]):
                status = 'missing'                 # 来源没下载
            elif not self._file_ok(rows[na]):
                status = 'target_missing'          # 目标没下载
            else:
                status = 'replace'
            # 并进模型的项目在原皮肤里不一定有同名资源，显示来源的名字
            items.append({'name': nb if status == 'merge' else na, 'from': nb, 'cat': cat, 'status': status})
        keep = sorted(n for n in rows if is_skin_asset(n, a) and rep(n, a, b) not in rows)
        mm = MasterMemory(self._read_orig(rows[MASTER_NAME]))
        labels = {r[0]: r[1] for r in mm.table('MasterTextLabel')}
        evolve = None
        if rb and len(rb) > 9:
            evolve = {'skinType': rb[6], 'deluxe': rb[6] == 1, 'base': str(rb[9] or rb[0]),
                      # 原皮肤没有专属边框、来源有：战斗边框和进化点插图只能一起换（游戏按同一个编号加载）
                      'frameLink': is_special(rb) and not is_special(ra),
                      # 换进化演出会连带换掉战斗边框和进化点插图
                      'evolveFrames': frame_native(ra, rb, ('evolve',)) or (is_special(ra) and not is_special(rb))}
        pat = re.compile(LABEL_PATTERN.replace('{id}', re.escape(b)))
        subs = [{'key': rep(k, b, a), 'text': v} for k, v in labels.items() if pat.match(k)]
        cats = []
        for key, label, _ in CATEGORIES:
            its = [x for x in items if x['cat'] == key]
            if its:
                good = ('replace', 'native', 'merge')
                cats.append({'key': key, 'label': label, 'count': sum(1 for x in its if x['status'] in good),
                             'missing': sum(1 for x in its if x['status'] not in good), 'items': its})
        if subs:
            cats.append({'key': 'subtitle', 'label': '字幕与名字', 'count': len(subs), 'missing': 0,
                         'items': [{'name': s['key'], 'text': s['text'], 'cat': 'subtitle', 'status': 'replace'} for s in subs]})
        good = ('replace', 'native', 'merge')
        ok = sum(1 for x in items if x['status'] in good)
        return {'target': a, 'source': b, 'categories': cats, 'total': ok + len(subs), 'evolve': evolve,
                'missing': sum(1 for x in items if x['status'] not in good),
                'keep': [{'name': n, 'cat': categorize(n)} for n in keep]}

    def export_image(self, kind, sid, cache_dir):
        """把皮肤的某张图导出为 WebP（带缓存），返回文件路径或 None。"""
        srcs = IMAGE_KINDS.get(kind)
        if not srcs:
            return None
        rows = self._rows()
        for tpl, size in srcs:
            row = rows.get(tpl.format(id=sid))
            if not row or not self._file_ok(row):
                continue
            os.makedirs(cache_dir, exist_ok=True)
            out = os.path.join(cache_dir, f'{kind}_{sid}_{row["h"][:10]}.webp')
            if os.path.exists(out):
                return out
            try:
                env = UnityPy.load(self._read_orig(row))
                for obj in env.objects:
                    if obj.type.name == 'Texture2D':
                        img = obj.read().image.convert('RGBA')
                        if size:
                            img.thumbnail(size)
                        tmp = out + f'.{threading.get_ident()}.tmp'
                        img.save(tmp, 'WEBP', quality=88, method=2)
                        os.replace(tmp, out)
                        return out
            except Exception:
                continue
        return None

    # ------------------------------------------------------------ meta 健康检查 / 一键恢复
    @staticmethod
    def meta_stats(path):
        from pathlib import Path
        con = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
        try:
            total, done = con.execute('select count(*), coalesce(sum(s=1),0) from a').fetchone()
            need = con.execute('select coalesce(sum(l),0) from a where s=0').fetchone()[0]
            root = (con.execute("select h from c where n='//root'").fetchone() or [''])[0]
            return {'total': total, 'done': done, 'needGb': round(need / 2 ** 30, 1), 'root': root,
                    'healthy': bool(total) and done >= total * 0.5}
        finally:
            con.close()

    def _meta_backups(self):
        """可用的健康 meta 备份（新的在前）：本工具的备份、旧版工具留在游戏目录的备份。"""
        out = []
        for p in (os.path.join(self.backup_dir, 'meta.good'), os.path.join(self.persist, 'meta.skinswap.bak')):
            if os.path.isfile(p):
                try:
                    st = self.meta_stats(p)
                    if st['healthy']:
                        out.append((os.path.getmtime(p), p, st))
                except Exception:
                    pass
        return [(p, st) for _, p, st in sorted(out, reverse=True)]

    def backup_meta_if_healthy(self):
        try:
            if not self.meta_stats(self.meta)['healthy']:
                return False
            os.makedirs(self.backup_dir, exist_ok=True)
            dst = os.path.join(self.backup_dir, 'meta.good')
            tmp = dst + '.tmp'
            shutil.copyfile(self.meta, tmp)
            os.replace(tmp, dst)
            return True
        except Exception:
            return False

    def meta_status(self):
        """{'broken': 游戏是否把索引重置了, 'needGb', 'canRestore'}"""
        try:
            cur = self.meta_stats(self.meta)
        except Exception as ex:
            return {'broken': True, 'needGb': 0, 'canRestore': bool(self._meta_backups()), 'error': str(ex)}
        if cur['healthy']:
            return {'broken': False, 'needGb': cur['needGb'], 'canRestore': False}
        return {'broken': True, 'needGb': cur['needGb'], 'canRestore': bool(self._meta_backups())}

    def restore_meta(self):
        """用健康的备份恢复 meta；备份里标为已下载但文件已不在的条目改回未下载（让游戏补下）。"""
        backs = self._meta_backups()
        if not backs:
            raise RuntimeError('没有找到可用的索引备份')
        src, st = backs[0]
        os.makedirs(self.backup_dir, exist_ok=True)
        shutil.copyfile(self.meta, os.path.join(self.backup_dir, f'meta.before-restore-{time.strftime("%Y%m%d-%H%M%S")}'))
        tmp = self.meta + '.svwbtmp'
        shutil.copyfile(src, tmp)
        os.replace(tmp, self.meta)
        con = sqlite3.connect(self.meta, timeout=30)
        try:
            bad = []
            for i, h, l in con.execute('select i,h,l from a where s=1 and h<>""'):
                p = self._path(h)
                try:
                    if os.path.getsize(p) == l or (h in self.state.get('files', {})):
                        continue
                except OSError:
                    pass
                bad.append(i)
            if bad:
                con.executemany('update a set s=0 where i=?', [(i,) for i in bad])
                con.commit()
        finally:
            con.close()
        self._rows_cache = None
        now = self.meta_stats(self.meta)
        return {'from': src, 'done': now['done'], 'total': now['total'], 'needGb': now['needGb'], 'fixed': len(bad)}

    # ------------------------------------------------------------ revert
    def revert_all(self):
        """把本工具改过的缓存文件原样拷回。被游戏重新下载/更新过的文件不动。"""
        self._wait_precompute(None)
        with self._io_lock:
            return self._revert_locked()

    def _revert_locked(self):
        reverted, skipped = 0, 0
        for h, rec in list(self.state.get('files', {}).items()):
            p, bp = self._path(h), self._backup_path(h)
            try:
                cur = open(p, 'rb').read() if os.path.exists(p) else None
            except OSError:
                cur = None
            if cur is not None and sha1(cur) == rec['new'] and os.path.exists(bp):
                tmp = p + '.svwbtmp'
                shutil.copyfile(bp, tmp)
                os.replace(tmp, p)
                reverted += 1
            else:
                skipped += 1
            if os.path.exists(bp):
                os.remove(bp)
        if self.state.get('hotfix'):
            reverted += hotfix.revert(self.state['hotfix'])
        self.state = {'engine': 'inplace', 'files': {}, 'applied': []}
        self._save_state()
        return reverted, skipped

    # ------------------------------------------------------------ apply
    @staticmethod
    def _norm_swaps(swaps):
        swaps = swap_list(swaps)
        targets = [a for a, _, _ in swaps]
        if len(set(targets)) != len(targets):
            raise ValueError('同一个目标皮肤只能设置一个来源')
        return swaps

    def apply(self, swaps, progress=None):
        """swaps: [(目标A, 来源B)]。先还原旧改动，再按当前资源重新生成。"""
        swaps = self._norm_swaps(swaps)
        self._wait_precompute(swaps, progress)
        with self._io_lock:
            return self._apply_locked(swaps, progress)

    def _apply_locked(self, swaps, progress):
        self.revert_all()
        if self.meta_status()['broken']:
            raise RuntimeError('游戏的资源索引现在处于被重置的状态，请先在顶部点“一键恢复”，再进行替换')
        self.backup_meta_if_healthy()
        rows = self._rows()
        os.makedirs(self.backup_dir, exist_ok=True)
        report = []
        stats = {'changed': 0, 'skipped': 0, 'failed': 0, 'too_big': 0, 'labels': 0, 'no_target': 0, 'native': 0}
        plan, results, total = self._compute(swaps, rows, stats, progress)

        files = {}
        for k in sorted(results):
            a, b, nb, na = plan[k]
            fitted, notes, add = results[k]
            report.extend(notes)
            for sk, v in add.items():
                stats[sk] = stats.get(sk, 0) + v
            if fitted is not None:
                dst = rows[na]
                files[dst['h']] = (na, xor_file(fitted, dst['e']))
        self._cache_prune()

        # 主数据：字幕/名字 + 让原皮肤按来源的豪华进化方式演出（BaseSkinId/SkinType），保持文件大小不变
        mrow = rows.get(MASTER_NAME)
        if mrow:
            mdata = self._read_orig(mrow)
            if mdata:
                out, n_labels, n_evo = self._rewrite_master(mdata, swaps)
                if n_labels or n_evo:
                    fitted = fit_size('master', out, mrow['l'])
                    if fitted is None:
                        report.append('主数据：改完后放不下，跳过字幕与进化演出设置')
                    else:
                        files[mrow['h']] = (MASTER_NAME, xor_file(fitted, mrow['e']))
                        stats['labels'] = n_labels
                        stats['evolve'] = n_evo
                        if n_evo and swaps:
                            report.append('主数据：已让原皮肤按来源皮肤的方式加载进化演出/战斗边框/进化点插图，并同步立绘摆放位置')
                        if self.card_art:
                            report.append(f'卡面：{len(self.card_art)} 张卡显示成动态异画（普通版和闪卡版都换）')

        # 写入：先备份原文件，再原子替换
        if progress:
            progress(total - 1, total, '写入游戏文件…')
        written = {}
        code_rec = {}
        try:
            if self.card_art and MASTER_NAME in {n for n, _ in files.values()}:
                # 卡面异画的人物动画按卡 ID 加载：改游戏代码让普通卡按主数据里的映射去找异画的动画
                code_rec, bad = hotfix.apply(self.data_dir)
                for p, why in bad:
                    report.append(f'卡面异画：认不出游戏代码 {p}（{why}），这份没改，异画人物可能不显示')
                if code_rec:
                    report.append(f'卡面异画：已修改游戏代码 {len(code_rec)} 份（让普通卡也加载异画的人物动画）')
            for h, (name, enc) in files.items():
                p, bp = self._path(h), self._backup_path(h)
                if not os.path.exists(bp):
                    shutil.copyfile(p, bp)
                tmp = p + '.svwbtmp'
                with open(tmp, 'wb') as fp:
                    fp.write(enc)
                os.replace(tmp, p)
                written[h] = {'name': name, 'new': sha1(enc)}
        finally:
            self.state = {'engine': 'inplace', 'files': written, 'applied': [swap_record(*s) for s in swaps],
                          'card_art': [int(c) for c in self.card_art] if MASTER_NAME in
                          {v['name'] for v in written.values()} else [],
                          'hotfix': code_rec,
                          'logic': APPLY_LOGIC, 'time': time.time()}
            self._save_state()
        stats['changed'] = sum(1 for v in written.values() if v['name'] != MASTER_NAME)
        if stats['native']:
            report.append(f'豪华进化演出：直接使用来源皮肤的原生进化特效（{stats["native"]} 项）、进化音效和战斗边框')
        if stats.get('native_frame'):
            report.append(f'战斗边框与进化点插图：直接使用来源皮肤的原生资源（{stats["native_frame"]} 项）')
        if stats.get('merged'):
            report.append(f'模型自带的进化动作/特效：{stats["merged"]} 个包已并入模型，随模型一起加载')
        if stats['no_target']:
            report.append(f'{stats["no_target"]} 项来源资源在原皮肤里没有对应位置（多为其它模式的图片），保持原样')
        if progress:
            progress(total, total, '完成')
        self.last_stats = stats
        return stats['changed'], report

    def _compute(self, swaps, rows, stats, progress=None, cancel=None):
        """生成每一项要写入的内容（不碰游戏文件，结果进缓存）。返回 (plan, {k: (明文, 日志, 统计)}, total)。"""
        plan = []
        merges = {}            # (A, 来源模型) -> [并进模型的来源依赖包]
        lsm = self._lsm(rows)
        deps = self._deps()
        for a, b, parts in swaps:
            frames = frame_native(lsm.get(a), lsm.get(b), parts)
            bound, mb = model_bound(rows, deps, b)
            for nb in sorted(n for n in rows if is_skin_asset(n, b)):
                na = rep(nb, b, a)
                if BASE_ID_ASSET.search(nb):
                    if wants(parts, 'evolve'):
                        stats['native'] = stats.get('native', 0) + 1     # 进化特效按 BaseSkinId 直接加载来源皮肤的原生资源
                    continue
                if frames and na not in rows and FRAME_ASSET.search(nb):
                    stats['native_frame'] = stats.get('native_frame', 0) + 1   # 战斗边框/进化点框同样直接用来源的
                    continue
                if nb in bound:
                    # 模型自己的进化时间轴、时间轴里生成的特效：跟着模型走，并进模型的主文件
                    if wants(parts, 'model') and rep(mb, b, a) in rows:
                        merges.setdefault((a, mb), []).append(nb)
                        stats['merged'] = stats.get('merged', 0) + 1
                    elif na in rows:
                        stats['excluded'] = stats.get('excluded', 0) + 1
                    continue
                if not wants(parts, categorize(nb)):
                    if na in rows:
                        stats['excluded'] = stats.get('excluded', 0) + 1   # 用户选择不替换这一部分
                    continue
                if na not in rows:
                    stats['no_target'] = stats.get('no_target', 0) + 1     # 原皮肤没有的来源资源，无法替换
                    continue
                plan.append((a, b, nb, na))
        total = len(plan) + 1

        # 先全部读出来（来源和目标都按原始内容）
        cab_maps, srcs = {}, {}

        def add_cabs(a, data):
            if data and data[:7] == b'UnityFS':
                for cab in bundle_cabs(data):
                    cab_maps.setdefault(a, {})[cab] = 'CAB-' + hashlib.md5(f'{cab}>{a}'.encode()).hexdigest()

        for k, (a, b, nb, na) in enumerate(plan):
            _check(cancel)
            if progress:
                progress(0, total, f'读取 {os.path.basename(nb)}')
            data = self._read_orig(rows[nb])
            srcs[(a, nb)] = data
            add_cabs(a, data)
        # 要并进模型的来源依赖包（改名规则和其它来源包一致，模型里对它们的引用才对得上）
        embeds = {}
        for (a, mb), names in merges.items():
            got = []
            for nb in names:
                data = self._read_orig(rows[nb])
                if not data or data[:7] != b'UnityFS':
                    stats['skipped'] = stats.get('skipped', 0) + 1
                    continue
                add_cabs(a, data)
                got.append((nb, data))
            embeds[(a, mb)] = got

        results, todo, futures = {}, [], {}
        video_pool = ThreadPoolExecutor(max_workers=2)    # 视频编码一开始就放到后台线程，和模型包处理全程并行
        try:
            for k, (a, b, nb, na) in enumerate(plan):
                data = srcs.pop((a, nb))
                dst = rows[na]
                if data is None:
                    results[k] = (None, [f'跳过（来源未下载）：{nb}'], {'skipped': 1})
                    continue
                if not self._file_ok(dst):
                    results[k] = (None, [f'跳过（原皮肤这项没下载）：{na}'], {'skipped': 1})
                    continue
                emb = embeds.get((a, nb), [])
                key = self._fit_key(a, b, nb, na, dst, data, cab_maps.get(a, {}), [d for _, d in emb])
                hit = self._cache_get(key)
                if hit is not None:
                    results[k] = hit
                    continue
                args = (k, total, a, b, nb, na, data, dst, cab_maps, emb, progress, cancel)
                if data[:4] == b'CRID':
                    futures[k] = (key, video_pool.submit(self._fit_item, *args))
                else:
                    todo.append((k, key, args))
            for k, key, args in todo:
                _check(cancel)
                if progress:
                    progress(k, total, plan[k][3])
                results[k] = self._fit_item(*args)
                self._cache_put(key, results[k])
            for k, (key, fut) in futures.items():
                if progress:
                    progress(len(plan) - 1, total, '等待视频编码完成…')
                results[k] = fut.result()
                self._cache_put(key, results[k])
        finally:
            video_pool.shutdown(wait=True)
            shutdown_pool()
        return plan, results, total

    # ------------------------------------------------------------ 后台预先计算
    # 用户在界面上选好方案后就开始算（结果进缓存）；点“一键替换”时多半已算完，只剩写文件
    def precompute(self, swaps, on_done=None):
        try:
            swaps = self._norm_swaps(swaps)
        except ValueError:
            return
        if not swaps:
            return
        with self._pre_lock:
            cur = self._pre
            if cur and cur['swaps'] == swaps and cur['thread'].is_alive():
                return
            if cur:
                cur['cancel'].set()
            job = {'swaps': swaps, 'cancel': threading.Event(), 'done': threading.Event(), 'progress': None}
            job['thread'] = threading.Thread(target=self._precompute_run, args=(job, cur, on_done), daemon=True)
            self._pre = job
        job['thread'].start()

    def _precompute_run(self, job, prev, on_done):
        try:
            if prev:
                prev['thread'].join()
            if job['cancel'].is_set():
                return
            # 替换/还原正在进行时先等着；等的过程中被取消就直接退出（不会和持锁的一方互相等）
            while not self._io_lock.acquire(timeout=0.2):
                if job['cancel'].is_set():
                    return
            try:
                if job['cancel'].is_set():
                    return
                t0 = time.time()
                rows = self._rows()
                self._compute(job['swaps'], rows, {}, lambda k, n, t: job.update(progress=(k, n, t)), job['cancel'])
                if on_done:
                    on_done(time.time() - t0, job['swaps'])
            finally:
                self._io_lock.release()
        except Cancelled:
            pass
        except Exception:
            pass
        finally:
            job['done'].set()

    def _wait_precompute(self, swaps, progress=None):
        """替换/还原前：后台正在算同一套方案就等它算完（之后全部命中缓存）；方案不同就让它停下。"""
        with self._pre_lock:
            job = self._pre
        if not job or not job['thread'].is_alive():
            return
        if job['swaps'] != swaps:
            job['cancel'].set()
        while not job['done'].wait(0.3):
            if progress and job['progress'] and not job['cancel'].is_set():
                k, n, t = job['progress']
                progress(k, n, t)
        job['thread'].join()

    def _fit_item(self, k, total, a, b, nb, na, data, dst, cab_maps, emb, progress, cancel=None):
        """处理一项资源，返回 (写入的明文 / None, 日志, 统计增量)。"""
        notes, add = [], {}
        try:
            fitted = None
            if data[:7] == b'UnityFS':
                data_a = self._read_orig(dst)
                is_image = nb.startswith(IMAGE_PREFIXES)
                repainted = False
                if data_a and data_a[:7] == b'UnityFS' and is_image and has_textures(data) and has_textures(data_a):
                    # 图片类包：把来源画面写进原皮肤自己的纹理（尺寸/格式/Sprite 区域都不变）
                    try:
                        full = repaint_bundle(data_a, data)
                        fitted = fit_size('bundle', full, dst['l'])
                        if fitted is None and can_resize(data_a, data):
                            # 放不下：降分辨率（Sprite 跟着换算，显示大小不变），比降颜色精度出色块好得多。
                            # 压缩后大小大致跟像素数成正比，按差多少估一个起点
                            packed = min(len(full), len(UnityPy.load(full).file.save(packer='lzma')))
                            s = min(0.95, (dst['l'] / packed) ** 0.5 * 0.97)
                            while fitted is None and s >= 0.35:
                                fitted = fit_size('bundle', repaint_bundle(data_a, data, 0, s), dst['l'])
                                if fitted is not None:
                                    notes.append(f'{short(na)}：分辨率降到 {round(s * 100)}% 以放进原文件（游戏里显示大小不变）')
                                s *= 0.9
                        for bits in (6, 5, 4, 3, 2):
                            if fitted is not None:
                                break
                            fitted = fit_size('bundle', repaint_bundle(data_a, data, bits), dst['l'])
                            if fitted is not None:
                                notes.append(f'{short(na)}：颜色精度降到每通道 {bits} 位以放进原文件')
                        repainted = True
                    except Exception as ex:
                        notes.append(f'重绘失败，改用整包替换：{short(na)}（{ex}）')
                if fitted is None and is_image and not repainted:
                    fitted = fit_size('bundle', rewrite_bundle(data, b, a, cab_maps.get(a, {})), dst['l'])
                elif fitted is None and not is_image:
                    cm = cab_maps.get(a, {})
                    out = rewrite_bundle(data, b, a, cm)
                    if emb:
                        # 模型自己的进化时间轴/特效：对象并进模型主文件，引用变成文件内引用
                        out = merge_cabs(out, [rewrite_bundle(d, b, a, cm) for _, d in emb])
                        notes.append(f'已把 {"、".join(short(dn) for dn, _ in emb)} 并入 {short(na)}')
                    fitted = self._fit_bundle(out, dst['l'], na, notes, progress, k, total, cancel)
            elif data[:4] == b'AKPK':
                data_a = self._read_orig(dst)
                out, s = rewrite_pck(data, nb, na, b, a, data_a)
                msg = f'语音/音效 {short(na)}：事件 {s["events"]}，识别 {s["mapped"]}'
                fitted = fit_size('pck', out, dst['l'])
                if fitted is None:
                    # 语音不删减：完整写入，比原文件大一些（游戏若重新下载覆盖，自动重新扫描会再替换回来）
                    fitted = out
                    add['grown'] = 1
                    msg += f'；比原文件大 {(len(out) - dst["l"]) // 1024 + 1} KB，已完整写入（不删任何语音）'
                notes.append(msg)
            elif data[:4] == b'CRID':
                # CRI 视频：小了用 USM 自带的填充补齐，大了重新编码成 VP9，都精确等于原文件大小
                cb = (lambda m: progress(k, total, f'{short(na)}：{m}')) if progress else None
                fitted, note = usm.fit(data, dst['l'], cb, cancel)
                if note:
                    notes.append(f'视频 {short(na)}：{note}')
            else:
                fitted = fit_size('raw', data, dst['l'])
        except Cancelled:
            raise
        except Exception as ex:
            return None, notes + [f'失败：{nb}：{ex}'], {'failed': 1}
        if fitted is None:
            return None, notes + [f'跳过（来源比原文件大，放不下）：{short(na)}'], {'too_big': 1}
        return fitted, notes, add

    # ------------------------------------------------------------ 结果缓存
    # 同样的输入（来源文件、目标位置、改名映射、嵌入包、算法版本）得到同样的输出：
    # 再次点“一键替换”、换回之前的方案、启动时自动重新替换，都直接用缓存，不再重新编码
    def _fit_key(self, a, b, nb, na, dst, data, cab_map, emb):
        h = hashlib.sha1()
        for part in (FIT_CACHE_VER, a, b, nb, na, dst['h'], dst['l'], sorted(cab_map.items())):
            h.update(repr(part).encode('utf-8') + b'\0')
        h.update(hashlib.sha1(data).digest())
        for e in emb:
            h.update(hashlib.sha1(e).digest())
        return h.hexdigest()

    def _cache_get(self, key):
        p = os.path.join(self.cache_dir, key)
        try:
            with open(p + '.json', encoding='utf-8') as fp:
                meta = json.load(fp)
            with open(p + '.bin', 'rb') as fp:
                data = fp.read()
            if sha1(data) != meta['sha1']:
                return None
            now = time.time()
            os.utime(p + '.bin', (now, now))
            return data, meta['notes'], meta['add']
        except Exception:
            return None

    def _cache_put(self, key, result):
        fitted, notes, add = result
        if fitted is None:
            return
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            p = os.path.join(self.cache_dir, key)
            with open(p + '.bin.tmp', 'wb') as fp:
                fp.write(fitted)
            os.replace(p + '.bin.tmp', p + '.bin')
            with open(p + '.json', 'w', encoding='utf-8') as fp:
                json.dump({'sha1': sha1(fitted), 'notes': notes, 'add': add}, fp, ensure_ascii=False)
        except OSError:
            pass

    def _cache_prune(self, limit=FIT_CACHE_LIMIT):
        try:
            items = []
            for f in os.listdir(self.cache_dir):
                if f.endswith('.bin'):
                    st = os.stat(os.path.join(self.cache_dir, f))
                    items.append((st.st_mtime, st.st_size, f[:-4]))
            total = sum(x[1] for x in items)
            for _, size, key in sorted(items):
                if total <= limit:
                    break
                for ext in ('.bin', '.json'):
                    try:
                        os.remove(os.path.join(self.cache_dir, key + ext))
                    except OSError:
                        pass
                total -= size
        except OSError:
            pass

    def _rewrite_master(self, mdata, swaps):
        mm = MasterMemory(mdata)
        lab = mm.table('MasterTextLabel')
        d = {r[0]: r for r in lab}
        n = 0
        for a, b, parts in swaps:
            if not wants(parts, 'subtitle'):
                continue
            pat = re.compile(LABEL_PATTERN.replace('{id}', re.escape(b)))
            for key, r in list(d.items()):
                if pat.match(key):
                    ka = rep(key, b, a)
                    if ka not in d:
                        continue                  # 只替换已有标签，不新增
                    nr = list(d[ka])
                    nr[1] = r[1]
                    d[ka] = nr
                    n += 1
        if n:
            newlab = [d[k] for k in sorted(d, key=lambda s: s.encode('utf-16-be'))]
            mm.set_table('MasterTextLabel', newlab, high=True)

        # LeaderSkinMaster：[0]=ID [6]=SkinType [9]=BaseSkinId（进化特效/音效/战斗边框/进化点框按它加载）
        lsm = mm.table('LeaderSkinMaster')
        by = {str(r[0]): r for r in lsm}
        n_evo = 0
        for a, b, parts in swaps:
            ra = by.get(a)
            want = frame_target(ra, by.get(b), parts)
            if want and (ra[6], ra[9]) != want:
                ra[6], ra[9] = want
                n_evo += 1
        if n_evo:
            mm.set_table('LeaderSkinMaster', lsm, high=True)

        # 各界面里立绘的摆放参数（位置/旋转/缩放）按皮肤单独配置：让原皮肤跟来源保持一致
        n_tf = 0
        for table, (mode, part) in TRANSFORM_TABLES.items():
            if table not in mm.header:
                continue
            rows = mm.table(table)
            by = {str(r[0]): r for r in rows}
            changed = False
            for a, b, parts in swaps:
                if not wants(parts, part):
                    continue                  # 摆放参数跟着对应的立绘走：不换那张图就保持原样
                rb = by.get(b)
                if rb is not None:
                    new = list(rb)
                    new[0] = int(a)
                    if by.get(a) != new:
                        by[a] = new
                        changed = True
                elif a in by and mode == 'mirror':
                    del by[a]                 # 来源用默认摆放 → 原皮肤也用默认
                    changed = True
            if changed:
                mm.set_table(table, sorted(by.values(), key=lambda r: r[0]), high=True)
                n_tf += 1
        n_card = self._rewrite_cards(mm)
        if not n and not n_evo and not n_tf and not n_card:
            return mdata, 0, 0
        return mm.build(), n, n_evo + n_tf + n_card

    def card_art_list(self):
        """可以换成动态异画的卡：[{id: 普通卡 ID, hp: 高级异画卡 ID, name}]。"""
        rows = self._rows()
        mrow = rows.get(MASTER_NAME)
        data = self._read_orig(mrow) if mrow else None
        if not data:
            return []
        mm = MasterMemory(data)
        cm = {r[0]: r for r in mm.table('CardMaster')}
        lab = {r[0]: r[1] for r in mm.table('MasterTextLabel')}
        out = []
        for r in mm.table('HighPremiumCard'):
            hp = cm.get(r[0])
            if not hp or hp[5] != 2 or hp[8] != hp[0]:
                continue                                 # 只列真正的异画卡（不列本工具复制出来的普通卡行）
            base = hp[6]
            res = str(hp[2])
            ok = all(self._file_ok(rows[n]) for n in (f'Card/Materials/{res}_2_M', f'Prefabs/Foil/{res}') if n in rows)
            out.append({'id': base, 'hp': hp[0], 'name': lab.get(f'CN_{base}') or r[1], 'downloaded': ok})
        return out

    def _rewrite_cards(self, mm):
        """把 card_art 里的卡（普通版和闪卡版）显示成它的动态异画：
        CardMaster 的卡面资源指向异画的资源、FoilType 设成 2（游戏按它走异画的材质和 Spine 动画），
        HighPremiumCard 里给这两张卡复制一份异画的动画缩放/位置。卡的 ID 不变，拥有/对战数据不受影响。"""
        if not self.card_art:
            return 0
        cmt = mm.table('CardMaster')
        by = {r[0]: r for r in cmt}
        hpt = mm.table('HighPremiumCard')
        hpby = {r[0]: r for r in hpt}
        # 人物动画按卡 ID 加载（Prefabs/Foil/{卡ID}{0|1}）：StarterCardIdMapper 加“普通卡 → 异画卡”，
        # 配合 hotfix.py 的代码补丁，各界面找动画时都换成异画卡的 ID
        mpt = mm.table('StarterCardIdMapper')
        mpby = {r[0]: r for r in mpt}
        n = 0
        for base in self.card_art:
            rb = by.get(int(base))
            hp = by.get(rb[8]) if rb else None          # [8] HighRankFoilCardId
            if not hp or hp[5] != 2 or hp[0] not in hpby:
                continue
            for cid in {rb[6], rb[7]}:                  # [6] NormalCardId [7] FoilCardId
                r = by.get(cid)
                if not r:
                    continue
                if (r[2], r[5]) != (hp[2], 2):          # [2] CardResourceId [5] FoilType
                    r[2], r[5] = hp[2], 2
                    n += 1
                if cid not in hpby:
                    nr = list(hpby[hp[0]])
                    nr[0] = cid
                    hpby[cid] = nr
                    n += 1
                if mpby.get(cid, [cid, None])[1] != hp[0]:
                    mpby[cid] = [cid, hp[0]]
                    n += 1
        if n:
            mm.set_table('CardMaster', cmt, high=True)
            mm.set_table('HighPremiumCard', sorted(hpby.values(), key=lambda r: r[0]), high=True)
            mm.set_table('StarterCardIdMapper', sorted(mpby.values(), key=lambda r: r[0]), high=True)
        return n

    def _deps(self):
        """{资源名: {它依赖的资源名}}（只读 meta 的 r 表；和 _rows 一样按 meta 文件缓存）。"""
        try:
            st = os.stat(self.meta)
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            key = None
        cached = getattr(self, '_deps_cache', None)
        if key and cached and cached[0] == key:
            return cached[1]
        con = self._connect()
        try:
            names = {i: n for i, n in con.execute('select i,n from a')}
            out = {}
            for f, t in con.execute('select f,t from r where f<>t'):
                if f in names and t in names:
                    out.setdefault(names[f], set()).add(names[t])
        finally:
            con.close()
        if key:
            self._deps_cache = (key, out)
        return out

    def _fit_bundle(self, out, size, name, report, progress, k, total, cancel=None):
        """改写后的包放进 size 字节：先试 LZ4，再试 LZMA，最后按比例缩小纹理。"""
        if len(out) <= size:
            return out + b'\x00' * (size - len(out))
        _check(cancel)
        if progress:
            progress(k, total, f'{short(name)}：来源比原文件大，正在估算压缩后大小')
        try:
            # 多线程分段估算 LZMA 大小（几秒 → 1 秒）；估算放得下才真正压一次
            est = estimate_lzma_size(UnityPy.load(out).file.save(packer='none'))
            base = est
            if est <= size * 1.02:
                if progress:
                    progress(k, total, f'{short(name)}：正在压缩')
                lz = UnityPy.load(out).file.save(packer='lzma')
                if len(lz) <= size:
                    return lz + b'\x00' * (size - len(lz))
                base = len(lz)
        except Exception:
            return None
        scale = min(0.95, math.sqrt(size * 0.9 / max(1, base)))
        for attempt in range(6):
            if scale < 0.2:
                break
            _check(cancel)
            if progress:
                progress(k, total, f'{short(name)}：纹理缩小到 {scale:.0%} 并重新编码')
            try:
                small = shrink_bundle(out, scale)
            except Exception as ex:
                report.append(f'{short(name)} 缩小纹理失败：{ex}')
                return None
            cand = small if len(small) <= size else UnityPy.load(small).file.save(packer='lzma')
            if len(cand) <= size:
                report.append(f'{short(name)}：来源比原文件大，纹理已缩小到 {scale:.0%}')
                return cand + b'\x00' * (size - len(cand))
            # 按实际大小自适应调整下一轮比例（纹理面积 ∝ scale²）
            scale *= max(0.5, min(0.95, math.sqrt(size / len(cand)) * 0.97))
        report.append(f'{short(name)}：纹理缩到 {scale:.0%} 仍放不下')
        return None

    # ------------------------------------------------------------ rescan check
    def needs_update(self, swaps):
        """返回 (是否需要更新, 原因)。"""
        want = sorted(swap_list(swaps), key=repr)
        have = sorted(swap_list(self.state.get('applied', [])), key=repr)
        if want != have:
            return True, '替换方案有变化'
        if sorted(map(int, self.card_art)) != sorted(map(int, self.state.get('card_art') or [])):
            return True, '卡面异画设置有变化'
        if self.card_art and any(st['state'] in ('orig', 'partial') for st in hotfix.status(self.data_dir)):
            return True, '游戏更新了代码文件，卡面异画需要重新替换一次'
        if self.logic_stale():
            return True, '工具更新了替换方式（卡面画质/进化动作特效/战斗边框/立绘位置），需要重新替换一次'
        for h, rec in self.state.get('files', {}).items():
            p = self._path(h)
            try:
                cur = open(p, 'rb').read()
            except OSError:
                return True, f'游戏更新替换了已改的资源（{rec["name"]}）'
            if sha1(cur) != rec['new']:
                return True, f'游戏更新替换了已改的资源（{rec["name"]}）'
        return False, '没有变化'
