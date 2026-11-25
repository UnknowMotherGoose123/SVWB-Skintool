# 影之诗 WB 主战者换皮工具

《影之诗：Worlds Beyond》（Shadowverse: Worlds Beyond）PC 版的本地换皮工具：把你**已拥有**的主战者皮肤，在本机上显示成另一款皮肤——模型、语音、字幕和名字、立绘、进化点插图、豪华进化演出一起换。

> [!WARNING]
> **非官方工具，与 Cygames 及游戏运营方无关。** 它只改你自己电脑上的游戏缓存文件，效果只有你自己看得到，对手和服务器看到的仍是你实际拥有的皮肤。修改客户端文件可能违反游戏的用户协议，**使用风险自负**。本仓库不包含任何游戏资源；请勿用它分发游戏资源或用于商业用途。

## 功能

- **一键替换**：选「我拥有的皮肤」和「想显示成的皮肤」，点一下即可。可以同时设置多组。
- **全部资源都换**：
  - 战斗模型（Spine 骨骼动画，含嘴型、表情）
  - 语音和音效：一句都不删，按事件逐条对应
  - 字幕和名字
  - UI 立绘、进化点插图、战斗边框
  - 豪华进化演出、BossSkinMovie 视频
- **可以只换一部分**：在替换预览里勾选要替换的部分（模型、对战立绘、进化演出、进化点插图、战斗界面、头像与卡组、语音、音效、字幕与名字……），没勾的保持原样。比如只换模型和语音、保留原来的立绘。
- **29 款豪华版皮肤**作为来源都能正常替换，带进化画面。
- **一键还原**：所有被改的文件都先完整备份，随时原样恢复。
- **游戏更新后自动重新替换**（可选）：启动时检查替换是否还有效，发现游戏更新覆盖了文件就重新应用。
- **按职业筛选**、搜索名字/ID/声优，预览图本地缓存，打开很快。
- **检查新版本**：启动时自动对比本仓库 GitHub 上的最新版本（优先看 Releases），有新版时左上角的版本号会变成金色，点一下打开下载页。只访问 GitHub，不上传任何信息。
- **速度**：第一次替换约 20 秒；同样的方案再替换约 1 秒。选好方案后会在后台预先计算，点替换时多半已经算完。

## 使用方法

1. 在 [Releases](../../releases) 下载 `SVWBSkinSwap-*.exe`，双击运行（单文件，免安装）。
2. 第一次打开时选择游戏安装目录（包含 `*_Data` 文件夹的那一层，例如 `E:\Shadowverse`）。
3. 在列表里先点**你拥有的皮肤**，再点**想显示成的皮肤**。右侧预览里可以取消勾选不想替换的部分，然后点「加入替换方案」。
4. **完全退出游戏**，点「一键替换」，然后启动游戏。
5. 想恢复原样：点「一键还原」。

> 替换和还原前必须先退出游戏，工具检测到游戏在运行时会拒绝操作。

目前在PC版的影之诗高清版已完成测试，运行无问题

## 工作原理（简述）

- **不动资源索引**：游戏的 `Persistent/meta`（SQLite）一旦被改，游戏会认为缓存损坏，要求重新下载约 30GB 资源。所以工具**只读** meta，所有改动都是把新内容写进目标皮肤原有的缓存文件，文件大小保持不变。
- **大小放不下时**：
  - Unity 包先换用 LZMA 压缩；仍然放不下，就按比例缩小贴图，并重新编码成 BC7。
  - Spine 图集是预乘透明度格式，缩小时逐通道处理，避免边缘出现白点。
  - CRI USM 视频比原文件小，就用 USM 自带的 padding 补齐；比原文件大，就逐帧重新编码成 VP9，帧数、分辨率和帧率都不变。
  - 语音包放不下时，宁可比原文件大一点也完整写入。
- **改名映射**：Unity 包内的 CAB 名、Spine 的骨骼和皮肤（游戏对部分皮肤 ID 写死了 `default`/`JP` 皮肤）、Wwise 事件 ID、主数据（MessagePack）里的字幕和豪华进化设置，都会按目标皮肤重写。
- **备份与缓存**：
  - 每个被改的原文件都备份在 `%LOCALAPPDATA%\SVWBSkinSwap\backup\`，还原时原样拷回。
  - 处理结果缓存在 `fitcache\`（最多 3GB），所以重复替换很快。

## 从源码运行 / 自己打包

需要 Windows 和 Python 3.11 或更高版本。

```powershell
pip install -r requirements.txt
python run_skinswap.py
```

打包成单个 exe：

```powershell
pip install -r requirements.txt pyinstaller
python tools\build_exe.py
```

输出在 `dist\`。推送 `v*` 形式的标签（例如 `v2.0.0`）后，GitHub Actions 会自动打包，并把 exe 附到对应的 Release 上，见 `.github/workflows/build.yml`。

自检（不开窗口，检查运行环境和打包是否完整）：

```powershell
影之诗WB换皮工具.exe --selftest --game E:\Shadowverse
```

## 目录结构

```
run_skinswap.py          入口
svwb_skinswap/
  server.py              本地 HTTP 后端 + 窗口（Edge/Chrome 应用模式）
  core.py                资源规划、原地替换、备份/还原、结果缓存、后台预计算
  texfit.py              纹理缩放（预乘透明度）、多进程 BC7 编码、LZMA
  usm.py                 CRI USM 视频解析、补齐、VP9 重编码
  spine.py               Spine 4.2 二进制骨骼解析与改写
  wwise.py               Wwise 音频包（AKPK/BNK）事件改写
  master.py              主数据（MessagePack + LZ4）读写
  crypto.py              缓存文件的异或加解密
  winpick.py             Windows 原生“选择文件夹”对话框
  updates.py             检查 GitHub 上的新版本（Windows 自带的 WinHTTP）
  web/                   界面（HTML/CSS/JS）
tools/
  build_exe.py           PyInstaller 打包脚本
  make_icon.py           生成程序图标
```

## 常见问题

**杀毒软件报毒？**
PyInstaller 打包的单文件 exe 偶尔会被误报。可以从源码运行，或者自己打包。

**游戏更新后替换失效了？**
游戏更新会重新下载被改过的文件。打开工具即可：开着「启动时自动重新扫描」时会自动重新替换；也可以点顶部的「检查替换」，再点一次「一键替换」。

**检查新版本失败？**
需要能访问 GitHub（api.github.com / raw.githubusercontent.com）。工具会自动使用系统的代理设置。

**游戏要求重新下载全部资源？**
本工具不会修改 meta，正常使用不会触发。如果遇到（例如用过其他改 meta 的工具），顶部会提示，可以用「一键恢复」把资源索引恢复到之前备份的健康状态。

## 作者

UnknowMotherGoose123

## 许可证

[MIT](LICENSE)
