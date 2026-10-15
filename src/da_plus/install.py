"""Install da-plus onto a DirectAdmin server, safely.

Three phases, in this order:

1. Preflight: read-only checks of everything the install would touch, including the
   current state of the reconcile units (masks, unknown states). A failed check stops
   the install before installation targets change. Lock artifacts may already exist.
   Dry-run runs the same preflight without a lock.
2. Apply, under an install lock: every step registers its undo before it changes
   anything. Hooks are published last, so account events never see a half-built setup.
3. Recovery: any failure, including Ctrl+C, rolls back the completed steps and says
   plainly when recovery itself is incomplete.

Nothing is switched on unless asked for. Use --dry-run to see the plan.

Not covered by file-level recovery: effects that already happened at run time, such as a
reconcile run triggered by starting the timer, and resource changes made by hooks during
the install. The README lists these.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from . import config as config_mod
from . import da_users
from . import paths
from . import redis_mgr
from .config import EXAMPLE_TEXT, ConfigError
from .hooks import HOOK_MARKER, HOOK_NAMES, hook_script


REDIS_BINARIES = (Path("/usr/local/bin/redis-server"), Path("/usr/bin/redis-server"))


REQUIRED_CONTROLLERS = {"cpu", "io", "memory", "pids"}
SERVICE = "da-plus-reconcile.service"
TIMER = "da-plus-reconcile.timer"
DELEGATE_DROPIN_NAME = "50-da-plus-delegate.conf"
UNKNOWN = "unknown"
MASKED = {"masked", "masked-runtime"}
# Timer states install may change. not-found means a fresh install, before its unit exists.
TIMER_ENABLED_OK = {"disabled", "enabled", "not-found"}
TIMER_ACTIVE_OK = {"active", "inactive", "failed"}

SERVICE_UNIT = """[Unit]
Description=Reapply da-plus resource limits

[Service]
Type=oneshot
ExecStart={exe} -m da_plus apply --all
"""

TIMER_UNIT = """[Unit]
Description=Run da-plus reconcile every 15 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min

