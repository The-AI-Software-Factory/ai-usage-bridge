"""Quita la firma Authenticode de un ejecutable PE (Windows).

Se usa en el build sobre las copias de pythonw.exe que muestran los íconos de la
bandeja: al cambiarles la descripción (rcedit) la firma original quedaría
inválida, así que se quita antes y el ejecutable queda simplemente sin firmar.

Uso: python pe_strip_signature.py <archivo.exe> [...]
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

SECURITY_DIR = 4  # IMAGE_DIRECTORY_ENTRY_SECURITY


def strip_signature(data: bytes) -> tuple[bytes, bool]:
    """Devuelve (ejecutable sin firma, si tenía firma)."""
    if data[:2] != b"MZ":
        raise ValueError("no es un ejecutable PE (falta MZ)")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise ValueError("no es un ejecutable PE (falta la firma PE)")
    opt = pe + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    if magic == 0x20B:  # PE32+
        dirs = opt + 112
    elif magic == 0x10B:  # PE32
        dirs = opt + 96
    else:
        raise ValueError(f"cabecera opcional desconocida (0x{magic:x})")
    entry = dirs + SECURITY_DIR * 8
    offset, size = struct.unpack_from("<II", data, entry)
    if not size:
        return data, False
    out = bytearray(data)
    struct.pack_into("<II", out, entry, 0, 0)
    struct.pack_into("<I", out, opt + 64, 0)  # CheckSum (no se usa en modo usuario)
    if offset + size >= len(out) - 8:  # el certificado está al final (con relleno)
        del out[offset:]
    return bytes(out), True


def main(paths: list[str]) -> int:
    for name in paths:
        path = Path(name)
        stripped, had = strip_signature(path.read_bytes())
        if had:
            path.write_bytes(stripped)
        print(f"{path.name}: {'firma quitada' if had else 'sin firma'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
