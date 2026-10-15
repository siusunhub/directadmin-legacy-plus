"""Write drop-ins atomically and push limits into live slices with systemctl.

Every external command runs as an argument list, never through a shell. The
runner is injectable, so the tests can check the exact commands without systemd.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .. import paths
from ..config import UNLIMITED, Config, resolve, size_bytes
from ..da_users import DAUser, list_users
from .render import properties, render_dropin

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]

# What systemd prints for "no limit" in `systemctl show`, depending on version.
UNLIMITED_SHOWN = {"infinity", str(2**64 - 1)}


class ApplyError(RuntimeError):
    pass


@dataclass
class UserResult:
    user: str
    ok: bool
    message: str
    profile: str = ""
    dropin: str = ""  # rendered text, so a dry run can show it


def run_command(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _systemctl(runner: Runner, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return runner(["systemctl", *args])
    except FileNotFoundError as exc:
        raise ApplyError("systemctl not found; apply must run on the server") from exc


def _check(result: subprocess.CompletedProcess[str], what: str) -> None:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise ApplyError(f"{what} failed: {detail or 'no output'}")


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _restore(path: Path, previous: str | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        _write_atomic(path, previous)


def _reload_quietly(runner: Runner) -> None:
    try:
        _systemctl(runner, "daemon-reload")
    except ApplyError:
        pass  # the caller already has the real error to report


def _matches(key: str, expected: str, shown: str) -> bool:
    if expected == UNLIMITED:
        return shown in UNLIMITED_SHOWN
    if key.startswith("Memory"):
        return shown == str(size_bytes(expected))
    return shown == expected


def _verify(runner: Runner, unit: str, values: dict[str, str]) -> list[str]:
    """Read back the live values and return a description of each mismatch."""
    expected = {
        "MemoryHigh": values["memory_high"],
        "MemoryMax": values["memory_max"],
        "TasksMax": values["tasks_max"],
    }
    result = _systemctl(runner, "show", unit, "-p", ",".join(expected))
    if result.returncode != 0:
        return [f"systemctl show failed for {unit}"]
    shown = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    return [
        f"{key} expected {want} got {shown.get(key, '?')}"
        for key, want in expected.items()
        if not _matches(key, want, shown.get(key, "?"))
    ]


def _apply_live(user: DAUser, values: dict[str, str], text: str, config: Config, runner: Runner) -> str:
    path = paths.dropin_path(user.uid)
    previous = path.read_text(encoding="utf-8") if path.exists() else None
    unit = f"user-{user.uid}.slice"
    try:
        _write_atomic(path, text)
        _check(_systemctl(runner, "daemon-reload"), "daemon-reload")
        props = properties(values, config.io_device)
        _check(_systemctl(runner, "set-property", "--runtime", unit, *props), "set-property")
    except ApplyError:
        _restore(path, previous)
        _reload_quietly(runner)  # put systemd back in step with the restored files
        raise
    mismatches = _verify(runner, unit, values)
    if mismatches:
        raise ApplyError("applied but verification failed: " + "; ".join(mismatches))
    return "applied and verified"


def apply_user(
    user: DAUser,
    config: Config,
    *,
    runner: Runner = run_command,
    dry_run: bool = False,
    check_devices: bool = True,
) -> UserResult:
    profile, values = resolve(config, user.name, user.package)
    if values["limits"] == "off":
        if not dry_run and user.uid is not None:
            try:
                plan = stop_limits_plan(user, config)
                if plan.owned:
                    result = stop_limits(plan, runner=runner)
                    if not result.ok:
                        return result
            except (ApplyError, OSError) as exc:
                return UserResult(user.name, False, str(exc), profile)
        return UserResult(user.name, True, "limits off (disabled by admin)", profile)
    if user.uid is None:
        return UserResult(user.name, False, "no Linux account with this name, skipped")
    if config.io_device and check_devices and not Path(config.io_device).exists():
        return UserResult(user.name, False, f"io_device {config.io_device} does not exist", profile)
    text = render_dropin(user.name, user.uid, values, config.io_device)
    if dry_run:
        return UserResult(user.name, True, "dry run, nothing written", profile, text)
    try:
        message = _apply_live(user, values, text, config, runner)
        return UserResult(user.name, True, message, profile, text)
    except (ApplyError, OSError) as exc:
        return UserResult(user.name, False, str(exc), profile, text)


def apply_all(
    config: Config,
    *,
    runner: Runner = run_command,
    dry_run: bool = False,
    check_devices: bool = True,
) -> list[UserResult]:
    return [
        apply_user(user, config, runner=runner, dry_run=dry_run, check_devices=check_devices)
        for user in list_users()
    ]


def remove_dropins_for(username: str, runner: Runner = run_command) -> int:
    """Delete every drop-in that belongs to one user. Used after the account is destroyed.

    The UID may already be gone by then, so the header line is matched instead.
    """
    marker = f"for user {username} (UID "
    removed = 0
    for path in paths.systemd_dir().glob("user-*.slice.d/50-da-plus.conf"):
        if marker in path.read_text(encoding="utf-8", errors="replace").splitlines()[0]:
            path.unlink()
            removed += 1
    if removed:
        _reload_quietly(runner)
    return removed


@dataclass(frozen=True)
class StopLimitsPlan:
    user: str
    uid: int
    path: Path
    content: bytes | None
    roots: tuple[Path, ...]
    argv: list[str]

    @property
    def owned(self) -> bool:
        lines = self.content.splitlines() if self.content is not None else []
        return bool(lines and b"Generated by da-plus" in lines[0])


def stop_limits_plan(user: DAUser, config: Config) -> StopLimitsPlan:
    """Snapshot one slice's drop-in and exact runtime reset for confirmation."""
    from ..uninstall import UninstallError, _absolute, _read, _target
    if user.uid is None or isinstance(user.uid, bool) or user.uid <= 0:
        raise ApplyError("stop limits requires a non-root Linux account UID")
    try:
        root = _absolute(paths.systemd_dir())
        if root == Path(root.anchor):
            raise ApplyError("refusing filesystem root as the systemd directory")
        roots = (root,)
        path = _target(paths.dropin_path(user.uid), roots)
        content = _read(path, roots)
    except (UninstallError, OSError, ValueError) as exc:
        raise ApplyError(str(exc)) from exc
    argv = ["systemctl", "set-property", "--runtime", f"user-{user.uid}.slice",
            "CPUQuota=", "MemoryHigh=infinity", "MemoryMax=infinity", "TasksMax=infinity"]
    if config.io_device is not None:
        argv.extend(f"{key}={config.io_device} infinity" for key in (
            "IOReadBandwidthMax", "IOWriteBandwidthMax", "IOReadIOPSMax", "IOWriteIOPSMax"))
    return StopLimitsPlan(user.name, user.uid, path, content, roots, argv)


