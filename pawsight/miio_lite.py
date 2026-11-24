"""Minimal client for the local miIO protocol: only what PTZ needs.

Why our own instead of python-miio: it pulls in the `netifaces` dependency,
which does not build without a compiler, and that would mean putting gcc into
the container. The protocol itself is about fifty lines, and correctness is
checked by comparing against python-miio on a host where it is installed.

Packet format:
    16-byte header: magic 0x2131 | length | 0 | did | stamp
    16-byte checksum: MD5(header + token + ciphertext),
        where the token sits in place of the checksum while computing it
    payload: AES-128-CBC(key=MD5(token), iv=MD5(key+token)),
        PKCS#7 padding
"""
from __future__ import annotations

import hashlib
import json
import socket
import struct
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = 0x2131
HELLO = bytes.fromhex("21310020" + "ff" * 28)
PORT = 54321


class MiioError(RuntimeError):
    pass


def _keys(token: bytes) -> tuple[bytes, bytes]:
    key = hashlib.md5(token).digest()
    iv = hashlib.md5(key + token).digest()
    return key, iv


def _pad(data: bytes) -> bytes:
    n = 16 - len(data) % 16
    return data + bytes([n]) * n


def _unpad(data: bytes) -> bytes:
    return data[: -data[-1]] if data else data


def _encrypt(token: bytes, plain: bytes) -> bytes:
    key, iv = _keys(token)
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return enc.update(_pad(plain)) + enc.finalize()


def _decrypt(token: bytes, cipher: bytes) -> bytes:
    key, iv = _keys(token)
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return _unpad(dec.update(cipher) + dec.finalize())


class MiioDevice:
    def __init__(self, ip: str, token: str, timeout: float = 5.0) -> None:
        if len(token) != 32:
            raise MiioError(f"token must be 32 hex characters, got {len(token)}")
        self.ip = ip
        self.token = bytes.fromhex(token)
        self.timeout = timeout
        self._did: int | None = None
        self._stamp: int = 0
        self._stamp_at: float = 0.0
        self._msg_id = 1

    # ---- transport ----

    def _sock(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(self.timeout)
        return s

    def handshake(self) -> tuple[int, int]:
        """Learns the device did and its current time counter."""
        s = self._sock()
        try:
            s.sendto(HELLO, (self.ip, PORT))
            data, _ = s.recvfrom(1024)
        except socket.timeout as e:
            raise MiioError(f"camera {self.ip} does not answer on UDP/{PORT}") from e
        finally:
            s.close()
        if len(data) < 32 or struct.unpack(">H", data[:2])[0] != MAGIC:
            raise MiioError("unexpected handshake reply")
        _, _, _, did, stamp = struct.unpack(">HHIII", data[:16])
        self._did, self._stamp, self._stamp_at = did, stamp, time.time()
        return did, stamp

    def _build(self, payload: bytes) -> bytes:
        # The device counter advances on its own; we advance it locally,
        # otherwise the packet is rejected as stale.
        stamp = self._stamp + int(time.time() - self._stamp_at)
        enc = _encrypt(self.token, payload)
        header = struct.pack(">HHIII", MAGIC, 32 + len(enc), 0, self._did, stamp)
        digest = hashlib.md5(header + self.token + enc).digest()
        return header + digest + enc

    # ---- commands ----

    def send(self, method: str, params=None, retries: int = 2):
        if self._did is None or time.time() - self._stamp_at > 60:
            self.handshake()

        self._msg_id += 1
        body = json.dumps(
            {"id": self._msg_id, "method": method, "params": params or []},
            separators=(",", ":"),
        ).encode()

        last: Exception | None = None
        for attempt in range(retries + 1):
            s = self._sock()
            try:
                s.sendto(self._build(body), (self.ip, PORT))
                data, _ = s.recvfrom(4096)
            except socket.timeout as e:
                last = e
                self.handshake()          # the counter may have drifted: resync
                continue
            finally:
                s.close()

            if len(data) <= 32:
                last = MiioError("empty reply")
                continue
            try:
                res = json.loads(_decrypt(self.token, data[32:]).rstrip(b"\x00"))
            except Exception as e:
                raise MiioError("could not decrypt the reply: wrong token?") from e
            if "error" in res:
                raise MiioError(f"device returned an error: {res['error']}")
            return res.get("result")

        raise MiioError(f"no reply from {self.ip} after {retries + 1} attempts") from last

    def info(self):
        return self.send("miIO.info")
