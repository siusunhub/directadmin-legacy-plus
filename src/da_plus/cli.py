"""Command-line entry point: `da-plus <command>`. The TUI is one of the commands."""

from __future__ import annotations

import argparse
import os
import sys
import re
import signal
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable

from . import config as config_mod
from . import da_users, redis_mgr, usage
from .config import ConfigError
from .hooks import HOOK_NAMES, run_hook
from .systemd.apply import ApplyError, UserResult, apply_all, apply_user, write_log
from .systemd.render import render_dropin


class CLIError(Exception):
    pass


def _require_root() -> None:
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise CLIError("this command must run as root")


def _user_or_fail(name: str) -> da_users.DAUser:
    try:
        user = da_users.get_user(name)
    except ValueError as exc:
        raise CLIError(str(exc)) from exc
    if user is None:
        raise CLIError(f"{name!r} is not a DirectAdmin user")
    return user


def cmd_validate(args: argparse.Namespace) -> int:
    config = config_mod.load()
    print(f"config OK: {len(config.profiles)} profile(s), {len(config.users)} user override(s)")
    return 0


def cmd_users(args: argparse.Namespace) -> int:
    config = config_mod.load()
    print(f"{'USER':<16}{'UID':>7}  {'PACKAGE':<16}PROFILE")
    for user in da_users.list_users():
        profile, _ = config_mod.resolve(config, user.name, user.package)
        uid = "-" if user.uid is None else str(user.uid)
        print(f"{user.name:<16}{uid:>7}  {user.package or '-':<16}{profile}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    user = _user_or_fail(args.user)
    config = config_mod.load()
    profile, values = config_mod.resolve(config, user.name, user.package)
    print(f"user {user.name}: profile {profile}")
    for key, value in values.items():
        print(f"  {key:<14} {value}")
    if user.uid is None:
        print("no Linux account for this user; no drop-in can be rendered")
    else:
        print()
        print(render_dropin(user.name, user.uid, values, config.io_device), end="")
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    user = _user_or_fail(args.user)
    if user.uid is None:
        raise CLIError(f"{user.name!r} has no Linux account, so there is no cgroup")
    snap = usage.snapshot(user.uid)
    if snap.memory_current is None:
        print("slice not found or not active; the user has no running processes")
        return 0
    print(f"memory    {usage.human(snap.memory_current)}")
    print(f"tasks     {snap.pids_current}")
    print(f"cpu time  {snap.cpu_usage_usec / 1_000_000:.1f} s total, {snap.cpu_throttled_usec / 1_000_000:.1f} s throttled")
    print(f"io total  read {usage.human(snap.io.rbytes)}, write {usage.human(snap.io.wbytes)}")
    for key in ("high", "max", "oom", "oom_kill"):
        print(f"event {key:<9} {snap.events.get(key, 0)}")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    if not args.dry_run:
        _require_root()
    config = config_mod.load()
    if args.all:
        results = apply_all(config, dry_run=args.dry_run)
    elif args.user:
        results = [apply_user(_user_or_fail(args.user), config, dry_run=args.dry_run)]
    else:
        raise CLIError("name a user or pass --all")
    for result in results:
        if args.user and result.ok and result.message == "limits off (disabled by admin)":
            print(f"skipped: limits off for {result.user}; enable it first")
        else:
            print(f"{'OK  ' if result.ok else 'FAIL'} {result.user}: {result.message}")
        if args.dry_run and result.dropin:
            print(result.dropin, end="")
    if not args.dry_run:
        write_log(results, source="apply")
    return 0 if all(result.ok for result in results) else 1


def cmd_limits(args: argparse.Namespace) -> int:
    user = _user_or_fail(args.user)
    raw = config_mod.load_raw()
    candidate = config_mod.set_limits(raw, user.name, args.state)
    config = config_mod.parse(candidate)
    print(f"users.{user.name}.limits: {args.state}" + (" (remove flag; default on)" if args.state == "on" else ""))
    if not args.yes:
        print("dry run; pass --yes as root to save and apply this change")
        result = apply_user(user, config, dry_run=True)
        if result.dropin:
            print(result.dropin, end="")
        return 0 if result.ok else 1
    _require_root()
    config_mod.save_raw(candidate)
    result = apply_user(user, config)
    write_log([result], source="limits")
    print(f"{'OK' if result.ok else 'FAIL'} {user.name}: {result.message}")
    return 0 if result.ok else 1


def cmd_hook(args: argparse.Namespace) -> int:
    return run_hook(args.name, args.user)


def cmd_install(args: argparse.Namespace) -> int:
    from .install import InstallError, _StepReport, install, interactive_install

    if not args.dry_run and not getattr(args, "yes", False) and sys.stdin.isatty():
        try:
            interactive_install(hooks=args.hooks, enable_timer=args.enable_timer, require_root=_require_root)
        except InstallError as exc:
            raise CLIError(str(exc)) from exc
        except (EOFError, KeyboardInterrupt):
            print("Install cancelled before changes.")
            return 0
        return 0

    if not args.dry_run:
        try:
            _require_root()
        except CLIError as exc:
            report = _StepReport(args.hooks, args.enable_timer, args.dry_run)
            report.mark("lock", "FAILED", f"root permission check: {exc}")
            report.finish(InstallError(str(exc)))
            raise
    try:
        install(hooks=args.hooks, enable_timer=args.enable_timer, dry_run=args.dry_run)
    except InstallError as exc:
        raise CLIError(str(exc)) from exc
    return 0


def cmd_redis_force_remove(args: argparse.Namespace) -> int:
    from .redis_force_remove import ForceRemoveError, force_remove

    try:
        return force_remove(purge_data=args.purge_data)
    except ForceRemoveError as exc:
        raise CLIError(str(exc)) from exc


def cmd_uninstall(args: argparse.Namespace) -> int:
    from .uninstall import uninstall

    return uninstall(yes=args.yes, reset_limits=args.reset_limits,
                     purge_config=args.purge_config, purge_logs=args.purge_logs)


def cmd_tui(args: argparse.Namespace) -> int:
    from .tui.app import DaPlusApp

    DaPlusApp(plain=getattr(args, "plain", False)).run()
    return 0


def _print_redis_status(st: redis_mgr.RedisStatus) -> None:
    print(f"redis-server   {st.binary or 'not installed'}")
    print(f"version        {st.version or '-'}")
    print(f"service        {st.service or '-'}" + (f" ({'active' if st.active else 'not active'})" if st.service else ""))
    print(f"package mgr    {st.package_manager or 'none found (need dnf or apt-get)'}")
    print(f"CustomBuild    {'present' if st.custombuild else 'not found'}")


def cmd_redis_status(args: argparse.Namespace) -> int:
    _print_redis_status(redis_mgr.status())
    return 0


def _confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        raise CLIError("not an interactive terminal; re-run with --yes to confirm")
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def cmd_redis_install(args: argparse.Namespace) -> int:
    st = redis_mgr.status()
    _print_redis_status(st)
    steps = redis_mgr.plan_install(st)
    if not st.installed and not st.service and st.package_manager is None:
        raise CLIError("no supported package manager found (need dnf or apt-get)")
    if not steps:
        print("\nExisting Redis left unchanged; no package or default-service changes required.")
        return 0

    print("\nplanned steps:")
    for step in steps:
        print(f"  {' '.join(step.argv)}    # {step.note}")
    if st.custombuild and not st.installed:
        print("\nwarning: CustomBuild is present and may manage Redis itself. Check its options first.")
    if args.dry_run:
        return 0

    _require_root()
    if not args.yes and not _confirm("\nProceed with these steps?"):
        print("cancelled, nothing changed")
        return 1

    ok, log = redis_mgr.execute(steps)
    for line in log:
        print(line)
    if ok:
        print()
        _print_redis_status(redis_mgr.status())
    return 0 if ok else 1


def cmd_redis(args: argparse.Namespace) -> int:
    handlers = {"status": cmd_redis_status, "install": cmd_redis_install}
    return handlers[args.redis_command](args)


def cmd_redis_user(args: argparse.Namespace) -> int:
    from . import redis_user

    try:
        action = args.redis_user_command
        if action == "status":
            for key, value in redis_user.status(args.user).items():
                print(f"{key:<16} {value}")
            return 0
        purge = getattr(args, "purge_redis_data", False)
        if not args.yes:
            print("redis-user preview (no changes; re-run with --yes):")
            for step in redis_user.preview(action, args.user, purge_redis_data=purge):
                print(f"  {step}")
            return 0
        if action == "enable":
            result = redis_user.enable(args.user)
        elif action == "disable":
            result = redis_user.disable(args.user, purge_redis_data=purge)
        else:
            result = redis_user.rotate_password(args.user)
        for key, value in result.items():
            print(f"{key:<16} {value}")
        return 0
    except (redis_user.RedisUserError, OSError, ValueError) as exc:
        raise CLIError(str(exc)) from exc


REDIS_CLEANUP_BACKUPS = Path("/root/da-plus-cleanup-backups")


def _redis_cleanup_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=5)