def stop_limits(plan: StopLimitsPlan, *, runner: Runner | None = None) -> UserResult:
    """Perform only the confirmed stop and report any partial changes."""
    from ..uninstall import UninstallError, _read, _remove
    runner = run_command if runner is None else runner
    changed: list[str] = []
    command = ""
    try:
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            raise ApplyError("run da-plus tui as root to stop limits")
        if _read(plan.path, plan.roots) != plan.content:
            raise ApplyError(f"{plan.path} changed since confirmation; left untouched")
        if plan.owned:
            _remove(plan.path, plan.content, plan.roots)
            changed.append(f"removed {plan.path}")
        for argv in (["systemctl", "daemon-reload"], plan.argv):
            command = shlex.join(argv)
            _check(runner(argv), command)
            changed.append("systemd daemon reloaded" if argv[1] == "daemon-reload"
                           else f"runtime limits cleared for user-{plan.uid}.slice")
    except (ApplyError, UninstallError, OSError, ValueError) as exc:
        detail = str(exc)
        if command and command not in detail:
            detail = f"{command} failed: {detail}"
        partial = "; ".join(changed) or "nothing"
        uncertainty = "; the failed set-property command may have changed some runtime properties" if command == shlex.join(plan.argv) else ""
        return UserResult(plan.user, False, f"{detail}; already changed: {partial}{uncertainty}; config overrides unchanged")
    kept = "" if plan.owned else f"; {'foreign drop-in kept' if plan.content is not None else 'drop-in absent'}"
    return UserResult(plan.user, True, "; ".join(changed) + kept + "; config overrides unchanged")


def write_log(results: list[UserResult], source: str) -> None:
    """Append one line per user to apply.log. Logging must never block an apply."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        path = paths.apply_log()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for result in results:
                status = "OK" if result.ok else "FAIL"
                handle.write(f"{stamp} {source} {status} {result.user}: {result.message}\n")
    except OSError:
        pass
