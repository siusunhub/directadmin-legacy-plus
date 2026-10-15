"""Screens for the da-plus TUI.

The TUI calls the same config and apply functions as the CLI, so there is one
code path that writes limits. Nothing here touches systemd directly.
"""

from __future__ import annotations

import asyncio
import difflib
import os
import shlex
import subprocess
import threading
import time
import yaml
from collections import deque
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, RadioButton, RadioSet, Select, Static, TextArea
from textual.widgets._footer import FooterKey

from .. import config as config_mod
from .. import da_users, paths, usage, redis_mgr, redis_user
from ..config import LIMIT_KEYS, ConfigError, normalize_value
from ..systemd.apply import ApplyError, apply_all, apply_user, stop_limits, stop_limits_plan, write_log
from ..systemd.render import render_dropin

POLL_SECONDS = 2
TABLE_SECONDS = 5
QUERY_TIMEOUT = 3
QUERY_CACHE_SECONDS = 30
HISTORY = 60  # samples kept for the sparklines
ALERT_SECONDS = 300  # how long an OOM or THROTTLED status stays visible


def _is_root() -> bool:
    return not hasattr(os, "geteuid") or os.geteuid() == 0


def _limits_present(user: da_users.DAUser) -> bool:
    return user.uid is not None and paths.dropin_path(user.uid).exists()


def _status_text(message: str, style: str) -> Text:
    """Use a span so widget CSS cannot override the status colour."""
    text = Text(message)
    text.stylize(style, 0, len(message))
    return text


def _limits_text(user: da_users.DAUser) -> Text:
    enabled = _limits_present(user)
    return _status_text("ON" if enabled else "OFF", "bold bright_green" if enabled else "bold bright_red")


def _panel(title: str, *, id: str, scroll: bool = False):
    panel = (VerticalScroll if scroll else Vertical)(id=id, classes="panel")
    panel.border_title = title
    return panel


class TopBar(Header):
    """Visual tabs; navigation continues to use the existing screen bindings."""

    def __init__(self, active: str) -> None:
        super().__init__(icon="")
        self.active_tab = active

    def compose(self) -> ComposeResult:
        yield Static(Text("da-plus", style="bold #ffb454"), classes="app-name")
        with Horizontal(classes="top-tabs"):
            for tab in ("Users", "Usage", "Logs", "Config"):
                yield Static(Text(tab, style="bold"), classes="tab active" if tab == self.active_tab else "tab")
        path = str(paths.config_file()).encode("ascii", "backslashreplace").decode("ascii")
        yield Static(Text(path, style="dim"), classes="config-path")
        yield from super().compose()


def _tail(path: Path, count: int = 200) -> str:
    try:
        lines = deque(path.read_text(encoding="utf-8", errors="replace").splitlines(), maxlen=count)
    except OSError:
        return "(no entries yet)"
    return "\n".join(lines) or "(empty)"


def _diff_text(diff: str) -> Text:
    text = Text()
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            style = "bold bright_green"
        elif line.startswith("-") and not line.startswith("---"):
            style = "bold bright_red"
        else:
            style = "bright_white"
        text.append(line + "\n", style=style)
    return text


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("left,shift+tab", "previous_button", show=False, priority=True),
        Binding("right,tab", "next_button", show=False, priority=True),
    ]
    DEFAULT_CSS = """
    ConfirmScreen {
        align: center middle;
    }
    #dialog {
        width: 80%;
        height: 80%;
        max-height: 90%;
        border: ascii $accent;
        background: #000000;
        color: #ffffff;
        padding: 1 2;
        overflow: hidden hidden;
    }
    #body {
        height: 1fr;
        min-height: 1;
    }
    #question {
        height: auto;
        margin-bottom: 1;
    }
    #buttons {
        height: 3;
        margin: 0;
        align-horizontal: right;
    }
    #buttons Button {
        height: 3;
        margin: 0 1;
        border: ascii #ffffff;
        text-style: bold;
    }
    #buttons Button:focus {
        text-style: bold reverse;
    }
    """

    def __init__(self, question: str, body: str = "") -> None:
        super().__init__()
        self.question = question
        self.body = body

    def compose(self) -> ComposeResult:
        with _panel("Confirm", id="dialog"):
            body = VerticalScroll(id="body")
            body.can_focus = False
            with body:
                yield Static(Text(self.question, style="bold"), id="question")
                if self.body:
                    yield Static(_diff_text(self.body))
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Confirm", id="confirm", variant="error")
        yield UiFooter()

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def action_next_button(self) -> None:
        self.focus_next("#buttons Button")

    def action_previous_button(self) -> None:
        self.focus_previous("#buttons Button")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)


