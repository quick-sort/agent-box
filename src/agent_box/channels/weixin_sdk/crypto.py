from __future__ import annotations

import base64

from .exceptions import MissingDependencyError, WeixinError


def _load_aes():
    try:
        from Crypto.Cipher import AES  # type: ignore
    except ModuleNotFoundError as exc:
        raise MissingDependencyError(
            "缺少 pycryptodome。请先执行 `pip install -e .` 或 `pip install pycryptodome`。"
        ) from exc
    return AES


def encrypt_aes_ecb(plaintext: bytes, key: bytes) -> bytes:
    aes = _load_aes()
    pad_len = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([pad_len]) * pad_len
    cipher = aes.new(key, aes.MODE_ECB)
    return cipher.encrypt(padded)


def decrypt_aes_ecb(ciphertext: bytes, key: bytes) -> bytes:
    aes = _load_aes()
    cipher = aes.new(key, aes.MODE_ECB)
    padded = cipher.decrypt(ciphertext)
    if not padded:
        return padded
    pad_len = padded[-1]
    if pad_len < 1 or pad_len > 16 or padded[-pad_len:] != bytes([pad_len]) * pad_len:
        raise WeixinError("无效的 AES-128-ECB PKCS7 padding")
    return padded[:-pad_len]


def aes_ecb_padded_size(plaintext_size: int) -> int:
    return ((plaintext_size // 16) + 1) * 16


def parse_aes_key_base64(aes_key_base64: str) -> bytes:
    decoded = base64.b64decode(aes_key_base64)
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32:
        ascii_hex = decoded.decode("ascii", errors="strict")
        if all(ch in "0123456789abcdefABCDEF" for ch in ascii_hex):
            return bytes.fromhex(ascii_hex)
    raise WeixinError(
        "aes_key 解码后既不是 16-byte 原始 key，也不是 32-char hex 字符串"
    )
