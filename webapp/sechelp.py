#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""口令哈希 + 会话 cookie 落库加密。"""
import base64
import hashlib
import hmac
import os
import secrets

from Crypto.Cipher import AES

_PBKDF2_ROUNDS = 200_000


def _master_key() -> bytes:
    raw = os.environ.get("ZHS_SECRET") or "dev-insecure-secret-change-me"
    return hashlib.sha256(("zhsweb::" + raw).encode()).digest()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return "pbkdf2${}${}${}".format(
        _PBKDF2_ROUNDS,
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), base64.b64decode(salt_b64), int(rounds)
        )
        return hmac.compare_digest(dk, base64.b64decode(hash_b64))
    except Exception:
        return False


def encrypt(plain: str) -> bytes:
    key = _master_key()
    nonce = secrets.token_bytes(12)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ct, tag = cipher.encrypt_and_digest(plain.encode("utf-8"))
    return nonce + tag + ct


def decrypt(blob: bytes) -> str:
    if not blob:
        return ""
    key = _master_key()
    nonce, tag, ct = blob[:12], blob[12:28], blob[28:]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    return cipher.decrypt_and_verify(ct, tag).decode("utf-8")