class UsersScreen(Screen):
    DEFAULT_CSS = """
    UsersScreen #status-panels {
        height: 12;
        max-height: 50%;
    }
    UsersScreen #verify-panel {
        height: 1fr;
        width: 60%;
    }
    UsersScreen #redis-panel {
        height: 1fr;
        width: 40%;
    }
    UsersScreen #verify, UsersScreen #redis-status {
        height: auto;
    }
    """
    # Enter is not bound here: DataTable consumes it and reports RowSelected instead.
    BINDINGS = [
        Binding("u", "open_usage", "Usage"),
        Binding("l", "open_logs", "Logs"),
        Binding("c", "open_config", "Config"),
        Binding("r", "reload", "Reload"),
        Binding("A,shift+a", "apply_all", "Apply all", key_display="Shift+A"),
        Binding("d", "toggle_redis", "Redis enable/disable"),
        Binding("q", "app.quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.config: config_mod.Config | None = None
        self.users: list[da_users.DAUser] = []
        self.previous: dict[str, usage.Usage] = {}
        self.alerts: dict[str, tuple[str, float]] = {}
        self.col: dict[str, object] = {}
        self._verify_cpu: dict[int, tuple[int, int]] = {}
        self._verify_oom_start: dict[int, int] = {}
        self._load_worker = None
        self._verify_worker = None
        self._reload_again = False
        self._first_load_complete = False
        self._initial_resume = True
        self._load_started = time.monotonic()
        self._load_error = ""
        self._table_cache: dict[str, dict[str, object]] = {}
        self._delegation_cache: dict[int, tuple[float, tuple[str, str]]] = {}
        self._dropin_directory_cache = None
        self._dropin_cache_lock = threading.Lock()
        self._redis_worker = None
        self._redis_action_worker = None
        self._redis_last_poll = 0.0
        self._redis_snapshot: dict = {}
        self._redis_data_worker = None
        self._redis_data = {}

    def compose(self) -> ComposeResult:
        yield TopBar("Users")
        with _panel("Users", id="users-panel"):
            yield DataTable(id="users", cursor_type="row", zebra_stripes=True)
            yield Static("loading...", id="message")
        with Horizontal(id="status-panels"):
            with _panel("Verify", id="verify-panel", scroll=True):
                yield Static(Text("loading...", style="dim"), id="verify")
            with _panel("Redis", id="redis-panel", scroll=True):
                yield Static(Text("loading...", style="dim"), id="redis-status")
        yield UiFooter()

    def on_mount(self) -> None:
        names = ("user", "uid", "package", "profile", "memory", "read", "write", "tasks", "status", "limits", "redis")
        headers = ("Name", "UID", "Package", "Profile", "Memory", "Read/s", "Write/s", "Tasks", "Status", "Limits", "Redis")
        # Reserve room for DISABLED up front, avoiding column-layout changes
        # from background updates while a confirmation dialog is open.
        table = self.query_one(DataTable)
        keys = [table.add_column(header, width=8 if header == "Limits" else None) for header in headers]
        self.col = dict(zip(names, keys))
        if not _is_root():
            self._message("Not running as root: you can look around, but changes will fail.", "bright_yellow")
        self.reload()
        self.set_interval(POLL_SECONDS, self.poll)
        self.set_interval(TABLE_SECONDS, self._refresh_table_status)
        self.set_timer(5, self._startup_guard)

    def _startup_guard(self) -> None:
        if not self._first_load_complete:
            self._message("loading system state...")

    def _message(self, text: str, style: str = "") -> None:
        self.query_one("#message", Static).update(_status_text(text, f"bold {style or 'bright_white'}"))

    def reload(self) -> None:
        if self._load_worker is not None and not self._load_worker.is_finished:
            self._reload_again = True
            return
        if not self._first_load_complete:
            self._message(self._loading_text())
        self._load_worker = self.run_worker(self._load_users, group="users-load")

    @staticmethod
    def _discover_users():
        error = ""
        try:
            config = config_mod.load()
        except (ConfigError, OSError) as exc:
            config = None
            error = f"config error: {exc}"
        try:
            users = da_users.list_users()
        except (OSError, UnicodeError) as exc:
            users = []
            error = f"N/A: account discovery failed: {exc}"
        return config, users, error

    def _loading_text(self) -> str:
        return "loading system state..." if time.monotonic() - self._load_started >= 5 else "loading..."

    async def _load_users(self) -> None:
        config, users, error = await asyncio.to_thread(self._discover_users)
        if not self.is_mounted:
            return
        table = self.query_one(DataTable)
        selected = self._selected()
        table.clear()
        self.config, self.users, self._load_error = config, users, error
        self._table_cache = {user.name: self._table_cache.get(user.name, {
            key: "N/A" for key in ("memory", "read", "write", "tasks", "status")
        }) for user in users}
        for user in self.users:
            profile = "-"
            if self.config is not None:
                profile = config_mod.resolve(self.config, user.name, user.package)[0]
            uid = "-" if user.uid is None else str(user.uid)
            table.add_row(user.name, uid, user.package or "-", profile, "-", "-", "-", "-", "-",
                          Text("N/A", style="dim"), Text("loading...", style="dim"), key=user.name)
        if selected:
            for row, user in enumerate(self.users):
                if user.name == selected:
                    table.move_cursor(row=row)
                    break
        self._load_worker = None
        self._render_cached_table()
        self._message(error or (self._loading_text() if not self._first_load_complete else ""))
        self._queue_redis(force=True)
        self.poll()
        if self._reload_again:
            self._reload_again = False
            self.reload()

    def poll(self) -> None:
        # No filesystem or subprocess operations run on this message-pump thread.
        if self.app.screen is self:
            self._queue_verify()
            self._queue_redis()

    @staticmethod
    def _redis_command(argv):
        return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=QUERY_TIMEOUT)

    def _queue_redis(self, *, force=False):
        if self._redis_worker is not None and not self._redis_worker.is_finished:
            return
        if not force and time.monotonic() - self._redis_last_poll < TABLE_SECONDS:
            return
        self._redis_worker = self.run_worker(self._refresh_redis, group="redis-status")

    def _refresh_table_status(self):
        if self.app.screen is self:
            self._queue_redis(force=True)
            self._render_cached_table()

    @staticmethod
    def _redis_colour(label):
        return {"OFF": "bold bright_red", "ON-stopped": "bold yellow",
                "ON-running": "bold bright_green", "FAILED": "bold bright_red"}.get(label, "dim")

    def _read_row_status(self, users):
        """Cheap status for every row, with one batched service query off the UI thread."""
        limits = {}
        for user in users:
            label = "N/A"
            disabled = self.config is not None and self.config.users.get(user.name, {}).get("limits", "on") == "off"
            if user.uid is not None:
                try:
                    dropin = self._dropin_for_verify(user.uid)
                    label = "ON" if dropin.is_file() else "OFF"
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
            label = "DISABLED" if disabled else label
            limits[user.name] = _status_text(label, "bold bright_green" if label == "ON" else
                                     "bold bright_red" if label in {"OFF", "DISABLED"} else "dim")

        query_error = ""
        def query(argv):
            nonlocal query_error
            try:
                result = self._redis_command(argv)
            except subprocess.TimeoutExpired as exc:
                query_error = "timeout"
                output = exc.stdout or ""
                if isinstance(output, bytes):
                    output = output.decode("utf-8", errors="replace")
                # Complete records already returned by a batch remain usable;
                # the absent/incomplete account alone is unavailable.
                if argv[:2] == ["systemctl", "show"]:
                    return subprocess.CompletedProcess(argv, 0, output, "")
                # A version timeout must not prevent the service-state batch.
                return subprocess.CompletedProcess(argv, 1, "", "timeout")
            if argv[:2] == ["systemctl", "show"] and result.returncode and result.stdout:
                query_error = (result.stderr or "query failed").strip()
                return subprocess.CompletedProcess(argv, 0, result.stdout, result.stderr)
            return result

        snapshot = redis_mgr.runtime_snapshot([user.name for user in users], query)
        if query_error:
            snapshot["error"] = query_error
        snapshot["units"] = {unit: state for unit, state in snapshot.get("units", {}).items()
                             if {"LoadState", "ActiveState"} <= state.keys()}
        return snapshot, limits

    async def _refresh_redis(self):
        users = list(self.users)
        names = [user.name for user in users]
        snapshot, limits = await asyncio.to_thread(self._read_row_status, users)
        self._redis_worker = None
        if not self.is_mounted:
            return
        if names != [user.name for user in self.users]:
            self._queue_redis(force=True)
            return
        self._redis_last_poll = time.monotonic()
        self._redis_snapshot = snapshot
        for name, state in limits.items():
            if self.config is not None and self.config.users.get(name, {}).get("limits", "on") == "off":
                state = _status_text("DISABLED", "bold bright_red")
            self._table_cache.setdefault(name, {})["limits"] = state
        self._render_redis_panel()
        self._render_cached_table()
        if self._redis_data_worker is None or self._redis_data_worker.is_finished:
            self._redis_data_worker = self.run_worker(self._refresh_redis_data(users, snapshot), group="redis-data")

    def _render_redis_panel(self):
        snapshot = self._redis_snapshot
        names = [user.name for user in self.users]
        install_style = self._redis_colour("ON-running" if snapshot["installed"] else "OFF")
        text = Text()
        text.append(f"Redis installed: {'yes' if snapshot['installed'] else 'no'}; version: {snapshot['version']}\n",
                    style=install_style)
        text.append(f"Install state: {'installed' if snapshot['installed'] else 'not installed'}\n", style=install_style)
        if snapshot.get("managed"):
            text.append(redis_mgr.DIRECTADMIN_REDIS_MESSAGE + "\n", style="bold bright_red")
        if snapshot.get("error"):
            text.append(f"N/A: {snapshot['error']}\n", style="dim")
        units = snapshot.get("units", {})
        for unit in ("redis.service", "redis-server.service"):
            state = units.get(unit, {})
            active = state.get("ActiveState", "N/A")
            label = "ON-running" if active in ("active", "activating", "reloading") else \
                "FAILED" if active == "failed" else "ON-stopped" if active == "inactive" \
                and state.get("UnitFileState") == "enabled" else "OFF" if active == "inactive" else "N/A"
            text.append(f"{unit}: {state.get('UnitFileState', 'N/A')}; {active}\n",
                        style=self._redis_colour(label))
        for name in names:
            state = units.get(f"da-plus-redis-{name}.service")
            label = redis_mgr.account_state(state)
            style = self._redis_colour(label)
            self._table_cache.setdefault(name, {})["redis"] = _status_text(label, style)
            text.append(f"{name}: {label}; {(state or {}).get('UnitFileState', 'N/A')}; "
                        f"{(state or {}).get('ActiveState', 'N/A')}\n", style=style)
            data = self._redis_data.get(name, {})
            text.append(f"  slice memory: {data.get('slice', 'loading...')}\n")
            text.append(f"  Redis memory: {data.get('redis', 'loading...')}; keyspace: {data.get('keyspace', 'loading...')}\n")
        self.query_one("#redis-status", Static).update(text)

    @staticmethod
    def _read_redis_data(user, running):
        data = {"slice": "N/A: no Linux account", "redis": "N/A: not running", "keyspace": "N/A: not running"}
        if user.uid is not None:
            try:
                root = paths.user_slice_dir(user.uid)
                used = int((root / "memory.current").read_text(encoding="utf-8").strip())
                limit = (root / "memory.max").read_text(encoding="utf-8").strip()
                data["slice"] = f"{usage.human(used)} / {'unlimited' if limit == 'max' else usage.human(int(limit))}"
            except FileNotFoundError:
                data["slice"] = "N/A: file missing"
            except (OSError, ValueError):
                data["slice"] = "N/A: file unreadable or invalid"
        if running:
            info = redis_user.info(user.name)
            if info["reason"]:
                data["redis"] = data["keyspace"] = f"N/A: {info['reason']}"
            else:
                try:
                    data["redis"] = usage.human(int(info["memory"]["used_memory"]))
                except (KeyError, ValueError):
                    data["redis"] = "N/A: invalid used_memory"
                data["keyspace"] = "; ".join(f"{key}: {value}" for key, value in info["keyspace"].items()) or "empty"
        return data

    async def _refresh_redis_data(self, users, snapshot):
        async def read(user):
            state = snapshot.get("units", {}).get(f"da-plus-redis-{user.name}.service")
            data = await asyncio.to_thread(self._read_redis_data, user, redis_mgr.account_state(state) == "ON-running")
            if self.is_mounted and user in self.users:
                self._redis_data[user.name] = data
                self._render_redis_panel()
        try:
            await asyncio.gather(*(read(user) for user in users))
        finally:
            self._redis_data_worker = None

    def action_toggle_redis(self):
        if not _is_root():
            self.app.notify("Run da-plus tui as root to change Redis.", severity="error")
            return
        name = self._selected()
        if not name:
            return
        if self._redis_action_worker is not None and not self._redis_action_worker.is_finished:
            self.app.notify("Redis action already in progress.", severity="warning")
            return
        state = self._redis_snapshot.get("units", {}).get(f"da-plus-redis-{name}.service")
        if state is None:
            self.app.notify("Redis state is loading or unavailable; wait for refresh.", severity="error")
            self._queue_redis(force=True)
            return
        action = "enable" if redis_mgr.account_state(state) == "OFF" else "disable"
        self._redis_action_worker = self.run_worker(self._preview_redis(name, action), group="redis-action")

    async def _preview_redis(self, name, action):
        try:
            lines = await asyncio.to_thread(redis_user.preview, action, name, runner=self._redis_command)
        except (redis_user.RedisUserError, OSError, ValueError, subprocess.SubprocessError) as exc:
            self.app.notify(str(exc), severity="error")
            return
        finally:
            self._redis_action_worker = None
        if self.is_mounted:
            self.app.push_screen(ConfirmScreen(f"Redis {action} for {name}?", "\n".join(lines)),
                                 lambda confirmed: self._redis_confirmed(confirmed, name, action))

    def _redis_confirmed(self, confirmed, name, action):
        if confirmed:
            self._redis_action_worker = self.run_worker(self._change_redis(name, action), group="redis-action")

    async def _change_redis(self, name, action):
        try:
            function = redis_user.enable if action == "enable" else redis_user.disable
            await asyncio.to_thread(function, name, runner=self._redis_command)
        except (redis_user.RedisUserError, OSError, ValueError, subprocess.SubprocessError) as exc:
            self.app.notify(str(exc), severity="error")
        else:
            self.app.notify(f"Redis {action} completed for {name}.")
        finally:
            self._redis_action_worker = None
            if self.is_mounted:
                self._queue_redis(force=True)

    def _render_cached_table(self) -> None:
        table = self.query_one(DataTable)
        now = time.monotonic()
        for user in self.users:
            cells = dict(self._table_cache.get(user.name, {}))
            if user.name in self.previous:
                cells["status"] = self._status(user.name, now)
            for name, value in cells.items():
                table.update_cell(user.name, self.col[name], value)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "users":
            self.poll()

    def _queue_verify(self) -> None:
        if not list(self.query("#verify")):
            return
        if self._load_worker is not None and not self._load_worker.is_finished:
            return
        name = self._selected()
        panel = self.query_one("#verify-panel")
        title = f"Verify: {name}" if name else "Verify"
        if panel.border_title != title:
            panel.border_title = title
            self.query_one("#verify", Static).update(Text("loading...", style="dim"))
        if self._verify_worker is not None and not self._verify_worker.is_finished:
            return
        self._verify_worker = self.run_worker(self._refresh_verify, group="verify")

    async def _refresh_verify(self) -> None:
        if not list(self.query("#verify")):
            return
        name = self._selected()
        user = next((user for user in self.users if user.name == name), None)
        panel = self.query_one("#verify-panel")
        title = f"Verify: {name}" if name else "Verify"
        if panel.border_title != title:
            # Do not show the previous account's values while this query runs.
            self.query_one("#verify", Static).update(Text("loading...", style="dim"))
        panel.border_title = title
        config = self.config
        text, cur, limits = await asyncio.to_thread(self._read_verify, user, config)
        self._verify_worker = None
        if not self.is_mounted:
            return
        # Children may already be removed while the screen itself is still
        # mounted during shutdown. Discard late background results then.
        verify_widgets = list(self.query("#verify"))
        if not verify_widgets or not verify_widgets[0].is_mounted:
            return
        if name != self._selected() or config is not self.config:
            self.poll()
            return
        verify_widgets[0].update(text)
        self._first_load_complete = True
        self._message(self._load_error)
        if user is not None:
            if config is not None and config.users.get(user.name, {}).get("limits", "on") == "off":
                limits = _status_text("DISABLED", "bold bright_red")
            cells: dict[str, object] = {"limits": limits}
            if cur is not None:
                prev = self.previous.get(user.name)
                self.previous[user.name] = cur
                rate = usage.rates(prev, cur) if prev else None
                label = usage.status_label(prev, cur)
                if label:
                    self.alerts[user.name] = (label, time.monotonic())
                cells.update({
                    "memory": usage.human(cur.memory_current),
                    "read": usage.human(rate.read_bps, "B/s") if rate else "-",
                    "write": usage.human(rate.write_bps, "B/s") if rate else "-",
                    "tasks": "-" if cur.pids_current is None else str(cur.pids_current),
                })
            self._table_cache.setdefault(user.name, {}).update(cells)
            self._render_cached_table()

    def _dropin_for_verify(self, uid: int) -> Path:
        # The selected Verify sampler and all-row status worker share discovery.
        with self._dropin_cache_lock:
            return self._cached_dropin_for_verify(uid)

    def _cached_dropin_for_verify(self, uid: int) -> Path:
        cached = self._dropin_directory_cache
        if cached is None or time.monotonic() >= cached[0]:
            try:
                path = paths.dropin_path(uid, timeout=QUERY_TIMEOUT)
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                self._dropin_directory_cache = (time.monotonic() + QUERY_CACHE_SECONDS, None, exc)
                raise
            self._dropin_directory_cache = (time.monotonic() + QUERY_CACHE_SECONDS, path.parent.parent, None)
            return path
        if cached[2] is not None:
            raise cached[2]
        return cached[1] / f"user-{uid}.slice.d" / "50-da-plus.conf"

    def _delegation(self, uid: int) -> tuple[str, str]:
        cached = self._delegation_cache.get(uid)
        if cached is not None and time.monotonic() < cached[0]:
            return cached[1]
        argv = ["systemctl", "show", f"user@{uid}.service", "-p", "DelegateControllers", "--value"]
        try:
            result = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=QUERY_TIMEOUT)
        except subprocess.TimeoutExpired:
            state = ("N/A", "timeout")
        except (OSError, UnicodeError):
            state = ("N/A", "query failed")
        else:
            raw = result.stdout.strip()
            error = (result.stderr or "").lower()
            no_unit = any(reason in error for reason in ("could not be found", "not loaded", "no such unit"))
            if (result.returncode == 0 and not raw) or no_unit:
                state = ("N/A", "user manager not running")
            elif result.returncode != 0:
                state = ("N/A", f"query failed (exit {result.returncode})")
            else:
                state = ("OK" if {"cpu", "io"} <= set(raw.split()) else "FAIL", raw)
        self._delegation_cache[uid] = (time.monotonic() + QUERY_CACHE_SECONDS, state)
        return state

    def _read_verify(self, user: da_users.DAUser | None, config: config_mod.Config | None):
        """Worker-only read/parse stage; never access Textual widgets here."""
        cur = None
        limits = Text("N/A", style="dim")
        checks: list[tuple[str, str | None, str]] = []

        def add(label: str, status: str | None, value: str) -> None:
            checks.append((label, status, value))

        labels = ("drop-in", "cpu.max", "io.max", "controllers", "memory", "delegation", "cpu.stat", "memory.events")
        if user is None or user.uid is None:
            reason = "no Linux account" if user else "no account selected"
            checks = [(label, "N/A", reason) for label in labels]
        else:
            uid = user.uid
            root = paths.user_slice_dir(uid)

            def read(filename: str) -> tuple[str | None, str]:
                try:
                    return (root / filename).read_text(encoding="utf-8").strip(), ""
                except FileNotFoundError:
                    return None, "file missing"
                except (OSError, UnicodeError):
                    return None, "file unreadable"

            dropin = None
            try:
                dropin = self._dropin_for_verify(uid)
                # stat distinguishes a missing drop-in (FAIL) from denied access (N/A).
                dropin.stat()
                enabled = dropin.is_file()
                add("drop-in", "OK" if enabled else "FAIL", str(dropin))
                limits = _status_text("ON" if enabled else "OFF", "bold bright_green" if enabled else "bold bright_red")
            except FileNotFoundError:
                add("drop-in", "FAIL" if dropin is not None else "N/A",
                    f"{dropin} (missing)" if dropin is not None else "path unavailable")
                if dropin is not None:
                    limits = _status_text("OFF", "bold bright_red")
            except subprocess.TimeoutExpired:
                add("drop-in", "N/A", "timeout")
            except (OSError, ValueError):
                add("drop-in", "N/A", "path unavailable")

            cpu_max, reason = read("cpu.max")
            parts = (cpu_max or "").split()
            if cpu_max is None:
                add("cpu.max", "N/A", reason)
            elif len(parts) != 2 or not parts[1].isdigit() or int(parts[1]) <= 0 or (
                parts[0] != "max" and (not parts[0].isdigit() or int(parts[0]) <= 0)
            ):
                add("cpu.max", "N/A", f"invalid value: {cpu_max}")
            else:
                add("cpu.max", "FAIL" if parts[0] == "max" else "OK", cpu_max)

            values = config_mod.resolve(config, user.name, user.package)[1] if config else {}
            io_max, reason = read("io.max")
            device = config.io_device if config else None
            if io_max is None:
                add("io.max", "N/A", reason)
            elif not device:
                add("io.max", "N/A", f"io_device not configured; {io_max or '(empty)'}")
            else:
                try:
                    device_id = paths.block_device_id(device)
                except (OSError, ValueError, AttributeError):
                    add("io.max", "N/A", f"device unavailable: {device}; {io_max or '(empty)'}")
                else:
                    present = any(line.split() and line.split()[0] == device_id for line in io_max.splitlines())
                    add("io.max", "OK" if present else "FAIL", f"{device} ({device_id}); {io_max or '(empty)'}")

            controllers, reason = read("cgroup.controllers")
            add("controllers", "N/A" if controllers is None else
                ("OK" if {"cpu", "io"} <= set(controllers.split()) else "FAIL"),
                reason if controllers is None else controllers or "(empty)")

            memory = []
            memory_states = []
            for filename, key in (("memory.max", "memory_max"), ("memory.high", "memory_high")):
                value, reason = read(filename)
                required = values.get(key) not in (None, config_mod.UNLIMITED)
                memory.append(f"{filename}={value if value is not None else reason}")
                if required:
                    memory_states.append("N/A" if value is None or (value != "max" and not value.isdigit())
                                         else "FAIL" if value == "max" else "OK")
            memory_status = next((status for status in ("FAIL", "N/A") if status in memory_states),
                                 "OK" if memory_states else "N/A")
            if not config:
                memory.append("config unavailable")
            elif not memory_states:
                memory.append("profile unlimited")
            add("memory", memory_status, ", ".join(memory))

            add("delegation", *self._delegation(uid))

            cpu_stat, reason = read("cpu.stat")
            cpu = usage.parse_keyed(cpu_stat)
            if "nr_throttled" not in cpu:
                add("cpu.stat", "N/A", reason or "nr_throttled unavailable")
            else:
                count = cpu["nr_throttled"]
                used = cpu.get("usage_usec", 0)
                previous = self._verify_cpu.get(uid)
                delta = max(count - previous[1], 0) if previous else 0
                active = previous is not None and used > previous[0]
                self._verify_cpu[uid] = (used, count)
                add("cpu.stat", "OK" if delta and active else None,
                    f"nr_throttled={count}, delta={f'+{delta}' if delta else '0'}")

            memory_events, reason = read("memory.events")
            events = usage.parse_keyed(memory_events)
            if not {"high", "oom_kill"} <= events.keys():
                add("memory.events", "N/A", reason or "counters unavailable")
            else:
                baseline = self._verify_oom_start.setdefault(uid, events["oom_kill"])
                delta = max(events["oom_kill"] - baseline, 0)
                add("memory.events", "FAIL" if delta else "OK",
                    f"high={events['high']}, oom_kill={events['oom_kill']}, since start={f'+{delta}' if delta else '0'}")
            try:
                cur = usage.snapshot(uid)
            except (OSError, UnicodeError):
                pass

        colours = {"OK": "bold #55ff55", "FAIL": "bold #ff5555", "N/A": "dim"}
        text = Text("Verify: ")
        for index, status in enumerate(("OK", "FAIL", "N/A")):
            if index:
                text.append(", ")
            text.append(f"{sum(state == status for _, state, _ in checks)} {status}", style=colours[status])
        for label, status, value in checks:
            text.append("\n")
            if status:
                text.append(f"{status} ", style=colours[status])
            text.append(f"{label}: {value}".encode("ascii", "backslashreplace").decode("ascii"))
        return text, cur, limits

    def _status(self, name: str, now: float) -> Text:
        alert = self.alerts.get(name)
        if alert and now - alert[1] < ALERT_SECONDS:
            style = "bold bright_red" if alert[0] == "OOM" else "bold bright_yellow"
            return _status_text(alert[0], style)
        return _status_text("OK", "bold bright_green")

    def _selected(self) -> str | None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.app.push_screen(LimitsScreen(str(event.row_key.value)))

    def action_open_usage(self) -> None:
        name = self._selected()
        if name:
            self.app.push_screen(UsageScreen(name))

    def action_open_logs(self) -> None:
        self.app.push_screen(LogsScreen())

    def action_open_config(self) -> None:
        self.app.push_screen(ConfigScreen())

    def on_screen_resume(self) -> None:
        # Mount already starts discovery. Textual also emits an initial resume.
        if self._initial_resume:
            self._initial_resume = False
            return
        # Returning from the config editor may have changed profiles, so reload.
        self.reload()

    def action_reload(self) -> None:
        self.reload()

    def action_apply_all(self) -> None:
        if not _is_root():
            self.app.notify("Run da-plus tui as root to apply limits.", severity="error")
            return
        self.app.push_screen(ConfirmScreen(f"Apply limits to ALL accounts ({len(self.users)} users)"), self._apply_all_confirmed)

    def _apply_all_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed:
            return
        try:
            config = config_mod.load()
        except ConfigError as exc:
            self.app.notify(str(exc), severity="error")
            return
        results = apply_all(config)
        write_log(results, source="tui")
        failed = [r for r in results if not r.ok]
        if failed:
            self.app.notify(f"{len(failed)} of {len(results)} failed; see Logs", severity="error")
        else:
            self.app.notify(f"applied to {len(results)} user(s)")
        self.reload()


