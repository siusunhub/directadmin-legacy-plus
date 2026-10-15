"""Conservative uninstall: preview by default, exact owned files only, no recursive deletion."""
from __future__ import annotations

import errno
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import config, install as installer, paths
from .hooks import HOOK_MARKER, HOOK_NAMES
from .systemd.render import properties


class UninstallError(ValueError):
    pass


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))  # normalize .. without resolving/following links


def _no_links(path: Path) -> None:
    for node in reversed((path, *path.parents)):
        try:
            mode = node.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode) or (hasattr(node, "is_junction") and node.is_junction()):
            raise UninstallError(f"refusing symlink or junction: {node}")


def _roots() -> tuple[Path, ...]:
    roots = tuple(_absolute(p) for p in (
        paths.systemd_dir(), paths.custom_scripts_dir(), paths.config_file().parent,
        paths.state_dir(), paths.log_dir(),
    ))
    for root in roots:
        if root == Path(root.anchor):
            raise UninstallError(f"refusing filesystem root as an uninstall directory: {root}")
        _no_links(root)
    return roots


def _target(path: Path, roots: tuple[Path, ...]) -> Path:
    path = _absolute(path)
    if not any(path.is_relative_to(root) for root in roots):
        raise UninstallError(f"{path} is outside the configured uninstall directories")
    _no_links(path)
    return path


@contextmanager
def _parent(path: Path, roots: tuple[Path, ...]):
    """On Unix anchor operations to directory descriptors, opening each parent without links."""
    path = _target(path, roots)
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        yield None  # Windows tests; actual removal requires a Unix root account
        return
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parent.parts[1:]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


