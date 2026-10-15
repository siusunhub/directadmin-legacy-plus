"""Discover DirectAdmin users, their package, and their Linux UID."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import paths

USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


@dataclass(frozen=True)
class DAUser:
    name: str
    package: str | None
    uid: int | None  # None when the Linux account does not exist


def require_username(name: str) -> str:
    """Reject anything that is not a plain username before it is used in a path."""
    if not USERNAME_RE.match(name):
        raise ValueError(f"invalid username {name!r}")
    return name


def read_conf(path: Path) -> dict[str, str]:
    """Read DirectAdmin's key=value files such as user.conf."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return values


def lookup_uid(name: str) -> int | None:
    try:
        import pwd  # Unix only; on Windows this module is missing
    except ImportError:
        return None
    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError:
        return None


def get_user(name: str) -> DAUser | None:
    require_username(name)
    entry = paths.user_data_dir() / name
    if not entry.is_dir():
        return None
    conf = read_conf(entry / "user.conf")
    return DAUser(name, conf.get("package") or None, lookup_uid(name))


def list_users() -> list[DAUser]:
    base = paths.user_data_dir()
    if not base.is_dir():
        return []
    users = []
    for entry in sorted(base.iterdir()):
        if entry.is_dir() and USERNAME_RE.match(entry.name):
            user = get_user(entry.name)
            if user is not None:
                users.append(user)
    return users
