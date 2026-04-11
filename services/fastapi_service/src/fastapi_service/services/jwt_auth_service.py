"""JWT signing + key distribution for the pylon master-server endpoints.

The R5 SDK game servers verify JWTs locally with a baked-in or pulled-in
RS256 public key. The token's `sessionId` claim binds a specific
(user, name, server) tuple so a token cannot be replayed for someone else.

Game-server side reference (r5v_sdk/src/engine/client/client.cpp):

    snprintf(newId, sizeof(newId), "%llu-%s-%s",
        (SteamID_t)userData, playerName,
        g_ServerHostManager.GetHostIP().c_str());
    sha256(newId)  // hex-encoded into the JWT `sessionId` claim
"""

from __future__ import annotations

import hashlib
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from loguru import logger
from shared_lib.config import settings


class JwtKeyError(RuntimeError):
    """Raised when the configured JWT keypair cannot be loaded."""


class _KeyMaterial:
    """Lazy-loaded JWT keypair, refreshed if the source files change on disk."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._private_key_obj = None
        self._private_path: Path | None = None
        self._private_mtime: float | None = None

        self._public_pem: bytes | None = None
        self._public_b64: str | None = None
        self._public_hash: str | None = None
        self._public_path: Path | None = None
        self._public_mtime: float | None = None

    def get_private_key(self):
        with self._lock:
            path = Path(settings.jwt_private_key_path)
            mtime = _safe_mtime(path)
            if (
                self._private_key_obj is None
                or self._private_path != path
                or self._private_mtime != mtime
            ):
                self._private_key_obj = _load_private_key(path)
                self._private_path = path
                self._private_mtime = mtime
                logger.info(f"Loaded JWT private key from {path}")
            return self._private_key_obj

    def get_public_pem(self) -> bytes:
        self._refresh_public_if_needed()
        assert self._public_pem is not None
        return self._public_pem

    def get_public_base64(self) -> str:
        self._refresh_public_if_needed()
        assert self._public_b64 is not None
        return self._public_b64

    def get_public_hash(self) -> str:
        self._refresh_public_if_needed()
        assert self._public_hash is not None
        return self._public_hash

    def _refresh_public_if_needed(self) -> None:
        with self._lock:
            path = Path(settings.jwt_public_key_path)
            mtime = _safe_mtime(path)
            if (
                self._public_pem is not None
                and self._public_path == path
                and self._public_mtime == mtime
            ):
                return

            pem = _read_public_pem(path, fallback_private=Path(settings.jwt_private_key_path))
            import base64

            self._public_pem = pem
            self._public_b64 = base64.b64encode(pem).decode("ascii")
            self._public_hash = hashlib.sha256(pem).hexdigest()
            self._public_path = path
            self._public_mtime = mtime
            logger.info(f"Loaded JWT public key from {path}")


def _safe_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return None


def _load_private_key(path: Path):
    if not path.exists():
        raise JwtKeyError(
            f"JWT private key not found at {path}. Generate one with:\n"
            f"  openssl genpkey -algorithm RSA -out {path} -pkeyopt rsa_keygen_bits:2048"
        )
    pem = path.read_bytes()
    passphrase = settings.jwt_private_key_passphrase or None
    try:
        return serialization.load_pem_private_key(
            pem,
            password=passphrase.encode("utf-8") if passphrase else None,
        )
    except Exception as exc:
        raise JwtKeyError(f"Failed to load JWT private key: {exc}") from exc


def _read_public_pem(path: Path, *, fallback_private: Path) -> bytes:
    """Return the public key PEM bytes.

    If the public key file does not exist, derive it from the private key on
    the fly so the deployer only has to ship one file.
    """
    if path.exists():
        return path.read_bytes()

    if not fallback_private.exists():
        raise JwtKeyError(
            f"Neither public key {path} nor private key {fallback_private} exist"
        )
    private_key = _load_private_key(fallback_private)
    return private_key.public_key().public_bytes(  # type: ignore[union-attr]
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


_KEY_MATERIAL = _KeyMaterial()


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


_IPV6_BRACKETED_RE = re.compile(r"^\[[^\]]+\]:\d+$")
_TRAILING_PORT_RE = re.compile(r"^(.*):(\d+)$")


def normalize_server_endpoint(endpoint: str, *, default_port: int | None = None) -> str:
    """Normalize a server address to the canonical form used in `sessionId`.

    Mirrors `r5v_master_server/src/pages/api/client/auth.ts:normalizeEndpoint`.
    The game server side computes the same string from
    `g_ServerHostManager.GetHostIP()`, so any divergence here breaks the
    sessionId match.
    """
    if not endpoint:
        return ""

    ep = endpoint.strip().replace('"', "")
    port = default_port if default_port is not None else settings.pylon_default_server_port

    # Already in [ipv6]:port form
    if _IPV6_BRACKETED_RE.match(ep):
        return ep

    # Multiple colons → likely IPv6
    if ep.count(":") > 1:
        m = _TRAILING_PORT_RE.match(ep)
        if m:
            addr, prt = m.group(1), m.group(2)
            bare = addr.lstrip("[").rstrip("]")
            return f"[{bare}]:{prt}"
        bare = ep.lstrip("[").rstrip("]")
        return f"[{bare}]:{port}"

    # IPv4 / hostname with optional :port
    if re.match(r"^.+:\d+$", ep):
        return ep
    return f"{ep}:{port}"


def create_auth_token(user_id: str, user_name: str, server_endpoint: str) -> str:
    """Sign a short-lived JWT carrying a `sessionId` bound to (user, name, ip).

    The R5 game server recomputes `sha256(f"{userId}-{playerName}-{serverIp}")`
    on the connecting client and rejects the token if the hash differs.
    """
    session_input = f"{user_id}-{user_name}-{server_endpoint}"
    session_hash = hashlib.sha256(session_input.encode("utf-8")).hexdigest()

    now = datetime.now(timezone.utc)
    payload = {
        "sessionId": session_hash,
        "iat": now,
        "exp": now + timedelta(seconds=max(1, int(settings.jwt_token_ttl_seconds))),
    }

    private_key = _KEY_MATERIAL.get_private_key()
    token = jwt.encode(payload, private_key, algorithm="RS256")
    if isinstance(token, bytes):  # PyJWT < 2 returns bytes
        token = token.decode("ascii")
    return token


def get_public_key_pem() -> bytes:
    return _KEY_MATERIAL.get_public_pem()


def get_public_key_base64() -> str:
    return _KEY_MATERIAL.get_public_base64()


def get_public_key_hash() -> str:
    return _KEY_MATERIAL.get_public_hash()
