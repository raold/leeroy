"""AzerothCore's account verifier (SRP6 over SHA-1, as the 3.3.5a client uses), for the dojo's synthetic accounts."""

from __future__ import annotations

import hashlib
import secrets

N = int("894B645E89E1535BBDAD5B8B290650530801B18EBFBF5E8FAB3C82872A3E9BB7", 16)
G = 7
SALT_LENGTH = 32
VERIFIER_LENGTH = 32


def verifier(username: str, password: str, salt: bytes) -> bytes:
    """The 32-byte little-endian verifier the account table stores for this name, password and salt."""
    if len(salt) != SALT_LENGTH:
        raise ValueError("salt must be 32 bytes")
    inner = hashlib.sha1(f"{username.upper()}:{password.upper()}".encode("utf-8")).digest()
    x = int.from_bytes(hashlib.sha1(salt + inner).digest(), "little")
    return pow(G, x, N).to_bytes(VERIFIER_LENGTH, "little")


def make(username: str, password: str) -> tuple[bytes, bytes]:
    salt = secrets.token_bytes(SALT_LENGTH)
    return salt, verifier(username, password, salt)
