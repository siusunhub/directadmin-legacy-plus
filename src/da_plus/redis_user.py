"""Explicit, locked per-account Redis operations. Never use a TCP client."""

from __future__ import annotations

import os
import secrets
import shlex
import shutil
import socket
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import config, da_users, paths, redis_mgr
from .install import InstallError, _install_lock
from .systemd.render import redis_marker, render_redis_conf, render_redis_unit


class RedisUserError(RuntimeError):
    pass


@dataclass(frozen=True)
class Locations:
    unit: Path
    data: Path
    socket: Path
    connection: Path

    @property
    def conf(self) -> Path:
        return self.data / "redis.conf"

    @property
    def marker(self) -> Path:
        return self.data / ".da-plus-redis-owned"


def locations(user: str) -> Locations:
    da_users.require_username(user)
    return Locations(
        paths.systemd_dir(timeout=3) / f"da-plus-redis-{user}.service",
        paths.state_dir() / "redis" / user,
        Path(os.environ.get("DA_PLUS_REDIS_RUN_ROOT", "/run/da-plus-redis")) / user / "redis.sock",
        Path(os.environ.get("DA_PLUS_HOME_ROOT", "/home")) / user / ".da-plus-redis.env",
    )


def run_command(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=3,
                          env={**os.environ, "SYSTEMD_COLORS": "0", "NO_COLOR": "1"})


def _run(runner, argv: list[str], *, check: bool = True):
    try:
        result = runner(argv)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RedisUserError(f"{' '.join(argv)}: {type(exc).__name__}") from exc
    if check and result.returncode:
        raise RedisUserError(f"{' '.join(argv)} failed: {(result.stderr or result.stdout or 'no output').strip()}")
    return result


def _require_root() -> None:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise RedisUserError("redis-user changes require root")


def _account(name: str):
    try:
        user = da_users.get_user(name)
    except ValueError as exc:
        raise RedisUserError(str(exc)) from exc
    if user is None or user.uid is None:
        raise RedisUserError(f"{name!r} must be a DirectAdmin account with a Linux UID")
    return user


def _identity(user) -> tuple[int, int]:
    try:
        import pwd
        account = pwd.getpwnam(user.name)
    except (ImportError, KeyError) as exc:
        raise RedisUserError(f"no Linux identity for {user.name!r}") from exc
    if account.pw_uid != user.uid:
        raise RedisUserError("the account's Linux identity changed")
    return account.pw_uid, account.pw_gid


def _safe(path: Path) -> None:
    for node in (path, *path.parents):
        if node.is_symlink() or (hasattr(node, "is_junction") and node.is_junction()):
            raise RedisUserError(f"refusing symbolic link or junction: {node}")
    if path.exists() and path.is_file() and path.stat().st_nlink != 1:
        raise RedisUserError(f"refusing hard-linked file: {path}")


def _mutation_roots(name: str) -> None:
    """Reject redirected targets before even the shared lock can create a file."""
    layout = locations(name)
    forbidden = paths.da_root().resolve()
    for target in (paths.state_dir(), layout.unit, layout.data, layout.socket, layout.connection):
        _safe(target)
        absolute = target.resolve()
        if absolute == forbidden or forbidden in absolute.parents:
            raise RedisUserError(f"refusing a write target inside DirectAdmin: {target}")


def _owned(path: Path, marker: str, uid: int, mode: int, *, required: bool = False) -> bytes | None:
    _safe(path)
    if not path.exists():
        if required:
            raise RedisUserError(f"missing managed file: {path}")
        return None
    if not path.is_file():
        raise RedisUserError(f"not owned by da-plus: {path}")
    body = path.read_bytes()
    if not body.splitlines() or body.splitlines()[0] != marker.encode("utf-8"):
        raise RedisUserError(f"not owned by da-plus: {path}")
    meta = path.stat()
    if os.name != "nt" and (meta.st_uid != uid or stat.S_IMODE(meta.st_mode) != mode):
        raise RedisUserError(f"insecure owner or mode on managed file: {path}")
    return body


def _data_owned(layout: Locations, user) -> None:
    _safe(layout.data)
    if layout.data.exists():
        _owned(layout.marker, redis_marker(user.name, user.uid), user.uid, 0o600, required=True)
        if os.name != "nt" and (layout.data.stat().st_uid != user.uid or
                                 stat.S_IMODE(layout.data.stat().st_mode) != 0o700):
            raise RedisUserError(f"insecure Redis data directory: {layout.data}")


