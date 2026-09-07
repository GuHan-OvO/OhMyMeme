"""LAN v1 wire protocol primitives kept separate from service commands."""

import hashlib
import hmac
import json
import os
import struct
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAX_FRAME = 64 * 1024 * 1024
IV_LEN = 12
TAG_LEN = 16


@dataclass(frozen=True, slots=True)
class ReplayKey:
    """Opaque server-side identity for one authenticated command frame."""

    session_id: str
    direction: str
    sequence: bytes | int | str
    command: str


class ReplayCache:
    """Bounded, expiring replay identities kept outside the v1 wire format."""

    def __init__(
        self,
        capacity: int = 4096,
        ttl_seconds: float = 600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._capacity = capacity
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = Lock()
        self._entries: OrderedDict[ReplayKey, float] = OrderedDict()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def _expire(self, now: float) -> None:
        while self._entries:
            _, seen_at = next(iter(self._entries.items()))
            if now - seen_at < self._ttl_seconds:
                return
            self._entries.popitem(last=False)

    def check_and_record(
        self,
        session_id: str,
        direction: str,
        sequence: bytes | int | str,
        command: str,
    ) -> bool:
        """Return true for a live replay, otherwise record the new identity."""
        with self._lock:
            now = self._clock()
            self._expire(now)
            key = ReplayKey(session_id, direction, sequence, command)
            if key in self._entries:
                return True
            self._entries[key] = now
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)
            return False


def derive_key(secret):
    """Derive the unchanged v1 AES-GCM session key."""
    if not secret:
        return b"\x00" * 32
    return hashlib.pbkdf2_hmac(
        "sha256", secret.encode(), b"ohmy-meme-lan", 100000, dklen=32
    )


def proof(secret, nonce):
    """Build the unchanged v1 HMAC proof."""
    return hmac.new(secret.encode(), nonce.encode(), hashlib.sha256).hexdigest()


def encode_plain(obj):
    """Encode a length-prefixed v1 plaintext message."""
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return struct.pack(">I", len(data)) + data


def decode_plain(recv_exact):
    """Decode one bounded v1 plaintext message from a socket reader."""
    (length,) = struct.unpack(">I", recv_exact(4))
    if length > MAX_FRAME:
        raise ValueError("帧过大")
    return json.loads(recv_exact(length).decode("utf-8"))


def encode_frame(key, obj):
    """Encode one v1 AES-GCM frame and return its replay identity."""
    plain = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    iv = os.urandom(IV_LEN)
    ciphertext = AESGCM(key).encrypt(iv, plain, None)
    body = iv + ciphertext
    return struct.pack(">I", len(body)) + body


def decode_frame(recv_exact, key):
    """Decode one bounded v1 AES-GCM frame with its opaque replay identity."""
    (length,) = struct.unpack(">I", recv_exact(4))
    if length > MAX_FRAME or length < IV_LEN + TAG_LEN:
        raise ValueError("帧长度非法")
    body = recv_exact(length)
    plain = AESGCM(key).decrypt(body[:IV_LEN], body[IV_LEN:], None)
    return json.loads(plain.decode("utf-8")), hashlib.sha256(body).digest()