[Install]
WantedBy=timers.target
"""

DELEGATE_DROPIN = """[Service]
Delegate=cpu io
"""

_DELEGATE_LINE = re.compile(r"^\s*Delegate\s*=\s*(.*?)\s*$")
_TIMER_INSTALL_LINE = re.compile(r"^\s*(Also|Alias)\s*=\s*\S")
_CAT_FILE = re.compile(r"^# ((?:/|[A-Za-z]:[/\\]).+\.(?:service|conf))\s*$")
_DELEGATE_FALSE = {"no", "false", "off", "0", "n", "f"}
_DELEGATE_TRUE = {"yes", "true", "on", "1", "y", "t"}
# systemd 252's _CGROUP_MASK_ALL, including legacy and BPF controller bits.
_DELEGATE_ALL = {
    "cpu", "cpuacct", "cpuset", "io", "blkio", "memory", "devices", "pids",
    "bpf-firewall", "bpf-devices", "bpf-foreign", "bpf-socket-bind",
    "bpf-restrict-network-interfaces",
}


class InstallError(RuntimeError):
    pass


def _systemd_directory() -> Path:
    try:
        return paths.systemd_dir()
    except paths.SystemdDirectoryError as exc:
        raise InstallError(str(exc)) from exc


_menu_selection: ContextVar[dict[Path, Path | None] | None] = ContextVar("install_menu_selection", default=None)
_menu_allowed_roots: ContextVar[tuple[Path, ...] | None] = ContextVar("install_menu_allowed_roots", default=None)


def _menu_check_path(path: Path) -> None:
    roots = _menu_allowed_roots.get()
    if roots is not None:
        from .uninstall import UninstallError, _target
        try:
            _target(path, roots)
        except UninstallError as exc:
            raise InstallError(str(exc)) from exc


def _menu_target(path: Path) -> Path:
    selection = _menu_selection.get()
    target = (selection.get(path) or path) if selection is not None else path
    _menu_check_path(target)
    return target


def _menu_declined(path: Path) -> bool:
    selection = _menu_selection.get()
    return selection is not None and path in selection and selection[path] is None


def _confirm_real_install(*, hooks: bool, enable_timer: bool) -> None:
    # Reuse the existing containment/no-link rules, freezing roots before choices.
    from .uninstall import UninstallError, _roots, _target
    try:
        systemd = _systemd_directory()
        roots = _roots()
        cfg, scripts = paths.config_file(), paths.custom_scripts_dir()
        lock = paths.state_dir() / "install.lock"
        delegation = systemd / "user@.service.d" / DELEGATE_DROPIN_NAME
        targets = [cfg, systemd / SERVICE, systemd / TIMER, delegation]
        if hooks:
            for name in HOOK_NAMES:
                targets.extend([scripts / f"{name}.sh", scripts / f"{name}.sh.orig"])
        targets.append(lock)
        for target in targets:
            _target(target, roots)
        print(f"Systemd unit directory: {systemd}")
        print("Possible installation writes (conditional writes may not be needed):")
        for target in targets:
            print(f"  {target}")
        print(f"Runtime logs (not written by install): {paths.log_dir() / 'apply.log'}, {paths.log_dir() / 'hook.log'}")
        print("Atomic writes also create temporary siblings; missing parent directories may be created.")
        choices: dict[Path, Path | None] = {}
        for target in targets:
            while True:
                answer = input(f"Confirm {target}? [y]es / [n]o skip / [c]hange path: ").strip().lower()
                if answer in {"y", "yes"}:
                    choices[target] = target
                    break
                if answer in {"n", "no"}:
                    choices[target] = None
                    break
                if answer in {"c", "change"}:
                    try:
                        choices[target] = _target(Path(input("New path: ").strip()), roots)
                    except (UninstallError, OSError, ValueError) as exc:
                        print(f"Path refused: {exc}")
                        continue
                    break
                print("Choose y, n or c.")
        print("Confirmed installation summary:")
        for source, destination in choices.items():
            print(f"  {source}: {'SKIPPED (declined)' if destination is None else destination}")
        if input("Proceed? [y/N]: ").strip().lower() not in {"y", "yes"}:
            print("Install cancelled; no changes.")
            return
        # These combinations cannot preserve the existing locking/runtime guarantees.
        if choices[lock] != lock:
            raise InstallError("the shared install lock cannot be skipped or relocated; no changes made")
        destinations = [os.path.normcase(os.path.abspath(p)) for p in choices.values() if p is not None]
        if len(destinations) != len(set(destinations)):
            raise InstallError("duplicate installation destinations; no changes made")
        if any(destination is not None and destination != source and destination in targets
               for source, destination in choices.items()):
            raise InstallError("a changed path cannot replace another installation target; no changes made")
        if choices[delegation] not in {None, delegation}:
            raise InstallError("systemd delegation must use its dedicated drop-in path; no changes made")
        if enable_timer and any(choices[p] != p for p in (cfg, systemd / SERVICE, systemd / TIMER, delegation)):
            raise InstallError("timer activation requires the normal config, units and delegation paths; no changes made")
        if hooks and choices[cfg] != cfg:
            raise InstallError("hooks require the normal config path; no changes made")
        if choices[cfg] not in {None, cfg}:
            print("Custom config path selected. Configure DA_PLUS_CONFIG for later commands; no runtime environment is installed.")
        token = _menu_selection.set(choices)
        roots_token = _menu_allowed_roots.set(roots)
        try:
            environment = {"DA_PLUS_CONFIG": str(choices[cfg])} if choices[cfg] is not None else {}
            with _menu_environment(environment):
                install(hooks=hooks, enable_timer=enable_timer)
        finally:
            _menu_allowed_roots.reset(roots_token)
            _menu_selection.reset(token)
    except UninstallError as exc:
        raise InstallError(str(exc)) from exc


@contextmanager
def _menu_environment(values: dict[str, str]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def interactive_install(*, hooks: bool, enable_timer: bool, require_root: Callable) -> None:
    """CLI-only selection; programmatic and non-interactive install stay unchanged."""
    available = paths.da_root().is_dir()
    if not available:
        print("DirectAdmin is not installed on this machine")
    while True:
        print("0. Test install (dry run: checks everything, changes nothing, writes nowhere)")
        print("1. Real install (writes to the real system paths after you confirm each one)")
        print("2. Force remove Redis (separate; real removal only; not part of Test or Real install)")
        print("9. Quit")
        answer = input("Choose [0/1/2/9]: ").strip()
        if answer == "2":
            from argparse import Namespace
            from .cli import cmd_redis_force_remove
            result = cmd_redis_force_remove(Namespace(purge_data=False))
            if result:
                raise InstallError("Redis force removal cancelled; no install was run.")
            return
        if answer == "9":
            return
        if answer == "0":
            install(hooks=hooks, enable_timer=enable_timer, dry_run=True)
            return
        if answer == "1" and not available:
            print("Real install refused: DirectAdmin is not installed on this machine")
        elif answer == "1":
            require_root()
            _confirm_real_install(hooks=hooks, enable_timer=enable_timer)
            return
        else:
            print("Choose 0, 1, 2 or 9.")


class _StepReport:
    """Observes the existing install flow; never performs checks or filesystem changes."""

    def __init__(self, hooks: bool, enable_timer: bool, dry_run: bool) -> None:
        self.dry_run = dry_run
        self.current: str | None = None
        self.steps: dict[str, dict] = {}
        self.rollback_problems: list[str] | None = None
        self.check_errors: list[BaseException] = []
        self.setup_error: InstallError | None = None
        try:
            systemd = _systemd_directory()
        except InstallError as exc:
            self.setup_error = exc
            systemd = Path("SYSTEMD_DIRECTORY_UNAVAILABLE")  # display only; install refuses before any write
        cfg, scripts = paths.config_file(), paths.custom_scripts_dir()

        def add(key, description, requested=True, change=False, reason="will check"):
            selection = _menu_selection.get() or {}
            for source, destination in sorted(selection.items(), key=lambda pair: len(str(pair[0])), reverse=True):
                if destination is not None:
                    description = description.replace(str(source), str(destination))
            self.steps[key] = dict(number=len(self.steps) + 1, description=description,
                                   status="PENDING" if requested else "SKIPPED", change=change,
                                   reason=reason if requested else "skipped (not requested)")

        add("redis-cleanup", "Cleanup DirectAdmin-deleted Redis leftovers (keep unit files)")
        add("redis-detect", "Redis gate: detect DirectAdmin ownership and existing Redis")
        add("lock", f"Acquire install lock {paths.state_dir() / 'install.lock'}", not dry_run)
        add("config-check", f"Validate config ({cfg})")
        add("links", "Check unit/config symbolic links")
        add("pending", "Pending reload checks for reconcile units")
        add("units", "Timer/service state checks")
        add("controllers", f"Cgroup controllers ({paths.cgroup_root() / 'cgroup.controllers'})")
        add("delegation-check", "Delegation checks (template and non-root account)")
        add("hook-check", "Hook conflicts", hooks)
        add("redis-package", "Redis package: ask y/N; install server only and disable default service")
        add("config-write", f"Write config {cfg} (only if missing)", change=True, reason="will perform if missing")
        add("service", f"Write service unit {systemd / SERVICE}", change=True, reason="will perform")
        add("timer", f"Write timer unit {systemd / TIMER}", change=True, reason="will perform")
        add("delegation-write", f"Write delegation drop-in {systemd / 'user@.service.d' / DELEGATE_DROPIN_NAME}"
            " (only if needed)", change=True, reason="will perform if needed")
        add("reload", "systemctl daemon-reload", change=True, reason="will perform")
        for name in HOOK_NAMES:
            path = scripts / f"{name}.sh"
            add(f"read-{name}", f"Inspect DirectAdmin hook {path}", hooks)
            add(f"backup-{name}", f"Back up DirectAdmin hook {path} to {path}.orig (only if foreign)",
                hooks, True, "will perform if needed")
            add(f"write-{name}", f"Install DirectAdmin hook {path} (only with --hooks)",
                hooks, True, "will perform")
        add("activation-check", "Check timer activation state and Also=/Alias= (only with --enable-timer)", enable_timer)
        add("enable", f"Enable timer: systemctl enable {TIMER} (only with --enable-timer)",
            enable_timer, True, "will perform unless already enabled")
        add("start-check", "Recheck timer/service state before start", enable_timer)
        add("start", f"Start timer: systemctl start {TIMER} (only with --enable-timer)",
            enable_timer, True, "will perform")
        add("release", "Release install lock", not dry_run)
        add("status", "Report final timer state", not dry_run)
        if dry_run:
            for key in ("lock", "release", "status"):
                self.steps[key]["reason"] = "dry-run; not performed"
        self._emit("Install plan (dry-run; no changes):" if dry_run else "Install plan:")
        self._emit(f"Systemd unit directory: {systemd}")
        for key in self.steps:
            self._row(key)
        if self.setup_error is not None:
            self.mark("units", "FAILED", str(self.setup_error))

    @staticmethod
    def _emit(text: str) -> None:
        try:
            print(text.encode("ascii", "backslashreplace").decode("ascii"), flush=True)
        except (OSError, ValueError):
            pass  # output failure must not change install's checks, mutations or recovery

    def _row(self, key: str) -> None:
        step = self.steps[key]
        description = " ".join(step["description"].splitlines())
        reason = " ".join(str(step["reason"]).splitlines())
        self._emit(f"{step['number']}. {step['status']} {description} - {reason}")

    def mark(self, key: str, status: str, reason: str) -> None:
        self.steps[key].update(status=status, reason=reason)
        self._row(key)

    def perform(self, key: str, action: Callable, reason="check passed"):
        previous, self.current = self.current, key
        self.mark(key, "RUNNING", "in progress")
        try:
            result = action()
        except BaseException as exc:
            self.mark(key, "FAILED", "interrupted" if isinstance(exc, KeyboardInterrupt) else str(exc))
            raise
        else:
            if self.steps[key]["status"] == "RUNNING":
                if self.dry_run and self.steps[key]["change"]:
                    self.mark(key, "SKIPPED", "dry-run; would perform this step")
                else:
                    detail = reason(result) if callable(reason) else reason
                    self.mark(key, "DONE", detail)
            return result
        finally:
            self.current = previous

    def skip(self, key: str, reason: str) -> None:
        self.mark(key, "SKIPPED", reason)

    def check(self, key: str, action: Callable, reason="check passed"):
        """Collect read-only dry-run failures; real checks remain fail-fast."""
        try:
            return self.perform(key, action, reason)
        except (InstallError, ConfigError, OSError, subprocess.SubprocessError) as exc:
            if not self.dry_run:
                raise
            self.check_errors.append(exc)
            return None

    @staticmethod
    def _problem_action(key: str, reason: str) -> str:
        if key == "redis-detect" and redis_mgr.DIRECTADMIN_REDIS_MESSAGE in reason:
            return ("DirectAdmin manages Redis on this server. Keep it as it is. "
                    "da-plus limits are not affected by this; da-plus will not install or change Redis.")
        if "symbolic link" in reason or "symlink" in reason or "junction" in reason:
            return "Replace the link named in the reason with a regular file, or remove it, then run the dry-run again."
        if "masked" in reason or "empty file" in reason:
            return "Unmask or restore the unit file named in the reason, then run the dry-run again."
        if "pending unit changes" in reason or "daemon-reload" in reason:
            return "Run systemctl daemon-reload by hand, then run the dry-run again."
        actions = {
            "config-check": "Open /etc/da-plus/config.yml, fix the line named in the reason, then run the dry-run again.",
            "controllers": ("This server does not offer cpu/io/memory/pids at the cgroup root. "
                            "Enable the controllers or check the kernel/cgroup setup, then run the dry-run again."),
            "delegation-check": ("Another systemd drop-in sets Delegate for user@.service. "
                                 "Merge cpu io into that setting by hand, then run the dry-run again."),
        }
        if key == "config-check" and "invalid" not in reason:
            key = "other"
        if key == "controllers" and "does not offer" not in reason:
            key = "other"
        if key == "delegation-check" and not any(word in reason for word in ("Delegate", "drop-in", "instance-specific delegation")):
            key = "other"
        return actions.get(key, "Read the reason, fix it, then run the dry-run again.")

    def recovered(self, results: dict[str, list[str]], problems: list[str]) -> None:
        self.rollback_problems = problems
        for key, step in self.steps.items():
            if not step["change"] or step["status"] not in {"DONE", "FAILED"}:
                continue
            failed_undos = results.get(key, [])
            if failed_undos:
                if step["status"] == "FAILED":
                    self.mark(key, "FAILED", f"{step['reason']}; rollback failed: {'; '.join(failed_undos)}")
                else:
                    self.mark(key, "FAILED", f"rollback failed: {'; '.join(failed_undos)}")
            elif key == "reload" and any(p.startswith("daemon-reload:") for p in problems):
                self.mark(key, "FAILED", "recovery daemon-reload failed")
            elif step["status"] == "DONE":
                self.mark(key, "ROLLED BACK", "completed change recovered")
            elif key in results:
                self.mark(key, "FAILED", f"{step['reason']}; partial change rolled back")

    def finish(self, error: BaseException | None = None) -> int:
        if error is not None:
            if not any(step["status"] == "FAILED" for step in self.steps.values()):
                key = next((key for key, step in self.steps.items() if step["status"] == "PENDING"), None)
                if key is not None:
                    self.mark(key, "FAILED", "interrupted" if isinstance(error, KeyboardInterrupt) else str(error))
            for key, step in self.steps.items():
                if step["status"] == "PENDING":
                    step.update(status="NOT REACHED", reason="stopped after failure")
            code = 130 if isinstance(error, KeyboardInterrupt) else 2 if isinstance(error, (InstallError, ConfigError)) else 1
        else:
            code = 0
        problems = [(key, step) for key, step in self.steps.items()
                    if step["status"] == "FAILED"]
        if problems:
            code = code or 2
            self._emit(f"Problems found: {len(problems)}")
            for number, (key, step) in enumerate(problems, 1):
                description = " ".join(step["description"].splitlines())
                reason = " ".join(str(step["reason"]).splitlines())
                self._emit(f"{number}. Problem: {description}")
                self._emit(f"   Reason: {reason}")
                self._emit(f"   What you need to do: {self._problem_action(key, reason)}")
            blocked = [step["number"] for step in self.steps.values() if step["status"] == "NOT REACHED"]
            if blocked:
                ranges = []
                start = last = blocked[0]
                for number in blocked[1:]:
                    if number == last + 1:
                        last = number
                    else:
                        ranges.append(str(start) if start == last else f"{start}-{last}")
                        start = last = number
                ranges.append(str(start) if start == last else f"{start}-{last}")
                self._emit(f"Blocked (fix problem 1 first): steps {', '.join(ranges)}")
        else:
            self._emit("No problems found. A real install can proceed.")
        counts = {status: sum(step["status"] == status for step in self.steps.values())
                  for status in ("DONE", "SKIPPED", "FAILED")}
        suffix = ""
        if error is not None:
            suffix = "; Rollback: " + ("INCOMPLETE" if self.rollback_problems else "COMPLETE")
            if self.rollback_problems is None:
                suffix += " (no installation changes to undo)"
        self._emit(f"Summary: DONE={counts['DONE']}, SKIPPED={counts['SKIPPED']}, FAILED={counts['FAILED']}; "
                   f"exit code {code}{suffix}")
        return code


def _reported(report: _StepReport | None, key: str, action: Callable, reason="completed"):
    return report.perform(key, action, reason) if report is not None else action()


def _skip(report: _StepReport | None, key: str, reason: str) -> None:
    if report is not None:
        report.skip(key, reason)


@contextmanager
def _reported_lock(report: _StepReport) -> Iterator[None]:
    report.mark("lock", "RUNNING", "acquiring exclusive install lock")
    try:
        with _install_lock():
            report.mark("lock", "DONE", "lock acquired")
            try:
                yield
            finally:
                report.mark("release", "RUNNING", "leaving install lock context")
    except BaseException as exc:
        if report.steps["lock"]["status"] == "RUNNING":
            report.mark("lock", "FAILED", str(exc))
        elif report.steps["release"]["status"] == "RUNNING":
            report.mark("release", "DONE", "lock context exited; see any lock cleanup warning")
        raise
    else:
        report.mark("release", "DONE", "lock context exited; see any lock cleanup warning")


def _exec(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one command. Tests replace this so they never touch systemd.

    Colour is switched off so that `systemctl cat` headers are plain text the parser can read.
    """
    env = {**os.environ, "SYSTEMD_COLORS": "0", "NO_COLOR": "1"}
    return subprocess.run(argv, capture_output=True, text=True, check=False, env=env)