def _read(path: Path, roots: tuple[Path, ...]) -> bytes | None:
    path = _target(path, roots)
    try:
        with _parent(path, roots) as parent:
            options = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                       | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0))
            fd = os.open(path.name if parent is not None else path, options,
                         **({"dir_fd": parent} if parent is not None else {}))
            with os.fdopen(fd, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise UninstallError(f"not a regular file: {path}")
                return handle.read()
    except FileNotFoundError:
        return None


def _log_identity(path: Path, roots: tuple[Path, ...]) -> tuple[int, int, int, int] | None:
    """Snapshot regular logs without reading potentially gigabytes of log data."""
    path = _target(path, roots)
    try:
        with _parent(path, roots) as parent:
            info = (os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                    if parent is not None else path.lstat())
        if not stat.S_ISREG(info.st_mode):
            raise UninstallError(f"not a regular log file: {path}")
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
    except FileNotFoundError:
        return None


def _directory_names(path: Path, roots: tuple[Path, ...]) -> list[str]:
    path = _target(path, roots)
    try:
        with _parent(path, roots) as parent:
            if parent is None:
                return sorted(os.listdir(_target(path, roots)))
            fd = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                return sorted(os.listdir(fd))
            finally:
                os.close(fd)
    except FileNotFoundError:
        return []


def _remove(path: Path, expected: bytes | tuple[int, int, int, int], roots: tuple[Path, ...]) -> None:
    path = _target(path, roots)
    current = _log_identity(path, roots) if isinstance(expected, tuple) else _read(path, roots)
    if current != expected:
        raise UninstallError(f"{path} changed since the plan; left untouched")
    with _parent(path, roots) as parent:
        _target(path, roots)
        os.unlink(path.name, dir_fd=parent) if parent is not None else path.unlink()


def _restore(path: Path, expected: bytes, backup: Path, original: bytes,
             roots: tuple[Path, ...]) -> None:
    path, backup = _target(path, roots), _target(backup, roots)
    if path.parent != backup.parent:
        raise UninstallError("hook backup and wrapper must share a directory")
    if _read(path, roots) != expected or _read(backup, roots) != original:
        raise UninstallError(f"{path} or {backup} changed since the plan; left untouched")
    with _parent(path, roots) as parent:
        _target(path, roots)
        _target(backup, roots)
        # Atomic replacement removes our wrapper and restores the original inode/mode.
        if parent is None:
            os.replace(backup, path)
        else:
            os.replace(backup.name, path.name, src_dir_fd=parent, dst_dir_fd=parent)


def _empty_directory(path: Path, roots: tuple[Path, ...]) -> bool:
    path = _target(path, roots)
    try:
        with _parent(path, roots) as parent:
            os.rmdir(path.name, dir_fd=parent) if parent is not None else path.rmdir()
        return True
    except OSError as exc:
        if exc.errno in {errno.ENOTEMPTY, errno.EEXIST}:
            return False
        raise


@contextmanager
def _locked(roots: tuple[Path, ...]):
    lock = _target(paths.state_dir() / "install.lock", roots)
    state = _target(lock.parent, roots)
    with _parent(state, roots) as parent:
        try:
            os.mkdir(state.name, 0o700, dir_fd=parent) if parent is not None else state.mkdir(mode=0o700)
        except FileExistsError:
            if not stat.S_ISDIR(state.lstat().st_mode):
                raise UninstallError(f"not a state directory: {state}")
    with _parent(lock, roots) as parent:
        try:
            fd = os.open(lock.name if parent is not None else lock,
                         os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600,
                         **({"dir_fd": parent} if parent is not None else {}))
        except FileExistsError as exc:
            raise UninstallError(f"install lock already held at {lock}; no removal performed") from exc
    identity = os.fstat(fd)
    try:
        try:
            pid = str(os.getpid()).encode("ascii")
            if os.write(fd, pid) != len(pid):
                raise UninstallError("cannot write complete PID to install lock")
        finally:
            os.close(fd)
        yield
    finally:
        _target(lock, roots)
        current = lock.lstat()
        if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
            raise UninstallError(f"install lock changed; not removing {lock}")
        with _parent(lock, roots) as parent:
            os.unlink(lock.name, dir_fd=parent) if parent is not None else lock.unlink()


def _service_owned(data: bytes) -> bool:
    normalized = re.sub(
        rb'(?m)^ExecStart=(?:"(?:\\.|[^"\\])*"|[^\s"]+) -m da_plus apply --all$',
        b"ExecStart=__EXE__ -m da_plus apply --all", data,
    )
    return normalized == installer.SERVICE_UNIT.format(exe="__EXE__").encode("utf-8")


@dataclass
class _Step:
    phase: int
    path: Path
    data: bytes | tuple[int, int, int, int]
    backup: Path | None = None
    original: bytes | None = None
    runtime: bool = False


@dataclass
class _Plan:
    steps: list[_Step] = field(default_factory=list)
    lines: list[tuple[int, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    users: dict[int, str] = field(default_factory=dict)
    resets: list[list[str]] = field(default_factory=list)


def _plan(roots, reset_limits, purge_config, purge_logs) -> _Plan:
    plan = _Plan()

    def inspect(path, owned, phase, *, hook=False, runtime=False,
                keep_reason="not da-plus content or modified"):
        try:
            path = _target(path, roots)
            data = _read(path, roots)
            if data is None or not owned(data):
                plan.lines.append((phase, f"keep {path}: {'missing' if data is None else keep_reason}"))
                if hook:
                    plan.lines.append((phase, f"leave {path.name}.orig in place: wrapper missing or not ours"))
                return
            backup = path.with_name(path.name + ".orig") if hook else None
            original = _read(backup, roots) if backup is not None else None
            plan.steps.append(_Step(phase, path, data, backup if original is not None else None,
                                    original, runtime))
            plan.lines.append((phase, f"restore {backup} to {path} (replace our wrapper)" if original is not None
                               else f"remove {path}"))
            if runtime:
                uid = int(path.parent.name.removeprefix("user-").removesuffix(".slice.d"))
                name = re.search(rb"for user ([a-z_][a-z0-9_-]*) \(UID ", data.splitlines()[0])
                plan.users[uid] = name.group(1).decode() if name else f"UID {uid}"
                plan.lines.append((phase, f"remove directory {path.parent} only if empty after removing its file"))
        except (OSError, ValueError) as exc:
            plan.errors.append(f"{path}: {exc}")

    for name in HOOK_NAMES:
        inspect(paths.custom_scripts_dir() / f"{name}.sh", lambda b: HOOK_MARKER.encode() in b, 0, hook=True)
    inspect(paths.systemd_dir() / installer.SERVICE, _service_owned, 1)
    inspect(paths.systemd_dir() / installer.TIMER, lambda b: b == installer.TIMER_UNIT.encode(), 1)
    inspect(paths.systemd_dir() / "user@.service.d" / installer.DELEGATE_DROPIN_NAME,
            lambda b: b == installer.DELEGATE_DROPIN.encode(), 2)
    for name in _directory_names(paths.systemd_dir(), roots):
        match = re.fullmatch(r"user-(0|[1-9][0-9]*)\.slice\.d", name)
        if match:
            inspect(paths.dropin_path(int(match.group(1))),
                    lambda b: bool(b.splitlines()) and b"Generated by da-plus" in b.splitlines()[0],
                    3, runtime=True)
    for uid, name in plan.users.items():
        plan.lines.append((3, f"runtime limits for {name} (UID {uid}): " + (
            "reset requested" if reset_limits else "kept; clear at reboot or when reset manually")))
    if reset_limits and plan.users:
        raw = _read(paths.config_file(), roots)
        if raw is None:
            raise UninstallError("cannot reset limits without config: the configured IO device is unknown")
        cfg = config.parse(yaml.safe_load(raw.decode("utf-8")))
        unlimited = {key: "infinity" for key in (
            "cpu_quota", "memory_high", "memory_max", "tasks_max",
            "io_read_bw", "io_write_bw", "iops_read", "iops_write",
        )}
        for uid in plan.users:
            if uid == 0:
                raise UninstallError("refusing to reset runtime limits for root UID 0")
            plan.resets.append(["systemctl", "set-property", "--runtime", f"user-{uid}.slice",
                                *properties(unlimited, cfg.io_device)])
    if purge_config:
        inspect(paths.config_file(), lambda b: b in {config.EXAMPLE_TEXT.encode(), config.STARTER_TEXT.encode()}, 4,
                keep_reason="custom config retained: not byte-identical to EXAMPLE_TEXT or STARTER_TEXT")
    else:
        plan.lines.append((4, f"keep {paths.config_file()}: config retained by default"))
    if purge_logs:
        for name in ("apply.log", "hook.log"):
            path = paths.log_dir() / name
            try:
                path = _target(path, roots)
                identity = _log_identity(path, roots)
                if identity is not None:
                    plan.steps.append(_Step(5, path, identity))
                    plan.lines.append((5, f"remove {path}"))
                else:
                    plan.lines.append((5, f"keep {path}: missing"))
            except (OSError, ValueError) as exc:
                plan.errors.append(f"{path}: {exc}")
        plan.lines.append((5, f"remove only this run's {paths.state_dir() / 'install.lock'} when releasing the lock"))
    else:
        plan.lines.append((5, f"keep logs in {paths.log_dir()} and state in {paths.state_dir()}"))
    return plan


def _query(runner, unit: str, prop: str) -> str:
    result = runner(["systemctl", "show", unit, "-p", prop, "--value"])
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        raise UninstallError(f"cannot query {unit} {prop}: {result.stderr.strip() or 'no value'}")
    return value


def _timer_state(plan: _Plan, runner) -> bool:
    service = _query(runner, installer.SERVICE, "ActiveState")
    if service not in {"inactive", "failed"}:
        raise UninstallError(f"reconcile service is {service}; wait for it to finish before uninstalling; not stopped")
    active = _query(runner, installer.TIMER, "ActiveState")
    result = runner(["systemctl", "is-enabled", installer.TIMER])
    enabled = result.stdout.strip()
    if (result.returncode not in {0, 1, 4} or result.stderr.strip()
            or (enabled in {"enabled", "enabled-runtime"} and result.returncode != 0)
            or (result.returncode == 4 and enabled != "not-found")):
        raise UninstallError("cannot reliably query timer enablement; no removal performed")
    if enabled == "not-found" and _query(runner, installer.TIMER, "LoadState") != "not-found":
        raise UninstallError("timer query disagrees with LoadState; no removal performed")
    if active not in {"active", "inactive", "failed"} or enabled not in {
        "enabled", "enabled-runtime", "disabled", "static", "indirect", "not-found", "linked", "linked-runtime",
    }:
        raise UninstallError(f"unknown/unsafe timer state: {enabled}, {active}")
    changing = enabled in {"enabled", "enabled-runtime"} or active == "active"
    owned = any(step.path.name == installer.TIMER and step.phase == 1 for step in plan.steps)
    if changing and not owned:
        raise UninstallError("timer is enabled/active but its unit is not ours to remove; left untouched")
    if changing:
        if _query(runner, installer.TIMER, "NeedDaemonReload") != "no":
            raise UninstallError("timer has pending changes; review and daemon-reload before uninstalling")
        fragment = _absolute(Path(_query(runner, installer.TIMER, "FragmentPath")))
        if fragment != _absolute(paths.systemd_dir() / installer.TIMER):
            raise UninstallError("loaded timer comes from another unit path; left untouched")
        result = runner(["systemctl", "cat", installer.TIMER])
        if result.returncode != 0:
            raise UninstallError("cannot inspect timer install settings")
        lines = installer._logical_lines(result.stdout)
        if any(re.match(r"^(Also|Alias)\s*=\s*\S", line) for line in lines):
            raise UninstallError("timer has Also= or Alias=; disable could affect other units/links")
        if lines != installer._logical_lines(installer.TIMER_UNIT):
            raise UninstallError("effective timer has additional or modified settings; left untouched")
    return changing


def _command(runner, argv: list[str]) -> None:
    result = runner(argv)
    if result.returncode != 0:
        raise UninstallError(f"{' '.join(argv)} failed: {result.stderr.strip() or result.stdout.strip() or 'no output'}")


def uninstall(*, yes: bool = False, reset_limits: bool = False, purge_config: bool = False,
              purge_logs: bool = False, runner=None, report=print) -> int:
    """Print the plan; execute only with explicit confirmation and Unix root privileges."""
    runner = runner or installer._exec
    errors: list[str] = []
    try:
        if yes and (not hasattr(os, "geteuid") or os.geteuid() != 0):
            raise UninstallError("real uninstall requires root; omit --yes for a dry run")
        roots = _roots()
        plan = _plan(roots, reset_limits, purge_config, purge_logs)
        stop_timer = _timer_state(plan, runner)
        report("uninstall plan:" if yes else "dry run, nothing changed; uninstall plan:")
        if stop_timer:
            report(f"  systemctl disable --now {installer.TIMER}")
        for phase in range(6):
            if phase == 4:
                report("  systemctl daemon-reload")
                for argv in plan.resets:
                    report(f"  {' '.join(argv)}")
            for line_phase, line in plan.lines:
                if line_phase == phase:
                    report(f"  {line}")
        errors.extend(plan.errors)
        if yes:
            with _locked(roots):
                if _timer_state(plan, runner) != stop_timer:
                    raise UninstallError("timer state changed since the plan; retry uninstall")
                if stop_timer:
                    timer_step = next(step for step in plan.steps if step.path.name == installer.TIMER)
                    if _read(timer_step.path, roots) != timer_step.data:
                        raise UninstallError("timer file changed since the plan; left untouched")
                    _command(runner, ["systemctl", "disable", "--now", installer.TIMER])
                    if _timer_state(plan, runner):
                        raise UninstallError("timer remains enabled/active; no files removed")
                for phase in range(6):
                    if phase == 4:
                        for argv in [["systemctl", "daemon-reload"], *plan.resets]:
                            try:
                                _command(runner, argv)
                            except (OSError, ValueError) as exc:
                                errors.append(str(exc))
                    for step in (step for step in plan.steps if step.phase == phase):
                        try:
                            if step.backup is not None:
                                _restore(step.path, step.data, step.backup, step.original, roots)
                            else:
                                _remove(step.path, step.data, roots)
                            if step.runtime:
                                if not _empty_directory(step.path.parent, roots):
                                    report(f"keep {step.path.parent}: directory is not empty")
                        except (OSError, ValueError) as exc:
                            errors.append(f"{step.path}: {exc}")
    except (OSError, ValueError, yaml.YAMLError) as exc:
        errors.append(str(exc))
    for error in errors:
        report(f"ERROR: {error}")
    return 1 if errors else 0
