"""Windows 原生“选择文件夹”对话框（IFileOpenDialog + FOS_PICKFOLDERS），纯 ctypes 实现。
不再依赖 tkinter（省掉打包进 exe 的 tcl/tk 约 8MB）。"""
import ctypes
import uuid
from ctypes import wintypes

_ole32 = ctypes.OleDLL('ole32')          # 失败的 HRESULT 自动抛 OSError
_ole32raw = ctypes.WinDLL('ole32')       # 需要看原始返回值的函数
_shell32 = ctypes.OleDLL('shell32')
_user32 = ctypes.WinDLL('user32')
_ole32raw.CoInitializeEx.restype = ctypes.c_long
_ole32raw.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
_ole32raw.CoTaskMemFree.argtypes = [ctypes.c_void_p]
_user32.GetForegroundWindow.restype = wintypes.HWND

CLSCTX_INPROC_SERVER = 1
COINIT_APARTMENTTHREADED = 2
FOS_PICKFOLDERS = 0x20
FOS_FORCEFILESYSTEM = 0x40
FOS_PATHMUSTEXIST = 0x800
SIGDN_FILESYSPATH = 0x80058000
HRESULT_CANCELLED = 0x800704C7


class GUID(ctypes.Structure):
    _fields_ = [('Data1', ctypes.c_ulong), ('Data2', ctypes.c_ushort), ('Data3', ctypes.c_ushort),
                ('Data4', ctypes.c_ubyte * 8)]

    def __init__(self, s):
        super().__init__()
        ctypes.memmove(ctypes.byref(self), uuid.UUID(s).bytes_le, 16)


CLSID_FileOpenDialog = GUID('DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7')
IID_IFileOpenDialog = GUID('D57C7288-D4AD-4768-BE02-9D969532D960')
IID_IShellItem = GUID('43826D1E-E718-42EE-BC55-A1E261C37BFE')

# 虚函数表下标（IUnknown 0-2，IModalWindow 3，IFileDialog 4 起）
_RELEASE, _SHOW, _SETOPTIONS, _GETOPTIONS, _SETFOLDER, _SETTITLE, _GETRESULT = 2, 3, 9, 10, 12, 17, 20
_SI_GETDISPLAYNAME = 5


def _method(obj, index, restype, *argtypes):
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return lambda *args: proto(vtbl[index])(obj, *args)


def _release(obj):
    if obj:
        _method(obj, _RELEASE, ctypes.c_ulong)()


def pick_folder(title='选择文件夹', initial=None, owner=None):
    """弹出对话框，返回选中的文件夹路径；取消返回 None。需要在单独的线程里调用（会初始化 COM）。"""
    hr_init = _ole32raw.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    if owner is None:
        owner = _user32.GetForegroundWindow()     # 用户刚在工具窗口里点了按钮：对话框挂在它上面、显示在最前
    dlg = ctypes.c_void_p()
    item = ctypes.c_void_p()
    folder = ctypes.c_void_p()
    try:
        _ole32.CoCreateInstance(ctypes.byref(CLSID_FileOpenDialog), None, CLSCTX_INPROC_SERVER,
                                ctypes.byref(IID_IFileOpenDialog), ctypes.byref(dlg))
        opts = wintypes.DWORD()
        _method(dlg, _GETOPTIONS, ctypes.HRESULT, ctypes.POINTER(wintypes.DWORD))(ctypes.byref(opts))
        _method(dlg, _SETOPTIONS, ctypes.HRESULT, wintypes.DWORD)(
            opts.value | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST)
        _method(dlg, _SETTITLE, ctypes.HRESULT, wintypes.LPCWSTR)(title)
        if initial:
            try:
                _shell32.SHCreateItemFromParsingName(initial, None, ctypes.byref(IID_IShellItem), ctypes.byref(folder))
                _method(dlg, _SETFOLDER, ctypes.HRESULT, ctypes.c_void_p)(folder)
            except OSError:
                pass
        try:
            _method(dlg, _SHOW, ctypes.HRESULT, wintypes.HWND)(owner)
        except OSError as ex:
            if (ex.winerror or 0) & 0xFFFFFFFF == HRESULT_CANCELLED:
                return None
            raise
        _method(dlg, _GETRESULT, ctypes.HRESULT, ctypes.POINTER(ctypes.c_void_p))(ctypes.byref(item))
        name = ctypes.c_wchar_p()
        _method(item, _SI_GETDISPLAYNAME, ctypes.HRESULT, wintypes.DWORD, ctypes.POINTER(ctypes.c_wchar_p))(
            SIGDN_FILESYSPATH, ctypes.byref(name))
        path = name.value
        _ole32raw.CoTaskMemFree(name)
        return path
    finally:
        _release(item)
        _release(folder)
        _release(dlg)
        if hr_init in (0, 1):
            _ole32raw.CoUninitialize()
