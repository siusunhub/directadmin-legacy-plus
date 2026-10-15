"""Explicit Redis removal, separate from every install/check/plan path."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path


class ForceRemoveError(RuntimeError):
    pass


UNIT_SUFFIX = r"\.(?:service|socket|timer|target|path|mount|automount|slice|scope|device|swap)"


def _require_root():
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise ForceRemoveError("Redis force removal requires root on Linux.")


def _runner(argv):
    return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=30,
                          env={**os.environ, "LC_ALL": "C"})


def _safe(path: Path, root: Path, *, leaf_link=False):
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if hasattr(current, "is_junction") and current.is_junction():
            raise ForceRemoveError(f"Refusing junction: {current}")
        if current.is_symlink() and not (current == path and leaf_link):
            raise ForceRemoveError(f"Refusing symbolic link: {current}")


def _exists(path):
    return path.exists() or path.is_symlink()


def _redis_enable_links(systemd, root):
    """Only direct Redis service symlinks in target wants directories."""
    links = []
    for directory in sorted(systemd.glob("*.target.wants")):
        _safe(directory, root)
        if not stat.S_ISDIR(directory.lstat().st_mode):
            continue
        for path in sorted(directory.glob("redis*.service")):
            _safe(path, root, leaf_link=True)
            if stat.S_ISLNK(path.lstat().st_mode):
                links.append(path)
    return links


def _dangling_link(path, root):
    _safe(path, root, leaf_link=True)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISLNK(mode):
        return False
    try:
        path.stat()
    except FileNotFoundError:
        return True
    return False


def _walk(path, root):
    if not _exists(path):
        return []
    _safe(path, root, leaf_link=True)
    mode = path.lstat().st_mode
    if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
        raise ForceRemoveError(f"Unsupported file type: {path}")
    result = [path]
    if stat.S_ISDIR(mode):
        for child in sorted(path.iterdir()):
            result.extend(_walk(child, root))
    return result


def _digest(stream):
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _file_digest(path):
    with path.open("rb") as stream:
        return _digest(stream)


def _redis_option(data):
    text = data.decode("utf-8")
    lines = text.splitlines(keepends=True)
    candidates = [(index, line) for index, line in enumerate(lines)
                  if re.match(r"^redis=", line)]
    if len(candidates) != 1:
        raise ForceRemoveError("Redis option missing or duplicated in options.conf; no format was guessed.")
    index, line = candidates[0]
    match = re.fullmatch(r"([ \t]*)(redis)([ \t]*=[ \t]*)([^\s#;]+)([ \t]*(?:[#;][^\r\n]*)?)(\r?\n)?", line, re.I)
    if match is None or match[4].lower() not in {"yes", "no", "on", "off"}:
        raise ForceRemoveError("Redis option format unclear in options.conf; expected an observed yes/no or on/off switch.")
    off = "no" if match[4].lower() in {"yes", "no"} else "off"
    if match[4].lower() == off:
        return data, match[2], match[4], off
    lines[index] = match[1] + match[2] + match[3] + off + match[5] + (match[6] or "")
    return "".join(lines).encode("utf-8"), match[2], match[4], off


def _verify_backup(backup, expected):
    manifest = (backup / "SHA256SUMS").read_text(encoding="ascii")
    wanted = "".join(f"{_file_digest(backup / name)}  {name}\n" for name in ("snapshot.tar", "restore-state.json"))
    if manifest != wanted:
        raise ForceRemoveError("Backup SHA256 verification failed.")
    with tarfile.open(backup / "snapshot.tar", "r:") as archive:
        if len(archive.getmembers()) != len(expected) or set(archive.getnames()) != set(expected):
            raise ForceRemoveError("Backup inventory verification failed.")
        for name, (kind, value) in expected.items():
            member = archive.getmember(name)
            if kind == "file":
                if not (member.isfile() or member.islnk() and member.linkname in expected):
                    raise ForceRemoveError(f"Backup file type mismatch: {name}")
                with archive.extractfile(member) as stream:
                    if _digest(stream) != value:
                        raise ForceRemoveError(f"Backup content mismatch: {name}")
            elif kind == "link":
                if not member.issym() or member.linkname != value:
                    raise ForceRemoveError(f"Backup link mismatch: {name}")
            elif not member.isdir():
                raise ForceRemoveError(f"Backup directory mismatch: {name}")


def _backup(backup, targets, root, restore_state):
    entries = {path.relative_to(root).as_posix(): path for target in targets for path in _walk(target, root)}
    expected = {}
    for name, path in entries.items():
        if path.is_symlink():
            expected[name] = "link", os.readlink(path)
        elif path.is_file():
            expected[name] = "file", _file_digest(path)
        else:
            expected[name] = "dir", None
    _safe(backup, root)
    backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup.mkdir(mode=0o700)
    descriptor = os.open(backup / "snapshot.tar", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        with tarfile.open(fileobj=stream, mode="w", dereference=False) as archive:
            for name, path in sorted(entries.items()):
                _safe(path, root, leaf_link=True)
                archive.add(path, arcname=name, recursive=False)
    descriptor = os.open(backup / "restore-state.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as stream:
        json.dump(restore_state, stream, ensure_ascii=True, indent=2)
        stream.write("\n")
    descriptor = os.open(backup / "SHA256SUMS", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as stream:
        for name in ("snapshot.tar", "restore-state.json"):
            stream.write(f"{_file_digest(backup / name)}  {name}\n")
    _verify_backup(backup, expected)
    return expected


def _restore_missing_data(backup, root, record):
    """Keep data/config even if an RPM scriptlet removed a backed-up path."""
    with tarfile.open(backup / "snapshot.tar", "r:") as archive:
        for member in archive.getmembers():
            if not any(member.name == prefix or member.name.startswith(prefix + "/")
                       for prefix in ("etc/redis", "var/lib/redis")):
                continue
            destination = root / member.name
            _safe(destination, root, leaf_link=True)
            if _exists(destination):
                continue
            record.append(f"Restoring missing retained path: {destination}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if member.isdir():
                destination.mkdir(mode=member.mode)
            elif member.issym():
                destination.symlink_to(member.linkname)
            else:
                with archive.extractfile(member) as source, destination.open("xb") as target:
                    shutil.copyfileobj(source, target)
                destination.chmod(member.mode)
            if not member.issym() and hasattr(os, "chown"):
                os.chown(destination, member.uid, member.gid)


def _server_self_test(root, runner, emit):
    """Show current Redis inventory without creating files or changing services."""
    errors = []

    def unavailable(label, detail):
        errors.append(label)
        emit(f"  {label}: UNAVAILABLE ({str(detail).strip()})")

    def query(argv, label, absent=None):
        try:
            result = runner(argv)
        except (OSError, subprocess.SubprocessError) as exc:
            unavailable(label, exc)
            return None
        message = result.stdout.strip()
        if result.returncode == 0:
            return message
        if absent and absent(result):
            return "not found"
        unavailable(label, result.stderr or result.stdout or f"exit {result.returncode}")
        return None

    def describe(path):
        try:
            _safe(path, root, leaf_link=True)
            if path.is_symlink():
                emit(f"  {path}: symlink -> {os.readlink(path)}")
            elif not path.exists():
                emit(f"  {path}: absent")
            elif path.is_dir():
                emit(f"  {path}: directory present")
            elif path.is_file():
                size = path.stat().st_size
                mask = "; 0-byte unit (mask)" if size == 0 and re.search(UNIT_SUFFIX + r"$", path.name) else ""
                emit(f"  {path}: file present, {size} bytes{mask}")
            else:
                emit(f"  {path}: non-regular file present")
        except (OSError, ForceRemoveError) as exc:
            unavailable(str(path), exc)

    emit("Redis server self-test (read-only):")
    emit("[Installed package]")
    package = query(["rpm", "-q", "redis"], "Redis RPM",
                    lambda result: result.returncode == 1 and "package redis is not installed" in result.stdout + result.stderr)
    if package is not None:
        emit(f"  Redis RPM installed: {'no' if package == 'not found' else 'yes'} ({package})")

    emit("[Binary paths]")
    for name in ("redis-server", "redis-cli", "redis-sentinel"):
        found = query(["which", name], f"PATH lookup for {name}",
                      lambda result: result.returncode == 1 and not result.stdout.strip())
        if found is not None:
            emit(f"  {name} in PATH: {found}")
    binary_paths = set()
    for directory in (root / "usr/bin", root / "usr/local/bin", root / "usr/sbin"):
        try:
            if directory.is_symlink():
                describe(directory)
                continue
            _safe(directory, root)
            binary_paths.update(directory / name for name in ("redis-server", "redis-cli", "redis-sentinel"))
            binary_paths.update(directory.glob("redis-*"))
        except (OSError, ForceRemoveError) as exc:
            unavailable(str(directory), exc)
    for path in sorted(binary_paths):
        describe(path)

    emit("[Service registration and state]")
    names = {"redis.service", "redis-server.service", "redis-sentinel.service"}
    listing = query(["systemctl", "list-unit-files", "--type=service", "--no-legend"], "Registered service inventory")
    registered = {}
    if listing is not None:
        for line in listing.splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0].endswith(".service") and fields[0].startswith(("redis", "da-plus-redis-")):
                names.add(fields[0])
                registered[fields[0]] = fields[1]
    for name in sorted(names):
        argv = ["systemctl", "show", name, "-p", "LoadState", "-p", "ActiveState",
                "-p", "UnitFileState", "-p", "FragmentPath", "-p", "DropInPaths"]
        try:
            result = runner(argv)
            properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            load = properties.get("LoadState")
            if result.returncode not in {0, 4} or load is None:
                unavailable(name, result.stderr or result.stdout or f"exit {result.returncode}")
            else:
                exists = name in registered or load != "not-found"
                emit(f"  {name}: registered={'yes' if exists else 'no'}; load={load}; "
                     f"active={properties.get('ActiveState', 'unknown')}; "
                     f"enabled={properties.get('UnitFileState') or registered.get(name, 'not-found' if not exists else 'unknown')}")
                emit(f"    Unit file: {properties.get('FragmentPath') or 'not set'}")
                emit(f"    Drop-ins: {properties.get('DropInPaths') or 'none'}")
        except (OSError, subprocess.SubprocessError) as exc:
            unavailable(name, exc)
        for directory in ("etc/systemd/system", "usr/lib/systemd/system", "run/systemd/system"):
            path = root / directory / name
            if directory == "etc/systemd/system" or _exists(path):
                describe(path)
        describe(root / "etc/systemd/system/multi-user.target.wants" / name)
    for directory in (root / "etc/systemd/system", root / "usr/lib/systemd/system"):
        try:
            _safe(directory, root)
            for dropin in sorted(directory.glob("redis*.service.d")):
                for path in _walk(dropin, root):
                    describe(path)
        except (OSError, ForceRemoveError) as exc:
            unavailable(f"Drop-ins under {directory}", exc)

    emit("[Config and data paths]")
    config_paths = {root / "etc/redis", root / "etc/redis.conf", root / "etc/redis-sentinel.conf"}
    try:
        config_paths.update(_walk(root / "etc/redis", root))
    except (OSError, ForceRemoveError) as exc:
        unavailable("Redis config inventory", exc)
    for path in sorted(config_paths):
        describe(path)
    describe(root / "var/lib/redis")
    options = root / "usr/local/directadmin/custombuild/options.conf"
    describe(options)
    try:
        _safe(options, root)
        text = options.read_text(encoding="utf-8")
        for key in ("redis", "php_redis"):
            values = [line[len(key) + 1:] for line in text.splitlines() if line.startswith(key + "=")]
            emit(f"  CustomBuild {key}: {', '.join(values) if values else 'key not found'}")
        _redis_option(text.encode("utf-8"))
    except (OSError, ValueError, ForceRemoveError) as exc:
        unavailable("CustomBuild Redis option validation", exc)

    emit("[Port 6379]")
    listeners = query(["ss", "-H", "-ltnp", "sport = :6379"], "TCP listener check")
    if listeners is not None:
        emit(f"  TCP 6379: {listeners or 'closed (no listeners)'}")
    emit(f"Self-test finished: {len(errors)} unavailable check(s). Nothing was changed.")
    return errors


def force_remove(*, purge_data=False, runner=None, confirm=None, output=print,
                 require_root=None, root=Path("/"), timestamp=None):
    """Run only after exact confirmations. Tests inject a runner and fake root."""
    (require_root or _require_root)()
    runner = runner or _runner
    confirm = confirm or input
    root = Path(root).resolve()
    options = root / "usr/local/directadmin/custombuild/options.conf"
    systemd = root / "etc/systemd/system"
    data_dirs = [root / "etc/redis", root / "var/lib/redis"]
    backup = None
    backup_verified = False
    stopped_data = None
    changed = []

    def emit(message):
        output(str(message).encode("ascii", "backslashreplace").decode("ascii"))

    def checked(argv):
        result = runner(argv)
        if result.returncode:
            raise ForceRemoveError(f"{' '.join(argv)} failed: {result.stderr or result.stdout or result.returncode}")
        return result

    def mutate(label, action):
        changed.append(label + " (attempted; may have partially changed)")
        result = action()
        changed[-1] = label
        return result

    def not_owned(path):
        result = runner(["rpm", "-qf", str(path)])
        if result.returncode == 0 and result.stdout.strip():
            emit(f"KEEP RPM-owned path: {path} ({result.stdout.strip()})")
            return False
        if result.returncode == 1 and re.fullmatch(r"file .+ is not owned by any package", (result.stdout + result.stderr).strip()):
            return True
        raise ForceRemoveError(f"RPM ownership could not be verified for {path}: {result.stderr or result.stdout}")

    def delete_file(path):
        _safe(path, root, leaf_link=True)
        if not (path.is_symlink() or stat.S_ISREG(path.lstat().st_mode)):
            raise ForceRemoveError(f"Refusing non-file removal: {path}")
        path.unlink()

    try:
        self_test_errors = _server_self_test(root, runner, emit)
        if self_test_errors:
            raise ForceRemoveError(f"Server self-test could not complete: {', '.join(self_test_errors)}. Removal was not started.")
        emit("WARNING: Force removal changes CustomBuild's Redis option, stops/disables redis.service and redis-sentinel.service,")
        emit("removes the redis RPM, package-free /usr/local/bin/redis-* binaries and Redis unit files/drop-ins.")
        emit("/etc/redis and /var/lib/redis are kept unless --purge-data is separately confirmed.")
        client_result = runner(["redis-cli", "-h", "127.0.0.1", "-p", "6379", "CLIENT", "LIST"])
        if client_result.returncode:
            if "connection refused" not in (client_result.stdout + client_result.stderr).lower():
                raise ForceRemoveError(f"Cannot check Redis clients: {client_result.stderr or client_result.stdout}")
            closed_port = checked(["ss", "-H", "-ltnp", "sport = :6379"])
            if closed_port.stdout.strip():
                raise ForceRemoveError("Redis connection refused but port 6379 has a listener; client count is unknown.")
            emit("Redis connection refused and port 6379 is closed: 0 connected clients.")
            clients = ""
        else:
            clients = client_result.stdout
        records = [line for line in clients.splitlines() if line.strip()]
        if any(not re.search(r"(?:^| )id=\d+(?: |$)", line) for line in records):
            raise ForceRemoveError("Cannot determine client count from redis-cli CLIENT LIST; removal stopped.")
        if records:
            emit(f"{len(records)} connected clients on 127.0.0.1:6379 (includes the inspection client if listed).")
            if confirm("Clients are connected. Type YES to continue: ") != "YES":
                emit("Cancelled; nothing was changed.")
                return 1
        if confirm("Type exactly FORCE REMOVE REDIS: ") != "FORCE REMOVE REDIS":
            emit("Cancelled; nothing was changed.")
            return 1
        if purge_data and confirm("Type exactly PURGE REDIS DATA: ") != "PURGE REDIS DATA":
            emit("Cancelled; nothing was changed.")
            return 1

        _safe(options, root)
        if not options.is_file():
            raise ForceRemoveError("Redis option file options.conf is missing or is not a regular file.")
        original = options.read_bytes()
        updated, key, value, off = _redis_option(original)
        emit(f"Observed CustomBuild option: {key}={value}; off value in this format: {off}")
        _safe(systemd, root)
        local_bin = root / "usr/local/bin"
        _safe(local_bin, root)
        binaries = sorted(local_bin.glob("redis-*"))
        for path in binaries:
            _safe(path, root)
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ForceRemoveError(f"Refusing non-regular Redis binary: {path}")
        for path in data_dirs:
            _safe(path, root)
            if path.exists() and not path.is_dir():
                raise ForceRemoveError(f"Redis data/config path is not a directory: {path}")
        unit_targets = sorted(systemd.glob("redis*"))
        unit_files, unit_dirs = [], []
        for path in unit_targets:
            if re.fullmatch(r"redis.*" + UNIT_SUFFIX, path.name) or \
                    re.fullmatch(r"redis.*" + UNIT_SUFFIX + r"\.d", path.name) and path.is_symlink():
                _safe(path, root, leaf_link=True)
                if not path.is_symlink() and not stat.S_ISREG(path.lstat().st_mode):
                    raise ForceRemoveError(f"Redis unit path is not a regular file: {path}")
                unit_files.append(path)
            elif re.fullmatch(r"redis.*" + UNIT_SUFFIX + r"\.d", path.name):
                for child in _walk(path, root):
                    (unit_dirs if child.is_dir() and not child.is_symlink() else unit_files).append(child)

        # Record restoration information while every command is still read-only.
        package_before = checked(["rpm", "-q", "redis"]).stdout.strip()
        if not package_before or any(not re.fullmatch(r"redis-[0-9][A-Za-z0-9.+:~_-]*", line)
                                     for line in package_before.splitlines()):
            raise ForceRemoveError("rpm -q redis did not identify the redis package; no removal was run.")
        states = {}
        enable_links = _redis_enable_links(systemd, root)
        units = ("redis.service", "redis-sentinel.service")
        # disable can remove links in any target, not just multi-user.target.
        for directory in sorted(systemd.iterdir()):
            if directory.name.endswith((".wants", ".requires")):
                _safe(directory, root)
                for entry in sorted(directory.iterdir()):
                    if entry.name in units or entry.is_symlink() and Path(os.readlink(entry)).name in units:
                        _safe(entry, root, leaf_link=True)
                        enable_links.append(entry)
            elif directory.is_symlink() and Path(os.readlink(directory)).name in units:
                _safe(directory, root, leaf_link=True)
                enable_links.append(directory)
        for unit in ("redis.service", "redis-sentinel.service"):
            link = systemd / "multi-user.target.wants" / unit
            _safe(link, root, leaf_link=True)
            enable_links.append(link)
            link_state = "symlink present" if link.is_symlink() else "regular file present" if link.exists() else "absent"
            emit(f"Before removal: multi-user.target.wants/{unit}: {link_state}")
            load = runner(["systemctl", "show", unit, "-p", "LoadState", "--value"])
            load_value = load.stdout.strip()
            if load.returncode not in {0, 4} or load_value not in {"loaded", "masked", "not-found"}:
                raise ForceRemoveError(f"Cannot record unit existence for {unit}: {load.stdout or load.stderr}")
            states[unit] = {"LoadState": load_value, "enable_link": link_state}
            if load_value == "not-found":
                states[unit].update({"is-active": "unknown", "is-enabled": "not-found"})
                emit(f"Before removal: is-active {unit}: unknown; is-enabled {unit}: not-found")
                continue
            if load_value != "masked":
                unit_text = checked(["systemctl", "cat", unit]).stdout
                install_section = False
                for line in unit_text.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("["):
                        install_section = stripped == "[Install]"
                    setting = re.match(r"^(Also|Alias)\s*=(.*)$", stripped)
                    if install_section and setting and setting[2].strip():
                        raise ForceRemoveError(f"Cannot safely bound systemctl disable: {unit} uses [Install] {setting[1]}=. No removal targets changed.")
            for verb, values, codes in (
                ("is-active", {"active", "inactive", "failed", "unknown", "not-found"}, {0, 3, 4}),
                ("is-enabled", {"enabled", "disabled", "static", "masked", "not-found", "indirect", "linked"}, {0, 1, 4}),
            ):
                result = runner(["systemctl", verb, unit])
                value_state = result.stdout.strip()
                if result.returncode not in codes or value_state not in values:
                    raise ForceRemoveError(f"Cannot record previous {verb} state for {unit}: {result.stdout or result.stderr}")
                states[unit][verb] = value_state
                emit(f"Before removal: {verb} {unit}: {value_state}")

        stamp = timestamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        if not re.fullmatch(r"[0-9TZ-]+", stamp):
            raise ForceRemoveError("Invalid backup timestamp.")
        backup = root / "root/da-plus-backups" / f"redis-force-remove-{stamp}"
        _backup(backup, [options, *data_dirs, *unit_targets, *enable_links, *binaries], root,
                {"redis_rpm": package_before, "units": states, "purge_data": bool(purge_data)})
        backup_verified = True
        emit(f"Backup verified: {backup}")
        free = {path: not_owned(path) for path in [*binaries, *unit_files, *unit_dirs]}
        if options.read_bytes() != original:
            raise ForceRemoveError("options.conf changed after inspection; removal stopped before changing it.")
        if updated != original:
            def write_option():
                _safe(options, root)
                with options.open("r+b") as stream:
                    if stream.read() != original:
                        raise ForceRemoveError("options.conf changed before writing; removal stopped.")
                    stream.seek(0)
                    stream.write(updated)
                    stream.truncate()
                    stream.flush()
                    os.fsync(stream.fileno())
            mutate(f"Updated {options}: {key}={value} -> {key}={off}", write_option)
            emit("".join(difflib.unified_diff(original.decode("utf-8").splitlines(True),
                                            updated.decode("utf-8").splitlines(True),
                                            fromfile="options.conf (before)", tofile="options.conf (after)")))
        else:
            emit("CustomBuild Redis option is already off; options.conf kept unchanged.")

        for unit in ("redis.service", "redis-sentinel.service"):
            state = runner(["systemctl", "show", unit, "-p", "LoadState", "--value"])
            if state.stdout.strip() == "not-found" and state.returncode in {0, 4}:
                emit(f"SKIPPED absent unit: {unit}")
            elif state.returncode == 0 and state.stdout.strip() in {"loaded", "masked"}:
                mutate(f"Stopped and disabled {unit}", lambda unit=unit: checked(["systemctl", "disable", "--now", unit]))
            else:
                raise ForceRemoveError(f"Cannot verify unit existence for {unit}: {state.stdout or state.stderr}")
        # Shutdown may write a newer dump/AOF. Preserve it before dnf scriptlets.
        data_snapshot = backup / "stopped-data"
        _backup(data_snapshot, data_dirs, root, {"phase": "after stopping Redis; before package removal"})
        stopped_data = data_snapshot
        emit(f"Stopped Redis data backup verified: {stopped_data}")
        package = checked(["rpm", "-q", "redis"])
        if not package.stdout.strip() or any(not re.fullmatch(r"redis-[0-9][A-Za-z0-9.+:~_-]*", line)
                                             for line in package.stdout.splitlines()):
            raise ForceRemoveError("rpm -q redis did not identify the redis package; no package removal was run.")
        mutate("Removed system package redis", lambda: checked(["dnf", "remove", "-y", "redis"]))
        if not purge_data:
            _restore_missing_data(stopped_data, root, changed)
        for path in binaries:
            if _exists(path) and free[path] and not_owned(path):
                _safe(path, root)
                if not stat.S_ISREG(path.lstat().st_mode):
                    raise ForceRemoveError(f"Redis binary changed type: {path}")
                mutate(f"Removed package-free binary {path}", lambda path=path: delete_file(path))
        for path in unit_files:
            if _exists(path) and free[path] and not_owned(path):
                mutate(f"Removed package-free unit/drop-in {path}", lambda path=path: delete_file(path))
        for path in sorted(unit_dirs, key=lambda path: len(path.parts), reverse=True):
            if path.exists() and free[path]:
                _safe(path, root)
                if not any(path.iterdir()) and not_owned(path):
                    mutate(f"Removed empty package-free drop-in directory {path}", path.rmdir)
        for path in _redis_enable_links(systemd, root):
            if _dangling_link(path, root):
                def remove_dangling(path=path):
                    if _dangling_link(path, root):
                        path.unlink()
                        emit(f"REMOVED dangling enable link: {path}")
                mutate(f"Removed dangling enable link {path}", remove_dangling)
        if purge_data:
            for path in data_dirs:
                _safe(path, root)
                if path.exists():
                    mutate(f"Purged Redis data/config {path}", lambda path=path: shutil.rmtree(path))
        mutate("Reloaded systemd units", lambda: checked(["systemctl", "daemon-reload"]))

        emit("Verification report:")
        dangling = [path for path in _redis_enable_links(systemd, root) if _dangling_link(path, root)]
        emit("dangling redis enable links: " + (", ".join(map(str, dangling)) if dangling else "none"))
        for unit in ("redis.service", "redis-sentinel.service"):
            load = runner(["systemctl", "show", unit, "-p", "LoadState", "--value"])
            absent = load.stdout.strip() == "not-found" and load.returncode in {0, 4}
            for verb, acceptable, codes in (
                ("is-active", {"inactive", "failed", "unknown", "not-found"}, {0, 3, 4}),
                ("is-enabled", {"disabled", "static", "masked", "not-found"}, {0, 1, 4}),
            ):
                result = runner(["systemctl", verb, unit])
                value = result.stdout.strip()
                if verb == "is-enabled" and absent and result.returncode == 1 and not value and "No such file or directory" in result.stderr:
                    value = "not-found"
                emit(f"{verb} {unit}: {value or result.stderr.strip()}")
                if result.returncode not in codes or value not in acceptable:
                    raise ForceRemoveError(f"Verification failed: {verb} {unit}")
            link = systemd / "multi-user.target.wants" / unit
            _safe(link, root, leaf_link=True)
            emit(f"multi-user.target.wants/{unit}: {'symlink present' if link.is_symlink() else 'regular file present' if link.exists() else 'absent'}")
        listeners = checked(["ss", "-H", "-ltnp", "sport = :6379"])
        emit(f"ss on 6379: {listeners.stdout.strip() or 'no listeners'}")
        if listeners.stdout.strip():
            raise ForceRemoveError("Verification failed: port 6379 still has a listener.")
        binary = runner(["which", "redis-server"])
        emit(f"which redis-server: {binary.stdout.strip() or 'not found'}")
        if binary.returncode != 1 or binary.stdout.strip():
            raise ForceRemoveError("Verification failed: redis-server is still found or lookup failed.")
        package = runner(["rpm", "-q", "redis"])
        emit(f"rpm -q redis: {package.stdout.strip() or package.stderr.strip()}")
        if package.returncode != 1 or "package redis is not installed" not in package.stdout + package.stderr:
            raise ForceRemoveError("Verification failed: redis package remains or RPM query failed.")
        _, final_key, final_value, final_off = _redis_option(options.read_bytes())
        emit(f"CustomBuild option: {final_key}={final_value}")
        if final_value.lower() != final_off:
            raise ForceRemoveError("Verification failed: CustomBuild Redis option is not off.")
        emit(f"Removal complete. Backup kept at {backup}")
        return 0
    except (ForceRemoveError, OSError, ValueError, subprocess.SubprocessError, tarfile.TarError, KeyboardInterrupt) as exc:
        emit(f"Removal stopped at the first error: {exc or 'interrupted'}")
        emit("Already changed:")
        for item in changed or ["No removal targets changed."]:
            emit(f"  {item}")
        if backup is not None and backup.exists():
            emit(f"Backup kept: {backup}")
            emit("Before restoring, verify SHA256SUMS and review which steps changed.")
            if backup_verified:
                emit(f"Restore files as root: tar -xpf {shlex.quote(str(backup / 'snapshot.tar'))} -C {shlex.quote(str(root))}")
            else:
                emit("The initial backup did not pass verification. Do not extract it unless independently verified.")
            if stopped_data is not None:
                emit(f"Then restore the latest stopped Redis data: tar -xpf {shlex.quote(str(stopped_data / 'snapshot.tar'))} -C {shlex.quote(str(root))}")
            emit(f"Package version and previous unit states: {backup / 'restore-state.json'}")
            emit("Reinstall that redis RPM version with dnf if package removal was attempted (archive restoration alone does not restore the RPM database).")
            emit("Run systemctl daemon-reload; use the saved unit states to enable/start previously enabled/active units by hand after reviewing the files.")
        else:
            emit("No backup created; no removal targets changed.")
        raise ForceRemoveError(str(exc) or "Removal interrupted") from exc
