"""Load, validate and resolve the da-plus config.

The YAML file is the single source of truth for limits. Validation is written
by hand so that every error names the exact key that is wrong.
"""

from __future__ import annotations

import copy
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import paths

LIMIT_KEYS = (
    "cpu_quota",
    "memory_high",
    "memory_max",
    "io_read_bw",
    "io_write_bw",
    "iops_read",
    "iops_write",
    "tasks_max",
)
IO_KEYS = ("io_read_bw", "io_write_bw", "iops_read", "iops_write")
UNLIMITED = "infinity"

_UNLIMITED_WORDS = {"infinity", "unlimited"}
_CPU_RE = re.compile(r"^(\d+)%$")
_SIZE_RE = re.compile(r"^(\d+)([KMGTkmgt])$")
_COUNT_RE = re.compile(r"^\d+$")
_DEVICE_RE = re.compile(r"^/dev/[A-Za-z0-9._/-]+$")
_UNITS = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}

TOP_KEYS = {"version", "defaults", "profiles", "package_map", "users"}
DEFAULT_KEYS = {"profile", "io_device"}
ADMIN_UNLIMITED_KEYS = {"cpu_quota", "io_read_bw", "io_write_bw", "iops_read", "iops_write"}

USER_KEYS = {"profile", "overrides", "redis", "limits"}
REDIS_KEYS = {"enabled", "maxmemory", "maxmemory_policy", "persistence", "binary"}
REDIS_POLICIES = {"allkeys-lru", "allkeys-lfu", "allkeys-random", "volatile-lru", "volatile-lfu",
                  "volatile-random", "volatile-ttl", "noeviction"}
REDIS_DEFAULTS = {"enabled": False, "maxmemory": None, "maxmemory_policy": "allkeys-lru",
                  "persistence": "off", "binary": None}


class ConfigError(ValueError):
    """A config problem. The message always names the key that is wrong."""


def normalize_value(key: str, value: Any, where: str) -> str:
    """Turn one YAML value into the exact string systemd expects."""
    text = str(value).strip()
    if text.lower() in _UNLIMITED_WORDS:
        return UNLIMITED
    if key == "cpu_quota":
        match = _CPU_RE.match(text)
        if not match or int(match.group(1)) == 0:
            raise ConfigError(f"{where}: cpu_quota must look like 50% (got {value!r})")
        return f"{int(match.group(1))}%"
    if key in ("iops_read", "iops_write", "tasks_max"):
        if not _COUNT_RE.match(text) or int(text) == 0:
            raise ConfigError(f"{where}: {key} must be a whole number above 0, or infinity (got {value!r})")
        return str(int(text))
    # Memory and IO bandwidth: a number with an explicit unit, never a bare number.
    match = _SIZE_RE.match(text)
    if not match or int(match.group(1)) == 0:
        raise ConfigError(f"{where}: {key} needs an explicit unit such as 512M or 2G, or infinity (got {value!r})")
    return f"{int(match.group(1))}{match.group(2).upper()}"


def size_bytes(text: str) -> int:
    """Convert a normalized size such as 512M into bytes."""
    return int(text[:-1]) * _UNITS[text[-1].upper()]


def check_memory(values: dict[str, str], where: str) -> None:
    high, limit = values["memory_high"], values["memory_max"]
    if UNLIMITED in (high, limit):
        return
    if size_bytes(high) > size_bytes(limit):
        raise ConfigError(f"{where}: memory_high ({high}) must not be larger than memory_max ({limit})")


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be a mapping")
    return value


def _check_keys(body: dict[str, Any], allowed: set[str] | tuple[str, ...], where: str) -> None:
    unknown = sorted(set(body) - set(allowed))
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)}")


def _parse_profile(name: str, body: Any) -> dict[str, str]:
    where = f"profiles.{name}"
    body = _mapping(body, where)
    _check_keys(body, set(LIMIT_KEYS) | {"redis"}, where)
    # A missing key means systemd's default, which is unlimited. Write infinity
    # in the profile to make that explicit.
    values = {key: normalize_value(key, body.get(key, UNLIMITED), f"{where}.{key}") for key in LIMIT_KEYS}
    check_memory(values, where)
    return values


