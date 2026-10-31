# Copyright (C) 2026 Fobal - https://github.com/Fobal/fab-downloader
# SPDX-License-Identifier: GPL-3.0-only
"""Encrypt data for the current Windows user with the Data Protection API (DPAPI)."""

from __future__ import annotations

import ctypes
from ctypes import wintypes

_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def protect(data: bytes) -> bytes:
    return _transform(data, protect=True)


def unprotect(data: bytes) -> bytes:
    """Decrypt data from protect(). Raises OSError if it was not encrypted by this user."""
    return _transform(data, protect=False)


def _transform(data: bytes, *, protect: bool) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    function.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    blob_in = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    blob_out = _DataBlob()
    if not function(
        ctypes.byref(blob_in), None, None, None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)
