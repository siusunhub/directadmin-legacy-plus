"""Read cgroup v2 accounting for one user slice.

Parsing is kept apart from file access, so the tests can feed in saved sample files.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from . import paths


@dataclass(frozen=True)
class IOTotals:
    rbytes: int = 0
    wbytes: int = 0
    rios: int = 0
    wios: int = 0


@dataclass(frozen=True)
class Usage:
    taken_at: float
    memory_current: int | None = None
    pids_current: int | None = None
    cpu_usage_usec: int = 0
    cpu_throttled_usec: int = 0
    events: dict[str, int] = field(default_factory=dict)  # memory.events: low, high, max, oom, oom_kill
    io: IOTotals = field(default_factory=IOTotals)


@dataclass(frozen=True)
class Rates:
    cpu_percent: float  # 100 means one full core
    read_bps: float
    write_bps: float
    read_iops: float
    write_iops: float


def parse_int(text: str | None) -> int | None:
    if text is None or not text.strip().isdigit():
        return None
    return int(text.strip())


def parse_keyed(text: str | None) -> dict[str, int]:
    """Parse 'key value' lines, as in memory.events and cpu.stat."""
    out: dict[str, int] = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1])
    return out


def parse_io_stat(text: str | None) -> IOTotals:
    """Parse io.stat ('MAJ:MIN rbytes=N wbytes=N rios=N wios=N ...') and sum over devices."""
    totals = {"rbytes": 0, "wbytes": 0, "rios": 0, "wios": 0}
    for line in (text or "").splitlines():
        for pair in line.split()[1:]:
            key, _, value = pair.partition("=")
            if key in totals and value.isdigit():
                totals[key] += int(value)
    return IOTotals(**totals)


def read_usage(slice_dir: Path, now: float | None = None) -> Usage:
    def read(name: str) -> str | None:
        try:
            return (slice_dir / name).read_text(encoding="utf-8")
        except OSError:
            return None  # slice missing or controller not enabled

    cpu = parse_keyed(read("cpu.stat"))
    return Usage(
        taken_at=time.monotonic() if now is None else now,
        memory_current=parse_int(read("memory.current")),
        pids_current=parse_int(read("pids.current")),
        cpu_usage_usec=cpu.get("usage_usec", 0),
        cpu_throttled_usec=cpu.get("throttled_usec", 0),
        events=parse_keyed(read("memory.events")),
        io=parse_io_stat(read("io.stat")),
    )


def snapshot(uid: int) -> Usage:
    return read_usage(paths.user_slice_dir(uid))


def rates(prev: Usage, cur: Usage) -> Rates | None:
    """Turn two samples into per-second rates. Counter resets are clamped to zero."""
    elapsed = cur.taken_at - prev.taken_at
    if elapsed <= 0:
        return None

    def per_sec(new: int, old: int) -> float:
        return max(new - old, 0) / elapsed

    return Rates(
        cpu_percent=per_sec(cur.cpu_usage_usec, prev.cpu_usage_usec) / 1_000_000 * 100,
        read_bps=per_sec(cur.io.rbytes, prev.io.rbytes),
        write_bps=per_sec(cur.io.wbytes, prev.io.wbytes),
        read_iops=per_sec(cur.io.rios, prev.io.rios),
        write_iops=per_sec(cur.io.wios, prev.io.wios),
    )


def event_delta(prev: Usage, cur: Usage, key: str) -> int:
    return max(cur.events.get(key, 0) - prev.events.get(key, 0), 0)


def status_label(prev: Usage | None, cur: Usage) -> str | None:
    """Name the limit event that happened between two samples, or None if nothing did."""
    if prev is None:
        return None
    if event_delta(prev, cur, "oom_kill") > 0:
        return "OOM"
    throttled = max(cur.cpu_throttled_usec - prev.cpu_throttled_usec, 0) > 0
    if event_delta(prev, cur, "high") > 0 or throttled:
        return "THROTTLED"
    return None


def human(value: float | None, suffix: str = "B") -> str:
    if value is None:
        return "-"
    size = float(value)
    for prefix in ("", "Ki", "Mi", "Gi", "Ti"):
        if abs(size) < 1024:
            return f"{size:.1f} {prefix}{suffix}"
        size /= 1024
    return f"{size:.1f} Pi{suffix}"
