"""Detect the shared Redis server and install it when the admin asks.

Per-user instances live in redis_user.py. Existing system Redis services are never started here.
Detection only reads state. Nothing is installed unless the caller executes a plan.
"""

from __future__ import annotations

import re
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .config import ConfigError
from .systemd.apply import Runner, run_command

SERVICE_NAMES = ("redis", "redis-server")
DEFAULT_UNIT = {"dnf": "redis", "apt-get": "redis-server"}
VERSION_RE = re.compile(r"v=(\d+(?:\.\d+)+)")

CLEANUP_UNIT_DIRS = (Path("/etc/systemd/system"), Path("/usr/lib/systemd/system"))
CLEANUP_PROC_ROOT = Path("/proc")
CLEANUP_BINARIES = (Path("/usr/local/bin/redis-server"), Path("/usr/bin/redis-server"),
                    Path("/usr/local/bin/redis-cli"), Path("/usr/bin/redis-cli"))


@dataclass(frozen=True)
class RedisCleanupUnit:
    path: Path
    binary: Path | None
    missing_binary: bool
    reason: str
    content: bytes
    error: str = ""


@dataclass(frozen=True)
class RedisCleanupProcess:
    pid: int
    executable: str
    start_time: str
    deleted: bool
    reason: str


@dataclass(frozen=True)
class RedisCleanupInventory:
    units: tuple[RedisCleanupUnit, ...]
    processes: tuple[RedisCleanupProcess, ...]
    binaries: tuple[Path, ...]
    errors: tuple[str, ...]


def cleanup_safe_path(path: Path) -> None:
    """Reject links, junctions and DirectAdmin paths before cleanup reads or writes."""
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise RedisSafetyError(f"refusing symlink/junction path {part}")
    absolute = path.absolute()
    for forbidden in (paths.da_root().absolute(), Path("/usr/local/directadmin").absolute()):
        if absolute == forbidden or forbidden in absolute.parents:
            raise RedisSafetyError(f"refusing DirectAdmin path {path}")


def cleanup_exec_path(content: bytes) -> Path | None:
    """Accept one unambiguous absolute ExecStart in the unit's Service section."""
    text = content.decode("utf-8")
    section = ""
    commands = []
    logical = ""
    for line in text.splitlines():
        line = logical + line.strip()
        if len(line) - len(line.rstrip("\\")) & 1:
            logical = line[:-1] + " "
            continue
        logical = ""
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            section = line
        elif section == "[Service]" and line.startswith("ExecStart="):
            value = line.partition("=")[2].strip()
            if not value:
                commands.clear()
            else:
                commands.append(value)
    if logical or len(commands) != 1:
        return None
    tokens = shlex.split(commands[0], posix=True)
    if not tokens:
        return None
    executable = Path(tokens[0].lstrip("-+!:@"))
    return executable if executable.is_absolute() and "$" not in str(executable) and "%" not in str(executable) else None


def cleanup_binary_missing(binary: Path) -> bool:
    try:
        binary.stat()
    except FileNotFoundError:
        return True
    return False  # Permission errors propagate; they do not prove absence.


def cleanup_unit_state(runner: Runner, unit: str) -> dict[str, str]:
    argv = ["systemctl", "show", unit]
    for prop in ("ActiveState", "SubState", "Result", "UnitFileState", "MainPID", "ExecStart"):
        argv.extend(["-p", prop])
    result = runner(argv)
    if result.returncode != 0:
        raise RedisSafetyError((result.stderr or result.stdout or "systemctl show failed").strip())
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if not values.get("ActiveState") or not values.get("MainPID", "").isdigit():
        raise RedisSafetyError("cannot verify unit state/MainPID")
    return values


def cleanup_process_identity(pid: int) -> tuple[str, str]:
    directory = CLEANUP_PROC_ROOT / str(pid)
    executable = os.readlink(directory / "exe")
    stat = (directory / "stat").read_text(encoding="utf-8")
    start_time = stat.rsplit(")", 1)[1].split()[19]
    return executable, start_time


