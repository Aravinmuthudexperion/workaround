"""Password hashing, ingest/API key generation, and symmetric encryption."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings

_PBKDF2_ROUNDS = 310_000
INGEST_KEY_PREFIX = "arc_ingest_"
API_TOKEN_PREFIX = "arc_api_"


# --- passwords ---------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        _, rounds, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds))
    return hmac.compare_digest(digest.hex(), digest_hex)


# --- opaque keys (ingest keys, API tokens) -----------------------------------

def new_opaque_key(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def hash_key(key: str) -> str:
    """Keys are stored only as SHA-256; the plain key is shown to the user once."""
    return hashlib.sha256(key.encode()).hexdigest()


def key_hint(key: str) -> str:
    return key[: len(key.split("_")[0]) + 12] + "…"


# --- encryption at rest ------------------------------------------------------

@lru_cache
def _fernet() -> Fernet:
    key = get_settings().secret_key
    if not key:
        raise RuntimeError("ARC_SECRET_KEY is not set - run `python start.py setup`")
    return Fernet(key.encode())


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("stored secret cannot be decrypted with the current ARC_SECRET_KEY") from exc


def basic_auth(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