def _unit_state(runner, unit: str) -> dict[str, str]:
    result = _run(runner, ["systemctl", "show", unit, "-p", "LoadState", "-p", "ActiveState",
                           "-p", "FragmentPath", "-p", "DropInPaths"], check=False)
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if values.get("LoadState") not in {"loaded", "not-found", "masked"} or values.get("ActiveState") not in {"inactive", "failed", "active"}:
        raise RedisUserError(f"cannot establish safe state for {unit}")
    if values.get("DropInPaths", "unknown"):
        raise RedisUserError(f"foreign or unknown unit drop-ins for {unit}; refusing changes")
    return values


def _atomic_write(path: Path, content: bytes | str, mode: int, owner: tuple[int, int], *, fresh: bool = False) -> None:
    _safe(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".da-plus-tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content.encode("utf-8") if isinstance(content, str) else content)
        if hasattr(os, "chown"):
            os.chown(temporary, *owner)
        os.chmod(temporary, mode)
        _safe(path)
        if fresh:
            # Exclusive publication prevents clobbering a path occupied after preflight.
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class _Transaction:
    def __init__(self, runner):
        self.runner = runner
        self.undo = []
        self.reload_needed = False
        self.restore_services = []

    def mkdir(self, path: Path, mode: int, owner: tuple[int, int]) -> None:
        _safe(path)
        if path.exists():
            if not path.is_dir():
                raise RedisUserError(f"not a directory: {path}")
            return
        if not path.parent.exists():
            self.mkdir(path.parent, 0o755, (0, 0))
        self.undo.append(lambda: path.rmdir() if path.exists() else None)
        path.mkdir(mode=mode)
        if hasattr(os, "chown"):
            os.chown(path, *owner)
        os.chmod(path, mode)

    def write(self, path: Path, content: str, mode: int, owner: tuple[int, int], marker: str) -> None:
        before = _owned(path, marker, owner[0], mode)
        meta = path.stat() if before is not None else None

        def restore():
            if before is None:
                if _owned(path, marker, owner[0], mode) is not None:
                    path.unlink()
            else:
                _owned(path, marker, owner[0], mode)
                _atomic_write(path, before, stat.S_IMODE(meta.st_mode), (meta.st_uid, meta.st_gid))

        self.undo.append(restore)
        _atomic_write(path, content, mode, owner, fresh=before is None)

    def remove(self, path: Path, marker: str, uid: int, mode: int) -> None:
        before = _owned(path, marker, uid, mode)
        if before is None:
            return
        meta = path.stat()
        self.undo.append(lambda: _atomic_write(path, before, stat.S_IMODE(meta.st_mode),
                                               (meta.st_uid, meta.st_gid), fresh=True))
        path.unlink()

    def command(self, argv: list[str], undo: list[str] | None = None, *, restore_after_reload: bool = False) -> None:
        if argv[1] == "daemon-reload":
            self.reload_needed = True
        if restore_after_reload and undo:
            self.undo.append(lambda: self.restore_services.append(undo))
        else:
            self.undo.append((lambda: _run(self.runner, undo)) if undo else lambda: None)
        _run(self.runner, argv)

    def rollback(self) -> list[str]:
        failures = []
        for undo in reversed(self.undo):
            try:
                undo()
            except BaseException as exc:
                failures.append(str(exc) or type(exc).__name__)
        if self.reload_needed:
            try:
                _run(self.runner, ["systemctl", "daemon-reload"])
            except BaseException as exc:
                failures.append(str(exc) or type(exc).__name__)
        if failures and self.restore_services:
            failures.append("service recovery skipped because file recovery was incomplete")
            return failures
        for argv in self.restore_services:
            try:
                _run(self.runner, argv)
            except BaseException as exc:
                failures.append(str(exc) or type(exc).__name__)
        return failures


def _transaction_error(journal: _Transaction, exc: BaseException):
    errors = journal.rollback()
    detail = str(exc) or type(exc).__name__
    if errors:
        detail += "; rollback incomplete: " + "; ".join(errors)
    raise RedisUserError(detail) from exc


def _connection_text(user, layout: Locations, password: str) -> str:
    return f"{redis_marker(user.name, user.uid)}\nREDIS_SOCKET={shlex.quote(str(layout.socket))}\nREDIS_PASSWORD={shlex.quote(password)}\n"


def _preflight_enable(name: str, runner):
    redis_mgr.refuse_account_redis(runner)
    user = _account(name)
    identity = _identity(user)
    settings = config.resolve_redis(config.load(), name, user.package, require_binary=True)
    layout = locations(name)
    for path in (layout.unit, layout.data, layout.socket, layout.connection):
        _safe(path)
    if not layout.connection.parent.is_dir():
        raise RedisUserError(f"account home does not exist: {layout.connection.parent}")
    if layout.unit.exists() or layout.socket.exists():
        raise RedisUserError("Redis unit or socket path is already occupied")
    dropins = layout.unit.parent / (layout.unit.name + ".d")
    _safe(dropins)
    if dropins.exists() and any(dropins.iterdir()):
        raise RedisUserError("Redis unit drop-in directory is already occupied")
    state = _unit_state(runner, layout.unit.name)
    if state["LoadState"] != "not-found" or state["ActiveState"] not in {"inactive", "failed"}:
        raise RedisUserError(f"Redis unit already exists or is active: {layout.unit.name}")
    redis_mgr.refuse_account_redis(runner)
    _data_owned(layout, user)
    marker = redis_marker(name, user.uid)
    _owned(layout.conf, marker, user.uid, 0o600)
    _owned(layout.connection, marker, user.uid, 0o600)
    return user, identity, settings, layout


def enable(name: str, *, runner=None) -> dict:
    _require_root()
    runner = runner or run_command
    journal = _Transaction(runner)
    try:
        redis_mgr.refuse_account_redis(runner)  # Refuse ownership conflicts before any lock write.
        _mutation_roots(name)
        with _install_lock():
            user, owner, settings, layout = _preflight_enable(name, runner)
            marker = redis_marker(name, user.uid)
            password = secrets.token_urlsafe(32)
            try:
                journal.mkdir(layout.data, 0o700, owner)
                journal.write(layout.marker, marker + "\n", 0o600, owner, marker)
                journal.write(layout.conf, render_redis_conf(name, user.uid, settings, password,
                              data_dir=str(layout.data), socket_path=str(layout.socket)), 0o600, owner, marker)
                journal.write(layout.connection, _connection_text(user, layout, password), 0o600, owner, marker)
                journal.write(layout.unit, render_redis_unit(name, user.uid, settings["binary"],
                              data_dir=str(layout.data)), 0o644, (0, 0), marker)
                journal.command(["systemctl", "daemon-reload"])
                loaded = _unit_state(runner, layout.unit.name)
                if loaded["LoadState"] != "loaded" or loaded.get("FragmentPath") != str(layout.unit):
                    raise RedisUserError("systemd did not load the managed Redis unit at its expected path")
                journal.command(["systemctl", "enable", layout.unit.name], ["systemctl", "disable", layout.unit.name])
                journal.command(["systemctl", "start", layout.unit.name], ["systemctl", "stop", layout.unit.name])
            except BaseException as exc:
                _transaction_error(journal, exc)
    except (InstallError, OSError, ValueError) as exc:
        raise RedisUserError(str(exc)) from exc
    return {"unit": layout.unit.name, "socket": str(layout.socket), "enabled": True}


def _managed(name: str, runner, *, require_files: bool = False):
    user = _account(name)
    owner = _identity(user)
    layout = locations(name)
    marker = redis_marker(name, user.uid)
    unit = _owned(layout.unit, marker, 0, 0o644, required=require_files)
    _owned(layout.conf, marker, user.uid, 0o600, required=require_files)
    _owned(layout.connection, marker, user.uid, 0o600, required=require_files)
    _data_owned(layout, user)
    if unit is not None and any(line.strip().startswith(("Alias=", "Also=", "ExecStop=", "ExecStopPost="))
                                for line in unit.decode("utf-8").splitlines()):
        raise RedisUserError("managed Redis unit has unsafe extra directives; refusing changes")
    state = _unit_state(runner, layout.unit.name)
    if state["LoadState"] != "not-found" and (unit is None or state.get("FragmentPath") != str(layout.unit)):
        raise RedisUserError("Redis unit is not owned by da-plus at the expected path")
    if unit is None and state["ActiveState"] not in {"inactive", "failed"}:
        raise RedisUserError("Redis unit is active without an owned unit file")
    return user, owner, layout, state


def disable(name: str, *, purge_redis_data: bool = False, runner=None) -> dict:
    _require_root()
    runner = runner or run_command
    journal = _Transaction(runner)
    try:
        redis_mgr.refuse_account_redis(runner)
        _mutation_roots(name)
        with _install_lock():
            user, _, layout, state = _managed(name, runner)
            marker = redis_marker(name, user.uid)
            if purge_redis_data and layout.data.exists():
                # Resolve only after rejecting links; the target must remain below redis/.
                layout.data.resolve().relative_to((paths.state_dir() / "redis").resolve())
                for node in layout.data.rglob("*"):
                    _safe(node)
            try:
                if layout.unit.exists():
                    enabled = _run(runner, ["systemctl", "is-enabled", layout.unit.name], check=False).stdout.strip()
                    journal.command(["systemctl", "stop", layout.unit.name],
                                    ["systemctl", "start", layout.unit.name] if state["ActiveState"] == "active" else None,
                                    restore_after_reload=True)
                    journal.command(["systemctl", "disable", layout.unit.name],
                                    ["systemctl", "enable", layout.unit.name] if enabled == "enabled" else None,
                                    restore_after_reload=True)
                journal.remove(layout.unit, marker, 0, 0o644)
                journal.remove(layout.conf, marker, user.uid, 0o600)
                journal.remove(layout.connection, marker, user.uid, 0o600)
                journal.command(["systemctl", "daemon-reload"])
            except BaseException as exc:
                _transaction_error(journal, exc)
            if purge_redis_data and layout.data.exists():
                _data_owned(layout, user)
                shutil.rmtree(layout.data)
    except (InstallError, OSError, ValueError) as exc:
        raise RedisUserError(str(exc)) from exc
    return {"unit": layout.unit.name, "enabled": False, "data_kept": not purge_redis_data}


def _read_connection(user, layout: Locations) -> dict[str, str]:
    data = _owned(layout.connection, redis_marker(user.name, user.uid), user.uid, 0o600, required=True)
    values = {}
    for line in data.decode("utf-8").splitlines()[1:]:
        key, separator, value = line.partition("=")
        if not separator or key not in {"REDIS_SOCKET", "REDIS_PASSWORD"} or key in values:
            raise RedisUserError("invalid managed connection file")
        words = shlex.split(value)
        if len(words) != 1:
            raise RedisUserError("invalid managed connection value")
        values[key] = words[0]
    if values.get("REDIS_SOCKET") != str(layout.socket) or not values.get("REDIS_PASSWORD"):
        raise RedisUserError("connection file does not match the account's socket")
    return values


def _ping(path: Path, password: str) -> bool:
    if not hasattr(socket, "AF_UNIX"):
        raise RedisUserError("Unix sockets are unavailable on this host")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(str(path))
        with client.makefile("rb") as reply:
            secret = password.encode("utf-8")
            client.sendall(b"*2\r\n$4\r\nAUTH\r\n$" + str(len(secret)).encode("ascii") + b"\r\n" + secret + b"\r\n")
            if reply.readline(4096) != b"+OK\r\n":
                return False
            client.sendall(b"*1\r\n$4\r\nPING\r\n")
            return reply.readline(4096) == b"+PONG\r\n"


def status(name: str, *, runner=None) -> dict:
    runner = runner or run_command
    user = _account(name)
    layout = locations(name)
    result = _run(runner, ["systemctl", "is-active", layout.unit.name], check=False)
    out = {"unit": layout.unit.name, "active": result.returncode == 0 and result.stdout.strip() == "active",
           "socket_exists": layout.socket.exists(), "ping": None}
    if out["socket_exists"]:
        try:
            _safe(layout.socket)
            if os.name != "nt" and layout.socket.stat().st_uid != user.uid:
                raise RedisUserError("socket owner does not match the account")
            connection = _read_connection(user, layout)
            out["ping"] = _ping(layout.socket, connection["REDIS_PASSWORD"])
        except (OSError, ValueError, RedisUserError) as exc:
            out["ping"] = False
            out["reason"] = str(exc)
    return out


def info(name: str, *, runner=None) -> dict:
    """Read INFO memory/keyspace over this account's Unix socket; never mutate Redis."""
    result = {"memory": {}, "keyspace": {}, "reason": ""}
    binary = shutil.which("redis-cli")
    if not binary:
        result["reason"] = "redis-cli missing"
        return result
    try:
        user, layout = _account(name), locations(name)
        if not layout.socket.exists():
            result["reason"] = "socket missing"
            return result
        _safe(layout.socket)
        env = dict(os.environ)
        env.pop("REDISCLI_AUTH", None)
        if layout.connection.exists():
            env["REDISCLI_AUTH"] = _read_connection(user, layout)["REDIS_PASSWORD"]
        query = runner or subprocess.run
        for section in ("memory", "keyspace"):
            argv = [binary, "-s", str(layout.socket), "--raw", "INFO", section]
            reply = query(argv, capture_output=True, text=True, check=False, timeout=3, env=env)
            output = (reply.stdout or "").strip()
            if reply.returncode or output.startswith(("NOAUTH", "WRONGPASS", "ERR", "(error)")):
                result["reason"] = f"INFO {section} query failed"
                return result
            result[section] = dict(line.split(":", 1) for line in output.splitlines()
                                   if ":" in line and not line.startswith("#"))
        if "used_memory" not in result["memory"]:
            result["reason"] = "INFO memory has no used_memory"
    except subprocess.TimeoutExpired:
        result["reason"] = "timeout"
    except (OSError, ValueError, RedisUserError):
        result["reason"] = "socket or connection file unreadable"
    return result


def rotate_password(name: str, *, runner=None) -> dict:
    _require_root()
    runner = runner or run_command
    journal = _Transaction(runner)
    try:
        _mutation_roots(name)
        with _install_lock():
            user, owner, layout, _ = _managed(name, runner, require_files=True)
            settings = config.resolve_redis(config.load(), name, user.package, require_binary=True)
            expected_unit = render_redis_unit(name, user.uid, settings["binary"], data_dir=str(layout.data))
            if layout.unit.read_text(encoding="utf-8") != expected_unit:
                raise RedisUserError("unit differs from the managed Redis renderer; refusing restart")
            password = secrets.token_urlsafe(32)
            marker = redis_marker(name, user.uid)
            try:
                journal.write(layout.conf, render_redis_conf(name, user.uid, settings, password,
                              data_dir=str(layout.data), socket_path=str(layout.socket)), 0o600, owner, marker)
                journal.write(layout.connection, _connection_text(user, layout, password), 0o600, owner, marker)
                journal.command(["systemctl", "restart", layout.unit.name])
            except BaseException as exc:
                errors = journal.rollback()
                if not errors:
                    try:
                        _run(runner, ["systemctl", "restart", layout.unit.name])
                    except RedisUserError as recovery:
                        errors.append(str(recovery))
                detail = str(exc) + ("; rollback incomplete: " + "; ".join(errors) if errors else "")
                raise RedisUserError(detail) from exc
    except (InstallError, OSError, ValueError) as exc:
        raise RedisUserError(str(exc)) from exc
    return {"unit": layout.unit.name, "password_rotated": True}


def preview(action: str, name: str, *, purge_redis_data: bool = False, runner=None) -> list[str]:
    """Read-only preview; no root requirement, password generation, lock or files."""
    runner = runner or run_command
    _mutation_roots(name)
    if action == "enable":
        user, _, settings, layout = _preflight_enable(name, runner)
        return [f"Create {layout.data} (0700, owner {user.name})",
                f"Write {layout.conf} and {layout.connection} (0600, owner {user.name}; password generated only with --yes)",
                f"Write {layout.unit} (0644, root); maxmemory={settings['maxmemory']}; socket={layout.socket}",
                f"daemon-reload; enable and start {layout.unit.name}"]
    user = _account(name)
    layout = locations(name)
    if action == "disable":
        redis_mgr.refuse_account_redis(runner)
        _managed(name, runner)
        return [f"Stop and disable {layout.unit.name}; remove owned unit/config/connection files",
                f"{'Purge' if purge_redis_data else 'Keep'} data directory {layout.data}"]
    if action == "rotate-password":
        _managed(name, runner, require_files=True)
        return [f"Rewrite owned config/connection files with a new password; restart {layout.unit.name}"]
    raise RedisUserError(f"unknown redis-user action: {action}")