def detect_redis_cleanup(runner: Runner = run_command) -> RedisCleanupInventory:
    """Read-only inventory of legacy Redis leftovers. Never probes a TCP port."""
    units = []
    processes: dict[int, RedisCleanupProcess] = {}
    binaries = []
    errors = []
    for directory in CLEANUP_UNIT_DIRS:
        try:
            cleanup_safe_path(directory)
            entries = list(directory.glob("redis*.service"))
        except (OSError, ValueError) as exc:
            errors.append(f"{directory}: {exc}")
            continue
        for path in sorted(entries):
            content = b""
            binary = None
            missing = False
            reason = ""
            error = ""
            try:
                cleanup_safe_path(path)
                content = path.read_bytes()
                binary = cleanup_exec_path(content)
                if binary is None:
                    raise RedisSafetyError("cannot verify an unambiguous absolute ExecStart binary")
                cleanup_safe_path(binary)
                missing = cleanup_binary_missing(binary)
                state = cleanup_unit_state(runner, path.name)
                if missing:
                    reason = f"missing ExecStart binary {binary}; state={state.get('ActiveState')} " \
                             f"result={state.get('Result')} enabled={state.get('UnitFileState')}"
                elif state.get("ActiveState") == "failed" or "start-limit" in state.get("Result", ""):
                    reason = "failed/start-limit unit with existing binary; report only, keep unit"
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                error = str(exc)
                if missing:
                    reason = f"missing ExecStart binary {binary}"
            units.append(RedisCleanupUnit(path, binary, missing, reason, content, error))
    try:
        entries = list(CLEANUP_PROC_ROOT.iterdir()) if CLEANUP_PROC_ROOT.exists() else []
    except OSError as exc:
        entries = []
        errors.append(f"process inventory: {exc}")
    for directory in entries:
        if not directory.name.isdigit():
            continue
        try:
            name = (directory / "comm").read_text(encoding="utf-8").strip()
            if name != "redis-server":
                continue
            executable, start_time = cleanup_process_identity(int(directory.name))
            if executable.endswith(" (deleted)"):
                pid = int(directory.name)
                processes[pid] = RedisCleanupProcess(pid, executable, start_time, True, "executable (deleted)")
        except FileNotFoundError:
            continue  # Process exited while inventory was being read.
        except (OSError, ValueError, IndexError) as exc:
            errors.append(f"process {directory.name}: {exc}")
    try:
        result = runner(["ss", "-ltnp"])
        if result.returncode != 0:
            raise RedisSafetyError((result.stderr or result.stdout or "ss failed").strip())
        for line in result.stdout.splitlines():
            if '"redis-server"' not in line:
                continue
            for pid_text in re.findall(r'"redis-server"\s*,\s*pid=(\d+)', line):
                pid = int(pid_text)
                try:
                    executable, start_time = cleanup_process_identity(pid)
                    deleted = executable.endswith(" (deleted)")
                    if deleted or cleanup_binary_missing(Path(executable)):
                        fields = line.split()
                        address = fields[3] if len(fields) > 3 else "unknown address"
                        processes[pid] = RedisCleanupProcess(pid, executable, start_time, deleted,
                                                             f"listener {address} without existing binary")
                except (OSError, ValueError, IndexError) as exc:
                    errors.append(f"listener process {pid}: {exc}")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        errors.append(f"listener inventory: {exc}")
    for binary in CLEANUP_BINARIES:
        try:
            if binary.exists():
                binaries.append(binary)
        except OSError as exc:
            errors.append(f"{binary}: {exc}")
    return RedisCleanupInventory(tuple(units), tuple(processes.values()), tuple(binaries), tuple(errors))


class RedisSafetyError(ConfigError):
    """Refuse Redis actions when DirectAdmin owns Redis or ownership is unknown."""


DIRECTADMIN_REDIS_MESSAGE = "DirectAdmin manages Redis; da-plus will not change it."


def directadmin_redis_enabled() -> bool:
    """Read the legacy CustomBuild ownership setting, not just directory presence."""
    options = paths.da_root() / "custombuild" / "options.conf"
    try:
        text = options.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except (OSError, UnicodeError) as exc:
        raise RedisSafetyError(f"cannot verify DirectAdmin Redis ownership: {exc}") from exc
    return any(re.fullmatch(r"redis\s*=\s*yes", line.split("#", 1)[0].strip(), re.I)
               for line in text.splitlines())


def _warn_standalone(detail: str) -> None:
    print(f"Warning: standalone Redis found ({detail}); existing Redis will be left unchanged.", file=sys.stderr)


