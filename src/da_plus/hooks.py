"""Entry point for DirectAdmin hook scripts: `da-plus hook <name> <user>`.

A hook must never block account creation, so run_hook always returns 0 and
writes its own errors to hook.log. The reconciler timer retries later.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from . import config as config_mod
from . import da_users, paths
from .systemd.apply import apply_user, remove_dropins_for, write_log

HOOK_NAMES = ("user_create_post", "user_modify_post", "user_destroy_post")


HOOK_MARKER = "# Installed by da-plus"


def hook_script(name: str, original: Path | None = None) -> str:
    """Generated shell wrapper. The logic stays in Python, so the script stays this small.

    `original` is a backed-up hook that the admin had before; it runs first so their
    automation keeps working. DirectAdmin may pass the username as $username or as $1,
    so both are accepted (verify which one your version uses on a test server).
    """
    # The original runs first and its exit status is kept, so its failures stay visible.
    # da-plus itself never fails the DirectAdmin action.
    if original is not None:
        # A backed-up hook that has gone missing is an error, not a silent success.
        chain = (
            "ORIG_RC=0\n"
            f'if [ -x "{original.as_posix()}" ]; then\n'
            f'  "{original.as_posix()}" "$@"\n'
            "  ORIG_RC=$?\n"
            "else\n"
            f'  echo "da-plus: backed-up hook {original.as_posix()} is missing or not executable" >&2\n'
            "  ORIG_RC=127\n"
            "fi\n"
        )
    else:
        chain = "ORIG_RC=0\n"
    python = Path(sys.executable).as_posix()
    return (
        "#!/bin/sh\n"
        f"{HOOK_MARKER}; do not edit, logic lives in the Python package.\n"
        + chain
        + 'U="${username:-$1}"\n'
        f'"{python}" -m da_plus hook {name} "$U" >/dev/null 2>&1\n'
        "exit $ORIG_RC\n"
    )


def _log(line: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        path = paths.hook_log()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {line}\n")
    except OSError:
        pass


def _apply(username: str) -> str:
    user = da_users.get_user(username)
    if user is None:
        return "not a DirectAdmin user, nothing to do"
    result = apply_user(user, config_mod.load())
    write_log([result], source="hook")
    return result.message


def _destroy(username: str) -> str:
    removed = remove_dropins_for(username)
    if paths.config_file().is_file():
        raw = config_mod.load_raw()
        if username in (raw.get("users") or {}):
            config_mod.save_raw(config_mod.remove_user(raw, username))
    return f"removed {removed} drop-in(s) and config entry"


def run_hook(name: str, username: str) -> int:
    try:
        username = da_users.require_username(username)
        message = _destroy(username) if name == "user_destroy_post" else _apply(username)
        _log(f"{name} {username}: {message}")
    except Exception as exc:  # a hook must never fail the DirectAdmin action
        _log(f"{name} {username}: ERROR {exc}")
    return 0