class UiFooterKey(FooterKey):
    """Colour the state word while preserving the standard footer rendering."""

    def render(self) -> Text:
        key = {"ctrl+s": "^s", "escape": "esc", "ctrl+p": "^p"}.get(self.key, self.key_display)
        description = "Stop / disable" if self.key == "s" and self.action == "stop_limits" else self.description
        key_padding = self.get_component_styles("footer-key--key").padding
        description_padding = self.get_component_styles("footer-key--description").padding
        text = Text.assemble(
            (" " * key_padding.left + key + " " * key_padding.right, self.get_component_rich_style("footer-key--key")),
            (" " * description_padding.left + description + " " * description_padding.right,
             self.get_component_rich_style("footer-key--description")),
        )
        text.stylize_before(self.rich_style)
        if self.key in {"ctrl+s", "s"}:
            word, style = ("enable", "bold bright_green") if self.key == "ctrl+s" else ("disable", "bold bright_red")
            text.highlight_words([word], style=style)
        return text


class MessageDock(Static):
    DEFAULT_CSS = """MessageDock { dock: bottom; height: 4; margin-bottom: 1; background: #262626; width: 100%; text-wrap: nowrap; text-overflow: ellipsis; }"""

    def on_mount(self):
        self.refresh_messages()

    def refresh_messages(self):
        entries = list(self.app.message_log)[-4:]
        text = Text("\n" * (4 - len(entries)))
        for index, entry in enumerate(entries):
            line = entry.render()
            line = _status_text(line.plain.replace("\n", " "), line.style)
            text.append_text(line)
            if index < len(entries) - 1:
                text.append("\n")
        self.update(text)