@dataclass(frozen=True)
class Config:
    default_profile: str
    io_device: str | None
    profiles: dict[str, dict[str, str]]
    package_map: dict[str, str]
    # Per user: {"profile": str | None, "overrides": {limit key: normalized value}}
    users: dict[str, dict[str, Any]]
    redis_profiles: dict[str, dict[str, Any]] = field(default_factory=dict)


def redis_binary(binary: str | None = None) -> str:
    """Validate the explicit executable or choose the documented default."""
    binary = binary or ("/usr/local/bin/redis-server" if Path("/usr/local/bin/redis-server").exists()
                        else "/usr/bin/redis-server")
    path = Path(binary)
    if not path.is_absolute() or any(character in binary for character in "\r\n\0"):
        raise ConfigError("redis.binary: must be an absolute executable path")
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ConfigError(f"redis.binary: {binary!r} must exist and be executable")
    return str(path)


def _parse_redis(body: Any, where: str) -> dict[str, Any]:
    body = _mapping(body, where)
    if any(not isinstance(key, str) for key in body):
        raise ConfigError(f"{where}: unknown key; Redis keys must be strings")
    _check_keys(body, REDIS_KEYS, where)
    values = dict(body)
    if "enabled" in values and not isinstance(values["enabled"], bool):
        raise ConfigError(f"{where}.enabled: must be true or false")
    if "maxmemory" in values:
        text = str(values["maxmemory"]).strip()
        match = _SIZE_RE.fullmatch(text)
        if not match or int(match[1]) <= 0:
            raise ConfigError(f"{where}.maxmemory: needs a positive explicit unit such as 64M")
        values["maxmemory"] = f"{int(match[1])}{match[2].upper()}"
    if "maxmemory_policy" in values and (not isinstance(values["maxmemory_policy"], str)
                                         or values["maxmemory_policy"] not in REDIS_POLICIES):
        raise ConfigError(f"{where}.maxmemory_policy: must be one of {', '.join(sorted(REDIS_POLICIES))}")
    # PyYAML's YAML 1.1 resolver reads an unquoted "off" as False.
    if values.get("persistence") is False:
        values["persistence"] = "off"
    if "persistence" in values and values["persistence"] not in ("off", "rdb"):
        raise ConfigError(f"{where}.persistence: must be off or rdb")
    if "binary" in values:
        if not isinstance(values["binary"], str) or not values["binary"]:
            raise ConfigError(f"{where}.binary: must be an executable path")
        values["binary"] = redis_binary(values["binary"])
    return values


def _check_redis_memory(settings: dict[str, Any], limits: dict[str, str], where: str) -> None:
    memory = settings.get("maxmemory")
    if memory is None:
        if settings.get("enabled", False):
            raise ConfigError(f"{where}.maxmemory: required when Redis is enabled")
        return
    if limits["memory_max"] != UNLIMITED and size_bytes(memory) >= size_bytes(limits["memory_max"]):
        raise ConfigError(f"{where}.maxmemory ({memory}) must be smaller than memory_max ({limits['memory_max']})")