def refuse_system_redis(runner: Runner = run_command, *, check_port: bool = False,
                       known_existing: bool = False) -> bool:
    """Gate Redis actions only. Return whether standalone Redis already exists.

    No options.conf redis=yes means standalone, even for a running named service.
    Queries and refusals are read-only; port inspection never connects.
    """
    if directadmin_redis_enabled():
        raise RedisSafetyError(DIRECTADMIN_REDIS_MESSAGE)
    findings = []
    for verb, options in (("list-unit-files", ["--type=service", "--no-legend"]),
                          ("list-units", ["--type=service", "--all", "--no-legend", "--plain"])):
        try:
            result = _run(runner, ["systemctl", verb, *options])
        except (OSError, subprocess.SubprocessError) as exc:
            raise RedisSafetyError("cannot check Redis services; Redis action refused") from exc
        if result is None or result.returncode != 0:
            raise RedisSafetyError("cannot establish whether Redis exists; Redis action refused")
        for line in result.stdout.splitlines():
            fields = line.lstrip("\u25cf ").split()
            if fields and fields[0] in ("redis.service", "redis-server.service"):
                findings.append(fields[0])
    if check_port:
        try:
            result = _run(runner, ["ss", "-H", "-ltn"])
        except (OSError, subprocess.SubprocessError) as exc:
            raise RedisSafetyError("cannot check listening TCP ports; Redis action refused") from exc
        if result is None or result.returncode != 0:
            raise RedisSafetyError("cannot check listening TCP ports; Redis action refused")
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 4 and fields[3].rsplit(":", 1)[-1] == "6379":
                findings.append(fields[3])
    if findings or known_existing:
        _warn_standalone(", ".join(sorted(set(findings))) or "existing binary/service")
        return True
    return False


@dataclass(frozen=True)
class RedisStatus:
    binary: str | None
    version: str | None
    service: str | None
    active: bool
    package_manager: str | None
    custombuild: bool  # Legacy CustomBuild options.conf sets redis=yes

    @property
    def installed(self) -> bool:
        return self.binary is not None


@dataclass(frozen=True)
class Step:
    argv: list[str]
    note: str


def _run(runner: Runner, argv: list[str]) -> subprocess.CompletedProcess[str] | None:
    """Run a command. Returns None when the program is not installed."""
    try:
        return runner(argv)
    except FileNotFoundError:
        return None


def package_manager() -> str | None:
    if shutil.which("dnf"):
        return "dnf"
    if shutil.which("apt-get"):
        return "apt-get"
    return None


def _service_name(runner: Runner) -> str | None:
    result = _run(runner, ["systemctl", "list-unit-files", "--type=service", "--no-legend"])
    if result is None or result.returncode != 0:
        return None
    units = {line.split()[0] for line in result.stdout.splitlines() if line.strip()}
    for name in SERVICE_NAMES:
        if f"{name}.service" in units:
            return name
    return None


def status(runner: Runner = run_command) -> RedisStatus:
    binary = shutil.which("redis-server")
    version = None
    if binary:
        result = _run(runner, [binary, "--version"])
        if result is not None:
            match = VERSION_RE.search(result.stdout)
            version = match.group(1) if match else None

    service = _service_name(runner)
    active = False
    if service:
        result = _run(runner, ["systemctl", "is-active", service])
        active = result is not None and result.stdout.strip() == "active"

    return RedisStatus(
        binary=binary,
        version=version,
        service=service,
        active=active,
        package_manager=package_manager(),
        custombuild=directadmin_redis_enabled(),
    )


def plan_install(st: RedisStatus, runner: Runner = run_command, *, ignore_orphan_units: bool = False) -> list[Step]:
    """Return installation commands after read-only ownership and availability checks."""
    if st.custombuild:
        raise RedisSafetyError(DIRECTADMIN_REDIS_MESSAGE)
    if not ignore_orphan_units and refuse_system_redis(runner, check_port=True, known_existing=bool(st.service or st.installed)):
        return []  # Existing standalone Redis needs no installation or service changes.
    if directadmin_redis_enabled():
        raise RedisSafetyError(DIRECTADMIN_REDIS_MESSAGE)
    steps: list[Step] = []
    unit = st.service
    manager = st.package_manager

    if not st.installed:
        if manager == "dnf":
            available = _run(runner, ["dnf", "-q", "info", "redis"])
            if available is None or available.returncode != 0:
                raise RedisSafetyError("Redis server package is unavailable in enabled repositories; configure repositories first")
            steps.append(Step(["dnf", "-y", "install", "redis"], "install only the distribution Redis server package"))
        elif manager == "apt-get":
            steps.append(Step(["apt-get", "update"], "refresh package lists"))
            steps.append(Step(["apt-get", "-y", "install", "redis-server"], "install the Redis server package"))
        else:
            return []
        unit = DEFAULT_UNIT[manager]

    if unit:
        steps.append(Step(["systemctl", "disable", "--now", f"{unit}.service"], "disable the default system Redis; per-account instances use Unix sockets"))
    return steps