class MessageLogScreen(Screen):
    BINDINGS = [Binding("escape", "back", "Back")]

    def compose(self):
        yield TopBar("Messages")
        with VerticalScroll():
            yield Static("", id="message-history")
        yield UiFooter()

    def on_mount(self):
        self.refresh_messages()

    def refresh_messages(self):
        text = Text()
        for entry in reversed(self.app.message_log):
            text.append_text(entry.render())
            text.append("\n")
        self.query_one("#message-history", Static).update(text)

    def action_back(self):
        self.app.pop_screen()


class UiFooter(Footer):
    async def on_mount(self):
        await self.screen.mount(MessageDock())

    def compose(self):
        if isinstance(self.screen, LimitsScreen):
            yield Static(Text("a Enable  s Disable  d Redis  Tab leave field", style="bold"), classes="footer-hint")
        else:
            for widget in super().compose():
                if isinstance(widget, FooterKey):
                    widget = UiFooterKey(widget.key, widget.key_display, widget.description, widget.action, disabled=widget._disabled, tooltip=widget.tooltip or "", classes=" ".join(widget.classes)).data_bind(compact=Footer.compact)
                yield widget


class ReadOnlyPathInput(Input):
    """Keep Input selection/copy/drag behaviour while blocking all edits."""
    read_only = True

    def check_consume_key(self, key, character):
        return False

    def replace(self, text, start, end):
        pass


