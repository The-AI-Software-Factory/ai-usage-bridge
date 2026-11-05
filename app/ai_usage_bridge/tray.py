"""App de bandeja de Windows: un ícono por IA con su % libre.

Corre en la sesión del usuario (no como servicio) y solo lee ``status.json``,
que escribe el servicio. No usa credenciales ni se conecta a ningún lado.

Uso: ``AIUsageTray.exe <carpeta de instalación>\\app\\ai_usage_tray.pyw``
"""

from __future__ import annotations

import ctypes
import json
import winreg
import os
import subprocess
import sys
import tempfile
from ctypes import wintypes as w
from pathlib import Path

from . import tray_render

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

WM_DESTROY, WM_COMMAND, WM_TIMER, WM_NULL, WM_APP = 0x0002, 0x0111, 0x0113, 0x0000, 0x8000
WM_LBUTTONUP, WM_RBUTTONUP = 0x0202, 0x0205
WM_TRAY = WM_APP + 1
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
SM_CXSMICON = 49
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x2, 0x100
MF_STRING, MF_SEPARATOR, MF_GRAYED = 0x0, 0x800, 0x1
ERROR_ALREADY_EXISTS = 183
NOTIFY_SETTINGS = r"Control Panel\NotifyIconSettings"
PINNED_MARK = "AIUsageBridgePinned"  # marca propia: se fija una sola vez, después decide el usuario
STOP_FILE = "tray.stop"  # el instalador lo crea para cerrar la bandeja prolijamente
REFRESH_MS = 5000
CMD_OPEN, CMD_REFRESH, CMD_CONFIG, CMD_LOGS, CMD_EXIT = 1, 2, 3, 4, 5


class GUID(ctypes.Structure):
    _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD), ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT), ("uFlags", w.UINT),
        ("uCallbackMessage", w.UINT), ("hIcon", w.HICON), ("szTip", w.WCHAR * 128),
        ("dwState", w.DWORD), ("dwStateMask", w.DWORD), ("szInfo", w.WCHAR * 256),
        ("uVersion", w.UINT), ("szInfoTitle", w.WCHAR * 64), ("dwInfoFlags", w.DWORD),
        ("guidItem", GUID), ("hBalloonIcon", w.HICON),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", w.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", w.HINSTANCE), ("hIcon", w.HICON),
        ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH), ("lpszMenuName", w.LPCWSTR),
        ("lpszClassName", w.LPCWSTR),
    ]