def _detect_existing_redis() -> str:
    """Inventory existing Redis without connecting to it or changing the server."""
    findings: list[str] = []
    errors: list[str] = []

    def query(argv: list[str], *, require_output: bool = False) -> str | None:
        try:
            result = _exec(argv)
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f"{' '.join(argv)}: {exc}")
            return None
        empty_no_match = (argv[:2] == ["systemctl", "list-unit-files"] and result.returncode == 1
                          and not result.stdout.strip() and not result.stderr.strip())
        if (result.returncode != 0 and not empty_no_match) or (require_output and not result.stdout.strip()):
            detail = (result.stderr or result.stdout or "no output").strip()
            errors.append(f"{' '.join(argv)}: {detail}")
            return None
        return result.stdout

    units: set[str] = set()
    services_checked = True
    for verb in ("list-unit-files", "list-units"):
        argv = ["systemctl", verb, "--no-legend", "--no-pager"]
        if verb == "list-units":
            argv.append("--all")
        output = query(argv + ["redis.service", "redis-server.service"])
        if output is None:
            services_checked = False
        for line in (output or "").splitlines():
            fields = line.split()
            if fields and fields[0] == "\u25cf":
                fields = fields[1:]
            if fields and fields[0] in ("redis.service", "redis-server.service"):
                units.add(fields[0])
    for unit in sorted(units):
        argv = ["systemctl", "show", unit, "--no-pager"]
        for prop in ("FragmentPath", "ExecStart", "ActiveState", "SubState", "MainPID"):
            argv.extend(["-p", prop])
        output = query(argv, require_output=True)
        findings.append(f"unit {unit}: {'; '.join((output or 'details unavailable').splitlines())}")

    output = query(["ss", "-ltnp"])
    for line in (output or "").splitlines():
        fields = line.split()
        if len(fields) >= 5 and fields[0] == "LISTEN" and fields[3].rsplit(":", 1)[-1] == "6379":
            findings.append(f"listening address {fields[3]}: {line.strip()}")

    for binary in REDIS_BINARIES:
        try:
            binary.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append(f"{binary}: {exc}")
            continue
        version = query([str(binary), "--version"], require_output=True)
        findings.append(f"binary {binary}: {' '.join((version or 'version unavailable').splitlines())}")

    try:
        managed = redis_mgr.directadmin_redis_enabled()
    except redis_mgr.RedisSafetyError as exc:
        managed = False
        errors.append(str(exc))
    reasons = findings.copy()
    if services_checked and not units:
        reasons.append("no Redis service found (systemd)")
    if managed:
        reasons.insert(0, redis_mgr.DIRECTADMIN_REDIS_MESSAGE + " Limits installation continues.")
    elif findings:
        reasons.insert(0, "Warning: standalone Redis found; limits installation continues; existing Redis left unchanged.")
    if errors:
        reasons.append("cannot verify Redis: " + "; ".join(errors) + "; limits installation continues")
    leftovers = redis_mgr.detect_redis_cleanup(_exec)
    for unit in leftovers.units:
        if unit.reason or unit.error:
            reasons.append(f"Redis leftover {unit.path}: {unit.reason or unit.error}; inspect with da-plus redis-cleanup")
    for process in leftovers.processes:
        reasons.append(f"ORPHAN Redis process PID {process.pid}: {process.reason}; inspect with da-plus redis-cleanup")
    if leftovers.errors:
        reasons.append("cannot verify Redis leftovers: " + "; ".join(leftovers.errors))
    return "; ".join(reasons)