def parse(raw: Any) -> Config:
    """Validate a raw YAML mapping and return the normalized Config."""
    raw = _mapping(raw, "config")
    _check_keys(raw, TOP_KEYS, "top level")
    if raw.get("version") != 1:
        raise ConfigError("version: must be 1")

    defaults = _mapping(raw.get("defaults"), "defaults")
    _check_keys(defaults, DEFAULT_KEYS, "defaults")

    profiles_raw = _mapping(raw.get("profiles"), "profiles")
    if not profiles_raw:
        raise ConfigError("profiles: define at least one profile")
    profiles = {str(name): _parse_profile(str(name), body) for name, body in profiles_raw.items()}
    redis_profiles = {str(name): _parse_redis(body["redis"], f"profiles.{name}.redis")
                      for name, body in profiles_raw.items() if isinstance(body, dict) and "redis" in body}
    for name, settings in redis_profiles.items():
        _check_redis_memory(settings, profiles[name], f"profiles.{name}.redis")
        if settings.get("enabled", False):
            redis_binary(settings.get("binary"))

    default_profile = defaults.get("profile")
    if default_profile not in profiles:
        raise ConfigError(f"defaults.profile: {default_profile!r} is not a defined profile")

    io_device = defaults.get("io_device")
    if io_device is not None:
        io_device = str(io_device)
        if not _DEVICE_RE.match(io_device):
            raise ConfigError(f"defaults.io_device: {io_device!r} must look like /dev/sda")

    package_map: dict[str, str] = {}
    for package, profile in _mapping(raw.get("package_map"), "package_map").items():
        if profile not in profiles:
            raise ConfigError(f"package_map.{package}: {profile!r} is not a defined profile")
        package_map[str(package)] = profile

    users: dict[str, dict[str, Any]] = {}
    for name, body in _mapping(raw.get("users"), "users").items():
        where = f"users.{name}"
        body = _mapping(body, where)
        _check_keys(body, USER_KEYS, where)
        profile = body.get("profile")
        if profile is not None and profile not in profiles:
            raise ConfigError(f"{where}.profile: {profile!r} is not a defined profile")
        overrides: dict[str, str] = {}
        for key, value in _mapping(body.get("overrides"), f"{where}.overrides").items():
            if name == "admin" and key in ADMIN_UNLIMITED_KEYS:
                raise ConfigError("users.admin: cpu and io limits are not allowed for the admin account")
            if key not in LIMIT_KEYS:
                raise ConfigError(f"{where}.overrides: unknown key {key!r}")
            overrides[key] = normalize_value(key, value, f"{where}.overrides")
        users[str(name)] = {"profile": profile, "overrides": overrides}
        if "limits" in body:
            state = body["limits"]
            # YAML 1.1 reads the unquoted words on/off as booleans.
            if isinstance(state, bool):
                state = "on" if state else "off"
            if not isinstance(state, str) or state not in {"on", "off"}:
                raise ConfigError(f"{where}.limits: must be on or off")
            users[str(name)]["limits"] = state
        if "redis" in body:
            users[str(name)]["redis"] = _parse_redis(body["redis"], f"{where}.redis")

    uses_io = any(
        value != UNLIMITED for values in profiles.values() for key, value in values.items() if key in IO_KEYS
    ) or any(
        value != UNLIMITED for entry in users.values() for key, value in entry["overrides"].items() if key in IO_KEYS
    )
    if uses_io and io_device is None:
        raise ConfigError("defaults.io_device: required because some profile or override sets IO limits")

    # Memory ordering must hold for every profile a user could end up with.
    for name, entry in users.items():
        for profile_name, values in profiles.items():
            check_memory({**values, **entry["overrides"]}, f"users.{name} (profile {profile_name})")
            if entry["profile"] is None or entry["profile"] == profile_name:
                settings = {**REDIS_DEFAULTS, **redis_profiles.get(profile_name, {}), **entry.get("redis", {})}
                _check_redis_memory(settings, {**values, **entry["overrides"]}, f"users.{name}.redis")
                if settings["enabled"]:
                    redis_binary(settings["binary"])

    return Config(
        default_profile=default_profile,
        io_device=io_device,
        profiles=profiles,
        package_map=package_map,
        users=users,
        redis_profiles=redis_profiles,
    )


def resolve(config: Config, user: str, package: str | None) -> tuple[str, dict[str, str]]:
    """Return (profile name, final limit values) for one DirectAdmin user.

    Profile precedence: the user's own profile, then the package map, then the default.
    Overrides are applied on top of the chosen profile.
    """
    entry = config.users.get(user, {})
    name = entry.get("profile") or config.package_map.get(package or "") or config.default_profile
    values = {**config.profiles[name], **entry.get("overrides", {})}
    if user == "admin":
        values.update(dict.fromkeys(ADMIN_UNLIMITED_KEYS, UNLIMITED))
    check_memory(values, f"users.{user}")
    values["limits"] = entry.get("limits", "on")
    return name, values


def resolve_redis(config: Config, user: str, package: str | None, *, require_binary: bool = False) -> dict[str, Any]:
    """Resolve Redis separately, keeping the existing systemd limit contract intact."""
    profile, limits = resolve(config, user, package)
    settings = {**REDIS_DEFAULTS, **config.redis_profiles.get(profile, {}), **config.users.get(user, {}).get("redis", {})}
    _check_redis_memory(settings, limits, f"users.{user}.redis")
    if require_binary or settings["enabled"]:
        if settings["maxmemory"] is None:
            raise ConfigError(
                f"Redis cannot be enabled for {user} yet: no memory budget (maxmemory). "
                "Set it in the TUI: press Enter on the account, go to the Redis tab of the Limits screen, "
                "set maxmemory (for example 128M), press Ctrl+S. "
                f"Or set it in {paths.config_file()} under profiles.{profile}.redis.maxmemory "
                f"or users.{user}.redis.maxmemory."
            )
        settings["binary"] = redis_binary(settings["binary"])
    return settings