class LimitsScreen(Screen):
    REDIS_FIELDS = ("enabled", "maxmemory", "maxmemory_policy", "persistence")
    REDIS_MEMORY_CHOICES = ("64M", "128M", "256M", "512M", "1G", "2G")
    TABS = ("limits", "redis")
    BINDINGS = [
        Binding("left", "previous_tab", "Previous tab", show=False, priority=True),
        Binding("right", "next_tab", "Next tab", show=False, priority=True),
        Binding("1", "show_tab('limits')", "Limits tab", show=False),
        Binding("2", "show_tab('redis')", "Redis tab", show=False),
        Binding("tab", "leave_field", show=False, priority=True),
        Binding("shift+tab", "leave_field_back", show=False, priority=True),
        Binding("ctrl+s", "apply_limits", "Apply / enable"),
        Binding("a", "save", "Enable"),
        Binding("s", "stop_limits", "Disable"),
        Binding("d", "toggle_redis", "Redis enable/disable"),
        Binding("escape", "back", "Back", priority=True),
    ]
    DEFAULT_CSS = """
    LimitsScreen #limits-tabs {
        height: 3;
        width: 100%;
    }
    LimitsScreen #limits-tabs Button {
        width: 20;
    }
    LimitsScreen #redis-editor {
        height: 1fr;
        width: 100%;
        margin: 0;
        display: none;
    }
    LimitsScreen .redis-allowed {
        height: auto;
        color: #bcbcbc;
    }
    LimitsScreen #redis-custom-row {
        display: none;
    }
    LimitsScreen RadioSet {
        layout: horizontal;
        height: 3;
        border: ascii #666666;
        padding: 0 1;
        background: #262626;
        color: #dedede;
    }
    LimitsScreen RadioButton {
        width: 1fr;
        height: 1;
    }
    LimitsScreen RadioSet:focus {
        border: ascii #ffb454;
    }
    LimitsScreen RadioButton.-on > .toggle--button {
        color: #55ff55;
        text-style: bold;
    }
    LimitsScreen #redis-editor SelectCurrent,
    LimitsScreen #redis-editor SelectOverlay {
        border: ascii #666666;
        background: #262626;
        color: #dedede;
    }
    LimitsScreen #redis-enabled-no.-on > .toggle--button,
    LimitsScreen #redis-persistence-off.-on > .toggle--button {
        color: #ff5555;
    }
    LimitsScreen .redis-setting { height: auto; min-height: 3; }
    LimitsScreen .redis-setting > Vertical { height: auto; }
    LimitsScreen #limits-actions, LimitsScreen #redis-actions { height: 3; }
    LimitsScreen #redis-effective-maxmemory { height: auto; }
    LimitsScreen #redis-enable-reason {
        width: 1fr;
        height: 3;
        content-align: left middle;
    }
    LimitsScreen #redis-editor { border: none; padding: 0 1; }
    LimitsScreen #redis-form { height: 1fr; min-height: 0; }
    LimitsScreen #redis-paths {
        width: 100%; height: auto;
        border: none; padding: 0; text-wrap: nowrap; text-overflow: fold;
    }
    LimitsScreen #redis-actions Button {
        width: 1fr; min-width: 0; padding: 0; height: 3; min-height: 3;
    }
    LimitsScreen #redis-enable-reason { height: auto; width: 100%; }
    LimitsScreen ReadOnlyPathInput, LimitsScreen ReadOnlyPathInput:focus {
        height: 1;
        border: none;
        padding: 0;
    }
    LimitsScreen #redis-copy-help { height: auto; }
    LimitsScreen Button:disabled { color: #888888 !important; text-style: dim; }
    .row {
        height: auto;
    }
    .head {
        text-style: bold;
    }
    .key {
        width: 18;
    }
    .cell {
        width: 1fr;
    }
    LimitsScreen #limits-state {
        height: auto;
        text-style: bold;
    }
    LimitsScreen FooterKey,
    LimitsScreen FooterKey > .footer-key--key,
    LimitsScreen FooterKey > .footer-key--description {
        text-style: bold;
    }
    #error {
        color: #ff5555;
        text-style: bold;
        height: auto;
        margin-top: 1;
    }
    LimitsScreen .redis-error {
        height: auto;
        color: #ff5555;
        text-style: bold;
    }
    LimitsScreen #redis-state {
        height: 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    LimitsScreen #redis-heading,
    LimitsScreen #redis-enabled-state, LimitsScreen #redis-how-to-use,
    LimitsScreen #redis-enable-help {
        height: auto;
    }
    """

    def __init__(self, username: str) -> None:
        super().__init__()
        self.username = username
        self.user: da_users.DAUser | None = None
        self.config: config_mod.Config | None = None
        self.raw: dict = {}
        self.base: dict[str, str] = {}
        self.redis_base: dict = {}
        self._redis_state_worker = None
        self._redis_action_worker = None
        self._redis_active = None
        self._redis_deployed = False
        self._active_tab = "limits"
        self._redis_layout = None
        self._redis_setup_error = ""
        self._redis_last_status = ""
        self._redis_form_ready = False
        self._redis_password_text = "******"
        self._redis_password_timer = None

    def action_apply_limits(self):
        self.action_save()

    def check_action(self, action, parameters):
        if action == "apply_limits" and self._active_tab != "limits":
            return False
        if action == "save" and self._active_tab != "limits":
            return False
        if action in {"save", "stop_limits", "toggle_redis"} and isinstance(self.focused, (Input, TextArea, Select, RadioSet)):
            return False
        return True

    def action_leave_field(self):
        if isinstance(self.focused, Input):
            self.query_one("#redis-save" if self._active_tab == "redis" else "#enable-limits", Button).focus()
        else:
            self.focus_next()

    def action_leave_field_back(self):
        if isinstance(self.focused, Input):
            self.query_one("#redis-save" if self._active_tab == "redis" else "#enable-limits", Button).focus()
        else:
            self.focus_previous()

    def on_input_submitted(self, event):
        self.query_one("#redis-save" if self._active_tab == "redis" else "#enable-limits", Button).focus()

    def compose(self) -> ComposeResult:
        yield TopBar("Users")
        with Horizontal(id="limits-tabs"):
            for index, name in enumerate(self.TABS, 1):
                yield Button(Text(f"[{index}] {name.title()}", style="bold reverse" if name == self._active_tab else "bold"),
                             id=f"{name}-tab")
        with Horizontal(id="limits-layout"):
            with _panel("Limits", id="limits-panel", scroll=True):
                yield Static("", id="limits-state")
                yield Static("", id="info")
                with Horizontal(classes="row head"):
                    yield Label("limit", classes="key")
                    yield Static("profile", classes="cell")
                    yield Static("final", classes="cell")
                for key in LIMIT_KEYS:
                    with Horizontal(classes="row setting"):
                        yield Label(key, classes="key")
                        yield Static("", id=f"profile-{key}", classes="cell")
                        yield Static("", id=f"final-{key}", classes="cell")
            with _panel("Overrides", id="overrides-panel", scroll=True):
                yield Static("Override (empty = profile)", id="override-heading")
                for key in LIMIT_KEYS:
                    with Horizontal(classes="row setting"):
                        yield Label(key, classes="key")
                        yield Input(id=f"input-{key}", classes="cell")
        with Horizontal(id="limits-actions"):
            yield Button("Enable (a)", id="enable-limits")
            yield Button("Disable (s)", id="disable-limits")
        # The shared panel helper installs an inline border. This tab uses
        # a borderless container so wrapped paths keep the available height.
        redis_panel = Vertical(id="redis-editor")
        redis_panel.styles.margin = 0
        with redis_panel:
            yield Static(Text("loading...", style="dim"), id="redis-state")
            with VerticalScroll(id="redis-form"):
                yield Static("", id="redis-effective-maxmemory")
                with Horizontal(classes="row head"):
                    yield Label("setting", classes="key")
                    yield Static("profile", classes="cell")
                    yield Static("override (empty = profile)", classes="cell")
                for key in self.REDIS_FIELDS:
                    with Horizontal(id=f"redis-row-{key}", classes="row redis-setting"):
                        yield Label(key, classes="key")
                        yield Static("", id=f"redis-profile-{key}", classes="cell")
                        if key in {"enabled", "persistence"}:
                            options = [("yes", True), ("no", False)] if key == "enabled" else [("off", "off"), ("rdb", "rdb")]
                            yield Select(options, value=False if key == "enabled" else "off", allow_blank=False,
                                         id=f"redis-input-{key}", classes="cell")
                        elif key == "maxmemory":
                            yield Select([("not set", ""), *((value, value) for value in self.REDIS_MEMORY_CHOICES),
                                          ("custom...", "custom")], value="", allow_blank=False,
                                         id=f"redis-input-{key}", classes="cell")
                        else:
                            yield Select([("profile", ""), *((value, value) for value in sorted(config_mod.REDIS_POLICIES))],
                                         value="allkeys-lru", allow_blank=False, id=f"redis-input-{key}", classes="cell")
                    if key == "maxmemory":
                        with Horizontal(id="redis-custom-row", classes="row setting"):
                            yield Label("custom maxmemory", classes="key")
                            yield Static("", classes="cell")
                            yield Input(placeholder="e.g. 300M", id="redis-input-maxmemory-custom", classes="cell")
                        yield Static(Text("Use M or G, e.g. 128M (not MB or GB)", style="dim"),
                                     id="redis-allowed-maxmemory", classes="redis-allowed")
                        yield Static("", id="redis-memory-hint", classes="redis-allowed")
                    yield Static("", id=f"redis-error-{key}", classes="redis-error")
                yield Static("", id="redis-enable-reason")
            with Horizontal(id="redis-actions"):
                yield Button("Save settings", id="redis-save")
                yield Button("Enable Redis (d)", id="enable-redis")
                yield Button("Disable Redis (d)", id="disable-redis")
                yield Button("View password", id="redis-view-password")
            yield Label(Text("socket path: loading...\npassword file: loading...\nRedis password: ******", overflow="fold"), id="redis-paths")
            yield Static(Text("To copy: hold Shift and drag to highlight the text, then copy with Ctrl+Shift+C.", style="dim"), id="redis-copy-help")
        yield Static("", id="error")
        yield UiFooter()

    def action_show_tab(self, name):
        if name not in self.TABS:
            return
        self._active_tab = name
        self.query_one("#limits-layout").display = name == "limits"
        self.query_one("#redis-editor").display = name == "redis"
        self.query_one("#limits-actions").display = name == "limits"
        self.query_one("#error").display = name == "limits"
        for index, tab in enumerate(self.TABS, 1):
            button = self.query_one(f"#{tab}-tab", Button)
            # Content equality compares text only; explicitly replace the style.
            button.set_reactive(Button.label, Content.from_rich_text(Text(
                f"[{index}] {tab.title()}", style="bold reverse" if tab == name else "bold")))
            button.refresh()
        self.query_one(f"#{name}-tab", Button).focus()

    def action_previous_tab(self):
        self.action_show_tab(self.TABS[(self.TABS.index(self._active_tab) - 1) % len(self.TABS)])

    def action_next_tab(self):
        self.action_show_tab(self.TABS[(self.TABS.index(self._active_tab) + 1) % len(self.TABS)])

    def on_button_pressed(self, event: Button.Pressed):
        actions = {"enable-limits": self.action_save, "disable-limits": self.action_stop_limits,
                   "redis-save": self.action_save, "enable-redis": self.action_toggle_redis,
                   "disable-redis": self.action_toggle_redis,
                   "redis-view-password": self.action_view_redis_password}
        if event.button.id in actions:
            event.stop()
            actions[event.button.id]()
        else:
            for tab in self.TABS:
                if event.button.id == f"{tab}-tab":
                    self.action_show_tab(tab)

    def on_mount(self) -> None:
        self.title = f"Limits: {self.username}"
        try:
            self.user = da_users.get_user(self.username)
            self.config = config_mod.load()
            self.raw = config_mod.load_raw()
        except (ConfigError, ValueError) as exc:
            self._error(str(exc))
            return
        if self.user is None:
            self._error(f"{self.username} is not a DirectAdmin user any more")
            return
        self._refresh_limits_state()
        self.set_interval(POLL_SECONDS, self._refresh_limits_state)
        profile, _ = config_mod.resolve(self.config, self.user.name, self.user.package)
        self.base = self.config.profiles[profile]
        self.redis_base = {**config_mod.REDIS_DEFAULTS, **self.config.redis_profiles.get(profile, {})}
        redis_overrides = self.config.users.get(self.username, {}).get("redis", {})
        for key in self.REDIS_FIELDS:
            profile_value = self.redis_base.get(key)
            profile_text = ("yes" if profile_value else "no") if key == "enabled" else str(profile_value or "-")
            self.query_one(f"#redis-profile-{key}", Static).update(Text(profile_text))
            current = redis_overrides.get(key, profile_value)
            if key in {"enabled", "persistence"}:
                self.query_one(f"#redis-input-{key}", Select).value = current
            elif key == "maxmemory":
                value = current or ""
                self.query_one("#redis-input-maxmemory", Select).value = value if not value or value in self.REDIS_MEMORY_CHOICES else "custom"
                self.query_one("#redis-input-maxmemory-custom", Input).value = value if value and value not in self.REDIS_MEMORY_CHOICES else ""
                self.query_one("#redis-custom-row").display = bool(value and value not in self.REDIS_MEMORY_CHOICES)
            else:
                self.query_one(f"#redis-input-{key}", Select).value = current
        self._redis_form_ready = True
        for arrow in self.query("#redis-editor .arrow"):
            arrow.update(Text("v" if arrow.has_class("down-arrow") else "^"))
        self._refresh_redis_memory_hint()
        self._queue_redis_state()
        self.set_interval(POLL_SECONDS, self._queue_redis_state)
        overrides = self.config.users.get(self.username, {}).get("overrides", {})
        uid = "-" if self.user.uid is None else self.user.uid
        self.query_one("#info", Static).update(
            Text(f"profile {profile}   package {self.user.package or '-'}   UID {uid}")
        )
        for key in LIMIT_KEYS:
            self.query_one(f"#profile-{key}", Static).update(Text(self.base[key]))
            inp = self.query_one(f"#input-{key}", Input)
            inp.disabled = self.username == "admin" and key in config_mod.ADMIN_UNLIMITED_KEYS
            inp.placeholder = "not allowed for admin" if inp.disabled else self.base[key]
            inp.value = overrides.get(key, "")
            self._refresh_final(key)

    def _refresh_limits_state(self) -> None:
        if self.user is not None:
            disabled = self.config is not None and self.config.users.get(self.username, {}).get("limits", "on") == "off"
            state = "ENABLED" if not disabled and _limits_present(self.user) else "DISABLED"
            text = Text(f"Limits for {self.username}:\n", style="bold")
            text.append(f"[ {state} ]", style="bold bright_green" if state == "ENABLED" else "bold bright_red")
            if disabled:
                text.append(" limits off (disabled by admin)", style="bold bright_red")
            self.query_one("#limits-state", Static).update(text)

    def _error(self, text: str) -> None:
        self.query_one("#error", Static).update(_status_text(text, "bold bright_red"))

    def _refresh_final(self, key: str) -> None:
        if self.username == "admin" and key in config_mod.ADMIN_UNLIMITED_KEYS:
            for column in ("profile", "final"):
                self.query_one(f"#{column}-{key}", Static).update(Text("not allowed for admin"))
            return
        text = self.query_one(f"#input-{key}", Input).value.strip()
        final = self.query_one(f"#final-{key}", Static)
        try:
            value = normalize_value(key, text, key) if text else self.base[key]
            final.update(Text(value, style="bold" if text else ""))
        except ConfigError:
            final.update(_status_text("invalid", "bold bright_red"))

    def on_input_changed(self, event: Input.Changed) -> None:
        if isinstance(event.input, ReadOnlyPathInput):
            return
        if event.input.id.startswith("redis-input-"):
            if self.config is not None and self.user is not None:
                self._redis_setup_error = ""
                self._redis_raw(self.raw)
                self._refresh_redis_memory_hint()
            return
        self._refresh_final(event.input.id.removeprefix("input-"))

    def on_select_changed(self, event: Select.Changed):
        if event.select.id and event.select.id.startswith("redis-input-"):
            if event.select.id == "redis-input-maxmemory":
                self.query_one("#redis-custom-row").display = event.value == "custom"
            self._redis_form_changed()

    def _redis_form_changed(self):
        if self._redis_form_ready:
            self._redis_setup_error = ""
            self._redis_raw(self.raw)
            self._refresh_redis_memory_hint()

    def _redis_form_value(self, key):
        widget = self.query_one(f"#redis-input-{key}")
        value = widget.value
        if key == "maxmemory" and value == "custom":
            return self.query_one("#redis-input-maxmemory-custom", Input).value.strip()
        saved = self.config.users.get(self.username, {}).get("redis", {})
        if key not in saved and value == self.redis_base.get(key):
            return ""  # Leave untouched inherited values as inherited.
        return value if isinstance(value, (str, bool)) else ""

    def _refresh_redis_memory_hint(self):
        override = self._redis_form_value("maxmemory")
        memory = override or self.redis_base.get("maxmemory")
        source = " (from override)" if override else " (from profile)" if memory else ""
        self.query_one("#redis-effective-maxmemory", Static).update(Text(f"effective maxmemory: {memory or 'not set'}{source}"))
        self.query_one("#redis-memory-hint", Static).update(Text(
            "" if memory else "maxmemory not set: required before enabling Redis", style="dim"))
        active = bool(self._redis_active)
        self.query_one("#enable-redis", Button).disabled = self._redis_deployed or not memory or self._redis_active is None
        self.query_one("#disable-redis", Button).disabled = not self._redis_deployed
        self.query_one("#redis-enable-reason", Static).update(Text("maxmemory is not set" if not memory else "", style="dim"))
        if self._redis_setup_error:
            message, style, level = self._redis_setup_error, "bold bright_red", "ERROR"
        elif active and self._redis_layout:
            message, style, level = f"ENABLED - socket: {self._redis_layout.socket}", "bold bright_green", "OK"
        elif not memory:
            message, style, level = "NOT READY - maxmemory is not set. Set it in the field below, then press Save settings.", "bold bright_red", "ERROR"
        else:
            message, style, level = "READY - press d to enable Redis.", "bold bright_green", "OK"
        self.query_one("#redis-state", Static).update(_status_text(message, style))
        if message != self._redis_last_status:
            self.app.log_message(message, level)
            self._redis_last_status = message

    def _redis_status_error(self, message):
        self._redis_setup_error = str(message)
        self._refresh_redis_memory_hint()

    def _redis_raw(self, raw):
        settings = dict(self.config.users.get(self.username, {}).get("redis", {}))
        invalid = False
        for key in self.REDIS_FIELDS:
            self.query_one(f"#redis-error-{key}", Static).update(Text(""))
            value = self._redis_form_value(key)
            if value == "":
                settings.pop(key, None)
                continue
            try:
                settings.update(config_mod._parse_redis({key: value}, f"users.{self.username}.redis"))
            except ConfigError as exc:
                self._redis_field_error(key, exc)
                invalid = True
        if invalid:
            return None
        # set_overrides supplies the existing deep-copy save path and preserves other account fields.
        candidate = config_mod.set_overrides(raw, self.username, raw.get("users", {}).get(self.username, {}).get("overrides", {}))
        entry = candidate.setdefault("users", {}).setdefault(self.username, {})
        if settings:
            entry["redis"] = settings
        else:
            entry.pop("redis", None)
        if not entry:
            candidate["users"].pop(self.username, None)
        try:
            parsed = config_mod.parse(candidate)
            config_mod.resolve_redis(parsed, self.username, self.user.package)
        except ConfigError as exc:
            field = next((key for key in self.REDIS_FIELDS if f".redis.{key}" in str(exc)), None)
            if field is not None:
                self._redis_field_error(field, exc)
            else:
                self._redis_status_error(str(exc))
            return None
        return candidate

    def _redis_field_error(self, key, error):
        examples = {
            "enabled": "enabled: choose yes or no",
            "maxmemory": "maxmemory: use M or G, for example 128M (not 128MB)",
            "maxmemory_policy": "maxmemory_policy: choose an allowed policy, for example allkeys-lru",
            "persistence": "persistence: use off or rdb, for example off",
        }
        self._redis_status_error(f"{examples[key]}: {error}")

    def _queue_redis_state(self):
        if self.app.screen is not self or self.user is None:
            return
        if self._redis_state_worker is not None and not self._redis_state_worker.is_finished:
            return
        self._redis_state_worker = self.run_worker(self._refresh_redis_state, group="limits-redis-status")

    def _read_redis_state(self):
        layout = redis_user.locations(self.username)
        try:
            state = redis_user.status(self.username, runner=UsersScreen._redis_command)
            return layout, state, state.get("active", False) or layout.unit.exists(), ""
        except (redis_user.RedisUserError, OSError, ValueError, subprocess.SubprocessError) as exc:
            return layout, None, False, str(exc)

    async def _refresh_redis_state(self):
        try:
            layout, state, deployed, error = await asyncio.to_thread(self._read_redis_state)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            if self.is_mounted:
                self.query_one("#redis-state", Static).update(Text(f"N/A: {exc}", style="dim"))

            return
        finally:
            self._redis_state_worker = None
        if not self.is_mounted:
            return
        self._redis_active = bool(state.get("active")) if state is not None else None
        self._redis_deployed = deployed
        self._redis_layout = layout
        self._refresh_redis_memory_hint()
        self._refresh_redis_paths()

    def _refresh_redis_paths(self):
        layout = self._redis_layout or redis_user.locations(self.username)
        self.query_one("#redis-paths", Label).update(Text(
            f"socket path: {layout.socket}\npassword file: {layout.connection}\n"
            f"Redis password: {self._redis_password_text}", overflow="fold"))

    def _hide_redis_password(self):
        self._redis_password_text = "******"
        self._redis_password_timer = None
        if self.is_mounted:
            self._refresh_redis_paths()

    def on_unmount(self):
        if self._redis_password_timer is not None:
            self._redis_password_timer.stop()
            self._redis_password_timer = None
        self._redis_password_text = "******"

    def action_view_redis_password(self):
        if self.user is None:
            return
        layout = self._redis_layout or redis_user.locations(self.username)
        try:
            password = redis_user._read_connection(self.user, layout).get("REDIS_PASSWORD")
        except (redis_user.RedisUserError, OSError, ValueError):
            password = None
        if self._redis_password_timer is not None:
            self._redis_password_timer.stop()
        self._redis_password_text = password or "not set (enable Redis first)"
        self._refresh_redis_paths()
        self._redis_password_timer = self.set_timer(60, self._hide_redis_password)

    def action_toggle_redis(self):
        if not _is_root():
            self._redis_status_error("Run da-plus tui as root to change Redis.")
            return
        if self._redis_active is None:
            self._redis_status_error("Redis state is loading or unavailable; wait for refresh.")
            return
        if self._redis_action_worker is not None and not self._redis_action_worker.is_finished:
            self._redis_status_error("Redis action already in progress.")
            return
        action = "disable" if self._redis_deployed else "enable"
        if action == "enable" and not (self._redis_form_value("maxmemory") or self.redis_base.get("maxmemory")):
            self._redis_setup_error = ""
            self._refresh_redis_memory_hint()
            return
        self._redis_action_worker = self.run_worker(self._preview_redis_action(action), group="limits-redis-action")

    async def _preview_redis_action(self, action):
        try:
            lines = await asyncio.to_thread(redis_user.preview, action, self.username, runner=UsersScreen._redis_command)
        except (redis_user.RedisUserError, OSError, ValueError, subprocess.SubprocessError) as exc:
            self._redis_status_error(str(exc))
            return
        finally:
            self._redis_action_worker = None
        if not self.is_mounted:
            return
        self.app.push_screen(ConfirmScreen(f"Redis {action} for {self.username}?", "\n".join(lines)),
                             lambda confirmed: self._redis_action_confirmed(confirmed, action))

    def _redis_action_confirmed(self, confirmed, action):
        if confirmed:
            self._redis_action_worker = self.run_worker(self._change_redis_action(action), group="limits-redis-action")

    async def _change_redis_action(self, action):
        try:
            function = redis_user.enable if action == "enable" else redis_user.disable
            await asyncio.to_thread(function, self.username, runner=UsersScreen._redis_command)
        except (redis_user.RedisUserError, OSError, ValueError, subprocess.SubprocessError) as exc:
            self._redis_status_error(str(exc))
        else:
            self._redis_setup_error = ""
            self.app.log_message(f"Redis {action} completed for {self.username}.", "OK")
        finally:
            self._redis_action_worker = None
            if self.is_mounted:
                self._queue_redis_state()

    def _collect(self) -> dict[str, str] | None:
        overrides: dict[str, str] = {}
        for key in LIMIT_KEYS:
            if self.username == "admin" and key in config_mod.ADMIN_UNLIMITED_KEYS:
                continue
            text = self.query_one(f"#input-{key}", Input).value.strip()
            if not text:
                continue
            try:
                overrides[key] = normalize_value(key, text, key)
            except ConfigError as exc:
                self._error(str(exc))
                return None
        try:
            config_mod.check_memory({**self.base, **overrides}, self.username)
        except ConfigError as exc:
            self._error(str(exc))
            return None
        self._error("")
        return overrides

    def action_save(self) -> None:
        if self.user is None or self.config is None:
            return
        if self.user.uid is None:
            self._error("this user has no Linux account, so limits cannot be applied")
            return
        if not _is_root():
            self._error("run da-plus tui as root to save and apply changes")
            return
        if self._active_tab == "redis":
            new_raw = self._redis_raw(self.raw)
            if new_raw is None:
                return
            diff = "".join(difflib.unified_diff(
                yaml.safe_dump(self.raw, sort_keys=False).splitlines(keepends=True),
                yaml.safe_dump(new_raw, sort_keys=False).splitlines(keepends=True),
                "config.yml/current", "config.yml/new"))
            if not diff:
                self.app.log_message("no changes to save", "INFO")
                return
            self.app.push_screen(ConfirmScreen(f"Save Redis settings for {self.username}?", diff),
                                 lambda confirmed: self._commit_redis_settings(new_raw, confirmed))
            return
        overrides = self._collect()
        if overrides is None:
            return
        saved_overrides = self.config.users.get(self.username, {}).get("overrides", {})
        new_raw = (self.raw if overrides == saved_overrides else
                   config_mod.set_overrides(self.raw, self.username, overrides))
        was_off = self.config.users.get(self.username, {}).get("limits", "on") == "off"
        new_raw = config_mod.set_limits(new_raw, self.username, "on")
        try:
            new_config = config_mod.parse(new_raw)
        except ConfigError as exc:
            self._error(str(exc))
            return
        _, values = config_mod.resolve(new_config, self.username, self.user.package)
        new_text = render_dropin(self.username, self.user.uid, values, new_config.io_device)
        path = paths.dropin_path(self.user.uid)
        old_text = path.read_text(encoding="utf-8") if path.exists() else ""
        diff = "".join(
            difflib.unified_diff(
                old_text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                "current",
                "new",
            )
        )
        title = (f"Enable limits for {self.username} (no config change)" if new_raw == self.raw
                 else f"Save and apply limits for {self.username}?")
        flag_change = f"users.{self.username}.limits: on (remove off flag)\n" if was_off else ""
        self.app.push_screen(ConfirmScreen(title, flag_change + new_text + "\n" + diff),
                             lambda confirmed: self._commit(new_raw, confirmed))

    def action_stop_limits(self) -> None:
        if not hasattr(os, "geteuid") or not _is_root():
            self._error("run da-plus tui as root to stop limits")
            return
        if self.user is None or self.user.uid is None:
            self._error("this user has no Linux account, so limits cannot be stopped")
            return
        try:
            plan = stop_limits_plan(self.user, config_mod.load())
            current_raw = config_mod.load_raw()
            new_raw = config_mod.set_limits(current_raw, self.username, "off")
        except (ApplyError, ConfigError) as exc:
            self._error(str(exc))
            return
        removal = (f"Remove {plan.path} (first line contains 'Generated by da-plus')." if plan.owned
                   else f"Keep {plan.path} ({'foreign drop-in' if plan.content is not None else 'not present'}).")
        body = "\n".join([
            removal,
            "systemctl daemon-reload",
            shlex.join(plan.argv),
            f"users.{self.username}.limits: off",
            "Config overrides are kept. Limits stay off across apply, timer, hooks and reboot until enabled.",
            "No running service or user manager is stopped.",
        ])
        self.app.push_screen(ConfirmScreen(f"Stop limits for {self.username} (UID {plan.uid})?", body),
                             lambda confirmed: self._stop_confirmed(plan, confirmed, current_raw, new_raw))

    def _stop_confirmed(self, plan, confirmed: bool | None, current_raw, new_raw) -> None:
        if not confirmed:
            return
        if not hasattr(os, "geteuid") or not _is_root():
            self._error("run da-plus tui as root to stop limits")
            return
        current = da_users.get_user(self.username)
        if current is None or current.uid != plan.uid:
            self._error("the user's Linux account changed since confirmation; nothing changed")
            return
        try:
            if current_raw is not None and config_mod.load_raw() != current_raw:
                self._error("config changed since confirmation; nothing changed")
                return
            config_mod.save_raw(new_raw)
            self.raw = new_raw
            self.config = config_mod.parse(new_raw)
        except (ConfigError, OSError) as exc:
            self._error(f"could not save limits off: {exc}")
            return
        result = stop_limits(plan)
        self._refresh_limits_state()
        write_log([result], source="tui-stop")
        if result.ok:
            self._error("")
            self.app.notify(result.message)
        else:
            self._error(result.message)

    def _commit(self, new_raw: dict, confirmed: bool | None) -> None:
        if not confirmed or self.user is None:
            return
        try:
            if new_raw != self.raw:
                config_mod.save_raw(new_raw)
        except (ConfigError, OSError) as exc:
            self._error(f"could not save config: {exc}")
            return
        self.raw = new_raw
        self.config = config_mod.parse(new_raw)
        result = apply_user(self.user, self.config)
        write_log([result], source="tui")
        if result.ok:
            self._refresh_limits_state()
            self.app.log_message(f"limits applied to {self.username}: drop-in written", "OK")
            self._error("")
        else:
            self.app.log_message(result.message, "ERROR")
            # The config is saved, so the reconciler timer retries this later.
            self._error(f"saved, but apply failed: {result.message} (the reconcile timer will retry)")

    def _commit_redis_settings(self, new_raw, confirmed):
        if not confirmed:
            return
        try:
            config_mod.save_raw(new_raw)
        except (ConfigError, OSError) as exc:
            self._error(f"could not save config: {exc}")
            return
        self.raw = new_raw
        self.config = config_mod.parse(new_raw)
        profile, _ = config_mod.resolve(self.config, self.username, self.user.package)
        self.redis_base = {**config_mod.REDIS_DEFAULTS, **self.config.redis_profiles.get(profile, {})}
        self._redis_setup_error = ""
        self._refresh_redis_memory_hint()
        self.app.log_message("saved Redis settings", "OK")

    def action_back(self) -> None:
        if isinstance(self.focused, Input):
            self.query_one("#redis-save" if self._active_tab == "redis" else "#enable-limits", Button).focus()
            return
        self.app.pop_screen()


