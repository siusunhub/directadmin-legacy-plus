"""Central place for every filesystem path da-plus touches.

The defaults match a real DirectAdmin server. Each one can be overridden with
an environment variable, which is how the tests and a laptop demo run against
a fake directory tree instead of /usr/local/directadmin.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path


def _root(env_name: str, default: str) -> Path:
    return Path(os.environ.get(env_name, default))


def da_root() -> Path:
    return _root("DA_PLUS_DA_ROOT", "/usr/local/directadmin")


def cgroup_root() -> Path:
    return _root("DA_PLUS_CGROUP_ROOT", "/sys/fs/cgroup")


class SystemdDirectoryError(ValueError):
    """The systemd administrator unit directory could not be selected safely."""


def systemd_dir(*, timeout: float | None = None) -> Path:
    if "DA_PLUS_SYSTEMD_DIR" in os.environ:
        return Path(os.environ["DA_PLUS_SYSTEMD_DIR"])
    manual = "Set DA_PLUS_SYSTEMD_DIR manually to the administrator unit directory."
    try:
        result = subprocess.run(["systemd-analyze", "unit-paths"], capture_output=True,
                                text=True, check=False,
                                **({"timeout": timeout} if timeout is not None else {}))
    except OSError as exc:
        raise SystemdDirectoryError(f"checked systemd-analyze unit-paths: {exc}. {manual}") from exc
    if result.returncode != 0:
        reason = " ".join((result.stderr or result.stdout or "no output").splitlines())
        raise SystemdDirectoryError(
            f"checked systemd-analyze unit-paths: exit {result.returncode}: {reason}. {manual}"
        )
    entries = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    administrator = "/etc/systemd/system"
    if administrator not in entries:
        raise SystemdDirectoryError(
            "checked systemd-analyze unit-paths entries: " + (", ".join(entries) or "(empty output)")
            + f"; {administrator} is not listed. {manual}"
        )
    candidate = Path(administrator)
    # Do not resolve links or consider runtime/vendor paths as alternatives.
    for component in reversed((candidate, *candidate.parents)):
        if component.is_symlink() or (hasattr(component, "is_junction") and component.is_junction()):
            raise SystemdDirectoryError(
                f"checked systemd-analyze unit-paths: refusing symbolic link or junction {component} "
                f"in directory {candidate}. {manual}"
            )
    if not candidate.is_dir():
        raise SystemdDirectoryError(
            f"checked systemd-analyze unit-paths: {administrator} is listed but is not a directory. {manual}"
        )
    return candidate


def config_file() -> Path:
    return _root("DA_PLUS_CONFIG", "/etc/da-plus/config.yml")


def state_dir() -> Path:
    return _root("DA_PLUS_STATE_DIR", "/var/lib/da-plus")


def log_dir() -> Path:
    return _root("DA_PLUS_LOG_DIR", "/var/log/da-plus")


def user_data_dir() -> Path:
    return da_root() / "data" / "users"


def custom_scripts_dir() -> Path:
    return da_root() / "scripts" / "custom"


def user_slice_dir(uid: int) -> Path:
    return cgroup_root() / "user.slice" / f"user-{uid}.slice"


def block_device_id(device: str) -> str:
    """Read the kernel major:minor identifier used by cgroup io.max."""
    info = Path(device).stat()
    if not stat.S_ISBLK(info.st_mode):
        raise ValueError("not a block device")
    return f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}"


def dropin_path(uid: int, *, timeout: float | None = None) -> Path:
    directory = systemd_dir() if timeout is None else systemd_dir(timeout=timeout)
    return directory / f"user-{uid}.slice.d" / "50-da-plus.conf"


def apply_log() -> Path:
    return log_dir() / "apply.log"


def hook_log() -> Path:
    return log_dir() / "hook.log"