def _check_result(argv: list[str], result: subprocess.CompletedProcess[str]) -> None:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "no output").strip()
        raise InstallError(f"{' '.join(argv)} failed: {detail}")


def _query(argv: list[str]) -> str:
    """Stdout of a read-only systemctl query. UNKNOWN when it cannot run or prints nothing."""
    try:
        result = _exec(argv)
    except OSError:
        return UNKNOWN
    return result.stdout.strip() or UNKNOWN


def _unit_enabled(name: str) -> str:
    load_state = _show_property(name, "LoadState")
    if load_state == "not-found":
        # No unit file, but a run can outlive its fragment. Only a quiescent unit counts as fresh.
        active = _show_property(name, "ActiveState")
        if active not in {"inactive", "failed"}:
            raise InstallError(f"{name} has no unit file but is '{active}'; install will not change it")
        return "not-found"
    if load_state == "masked":
        return "masked"
    if load_state != "loaded":
        raise InstallError(f"{name} LoadState is '{load_state}'; install will not change it")
    state = _query(["systemctl", "is-enabled", name])
    if state == "not-found":
        return UNKNOWN  # only a successful LoadState query establishes absence
    return state


def _unit_active(name: str) -> str:
    """Live activity. Asked of the manager even when the unit file is not found: a running unit
    can outlive its fragment, so not-found must never be read as inactive."""
    load_state = _show_property(name, "LoadState")
    if load_state not in {"loaded", "masked", "not-found"}:
        raise InstallError(f"{name} LoadState is '{load_state}'; install will not change it")
    return _show_property(name, "ActiveState")


def timer_status() -> tuple[str, str]:
    """(enabled state, active state) of the reconcile timer, as systemd reports them now."""
    return _unit_enabled(TIMER), _unit_active(TIMER)


def _check_timer_can_change(enabled: str, active: str) -> None:
    if enabled not in TIMER_ENABLED_OK:
        raise InstallError(f"reconcile timer state is '{enabled}'; install will not change it")
    if active not in TIMER_ACTIVE_OK:
        raise InstallError(f"reconcile timer is '{active}'; install will not change it")


def _service_state() -> str:
    state = _unit_active(SERVICE)
    if state not in {"active", "inactive", "failed"}:
        raise InstallError(f"reconcile service is '{state}'; wait for it to settle and check it before installing")
    return state


def _report_started_service() -> None:
    """Recovery never stops a reconcile run automatically.

    A run that is active after a failed start may have been started by the timer we enabled,
    or by something else in the same window. State alone cannot prove which, so it is reported
    as an unresolved item instead of being stopped. The rollback then reports INCOMPLETE.
    """
    state = _unit_active(SERVICE)
    if state in {"active", "activating", "deactivating", "reloading"}:
        raise InstallError(
            f"a reconcile run is {state} and was NOT stopped, because install cannot prove it started it; "
            "check it by hand"
        )
    if state not in {"inactive", "failed"}:
        raise InstallError(f"cannot confirm the reconcile service state '{state}'; check it by hand")


def _check_timer_also() -> None:
    """Refuse Also=/Alias=: enable and rollback disable can change units or alias links."""
    try:
        result = _exec(["systemctl", "cat", TIMER])
    except OSError as exc:
        raise InstallError(f"cannot run systemctl cat {TIMER} ({exc}); install stopped") from exc
    if result.returncode != 0:
        return  # not installed yet, so there is nothing to inherit
    for line in _logical_lines(result.stdout):
        setting = _TIMER_INSTALL_LINE.match(line)
        if setting:
            raise InstallError(
                f"{TIMER} has an {setting.group(1)}= setting; enabling or disabling it during rollback "
                "could change other units or alias links. "
                "Review it by hand before installing."
            )