def _cleanup_missing_unit(unit: redis_mgr.RedisCleanupUnit) -> None:
    redis_mgr.cleanup_safe_path(unit.path)
    if unit.path.parent.absolute() not in [p.absolute() for p in redis_mgr.CLEANUP_UNIT_DIRS]:
        raise CLIError("unit outside cleanup systemd directories")
    if not re.fullmatch(r"redis.*\.service", unit.path.name):
        raise CLIError("not a Redis unit")
    if unit.path.read_bytes() != unit.content:
        raise CLIError("unit changed since detection; refused")
    if re.search(rb"(?m)^\s*(?:Also|Alias)\s*=\s*\S+", unit.content):
        raise CLIError("unit Also=/Alias= may affect other services; disable refused")
    binary = redis_mgr.cleanup_exec_path(unit.content)
    if binary is not None:
        redis_mgr.cleanup_safe_path(binary)
    if binary is None or not redis_mgr.cleanup_binary_missing(binary) or not unit.missing_binary:
        raise CLIError("unit is not a missing-binary orphan; refused")


def _cleanup_may_stop(unit, *, kill_orphan: bool, runner=None) -> bool:
    _cleanup_missing_unit(unit)
    state = redis_mgr.cleanup_unit_state(runner or _redis_cleanup_run, unit.path.name)
    # A drop-in may override the file's ExecStart with a working executable.
    effective = state.get("ExecStart", "")
    if effective:
        matches = re.findall(r"path=([^ ;}]+)", effective)
        if not matches or any(not redis_mgr.cleanup_binary_missing(Path(value)) for value in matches):
            raise CLIError("effective ExecStart may use an existing binary; refused")
    pid = int(state["MainPID"])
    if pid:
        executable, _ = redis_mgr.cleanup_process_identity(pid)
        if not executable.endswith(" (deleted)") or not redis_mgr.cleanup_binary_missing(Path(executable)):
            raise CLIError("running process has an existing/unknown binary; never stop it")
        if not kill_orphan:
            return False  # Disable boot activation without stopping the deleted process.
    elif (state["ActiveState"] not in ("inactive", "failed")
          and (state["ActiveState"], state.get("SubState")) != ("activating", "auto-restart")):
        raise CLIError("active unit has no verifiable process; refused")
    return True


