"""Windows Credential Manager helpers built on the standard-library ctypes.

The module imports safely on every platform; calling the functions outside
Windows raises ``OSError`` so callers can fall back to environment variables.
Secrets are passed in process memory only: never on a command line, in a
file, in the environment, or in logs.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
CRED_MAX_CREDENTIAL_BLOB_SIZE = 5 * 512
ERROR_NOT_FOUND = 1168


class _CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", wintypes.LPBYTE),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _advapi32() -> ctypes.WinDLL:
    if os.name != "nt":
        raise OSError("Windows Credential Manager is only available on Windows")
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), wintypes.DWORD]
    advapi32.CredWriteW.restype = wintypes.BOOL
    advapi32.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_CREDENTIAL)),
    ]
    advapi32.CredReadW.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = [ctypes.c_void_p]
    advapi32.CredFree.restype = None
    return advapi32


def write_credential(target: str, secret: str, *, user: str | None = None) -> None:
    """Store ``secret`` as a generic Windows credential named ``target``.

    Replaces any existing entry with the same target name. Raises ``OSError``
    on failure.
    """
    advapi32 = _advapi32()
    if not secret:
        raise ValueError("refusing to store an empty credential")
    encoded = secret.encode("utf-16-le")
    if len(encoded) > CRED_MAX_CREDENTIAL_BLOB_SIZE:
        raise ValueError("credential exceeds the Windows Credential Manager limit")
    blob = ctypes.create_string_buffer(encoded)
    credential = _CREDENTIAL()
    credential.Type = CRED_TYPE_GENERIC
    credential.TargetName = target
    credential.CredentialBlobSize = len(encoded)
    credential.CredentialBlob = ctypes.cast(blob, wintypes.LPBYTE)
    credential.Persist = CRED_PERSIST_LOCAL_MACHINE
    credential.UserName = user
    try:
        if not advapi32.CredWriteW(ctypes.byref(credential), 0):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        ctypes.memset(ctypes.addressof(blob), 0, ctypes.sizeof(blob))
        encoded = b""


def read_credential(target: str) -> str | None:
    """Return the generic credential named ``target`` or ``None`` if absent."""
    advapi32 = _advapi32()
    handle = ctypes.POINTER(_CREDENTIAL)()
    if not advapi32.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(handle)):
        error = ctypes.get_last_error()
        if error == ERROR_NOT_FOUND:
            return None
        raise ctypes.WinError(error)
    try:
        credential = handle.contents
        raw = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        try:
            return raw.decode("utf-16-le")
        except UnicodeError as error:
            raise OSError("Windows Credential Manager returned invalid UTF-16") from error
    finally:
        advapi32.CredFree(handle)
