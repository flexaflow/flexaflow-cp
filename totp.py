"""TOTP (RFC 6238) for admin two-factor login. Standard library only.

Compatible with Google Authenticator, Microsoft Authenticator, Authy and similar apps:
30-second steps, 6 digits, HMAC-SHA1.
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP_SEC = 30
DIGITS = 6
WINDOW = 1  # accept the previous and next 30-second step to allow for clock drift


def new_secret() -> str:
    """160-bit random secret, base32 (the form authenticator apps expect)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    padded = secret.upper() + "=" * (-len(secret) % 8)
    return base64.b32decode(padded)


def hotp(secret: str, counter: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** DIGITS)
    return f"{code:0{DIGITS}d}"


def counter_at(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // STEP_SEC)


def verify(secret: str, code: str, last_counter: int = -1, now: float | None = None):
    """Return the matched time-step counter, or None.

    last_counter prevents replay: a code that was already used cannot be used again
    within its window."""
    code = (code or "").replace(" ", "").strip()
    if len(code) != DIGITS or not code.isdigit():
        return None
    current = counter_at(now)
    for delta in range(-WINDOW, WINDOW + 1):
        candidate = current + delta
        if candidate <= last_counter:
            continue
        if hmac.compare_digest(hotp(secret, candidate), code):
            return candidate
    return None


def provisioning_uri(secret: str, account: str, issuer: str) -> str:
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&digits={DIGITS}&period={STEP_SEC}"