class ConfigScreen(Screen):
    """Edit the whole config file as text. Checked before saving; comments are kept."""

    BINDINGS = [
        Binding("ctrl+s", "save", "Save"),
        Binding("ctrl+t", "check", "Check"),
        Binding("escape", "back", "Back"),
    ]
    DEFAULT_CSS = """
    #editor {
        height: 1fr;
    }
    #status {
        height: auto;
        padding: 0 1;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.path = paths.config_file()
        self.original = ""
        self.exists = False

    def compose(self) -> ComposeResult:
        yield TopBar("Config")
        with _panel("Config", id="config-panel"):
            yield TextArea(id="editor", show_line_numbers=True)
            yield Static("", id="status")
        yield UiFooter()

    def on_mount(self) -> None:
        self.title = "Config"
        self.sub_title = str(self.path)
        self.exists = self.path.is_file()
        self.original = config_mod.load_text(self.path) if self.exists else config_mod.STARTER_TEXT
        self.query_one(TextArea).load_text(self.original)
        if not self.exists:
            self._status(f"{self.path} does not exist yet; saving will create it", "bright_yellow")
        if not _is_root():
            self._status("not running as root: you can read, but saving will fail", "bright_yellow")

    def _status(self, text: str, style: str = "") -> None:
        self.query_one("#status", Static).update(_status_text(text, f"bold {style or 'bright_white'}"))

    def _text(self) -> str:
        return self.query_one(TextArea).text

    def action_check(self) -> None:
        try:
            config_mod.validate_text(self._text())
        except ConfigError as exc:
            self._status(str(exc), "bright_red")
            return
        self._status("config is valid", "bright_green")

    def action_save(self) -> None:
        if not _is_root():
            self._status("run da-plus tui as root to save", "bright_red")
            return
        text = self._text()
        try:
            config_mod.validate_text(text)
        except ConfigError as exc:
            self._status(f"not saved: {exc}", "bright_red")
            return
        if self.exists and text == self.original:
            self.app.notify("no changes to save")
            return
        diff = "".join(
            difflib.unified_diff(
                self.original.splitlines(keepends=True),
                text.splitlines(keepends=True),
                "current",
                "new",
            )
        )
        self.app.push_screen(
            ConfirmScreen(f"Write {self.path}?", diff),
            lambda confirmed: self._commit(text, confirmed),
        )

    def _commit(self, text: str, confirmed: bool | None) -> None:
        if not confirmed:
            return
        try:
            config_mod.save_text(text, self.path)
        except (ConfigError, OSError) as exc:
            self._status(f"could not save: {exc}", "bright_red")
            return
        self.original = text
        self.exists = True
        self._status("saved. Apply it from the Users screen with a.", "bright_green")

    def action_back(self) -> None:
        if self._text() == self.original:
            self.app.pop_screen()
            return
        self.app.push_screen(
            ConfirmScreen("Discard unsaved changes?"),
            lambda discard: self.app.pop_screen() if discard else None,
        )


class UsageScreen(Screen):
    BINDINGS = [Binding("escape", "back", "Back")]
    DEFAULT_CSS = """
    #stats {
        height: auto;
        padding: 1 2;
    }
    .label {
        padding: 0 2;
        text-style: bold;
    }
    #memory, #write {
        margin: 0 2 1 2;
        height: 2;
    }
    """

    def __init__(self, username: str) -> None:
        super().__init__()
        self.username = username
        self.uid: int | None = None
        self.previous: usage.Usage | None = None
        self.memory_history: deque[int] = deque(maxlen=HISTORY)
        self.write_history: deque[int] = deque(maxlen=HISTORY)
        self._usage_worker = None
        self._first_load_complete = False

    def compose(self) -> ComposeResult:
        yield TopBar("Usage")
        with _panel("Usage", id="usage-panel", scroll=True):
            yield Static("loading...", id="stats")
            yield Static(Text("memory: loading..."), id="memory")
            yield Static(Text("write rate: loading..."), id="write")
        yield UiFooter()

    def on_mount(self) -> None:
        self.title = f"Usage: {self.username}"
        self.tick()
        self.set_interval(POLL_SECONDS, self.tick)
        self.set_timer(5, self._startup_guard)

    def _startup_guard(self) -> None:
        if not self._first_load_complete:
            self.query_one("#stats", Static).update(Text("loading system state...", style="dim"))

    def tick(self) -> None:
        if self.app.screen is not self:
            return
        if self._usage_worker is not None and not self._usage_worker.is_finished:
            return
        self._usage_worker = self.run_worker(self._refresh_usage, group="usage")

    @staticmethod
    def _read_usage(username: str):
        user = da_users.get_user(username)
        if user is None or user.uid is None:
            return None, None
        return user.uid, usage.snapshot(user.uid)

    @staticmethod
    def _ascii_history(label: str, value: str, history) -> Text:
        values = list(history)
        peak = max(values, default=0)
        glyphs = "_.-=#"
        bars = "".join(glyphs[min(len(glyphs) - 1, max(0, int(sample * (len(glyphs) - 1) / peak)))]
                       if peak else glyphs[0] for sample in values)
        return Text(f"{label}: {value}\n{bars or '(no samples)'}")

    async def _refresh_usage(self) -> None:
        try:
            uid, cur = await asyncio.to_thread(self._read_usage, self.username)
        except (OSError, UnicodeError):
            uid, cur = None, None
            reason = "N/A: system state unreadable"
        else:
            reason = "N/A: no Linux account for this user, so there is no cgroup to read"
        self._usage_worker = None
        if not self.is_mounted:
            return
        self.uid = uid
        self._first_load_complete = True
        stats = self.query_one("#stats", Static)
        if cur is None:
            stats.update(Text(reason, style="dim"))
            return
        rate = usage.rates(self.previous, cur) if self.previous else None
        label = usage.status_label(self.previous, cur)
        self.previous = cur
        self.memory_history.append(cur.memory_current or 0)
        self.write_history.append(int(rate.write_bps) if rate else 0)
        self.query_one("#memory", Static).update(self._ascii_history(
            "memory", usage.human(cur.memory_current), self.memory_history))
        self.query_one("#write", Static).update(self._ascii_history(
            "write rate", usage.human(rate.write_bps, "B/s") if rate else "-", self.write_history))

        lines = [
            f"memory        {usage.human(cur.memory_current)}",
            f"tasks         {cur.pids_current if cur.pids_current is not None else '-'}",
            f"cpu           {rate.cpu_percent:.1f}% of one core" if rate else "cpu           -",
            f"read          {usage.human(rate.read_bps, 'B/s')}  {rate.read_iops:.0f} IOPS" if rate else "read          -",
            f"write         {usage.human(rate.write_bps, 'B/s')}  {rate.write_iops:.0f} IOPS" if rate else "write         -",
            "",
            "events since start:",
            f"  high {cur.events.get('high', 0)}   max {cur.events.get('max', 0)}   "
            f"oom {cur.events.get('oom', 0)}   oom_kill {cur.events.get('oom_kill', 0)}",
            f"last change   {label or 'none'}",
        ]
        stats.update(Text("\n".join(lines)))

    def action_back(self) -> None:
        self.app.pop_screen()


class LogsScreen(Screen):
    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("escape", "back", "Back"),
    ]

    def compose(self) -> ComposeResult:
        yield TopBar("Logs")
        with _panel("Logs", id="logs-panel", scroll=True):
            yield Static("", id="log")
        yield UiFooter()

    def on_mount(self) -> None:
        self.title = "Logs"
        self.action_refresh()

    def action_refresh(self) -> None:
        parts = []
        for title, path in (("apply", paths.apply_log()), ("hook", paths.hook_log())):
            parts.append(f"== {title} log: {path} ==")
            parts.append(_tail(path))
            parts.append("")
        self.query_one("#log", Static).update(Text("\n".join(parts), style="bold bright_white"))

    def action_back(self) -> None:
        self.app.pop_screen()