user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID]
user32.CreateWindowExW.restype = w.HWND
user32.LoadImageW.argtypes = [w.HINSTANCE, w.LPCWSTR, w.UINT, ctypes.c_int, ctypes.c_int, w.UINT]
user32.LoadImageW.restype = w.HANDLE
user32.DestroyIcon.argtypes = [w.HICON]
user32.TrackPopupMenu.argtypes = [w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.LPVOID]
user32.AppendMenuW.argtypes = [w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR]
user32.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.SetTimer.argtypes = [w.HWND, ctypes.c_size_t, w.UINT, w.LPVOID]
shell32.Shell_NotifyIconW.argtypes = [w.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = w.BOOL
kernel32.CreateMutexW.argtypes = [w.LPVOID, w.BOOL, w.LPCWSTR]
kernel32.CreateMutexW.restype = w.HANDLE


class Tray:
    def __init__(self, install_dir: Path):
        self.install_dir = install_dir
        self.status_path = install_dir / "status.json"
        self.icon_dir = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "AIUsageBridge" / "icons"
        self.icon_dir.mkdir(parents=True, exist_ok=True)
        self.status: dict | None = None
        self.status_mtime: float | None = None
        self.shown: dict[int, str] = {}  # uID -> clave de la IA
        self.handles: dict[int, int] = {}  # uID -> HICON
        self.rendered: dict[int, tuple] = {}  # uID -> (texto, tooltip, ...) para no redibujar igual
        self.hwnd = None
        self._wndproc = WNDPROC(self._proc)  # referencia viva para ctypes
        self.taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        self.pin_pending = True
        self.pin_attempts = 0

    # ------------------------------------------------------------ estado
    def _load(self) -> None:
        try:
            mtime = self.status_path.stat().st_mtime
        except OSError:
            self.status, self.status_mtime = None, None
            return
        if mtime == self.status_mtime:
            return
        try:
            self.status = json.loads(self.status_path.read_text(encoding="utf-8"))
            self.status_mtime = mtime
        except (OSError, ValueError):
            pass  # se reintenta en el próximo ciclo

    def _wanted(self) -> list[str]:
        tray = (self.status or {}).get("tray") or {}
        show = tray.get("show") or ["claude", "codex", "cursor"]
        providers = (self.status or {}).get("providers") or {}
        return [k for k in show if not providers or k in providers]

    # ------------------------------------------------------------ íconos
    def _icon_handle(self, key: str, item: dict | None, stale: bool) -> int:
        size = user32.GetSystemMetrics(SM_CXSMICON) or 16
        image = tray_render.render_icon(key, item, size, stale=stale)
        path = self.icon_dir / f"{key}.ico"
        image.save(path, format="ICO", sizes=[(size, size)])
        return user32.LoadImageW(None, str(path), IMAGE_ICON, size, size, LR_LOADFROMFILE)

    def _nid(self, uid: int) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = uid
        return nid

    def refresh(self, force: bool = False) -> None:
        self._load()
        stale = tray_render.is_stale(self.status)
        providers = (self.status or {}).get("providers") or {}
        best = (self.status or {}).get("best")
        wanted = self._wanted()
        # Quita íconos que ya no corresponden (por ejemplo, si cambió "show").
        for uid in list(self.shown):
            if uid > len(wanted) or self.shown[uid] != wanted[uid - 1]:
                self._remove(uid)
        for index, key in enumerate(wanted, start=1):
            item = providers.get(key)
            tip = tray_render.tooltip(key, item, best, stale)
            signature = (tray_render.icon_text(item), stale, tray_render.level_color(item and item.get("margin")), tip)
            if not force and self.shown.get(index) == key and self.rendered.get(index) == signature:
                continue
            handle = self._icon_handle(key, item, stale)
            nid = self._nid(index)
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAY
            nid.hIcon = handle
            nid.szTip = tip
            action = NIM_MODIFY if index in self.shown else NIM_ADD
            ok = shell32.Shell_NotifyIconW(action, ctypes.byref(nid))
            if not ok and action == NIM_MODIFY:
                ok = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
            old = self.handles.pop(index, None)
            if old:
                user32.DestroyIcon(old)
            self.handles[index] = handle
            if ok:
                self.shown[index] = key
                self.rendered[index] = signature
            else:
                self.rendered.pop(index, None)  # la barra todavía no existe: se reintenta

    def _pin_once(self) -> None:
        """Muestra los íconos en la barra (no en el menú ^) la primera vez que aparecen.

        Windows 11 guarda cada ícono en HKCU\\Control Panel\\NotifyIconSettings con
        IsPromoted. Se activa una sola vez por ícono (marca propia); si después el
        usuario lo esconde, se respeta.
        """
        tray = (self.status or {}).get("tray") or {}
        if tray.get("pin_icons") is False or not self.shown:
            self.pin_pending = False
            return
        exe = Path(sys.executable).name.lower()
        if exe != "aiusagetray.exe":
            self.pin_pending = False  # solo con el ejecutable propio (no tocar otras apps de Python)
            return
        changed = []
        found = 0
        try:
            root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, NOTIFY_SETTINGS)
        except OSError:
            self.pin_pending = False
            return
        with root:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, name, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
                        path = str(winreg.QueryValueEx(key, "ExecutablePath")[0])
                        if not path.lower().endswith("\\" + exe):
                            continue
                        found += 1
                        try:
                            winreg.QueryValueEx(key, PINNED_MARK)
                            continue  # ya lo hicimos antes
                        except OSError:
                            pass
                        try:
                            uid = int(winreg.QueryValueEx(key, "UID")[0])
                        except OSError:
                            uid = None
                        winreg.SetValueEx(key, "IsPromoted", 0, winreg.REG_DWORD, 1)
                        winreg.SetValueEx(key, PINNED_MARK, 0, winreg.REG_DWORD, 1)
                        changed.append(uid)
                except OSError:
                    continue
        self.pin_attempts += 1
        # Windows crea las entradas al agregar los íconos; se reintenta unos minutos.
        if found >= len(self.shown) or self.pin_attempts >= 60:
            self.pin_pending = False
        # Se vuelven a agregar para que Windows aplique el cambio en el momento.
        for uid in changed:
            if uid in self.shown:
                self._remove(uid)
        if changed:
            self.refresh(force=True)

    def _remove(self, uid: int) -> None:
        nid = self._nid(uid)
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        self.shown.pop(uid, None)
        self.rendered.pop(uid, None)
        handle = self.handles.pop(uid, None)
        if handle:
            user32.DestroyIcon(handle)

    # ------------------------------------------------------------ acciones
    def _menu(self) -> None:
        url = ((self.status or {}).get("tray") or {}).get("dashboard_url")
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING if url else MF_GRAYED, CMD_OPEN,
                           "Abrir dashboard" if url else "Abrir dashboard (configurá la URL)")
        user32.AppendMenuW(menu, MF_STRING, CMD_REFRESH, "Actualizar")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, CMD_CONFIG, "Configurar…")
        user32.AppendMenuW(menu, MF_STRING, CMD_LOGS, "Ver logs")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, CMD_EXIT, "Salir")
        point = w.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, point.x, point.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        if cmd:
            self._command(cmd)

    def _command(self, cmd: int) -> None:
        if cmd == CMD_OPEN:
            self._open_dashboard()
        elif cmd == CMD_REFRESH:
            self.status_mtime = None
            self.refresh(force=True)
        elif cmd == CMD_CONFIG:
            target = self.install_dir / "configure.cmd"
            if target.is_file():
                os.startfile(str(target))  # se eleva solo
        elif cmd == CMD_LOGS:
            subprocess.Popen(["explorer.exe", str(self.install_dir / "logs")])
        elif cmd == CMD_EXIT:
            user32.DestroyWindow(self.hwnd)

    def _open_dashboard(self) -> None:
        url = ((self.status or {}).get("tray") or {}).get("dashboard_url")
        if url and url.lower().startswith(("http://", "https://")):
            os.startfile(url)

    # ------------------------------------------------------------ ventana
    def _proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            if lparam == WM_LBUTTONUP:
                self._open_dashboard()
            elif lparam == WM_RBUTTONUP:
                self._menu()
            return 0
        if msg == WM_TIMER:
            if (self.install_dir / STOP_FILE).exists():
                user32.DestroyWindow(hwnd)  # actualización o desinstalación en curso
                return 0
            self.refresh()
            if self.pin_pending:
                self._pin_once()
            return 0
        if msg == self.taskbar_created:  # Explorer se reinició: hay que volver a agregar los íconos
            self.shown.clear()
            self.refresh(force=True)
            return 0
        if msg == WM_DESTROY:
            for uid in list(self.shown):
                self._remove(uid)
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run(self) -> int:
        hinst = kernel32.GetModuleHandleW(None)
        cls = WNDCLASSW()
        cls.lpfnWndProc = self._wndproc
        cls.hInstance = hinst
        cls.lpszClassName = "AIUsageBridgeTray"
        user32.RegisterClassW(ctypes.byref(cls))
        self.hwnd = user32.CreateWindowExW(0, cls.lpszClassName, "AI Usage Bridge", 0, 0, 0, 0, 0,
                                           None, None, hinst, None)
        if not self.hwnd:
            return 1
        self.refresh(force=True)
        user32.SetTimer(self.hwnd, 1, REFRESH_MS, None)
        msg = w.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    install_dir = Path(argv[0]) if argv else Path(__file__).resolve().parents[2]
    # Una sola instancia por usuario.
    kernel32.CreateMutexW(None, False, "Local\\AIUsageBridgeTray")
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        return 0
    try:  # íconos nítidos con escalado de pantalla
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass
    status = install_dir / "status.json"
    try:
        tray_cfg = json.loads(status.read_text(encoding="utf-8")).get("tray", {})
        if tray_cfg.get("enabled") is False:
            return 0
    except (OSError, ValueError):
        pass
    return Tray(install_dir).run()