def execute(steps: list[Step], runner: Runner = run_command, *, package_only: bool = False) -> tuple[bool, list[str]]:
    """Run the steps in order. Stops at the first failure."""
    log: list[str] = []
    try:
        if directadmin_redis_enabled():
            raise RedisSafetyError(DIRECTADMIN_REDIS_MESSAGE)
        if package_only:
            refuse_account_redis(runner)
        if not package_only and refuse_system_redis(runner, check_port=True):
            return True, ["Warning: standalone Redis found; existing Redis left unchanged."]
    except RedisSafetyError as exc:
        return False, [f"REFUSED: {exc}"]
    for step in steps:
        if step.argv[:1] == ["systemctl"] and step.argv[1:2] in (["start"], ["enable"]):
            try:
                # Recheck after package operations; never start an existing service,
                # including one created by a package while this plan ran.
                if refuse_system_redis(runner, check_port=True):
                    return True, [*log, "Warning: standalone Redis found; existing Redis left unchanged."]
            except RedisSafetyError as exc:
                return False, [*log, f"REFUSED: {exc}"]
        command = " ".join(step.argv)
        result = _run(runner, step.argv)
        if result is None:
            log.append(f"FAILED: {command}: program not found")
            return False, log
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "no output").strip().splitlines()[-1:]
            log.append(f"FAILED: {command}: {' '.join(detail)}")
            return False, log
        log.append(f"ok: {command}")
    return True, log


def refuse_account_redis(runner: Runner = run_command) -> None:
    """One shared refusal for CLI previews, deployment and dashboard toggles."""
    if directadmin_redis_enabled():
        raise RedisSafetyError(DIRECTADMIN_REDIS_MESSAGE)
    for unit in ("redis.service", "redis-server.service"):
        result = runner(["systemctl", "is-active", unit])
        state = result.stdout.strip()
        if state in ("active", "activating", "reloading"):
            raise RedisSafetyError(f"system {unit} is active; per-account Redis refused. Disable the shared service first.")
        if result.returncode not in (0, 3, 4) or state not in ("inactive", "failed", "unknown", "active"):
            raise RedisSafetyError(f"cannot verify system {unit} state; per-account Redis refused")


def runtime_snapshot(users: list[str], runner: Runner = run_command) -> dict:
    """Read-only dashboard snapshot; callers run this off the UI thread with timeout."""
    names = ["redis.service", "redis-server.service", *(f"da-plus-redis-{name}.service" for name in users)]
    argv = ["systemctl", "show", *names]
    for prop in ("Id", "LoadState", "ActiveState", "UnitFileState"):
        argv.extend(["-p", prop])
    result = {"installed": False, "version": "-", "managed": False, "units": {}, "error": ""}
    try:
        result["managed"] = directadmin_redis_enabled()
        binary = next((path for path in CLEANUP_BINARIES if path.name == "redis-server" and path.exists()), None)
        if binary is None:
            found = shutil.which("redis-server")
            binary = Path(found) if found else None
        result["installed"] = binary is not None
        if binary is not None:
            version = runner([str(binary), "--version"])
            match = VERSION_RE.search(version.stdout)
            result["version"] = match.group(1) if match else "N/A"
        output = runner(argv)
        if output.returncode:
            raise RedisSafetyError((output.stderr or output.stdout or "query failed").strip())
        for block in output.stdout.strip().split("\n\n"):
            properties = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            if properties.get("Id") in names:
                result["units"][properties["Id"]] = properties
    except subprocess.TimeoutExpired:
        result["error"] = "timeout"
    except (OSError, ValueError) as exc:
        result["error"] = str(exc)
    return result


def account_state(state: dict[str, str] | None) -> str:
    if state is None:
        return "N/A"
    if state.get("ActiveState") == "failed":
        return "FAILED"
    if state.get("LoadState") != "loaded":
        return "OFF"
    return "ON-running" if state.get("ActiveState") == "active" else "ON-stopped"