def _systemd_arg(value: str) -> str:
    """Quote one ExecStart argument the way systemd parses it (double quotes; escape \\ " $ %)."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$").replace("%", "%%")
    return f'"{escaped}"'


def _write_atomic(path: Path, content: bytes | str, mode: int, owner: tuple[int, int] | None = None) -> None:
    """Write through a new temp file created exclusively next to the target, then replace it.

    Ownership is set before the mode: chown can clear setuid/setgid bits, so the mode goes last.
    """
    _menu_check_path(path)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".da-plus-tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content.encode("utf-8") if isinstance(content, str) else content)
        if owner is not None and hasattr(os, "chown"):
            os.chown(tmp_name, owner[0], owner[1])
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


class _Journal:
    """Performs install steps and remembers how to undo each one."""

    def __init__(self, dry_run: bool, report: _StepReport | None = None) -> None:
        self.dry_run = dry_run
        self.report = report
        self.done: list[str] = []
        self._undo: list[Callable[[], None]] = []
        self._undo_steps: dict[int, str] = {}

    def add_undo(self, undo: Callable[[], None]) -> None:
        self._undo.append(undo)
        if self.report is not None and self.report.current is not None:
            self._undo_steps[id(undo)] = self.report.current

    def write(self, path: Path, content: str, mode: int = 0o644) -> None:
        if _menu_declined(path):
            if self.report is not None and self.report.current is not None:
                self.report.skip(self.report.current, f"declined by user: {path}")
            self.done.append(f"SKIPPED {path} (declined by user)")
            return
        path = _menu_target(path)
        self.done.append(f"write {path}")
        if self.dry_run:
            return
        if path.is_symlink():
            raise InstallError(f"{path} is a symbolic link; install will not replace it")
        previous: bytes | None = None
        prev_meta: tuple[int, int, int] | None = None
        if path.exists():
            st = path.stat()
            previous = path.read_bytes()
            prev_meta = (st.st_mode & 0o7777, st.st_uid, st.st_gid)

        def restore() -> None:
            _menu_check_path(path)
            if previous is None or prev_meta is None:
                path.unlink(missing_ok=True)
                return
            mode_bits, uid, gid = prev_meta
            _write_atomic(path, previous, mode_bits, owner=(uid, gid))  # exact mode, including 0000

        # Registered before the change, so a failure midway is still undone.
        self.add_undo(restore)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(path, content, mode)

    def move(self, src: Path, dst: Path) -> None:
        src, dst = _menu_target(src), _menu_target(dst)
        self.done.append(f"back up {src} to {dst}")
        if self.dry_run:
            return
        def restore_move():
            _menu_check_path(src)
            _menu_check_path(dst)
            if dst.exists():
                os.replace(dst, src)
        self.add_undo(restore_move)
        os.replace(src, dst)

    def run(self, argv: list[str], undo: list[str] | None = None) -> None:
        self.done.append(" ".join(argv))
        if self.dry_run:
            return
        if undo is not None:
            # Registered before the command: systemctl can change state and still fail.
            self.add_undo(lambda: _check_result(undo, _exec(undo)))
        _check_result(argv, _exec(argv))

    def rollback(self) -> list[str]:
        """Undo completed steps, newest first. Every undo runs, even after an interrupt.

        Returns every problem met while undoing; an empty list means recovery is complete.
        """
        problems: list[str] = []
        results: dict[str, list[str]] = {}
        for undo in reversed(self._undo):
            key = self._undo_steps.get(id(undo))
            if key is not None:
                results.setdefault(key, [])
            try:
                undo()
            except BaseException as exc:  # a Ctrl+C here must not skip the remaining undo steps
                detail = str(exc) or type(exc).__name__
                problems.append(detail)
                if key is not None:
                    results[key].append(detail)
        if self._undo:
            try:
                _check_result(["systemctl", "daemon-reload"], _exec(["systemctl", "daemon-reload"]))
            except BaseException as exc:
                problems.append(f"daemon-reload: {exc or type(exc).__name__}")
        if self.report is not None:
            self.report.recovered(results, problems)
        return problems


def _remove_lock(path: Path) -> None:
    try:
        _menu_check_path(path)
        path.unlink(missing_ok=True)
    except (OSError, InstallError) as exc:
        print(f"warning: could not remove install lock {path} ({exc}); remove it by hand if no install is running",
              file=sys.stderr)


@contextmanager
def _install_lock() -> Iterator[None]:
    """One install at a time. A stale lock is reported, never removed automatically."""
    path = paths.state_dir() / "install.lock"
    _menu_check_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise InstallError(
            f"another da-plus install is running, or a stale lock exists at {path}; "
            "remove it only if no install is running"
        ) from exc
    try:
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
        finally:
            os.close(fd)
    except BaseException:
        _remove_lock(path)  # we created it, so we remove it
        raise
    try:
        yield
    finally:
        _remove_lock(path)


def _refuse_links(*candidates: Path) -> None:
    for path in candidates:
        if path.is_symlink():
            raise InstallError(
                f"{path} is a symbolic link (possibly a mask or a redirect); install does not replace links"
            )


def _check_config() -> None:
    """Validate exactly the config that will be kept, before anything is changed."""
    path = paths.config_file()
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else EXAMPLE_TEXT
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        config_mod.validate_text(text)
    except ConfigError as exc:
        source = str(path) if path.exists() else "the built-in example"
        raise ConfigError(f"config from {source} is invalid, install stopped before changing anything: {exc}") from exc


def _root_controllers() -> set[str]:
    path = paths.cgroup_root() / "cgroup.controllers"
    try:
        return set(path.read_text(encoding="utf-8").split())
    except OSError as exc:
        raise InstallError(f"cannot read {path} ({exc}); this server does not expose cgroups v2") from exc


def _check_pending_unit_changes() -> None:
    """Refuse pending changes to the reconcile units before replacing their files."""
    for name in (SERVICE, TIMER):
        pending = _show_property(name, "NeedDaemonReload")
        if pending == "yes":
            raise InstallError(
                f"pending unit changes for {name}; review the on-disk configuration and run "
                "systemctl daemon-reload by hand before retrying. Install did not change it."
            )
        if pending != "no":
            raise InstallError(f"{name} NeedDaemonReload is '{pending}'; check it by hand before installing")


def _check_units(systemd: Path, enable_timer: bool) -> None:
    """Refuse to touch units that are masked, empty (systemd reads those as masks), or in an unknown state."""
    for name in (SERVICE, TIMER):
        path = _menu_target(systemd / name)
        if path.is_file() and path.stat().st_size == 0:
            raise InstallError(f"{path} is an empty file, which systemd treats as a mask; install will not replace it")
        state = _unit_enabled(name)
        if state in MASKED or state == UNKNOWN:
            raise InstallError(f"{name} state is '{state}'; install will not change it. Check it by hand first.")
    if enable_timer:
        _check_timer_can_change(_unit_enabled(TIMER), _unit_active(TIMER))
        _service_state()
        _check_timer_also()


def _delegation_disk(systemd: Path, unit: str = "user@.service") -> tuple[list[str], dict[str, str]]:
    """Read backing files, then overlay local drop-ins by basename and precedence.

    Backing files stay separate: a line continuation in one file must never join the next file.
    File headers must follow a blank line and name an absolute .service/.conf path under a
    systemd load directory. Header-looking comments outside those directories stay content.
    """
    try:
        result = _exec(["systemctl", "cat", unit])
    except OSError as exc:
        raise InstallError(f"cannot run systemctl cat {unit} ({exc}); install stopped") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise InstallError(f"cannot read the on-disk {unit} configuration; install stopped")
    blocks: list[tuple[str, str]] = []
    source, lines = unit, []
    previous = ""
    header_roots = [Path(os.path.abspath(root)) for root in (
        paths.systemd_dir(), "/etc/systemd/system", "/run/systemd/system",
        "/usr/lib/systemd/system", "/lib/systemd/system", "/usr/local/lib/systemd/system",
    )]
    for line in result.stdout.splitlines():
        header = _CAT_FILE.match(line)
        if (header and previous.strip() == ""
                and any(Path(os.path.abspath(header.group(1))).is_relative_to(root)
                        for root in header_roots)):
            if lines:
                blocks.append((source, "\n".join(lines)))
            source, lines = header.group(1), []
        else:
            lines.append(line)
        previous = line
    if lines:
        blocks.append((source, "\n".join(lines)))
    main: list[str] = []
    dropins: dict[str, tuple[str, str]] = {}
    for source, text in blocks:
        if source.endswith(".conf"):
            dropins[Path(source).name] = (source, text)
        else:
            main.append(text)
    directories = [systemd / "user@.service.d"]
    if unit != "user@.service":
        directories.append(systemd / f"{unit}.d")
    try:
        for directory in directories:
            for path in sorted(directory.glob("*.conf")):
                previous = dropins.get(path.name)
                # A selected instance's drop-in takes precedence over a template's namesake.
                if (unit != "user@.service" and directory.name == "user@.service.d"
                        and previous and Path(previous[0]).parent.name == f"{unit}.d"):
                    continue
                # utf-8-sig drops a byte-order mark, which would otherwise hide the [Service] header.
                dropins[path.name] = (str(path), path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError) as exc:
        raise InstallError(f"cannot read on-disk delegation files ({exc}); install stopped") from exc
    return main, {name: text for name, (_, text) in dropins.items()}


def _logical_lines(text: str) -> list[str]:
    """Logical lines as systemd reads them.

    Blank lines and whole-line comments are skipped. An odd trailing backslash count joins
    the next line; an even count remains literal and does not continue the line.
    Comment lines inside a continuation are skipped. A blank line ends a pending continuation.
    A byte-order mark at the start of a line is ignored.
    """
    lines: list[str] = []
    pending: str | None = None
    for raw in text.splitlines():
        line = raw.lstrip("﻿").strip()
        if pending is not None:
            if not line:
                lines.append(pending)
                pending = None
                continue
            if line.startswith(("#", ";")):
                continue
            line = f"{pending} {line}"
            pending = None
        elif not line or line.startswith(("#", ";")):
            continue
        trailing_backslashes = len(line) - len(line.rstrip("\\"))
        if trailing_backslashes % 2:
            pending = line[:-1].rstrip()
        else:
            lines.append(line)
    if pending is not None:
        raise InstallError("unfinished line continuation at the end of a unit file; fix it before installing")
    return lines


def _delegate_values(text: str) -> list[str]:
    """Delegate assignments in [Service] sections of one file, in order."""
    section = ""
    values = []
    for line in _logical_lines(text):
        if line.startswith("[") and line.endswith("]"):
            section = line
            continue
        match = _DELEGATE_LINE.match(line)
        if section == "[Service]" and match:
            values.append(match.group(1))
    return values


def _delegation_layout(main: list[str], dropins: dict[str, str]) -> list[list[str]]:
    """One list of assignments per file: backing files first, then drop-ins in filename order."""
    return [_delegate_values(text) for text in main] + [_delegate_values(dropins[name]) for name in sorted(dropins)]


def _delegation_result(layout: list[list[str]]) -> set[str]:
    controllers: set[str] = set()
    for values in layout:
        for value in values:
            if not value or value.lower() in _DELEGATE_FALSE:
                controllers.clear()
            elif value.lower() in _DELEGATE_TRUE:
                controllers = set(_DELEGATE_ALL)
            else:
                try:
                    names = set(shlex.split(value))
                except ValueError as exc:
                    raise InstallError(f"invalid Delegate assignment {value!r}; review it before installing") from exc
                if not names <= _DELEGATE_ALL:
                    raise InstallError(f"unsupported Delegate controllers in {value!r}; review them before installing")
                controllers.update(names)
    return controllers


def _show_property(name: str, prop: str, *, allow_empty: bool = False) -> str:
    """Read a normalized manager property; an empty controller list can be valid."""
    try:
        result = _exec(["systemctl", "show", name, "-p", prop, "--value"])
    except OSError as exc:
        raise InstallError(f"cannot read {prop} for {name} ({exc})") from exc
    value = result.stdout.strip()
    if result.returncode != 0 or (not value and not allow_empty):
        detail = (result.stderr or "missing property value").strip()
        raise InstallError(f"cannot read {prop} for {name} ({detail})")
    return value


def _effective_delegation() -> tuple[str, set[str]]:
    """Sample an existing non-root DirectAdmin account; never substitute the root manager."""
    candidates = [user for user in da_users.list_users() if user.uid is not None and user.uid > 0]
    if not candidates:
        raise InstallError("no non-root DirectAdmin user with a Linux UID is available; create or restore an account before installing")
    problems = []
    for user in candidates:
        unit = f"user@{user.uid}.service"
        try:
            controllers = _show_property(unit, "DelegateControllers", allow_empty=True)
        except InstallError as exc:
            problems.append(str(exc))
            continue
        return unit, set(controllers.split())
    raise InstallError("cannot query delegation for any non-root DirectAdmin user; check systemctl before retrying: "
                       + "; ".join(problems))


def _check_delegation(systemd: Path) -> bool:
    """Return True when a delegation drop-in must be added. Raises when that would conflict."""
    dropin = systemd / "user@.service.d" / DELEGATE_DROPIN_NAME
    _refuse_links(dropin)
    if dropin.exists() and dropin.read_text(encoding="utf-8", errors="replace") != DELEGATE_DROPIN:
        raise InstallError(f"{dropin} exists with other content; move it aside. Install did not change it.")
    main, dropins = _delegation_disk(systemd)
    layout = _delegation_layout(main, dropins)
    if any(value.lower() in _DELEGATE_FALSE for values in layout for value in values):
        raise InstallError("user@.service explicitly disables delegation (Delegate=no or equivalent); "
                           "review that setting by hand before installing. Install did not change it.")
    disk = _delegation_result(layout)
    unit, loaded = _effective_delegation()
    if loaded != disk:
        raise InstallError(f"delegation for {unit} differs from the on-disk template; run systemctl daemon-reload "
                           "first, then retry. If it still differs, review instance-specific overrides. Install did not change it.")
    instance_main, instance_dropins = _delegation_disk(systemd, unit)
    # Equal current masks alone cannot prove a proposed addition survives an instance reset.
    template_settings = {name: _delegate_values(text) for name, text in dropins.items() if _delegate_values(text)}
    instance_settings = {name: _delegate_values(text) for name, text in instance_dropins.items() if _delegate_values(text)}
    if ([_delegate_values(text) for text in instance_main] != [_delegate_values(text) for text in main]
            or instance_settings != template_settings):
        raise InstallError(f"instance-specific delegation configuration for {unit}; review its fragment and drop-ins "
                           "before installing. Install will not guess how to change them.")
    if REQUIRED_CONTROLLERS <= disk:
        return False
    proposed = {**dropins, DELEGATE_DROPIN_NAME: DELEGATE_DROPIN}
    missing = REQUIRED_CONTROLLERS - _delegation_result(_delegation_layout(main, proposed))
    if missing:
        raise InstallError("the proposed CPU/IO drop-in would not supply all required controllers "
                           f"(missing {', '.join(sorted(missing))}); review Delegate resets and drop-in order, "
                           "restore memory/pids delegation, and run systemctl daemon-reload before retrying. Install did not change it.")
    return True


def _check_hooks() -> None:
    scripts = paths.custom_scripts_dir()
    for name in HOOK_NAMES:
        path = _menu_target(scripts / f"{name}.sh")
        original = _menu_target(scripts / f"{name}.sh.orig")
        _refuse_links(path, original)
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if HOOK_MARKER in text:
            canonical_original = scripts / f"{name}.sh.orig"
            if original != canonical_original and canonical_original.as_posix() in text:
                raise InstallError(f"{path} chains to {canonical_original}; its backup cannot be relocated on reinstall")
            # Ours. If it still refers to a backup that has gone, reinstalling would hide that.
            if original.as_posix() in text and not original.exists():
                raise InstallError(
                    f"{path} still expects its backup {original}, which is missing; "
                    "restore the backup or remove the wrapper before installing again"
                )
            continue
        if original.exists():
            raise InstallError(f"{original} already exists; move it aside, then run install again")
        if not os.access(path, os.X_OK):
            raise InstallError(
                f"{path} exists but is not executable. DirectAdmin would not run it, so install will not "
                "guess what to chain. Make it executable or move it aside."
            )


def _preflight(hooks: bool, enable_timer: bool, report: _StepReport | None = None) -> bool:
    """Read-only checks. Returns True when a delegation drop-in is needed."""
    def check(key, action, reason):
        if report is not None and report.dry_run:
            return report.check(key, action, reason)
        return _reported(report, key, action, reason)

    check("config-check", _check_config, "config valid")
    systemd = paths.systemd_dir()
    check("links", lambda: _refuse_links(_menu_target(systemd / SERVICE), _menu_target(systemd / TIMER), paths.config_file()),
              "no target symbolic links")
    check("pending", _check_pending_unit_changes, "no pending reconcile-unit reload")
    check("units", lambda: _check_units(systemd, enable_timer), "unit masks and states acceptable")

    def check_controllers():
        missing = REQUIRED_CONTROLLERS - _root_controllers()
        if missing:
            raise InstallError(
                f"this host does not offer {', '.join(sorted(missing))} at the cgroup root; "
                "delegation cannot fix that, so install stopped"
            )

    check("controllers", check_controllers, "cpu, io, memory, pids available")
    needs_delegation = check("delegation-check", lambda: _check_delegation(systemd),
                                 lambda needed: "drop-in needed" if needed else "already sufficient")
    if hooks:
        check("hook-check", _check_hooks, "hook conflicts checked")
    return needs_delegation


def _dry_run_timer_checks(report: _StepReport) -> None:
    """Run remaining read-only activation checks when installation is blocked."""
    def activation_check():
        _check_timer_also()
        _check_timer_can_change(*timer_status())
        _service_state()

    def start_check():
        _check_timer_can_change(_unit_enabled(TIMER), _unit_active(TIMER))
        _service_state()

    report.check("activation-check", activation_check, "timer activation state checked")
    report.check("start-check", start_check, "timer/service start state checked")


def _install_hooks(journal: _Journal) -> None:
    scripts = paths.custom_scripts_dir()
    for name in HOOK_NAMES:
        path = scripts / f"{name}.sh"
        original = scripts / f"{name}.sh.orig"
        if _menu_declined(path) or _menu_declined(original):
            for key in (f"read-{name}", f"backup-{name}", f"write-{name}"):
                _skip(journal.report, key, "hook or its backup declined by user; pair kept unchanged")
            continue
        path, original = _menu_target(path), _menu_target(original)
        existing = _reported(journal.report, f"read-{name}",
                             lambda: path.read_text(encoding="utf-8", errors="replace") if path.exists() else None,
                             "existing hook inspected")
        if existing is not None and HOOK_MARKER not in existing:
            # Someone else's hook: keep it and chain to it, never overwrite it.
            _reported(journal.report, f"backup-{name}", lambda: journal.move(path, original), "original hook backed up")
            _reported(journal.report, f"write-{name}",
                      lambda: journal.write(path, hook_script(name, original), 0o755), "wrapper published")
        else:
            _skip(journal.report, f"backup-{name}", "no foreign hook to back up")
            _reported(journal.report, f"write-{name}",
                      lambda: journal.write(path, hook_script(name, original if original.exists() else None), 0o755),
                      "wrapper published")


def _enable_timer(journal: _Journal) -> None:
    # Checked again here: before the reload the timer may have had no unit file, so a drop-in
    # carrying Also= (which would make rollback disable other units) was not visible yet.
    def activation_check():
        _check_timer_also()
        enabled, active = timer_status()  # recheck after reload, or read the existing state for dry-run
        _check_timer_can_change(enabled, active)
        _service_state()
        return enabled, active

    enabled, active = _reported(journal.report, "activation-check", activation_check,
                                lambda states: f"timer {states[0]}, {states[1]}; service state acceptable")
    if enabled != "enabled":
        _reported(journal.report, "enable",
                  lambda: journal.run(["systemctl", "enable", TIMER], undo=["systemctl", "disable", TIMER]),
                  "timer enabled")
    else:
        _skip(journal.report, "enable", "already enabled")
    # Enable may take time: do not take ownership of work that became busy meanwhile.
    def start_check():
        active = _unit_active(TIMER)
        _check_timer_can_change(enabled, active)
        return active, _service_state()

    active, service_state = _reported(journal.report, "start-check", start_check,
                                      lambda states: f"timer {states[0]}; service {states[1]}")

    def start():
        if not journal.dry_run and active != "active" and service_state in {"inactive", "failed"}:
            journal.add_undo(_report_started_service)
        journal.run(["systemctl", "start", TIMER], undo=None if active == "active" else ["systemctl", "stop", TIMER])

    _reported(journal.report, "start", start, "timer start command completed")


def _apply(journal: _Journal, hooks: bool, enable_timer: bool, needs_delegation: bool) -> None:
    systemd = paths.systemd_dir()
    config_path = paths.config_file()
    def write_config():
        if not config_path.exists():
            journal.write(config_path, EXAMPLE_TEXT, 0o640)
        else:
            _skip(journal.report, "config-write", "existing config kept")

    _reported(journal.report, "config-write", write_config, "example config written")
    _reported(journal.report, "service",
              lambda: journal.write(systemd / SERVICE, SERVICE_UNIT.format(exe=_systemd_arg(sys.executable))),
              "service unit written")
    _reported(journal.report, "timer", lambda: journal.write(systemd / TIMER, TIMER_UNIT), "timer unit written")
    if needs_delegation:
        _reported(journal.report, "delegation-write",
                  lambda: journal.write(systemd / "user@.service.d" / DELEGATE_DROPIN_NAME, DELEGATE_DROPIN),
                  "delegation drop-in written")
    else:
        _skip(journal.report, "delegation-write", "not needed; controllers already delegated")
    _reported(journal.report, "reload", lambda: journal.run(["systemctl", "daemon-reload"]), "manager reloaded")
    if hooks:
        _install_hooks(journal)  # last: DirectAdmin account events only see a finished setup
    if enable_timer:
        _enable_timer(journal)


def _failure(exc: BaseException, problems: list[str]) -> InstallError:
    reason = "interrupted" if isinstance(exc, KeyboardInterrupt) else str(exc)
    if problems:
        return InstallError(
            f"install failed ({reason}). Rollback was INCOMPLETE; check these by hand: {'; '.join(problems)}"
        )
    return InstallError(f"install failed ({reason}). Completed steps were rolled back.")


def _timer_report() -> str:
    try:
        enabled, active = timer_status()
    except KeyboardInterrupt:
        return "reconcile timer not re-checked (interrupted); the installation itself completed"
    except InstallError as exc:
        return f"reconcile timer not re-checked ({exc}); the installation itself completed"
    return f"reconcile timer is {enabled}, {active}"


def _ask_redis_install() -> bool:
    _StepReport._emit("Redis is absent. Install the redis-server package and disable the default Redis service? [y/N]")
    if not sys.stdin.isatty():
        return False  # --yes for limits never implies Redis consent.
    try:
        return input("Redis installation [y/N]: ").strip().lower() in {"y", "yes"}
    except (EOFError, OSError, StopIteration):
        return False


def _redis_before_limits(report: _StepReport, dry_run: bool) -> tuple[redis_mgr.RedisCleanupInventory, str]:
    # Ownership is read before step 1 can write backups or disable anything.
    try:
        managed = redis_mgr.directadmin_redis_enabled()
    except redis_mgr.RedisSafetyError as exc:
        report.skip("redis-cleanup", "ownership unknown; no cleanup writes")
        report.mark("redis-detect", "FAILED", str(exc))
        raise InstallError(str(exc)) from exc
    if managed:
        report.skip("redis-cleanup", "DirectAdmin manages Redis; no cleanup or other writes")
        report.skip("redis-package", "DirectAdmin Redis present; da-plus does not install a Redis package on this server")
        def refuse():
            detail = "A real install would stop here; nothing was changed." if dry_run else \
                "Whole install stopped before any write."
            raise InstallError(redis_mgr.DIRECTADMIN_REDIS_MESSAGE + " " + detail)
        report.perform("redis-detect", refuse)
    inventory = redis_mgr.detect_redis_cleanup(_exec)
    def cleanup():
        from argparse import Namespace
        from .cli import cmd_redis_cleanup
        if any(unit.missing_binary for unit in inventory.units) or inventory.processes:
            result = cmd_redis_cleanup(Namespace(yes=not dry_run, kill_orphan_process=False),
                                       runner=_exec, inventory=inventory)
            if result and not dry_run:
                raise InstallError("Redis leftover cleanup failed; unit files retained; inspect redis-cleanup")
        else:
            report.skip("redis-cleanup", "no orphan units/processes; " +
                        (", ".join(f"KEEP binary {binary}" for binary in inventory.binaries) or "no leftover binaries"))
    report.perform("redis-cleanup", cleanup, "orphan units disabled; files kept" if not dry_run else
                   "planned disable/reset-failed; KEEP unit files; processes require --kill-orphan-process")
    def gate():
        if redis_mgr.directadmin_redis_enabled():
            report.skip("redis-package", "DirectAdmin Redis present; da-plus does not install a Redis package on this server")
            detail = "A real install would stop here; nothing was changed." if dry_run else "Limits installation stopped."
            raise InstallError(redis_mgr.DIRECTADMIN_REDIS_MESSAGE + " " + detail)
        return _detect_existing_redis()
    info = report.perform("redis-detect", gate, lambda value: value or "no Redis found; gate passed")
    return inventory, info


def _redis_package_after_preflight(report: _StepReport, dry_run: bool,
                                   inventory: redis_mgr.RedisCleanupInventory, info: str) -> None:
    """Optional package action after limits checks; packages are not journaled or rolled back."""
    try:
        st = redis_mgr.status(runner=_exec)
    except (OSError, ValueError, subprocess.SubprocessError):
        st = redis_mgr.RedisStatus(None, None, None, False, redis_mgr.package_manager(), False)
    present = st.installed or st.active or any(binary.exists() for binary in REDIS_BINARIES)
    # Loaded, non-orphan standalone units are also existing Redis.
    present = present or any(not unit.missing_binary and not unit.error for unit in inventory.units)
    present = present or "listening address" in info or bool(st.service and not any(
        unit.path.name == f"{st.service}.service" and unit.missing_binary for unit in inventory.units))
    if present:
        report.skip("redis-package", "existing non-DirectAdmin Redis: warn and continue; no package changes")
    elif dry_run:
        report.skip("redis-package", "dry-run: would ask [y/N], then use redis install server-package plan and disable default service")
    elif not _ask_redis_install():
        report.skip("redis-package", "Redis package declined (default N); limits installation continues")
    else:
        def package():
            # A retained orphan unit is not a working Redis installation.
            absent = redis_mgr.RedisStatus(None, None, None, False, st.package_manager, False)
            if absent.package_manager is None:
                raise InstallError("no supported Redis package manager (dnf or apt-get)")
            steps = redis_mgr.plan_install(absent, runner=_exec, ignore_orphan_units=True)
            ok, log = redis_mgr.execute(steps, runner=_exec, package_only=True)
            for line in log:
                print(line)
            if not ok:
                raise InstallError("Redis server-package installation failed; " + "; ".join(log))
        try:
            report.perform("redis-package", package, "server package installed; default service disabled; package install not rolled back")
        except (InstallError, ConfigError, OSError, subprocess.SubprocessError) as exc:
            report._emit(f"Warning: optional Redis package step failed ({exc}); limits installation continues. "
                         "Package changes are not rolled back.")


def install(*, hooks: bool = False, enable_timer: bool = False, dry_run: bool = False) -> list[str]:
    """Install files and return what was done (or what would be done with dry_run).

    hooks: install the DirectAdmin hook scripts. They apply limits on account events.
    enable_timer: enable and start the 15-minute reconcile timer.
    """
    report = _StepReport(hooks, enable_timer, dry_run)
    try:
        if report.setup_error is not None:
            raise report.setup_error
        gate_error = None
        try:
            inventory, info = _redis_before_limits(report, dry_run)
        except (InstallError, ConfigError, OSError, subprocess.SubprocessError) as exc:
            if not dry_run:
                raise
            gate_error = exc
        if dry_run:
            journal = _Journal(dry_run=True, report=report)
            needs_delegation = _preflight(hooks, enable_timer, report)  # same checks as a real run
            if gate_error is not None or report.check_errors:
                if enable_timer:
                    _dry_run_timer_checks(report)
                raise gate_error or report.check_errors[0]
            _redis_package_after_preflight(report, dry_run, inventory, info)
            _apply(journal, hooks, enable_timer, needs_delegation)
            report.finish()
            return journal.done

        with _reported_lock(report):
            needs_delegation = _preflight(hooks, enable_timer, report)  # nothing is changed yet
            _redis_package_after_preflight(report, dry_run, inventory, info)
            journal = _Journal(dry_run=False, report=report)
            try:
                _apply(journal, hooks, enable_timer, needs_delegation)
            except BaseException as exc:
                problems = journal.rollback()
                report.rollback_problems = problems
                raise _failure(exc, problems) from exc
        # Past the commit point: a failed status check must not turn a finished install into an error.
        def final_status():
            line = _timer_report()
            if "not re-checked" in line:
                report.skip("status", line)
            return line

        status = _reported(report, "status", final_status, lambda line: line)
        journal.done.append(status)
    except BaseException as exc:
        report.finish(exc)
        raise
    if report.finish():
        raise InstallError("install completed with problems; see Problems found")
    return journal.done