def cmd_redis_cleanup(args: argparse.Namespace, *, runner=None, inventory=None) -> int:
    runner = runner or _redis_cleanup_run
    inventory = inventory or redis_mgr.detect_redis_cleanup(runner)
    print("Redis cleanup plan (dry-run; no changes)" if not args.yes else "Redis cleanup plan")
    for unit in inventory.units:
        if unit.error:
            print(f"FAILED inspect {unit.path}: {unit.error}")
        else:
            label = "ORPHAN unit" if unit.reason else "KEEP unit (not an orphan)"
            print(f"{label} {unit.path}: {unit.reason or 'existing ExecStart binary'}")
        if unit.missing_binary and not unit.error:
            print(f"  backup; disable {unit.path.name} (stop only when safe/explicit); reset-failed; KEEP unit file; daemon-reload")
    for process in inventory.processes:
        print(f"ORPHAN process PID {process.pid}: {process.executable}; {process.reason}")
        print("  report only; requires --yes --kill-orphan-process to signal a verified deleted executable")
    for binary in inventory.binaries:
        label = "client-only" if binary.name == "redis-cli" else "server binary"
        print(f"KEEP leftover {label}: {binary}")
    for error in inventory.errors:
        print(f"FAILED detection: {error}")
    failed = bool(inventory.errors or any(unit.error for unit in inventory.units))
    if not args.yes:
        return 1 if failed else 0
    _require_root()

    def step(label, action):
        nonlocal failed
        try:
            result = action()
        except (OSError, ValueError, CLIError, subprocess.SubprocessError, IndexError) as exc:
            failed = True
            print(f"FAILED {label}: {exc}")
            return False
        print(f"DONE {label}")
        return True

    def command(argv):
        result = runner(argv)
        if result.returncode != 0:
            raise CLIError((result.stderr or result.stdout or "command failed").strip())

    candidates = [unit for unit in inventory.units if unit.missing_binary and not unit.error]
    backed_up = []
    backup_directory = REDIS_CLEANUP_BACKUPS / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")

    def backup(unit):
        _cleanup_missing_unit(unit)
        redis_mgr.cleanup_safe_path(backup_directory)
        parent = backup_directory / str(redis_mgr.CLEANUP_UNIT_DIRS.index(unit.path.parent))
        redis_mgr.cleanup_safe_path(parent / unit.path.name)
        for directory in (REDIS_CLEANUP_BACKUPS, backup_directory, parent):
            redis_mgr.cleanup_safe_path(directory)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (parent / unit.path.name).open("xb") as stream:
            os.chmod(stream.name, 0o600)
            stream.write(unit.content)

    # Complete the backup phase for every candidate before any service mutation or signal.
    for unit in candidates:
        if step(f"backup {unit.path} to {backup_directory}", lambda unit=unit: backup(unit)):
            backed_up.append(unit)
    changed = False
    for unit in backed_up:
        may_stop = [False]
        if not step(f"verify orphan safety {unit.path}",
                    lambda unit=unit: may_stop.__setitem__(0, _cleanup_may_stop(
                        unit, kill_orphan=args.kill_orphan_process, runner=runner))):
            print(f"SKIPPED disable/reset/remove {unit.path}: safety check failed")
            continue
        changed = True
        disable = ["systemctl", "disable", *(["--now"] if may_stop[0] else []), unit.path.name]
        stopped = step(" ".join(disable[1:]),
                       lambda: command(disable))
        step(f"reset-failed {unit.path.name}",
             lambda unit=unit: command(["systemctl", "reset-failed", unit.path.name]))

        if stopped:
            def verify_disabled(unit=unit):
                state = redis_mgr.cleanup_unit_state(runner, unit.path.name)
                if may_stop[0] and (state["ActiveState"] not in ("inactive", "failed") or state["MainPID"] != "0"):
                    raise CLIError("unit still has an active process; file retained")
                if state.get("UnitFileState") in ("enabled", "enabled-runtime"):
                    raise CLIError("unit remains enabled; file retained")
            step(f"keep orphan unit file {unit.path} (verify disabled; never delete)", verify_disabled)
            if not may_stop[0]:
                print(f"SKIPPED stop process for {unit.path.name}: --kill-orphan-process required; unit disabled only")
        else:
            print(f"SKIPPED keep-disabled verification {unit.path}: disable --now failed; file retained")
    if changed:
        step("daemon-reload", lambda: command(["systemctl", "daemon-reload"]))
    for process in inventory.processes:
        if len(backed_up) != len(candidates) or any(unit.error for unit in inventory.units):
            print(f"SKIPPED PID {process.pid}: unit backup/inspection failed; no signal sent")
            continue
        if not args.kill_orphan_process or not process.deleted:
            print(f"SKIPPED PID {process.pid}: report only; --kill-orphan-process required for deleted executables")
            continue

        def kill(process=process):
            descriptor = None
            try:
                # A pidfd binds the signal to this process even if its numeric PID is reused.
                if redis_mgr.CLEANUP_PROC_ROOT == Path("/proc"):
                    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
                        raise CLIError("safe pidfd signalling unavailable; orphan process left running")
                    descriptor = os.pidfd_open(process.pid)
                try:
                    identity = redis_mgr.cleanup_process_identity(process.pid)
                except FileNotFoundError:
                    return
                if (identity != (process.executable, process.start_time)
                        or not identity[0].endswith(" (deleted)")
                        or not redis_mgr.cleanup_binary_missing(Path(identity[0]))):
                    raise CLIError("process identity changed or binary exists; signal refused")
                if descriptor is not None:
                    signal.pidfd_send_signal(descriptor, signal.SIGTERM)
                else:
                    os.kill(process.pid, signal.SIGTERM)  # Redirected /proc roots used by fake tests.
            except ProcessLookupError:
                return  # Already stopped by disable --now or exited independently.
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        step(f"SIGTERM verified deleted orphan PID {process.pid}", kill)
    return 1 if failed else 0


HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {
    "validate": cmd_validate,
    "users": cmd_users,
    "show": cmd_show,
    "usage": cmd_usage,
    "apply": cmd_apply,
    "limits": cmd_limits,
    "hook": cmd_hook,
    "install": cmd_install,
    "uninstall": cmd_uninstall,
    "tui": cmd_tui,
    "redis": cmd_redis,
    "redis-user": cmd_redis_user,
    "redis-cleanup": cmd_redis_cleanup,
    "redis-force-remove": cmd_redis_force_remove,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="da-plus", description="Per-user resource limits for DirectAdmin.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate", help="check the config file")
    sub.add_parser("users", help="list DirectAdmin users with package and profile")

    show = sub.add_parser("show", help="effective limits and rendered drop-in for a user")
    show.add_argument("user")

    usage_p = sub.add_parser("usage", help="current cgroup usage for a user")
    usage_p.add_argument("user")

    apply_p = sub.add_parser("apply", help="write drop-ins and apply limits")
    apply_p.add_argument("user", nargs="?")
    apply_p.add_argument("--all", action="store_true", help="apply to every DirectAdmin user")
    apply_p.add_argument("--dry-run", action="store_true", help="render and print only; change nothing")

    limits = sub.add_parser("limits", help="persistently enable or disable account limits; default previews only")
    limits.add_argument("user")
    limits.add_argument("state", choices=("on", "off"))
    limits.add_argument("--yes", action="store_true", help="save and apply the change as root")

    hook = sub.add_parser("hook", help="entry point for DirectAdmin hook scripts")
    hook.add_argument("name", choices=HOOK_NAMES)
    hook.add_argument("user")

    install_p = sub.add_parser("install", help="menu: test with dry-run or confirm real installation (root); validates config first")
    install_p.add_argument("--hooks", action="store_true", help="install DirectAdmin hooks (they apply limits on account events)")
    install_p.add_argument("--enable-timer", action="store_true", help="start the 15-minute reconcile timer now and at boot")
    install_p.add_argument("--dry-run", action="store_true", help="same as menu Test install: check everything, write nowhere; no menu")
    install_p.add_argument("--yes", action="store_true", help="real install without the menu or confirmations")
    uninstall_p = sub.add_parser("uninstall", help="preview removal of da-plus files; --yes removes them (root)")
    uninstall_p.add_argument("--yes", action="store_true", help="perform removal; default is a dry run")
    uninstall_p.add_argument("--reset-limits", action="store_true", help="reset generated user slices' runtime limits")
    uninstall_p.add_argument("--purge-config", action="store_true", help="remove only an unchanged example/starter config")
    uninstall_p.add_argument("--purge-logs", action="store_true", help="remove apply.log and hook.log; retain directories")
    tui_p = sub.add_parser("tui", help="open the terminal UI (root to make changes)")
    tui_p.add_argument("--plain", action="store_true", help="use monochrome bold/reverse text for basic terminals")

    redis = sub.add_parser("redis", help="shared Redis server: check and install")
    redis_sub = redis.add_subparsers(dest="redis_command", required=True)
    redis_sub.add_parser("status", help="show whether Redis is installed and running")
    redis_install = redis_sub.add_parser("install", help="install Redis after confirmation (root)")
    redis_install.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    redis_install.add_argument("--dry-run", action="store_true", help="print the steps only")
    cleanup = sub.add_parser("redis-cleanup", help="preview legacy Redis leftovers; --yes cleans orphan units (root)")
    cleanup.add_argument("--yes", action="store_true", help="perform the cleanup; default is a read-only plan")
    cleanup.add_argument("--kill-orphan-process", action="store_true", help="also SIGTERM verified deleted-executable Redis orphans")
    force_remove = sub.add_parser("redis-force-remove", help="separate root-only Redis removal after exact typed confirmations")
    force_remove.add_argument("--purge-data", action="store_true", help="also remove /etc/redis and /var/lib/redis after separate typed confirmation")
    redis_user_p = sub.add_parser("redis-user", help="explicit per-account Redis, Unix socket only")
    redis_user_sub = redis_user_p.add_subparsers(dest="redis_user_command", required=True)
    for action in ("status", "enable", "disable", "rotate-password"):
        command = redis_user_sub.add_parser(action, help=f"{action} one account's Redis")
        command.add_argument("user")
        if action != "status":
            command.add_argument("--yes", action="store_true", help="perform the change as root; default previews only")
        if action == "disable":
            command.add_argument("--purge-redis-data", action="store_true", help="also remove the owned Redis data directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return HANDLERS[args.command](args)
    except (ConfigError, ApplyError, CLIError) as exc:
        print(f"da-plus: {exc}", file=sys.stderr)
        return 2
