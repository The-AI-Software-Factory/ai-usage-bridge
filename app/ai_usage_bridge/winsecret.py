"""Descifrado de secretos guardados con Electron safeStorage en Windows.

Desde la versión 3.27, Cursor guarda su token como
``cursor-safe-storage:v1:<base64>``. El blob es el formato ``v10`` de Chromium
(AES-256-GCM: ``b"v10" + nonce(12) + texto cifrado + tag(16)``) y la clave AES
está en ``Local State`` → ``os_crypt.encrypted_key``, protegida con DPAPI para el
usuario de Windows.

El servicio corre como LocalSystem, que no puede abrir la clave DPAPI de un
usuario. Si el intento directo falla, se toma prestada la identidad de las
sesiones de Windows abiertas (WTSQueryUserToken + ImpersonateLoggedOnUser,
mecanismo estándar de los servicios), se descifra la clave y se vuelve a la
identidad del servicio. Nada descifrado se escribe en disco.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

CURSOR_PREFIX = "cursor-safe-storage:"


class SecretError(Exception):
    """No se pudo descifrar el secreto."""


class NoUserSession(SecretError):
    """No hay una sesión de Windows abierta del usuario que pueda abrir la clave."""


def is_encrypted(value: str) -> bool:
    return value.startswith(CURSOR_PREFIX)


# --------------------------------------------------------------------- DPAPI
def _win32():
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    class WTS_SESSION_INFOW(ctypes.Structure):
        _fields_ = [("SessionId", wintypes.DWORD), ("pWinStationName", wintypes.LPWSTR), ("State", ctypes.c_int)]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)

    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi32.ImpersonateLoggedOnUser.argtypes = [wintypes.HANDLE]
    advapi32.ImpersonateLoggedOnUser.restype = wintypes.BOOL
    advapi32.RevertToSelf.restype = wintypes.BOOL
    wtsapi32.WTSEnumerateSessionsW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(WTS_SESSION_INFOW)), ctypes.POINTER(wintypes.DWORD),
    ]
    wtsapi32.WTSEnumerateSessionsW.restype = wintypes.BOOL
    wtsapi32.WTSFreeMemory.argtypes = [ctypes.c_void_p]
    wtsapi32.WTSQueryUserToken.argtypes = [wintypes.ULONG, ctypes.POINTER(wintypes.HANDLE)]
    wtsapi32.WTSQueryUserToken.restype = wintypes.BOOL
    return ctypes, wintypes, DATA_BLOB, WTS_SESSION_INFOW, crypt32, kernel32, advapi32, wtsapi32


def dpapi_unprotect(data: bytes) -> bytes:
    """CryptUnprotectData con la identidad actual del hilo."""
    ctypes, _, DATA_BLOB, _, crypt32, kernel32, _, _ = _win32()
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    if not crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None,
                                      CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out)):
        raise SecretError(f"DPAPI no pudo abrir la clave (error {ctypes.get_last_error()})")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _active_session_tokens():
    """Tokens de las sesiones de Windows activas (requiere correr como LocalSystem)."""
    ctypes, wintypes, _, WTS_SESSION_INFOW, _, kernel32, _, wtsapi32 = _win32()
    info = ctypes.POINTER(WTS_SESSION_INFOW)()
    count = wintypes.DWORD()
    if not wtsapi32.WTSEnumerateSessionsW(None, 0, 1, ctypes.byref(info), ctypes.byref(count)):
        return
    try:
        WTS_ACTIVE, WTS_DISCONNECTED = 0, 4
        sessions = [info[i].SessionId for i in range(count.value)
                    if info[i].State in (WTS_ACTIVE, WTS_DISCONNECTED) and info[i].SessionId != 0]
    finally:
        wtsapi32.WTSFreeMemory(info)
    for session_id in sessions:
        token = wintypes.HANDLE()
        if wtsapi32.WTSQueryUserToken(session_id, ctypes.byref(token)):
            try:
                yield token
            finally:
                kernel32.CloseHandle(token)


def dpapi_unprotect_as_user(data: bytes) -> bytes:
    """Intenta con la identidad actual y, si falla, con cada sesión de usuario abierta."""
    try:
        return dpapi_unprotect(data)
    except SecretError as exc:
        first_error = exc
    _, _, _, _, _, _, advapi32, _ = _win32()
    tried = False
    for token in _active_session_tokens():
        tried = True
        if not advapi32.ImpersonateLoggedOnUser(token):
            continue
        try:
            return dpapi_unprotect(data)
        except SecretError:
            pass
        finally:
            if not advapi32.RevertToSelf():
                # Sin poder volver a la identidad del servicio no es seguro seguir.
                raise SystemExit("RevertToSelf falló")
    if not tried:
        raise NoUserSession(
            "Cursor guarda el token cifrado con la clave de tu usuario de Windows y no hay "
            "una sesión abierta para usarla; se va a leer cuando inicies sesión"
        ) from first_error
    raise SecretError(
        "Ninguna sesión de Windows abierta pudo abrir la clave de Cursor "
        "(¿el servicio apunta al perfil de otro usuario?)"
    ) from first_error


# --------------------------------------------------------------- safeStorage
class SafeStorage:
    """Descifra valores de Electron safeStorage usando la clave de ``Local State``."""

    def __init__(self, local_state: Path, unprotect=None):
        self.local_state = local_state
        self._unprotect = unprotect or dpapi_unprotect_as_user
        self._key: bytes | None = None
        self._key_stamp: float | None = None

    def _aes_key(self) -> bytes:
        try:
            stamp = self.local_state.stat().st_mtime
        except OSError as exc:
            raise SecretError(f"No existe {self.local_state}") from exc
        if self._key is not None and stamp == self._key_stamp:
            return self._key
        try:
            state = json.loads(self.local_state.read_text(encoding="utf-8"))
            wrapped = base64.b64decode(state["os_crypt"]["encrypted_key"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise SecretError(f"{self.local_state} no tiene os_crypt.encrypted_key") from exc
        if not wrapped.startswith(b"DPAPI"):
            raise SecretError("La clave de Cursor no está protegida con DPAPI (formato desconocido)")
        key = self._unprotect(wrapped[5:])
        if len(key) != 32:
            raise SecretError("La clave de Cursor no tiene 32 bytes (formato desconocido)")
        self._key, self._key_stamp = key, stamp
        return key

    def decrypt(self, value: str) -> str:
        if not is_encrypted(value):
            return value
        # "cursor-safe-storage:v1:<base64>"
        try:
            payload = value[len(CURSOR_PREFIX):].split(":", 1)[1]
            blob = base64.b64decode(payload)
        except (IndexError, ValueError) as exc:
            raise SecretError("El token cifrado de Cursor tiene un formato desconocido") from exc
        if sys.platform != "win32" and self._unprotect is dpapi_unprotect_as_user:
            raise SecretError("Descifrar el token de Cursor solo está soportado en Windows")
        if blob[:3] != b"v10" or len(blob) < 3 + 12 + 16:
            raise SecretError(f"Formato de cifrado de Cursor no soportado ({blob[:3]!r})")
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce, sealed = blob[3:15], blob[15:]
        try:
            plain = AESGCM(self._aes_key()).decrypt(nonce, sealed, None)
        except SecretError:
            raise
        except Exception as exc:  # InvalidTag: la clave en memoria quedó vieja
            self._key = None
            raise SecretError("No se pudo descifrar el token de Cursor (clave inválida)") from exc
        return plain.decode("utf-8")
