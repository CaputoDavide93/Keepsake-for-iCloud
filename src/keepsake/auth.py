"""The page password: the one from the environment, or one changed in the page.

``KEEPSAKE_PASSWORD`` (or ``KEEPSAKE_PASSWORD_FILE``) is where the password starts.
Changing it in Settings, or running ``reset-password`` on the Mac, stores a salted
hash in ``password.json`` in the data folder, and from then on that hash is the
password: the environment value is ignored, so an old value in a ``.env`` file or
a shell history cannot log in any more. Only a hash is ever written, and nothing
here returns it.

Each stored password carries an ``epoch``, a random id that changes with every
change or reset. A logged-in session remembers the epoch it was created under, so
a change made by another process (the reset command runs in a second process in
the container) signs everyone out without the two having to talk.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from keepsake.config import ConfigError, atomic_write_json

MIN_PASSWORD = 10
MAX_PASSWORD = 256
FILE_NAME = "password.json"
# scrypt at 16 MiB of memory, comfortably inside the default OpenSSL limit and the
# container's 768 MB; PBKDF2 is the fallback for a Python built without scrypt.
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
PBKDF2_ROUNDS = 600_000


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _derive(algo: str, password: str, salt: bytes, params: Dict[str, int]) -> bytes:
    data = password.encode("utf-8")
    if algo == "scrypt":
        return hashlib.scrypt(data, salt=salt, n=params["n"], r=params["r"],
                              p=params["p"], dklen=32, maxmem=64 * 1024 * 1024)
    if algo == "pbkdf2-sha256":
        return hashlib.pbkdf2_hmac("sha256", data, salt, params["rounds"], dklen=32)
    raise ValueError("unknown password algorithm")


def make_record(password: str) -> Dict[str, Any]:
    """A new stored password: salted hash, a fresh epoch, and when it was set."""
    salt = secrets.token_bytes(16)
    if hasattr(hashlib, "scrypt"):
        algo, params = "scrypt", dict(SCRYPT)
    else:  # pragma: no cover - depends on how Python was built
        algo, params = "pbkdf2-sha256", {"rounds": PBKDF2_ROUNDS}
    return {"v": 1, "algo": algo, "params": params, "salt": _b64(salt),
            "hash": _b64(_derive(algo, password, salt, params)),
            "epoch": secrets.token_hex(8),
            "changed": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def check_record(record: Dict[str, Any], password: str) -> bool:
    """Constant-time comparison of a given password against a stored record."""
    try:
        salt = base64.b64decode(record["salt"])
        want = base64.b64decode(record["hash"])
        got = _derive(record["algo"], password, salt, record["params"])
    except (KeyError, TypeError, ValueError):
        return False
    return hmac.compare_digest(got, want)


def check_new_password(new: Any, confirm: Any = None, *, confirming: bool = True) -> str:
    """The password, or a ConfigError with a plain sentence saying why not."""
    if not isinstance(new, str) or not new:
        raise ConfigError("type the new password")
    if len(new) < MIN_PASSWORD:
        raise ConfigError(f"the new password needs at least {MIN_PASSWORD} characters")
    if len(new) > MAX_PASSWORD:
        raise ConfigError(f"the new password can have at most {MAX_PASSWORD} characters")
    if confirming and new != confirm:
        raise ConfigError("the two new passwords are not the same")
    return new


class PasswordStore:
    """The password the page checks, re-read when ``password.json`` changes."""

    def __init__(self, data_dir: Path, env_password: str = "") -> None:
        self.path = Path(data_dir) / FILE_NAME
        self.env_password = env_password
        self.lock = threading.Lock()
        self._stamp: Optional[Tuple[int, int, int]] = None
        self._record: Optional[Dict[str, Any]] = None

    def _load(self) -> Optional[Dict[str, Any]]:
        """The stored record, or None. Re-read only when the file changed."""
        try:
            st = self.path.stat()
        except OSError:
            self._stamp, self._record = None, None
            return None
        stamp = (st.st_ino, st.st_mtime_ns, st.st_size)
        if stamp != self._stamp:
            try:
                data = json.loads(self.path.read_text())
                record = data if isinstance(data, dict) and data.get("hash") else None
            except (OSError, ValueError):
                record = None
            self._stamp, self._record = stamp, record
        return self._record

    @property
    def configured(self) -> bool:
        """Is any password set at all (the page then needs a login)?"""
        with self.lock:
            return bool(self._load() or self.env_password)

    @property
    def stored(self) -> bool:
        with self.lock:
            return self._load() is not None

    @property
    def epoch(self) -> str:
        """Changes whenever the stored password changes; "env" before the first."""
        with self.lock:
            record = self._load()
            return str(record.get("epoch", "stored")) if record else "env"

    def verify(self, given: str) -> bool:
        """Once a password is stored the environment value no longer counts."""
        with self.lock:
            record = self._load()
            env = self.env_password
        if record is not None:
            return check_record(record, given)
        return bool(env) and hmac.compare_digest(given.encode(), env.encode())

    def set(self, password: str) -> str:
        """Store a new password, return its epoch. Never returns the hash."""
        record = make_record(check_new_password(password, confirming=False))
        with self.lock:
            atomic_write_json(self.path, record)
            self._stamp = None  # re-read: the file now has a new inode
        return record["epoch"]


def stored_password_exists(data_dir: Path) -> bool:
    """Used at start-up, before the server and its store exist."""
    return PasswordStore(data_dir).stored


def reset_password_command(argv: List[str]) -> int:
    """``python3 -m keepsake reset-password [--stdin | --password-file FILE]``."""
    usage = "usage: python3 -m keepsake reset-password [--stdin | --password-file FILE]"
    source, path = "prompt", ""
    if argv == ["--stdin"]:
        source = "stdin"
    elif len(argv) == 2 and argv[0] == "--password-file":
        source, path = "file", argv[1]
    elif argv:
        print(usage, file=sys.stderr)
        return 2
    try:
        if source == "file":
            new = Path(path).read_text().rstrip("\r\n")
        elif source == "stdin":
            new = sys.stdin.readline().rstrip("\r\n")
        else:
            if not sys.stdin.isatty():
                print("this needs a terminal to ask for the password without showing it; "
                      "use docker exec -it, or --stdin, or --password-file", file=sys.stderr)
                return 2
            new = getpass.getpass("New password: ")
            if getpass.getpass("New password again: ") != new:
                print("the two passwords are not the same; nothing was changed",
                      file=sys.stderr)
                return 1
        check_new_password(new, confirming=False)
        data_dir = Path(os.environ.get("KEEPSAKE_DATA") or "/data")
        data_dir.mkdir(parents=True, exist_ok=True)
        PasswordStore(data_dir).set(new)
    except (ConfigError, OSError) as exc:
        print(f"nothing was changed: {exc}", file=sys.stderr)
        return 1
    print("Password changed. Everyone who was logged in has been signed out.")
    return 0