def load_raw(path: Path | None = None) -> dict[str, Any]:
    path = path or paths.config_file()
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    return _mapping(data, str(path))


def load(path: Path | None = None) -> Config:
    return parse(load_raw(path))


STARTER_TEXT = """\
# da-plus config. Edit the limits below, then save and apply.
version: 1

defaults:
  profile: basic
  io_device: /dev/sda          # the disk behind the home directories

profiles:
  basic:
    cpu_quota: 50%
    memory_high: 512M
    memory_max: 768M
    tasks_max: 200

package_map: {}

users: {}
"""


EXAMPLE_TEXT = """\
# Example da-plus config. Copy to /etc/da-plus/config.yml (mode 0640, root:root).
# Values follow the plan's section 6. Missing keys mean "infinity" (unlimited).
version: 1

defaults:
  profile: basic
  io_device: /dev/sda          # the disk behind the home directories; check with: lsblk -dno PATH

profiles:
  basic:
    cpu_quota: 50%
    memory_high: 512M
    memory_max: 768M
    io_read_bw: 50M
    io_write_bw: 20M
    iops_read: 1000
    iops_write: 500
    tasks_max: 200
    # Redis is off in the example. Set enabled: true on a server that has redis-server installed.
    redis:
      enabled: false
      maxmemory: 64M              # must be smaller than memory_max
      maxmemory_policy: allkeys-lru
      persistence: off           # off or rdb
  business:
    cpu_quota: 100%
    memory_high: 1G
    memory_max: 1536M
    io_read_bw: 100M
    io_write_bw: 50M
    iops_read: 3000
    iops_write: 1500
    tasks_max: 500
    redis:
      enabled: false
      maxmemory: 256M
      maxmemory_policy: allkeys-lru
      persistence: off

# DirectAdmin package name -> profile
package_map:
  Starter: basic
  Business: business

users:
  fred:
    profile: business
    overrides:
      memory_max: 2G
"""


def validate_text(text: str) -> Config:
    """Check config text the same way the program loads it. Raises ConfigError with the problem."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML: {exc}") from exc
    return parse(data)


def load_text(path: Path | None = None) -> str:
    path = path or paths.config_file()
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(tmp_name, 0o640)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def save_raw(raw: dict[str, Any], path: Path | None = None) -> None:
    """Validate, then atomically replace the config file. Comments in the old file are not kept."""
    parse(raw)  # refuse to write a config that could not be loaded again
    _atomic_write(path or paths.config_file(), yaml.safe_dump(raw, sort_keys=False))


def save_text(text: str, path: Path | None = None) -> None:
    """Validate, then write the text exactly as given, so comments and layout are kept."""
    validate_text(text)
    _atomic_write(path or paths.config_file(), text)


def remove_user(raw: dict[str, Any], user: str) -> dict[str, Any]:
    """Return a copy of raw with the user's whole entry removed."""
    new = copy.deepcopy(raw)
    (new.get("users") or {}).pop(user, None)
    return new


def set_overrides(raw: dict[str, Any], user: str, overrides: dict[str, str]) -> dict[str, Any]:
    """Return a copy of raw with one user's overrides replaced. Empty overrides drop the user entry."""
    new = copy.deepcopy(raw)
    users = new.get("users") or {}
    entry = dict(users.get(user) or {})
    if overrides:
        entry["overrides"] = dict(overrides)
    else:
        entry.pop("overrides", None)
    if entry:
        users[user] = entry
    else:
        users.pop(user, None)
    new["users"] = users
    return new


def set_limits(raw: dict[str, Any], user: str, state: str) -> dict[str, Any]:
    """Return a copy with persistent OFF set, or the flag removed for default ON."""
    if state not in {"on", "off"}:
        raise ConfigError("limits: must be on or off")
    new = copy.deepcopy(raw)
    users = new.get("users") or {}
    entry = dict(users.get(user) or {})
    if state == "on" and "limits" not in entry:
        return new
    if state == "off":
        entry["limits"] = "off"
    else:
        entry.pop("limits", None)
    if entry:
        users[user] = entry
    else:
        users.pop(user, None)
    new["users"] = users
    return new
