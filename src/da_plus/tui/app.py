"""Textual application for da-plus. Start it with `da-plus tui`."""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass
from datetime import datetime

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.app import App
from textual.content import Content
from textual.filter import LineFilter
from textual.notifications import Notify
from textual.message import Message
from textual.binding import Binding

from .screens import UsersScreen, MessageDock, MessageLogScreen


class PlainFilter(LineFilter):
    """Keep only basic text attributes, without foreground/background SGR codes."""

    def apply(self, segments, background):
        return [Segment(text, Style(bold=style.bold, reverse=style.reverse) if style else None, control)
                for text, style, control in segments]


class AsciiChromeFilter(LineFilter):
    """Use ASCII thumb glyphs for scrollbars, preserving their mouse metadata."""

    def apply(self, segments, background):
        result = []
        for text, style, control in segments:
            if style and style.meta.get("@mouse.down") in {"grab", "scroll_up", "scroll_down", "scroll_left", "scroll_right"}:
                text = "".join("#" if "\u2580" <= character <= "\u259f" else character for character in text)
            result.append(Segment(text, style, control))
        return result


@dataclass
class LogEntry:
    time: str
    level: str
    text: str
    count: int = 1

    def render(self):
        suffix = f" x{self.count}" if self.count > 1 else ""
        style = {"OK": "bold bright_green", "WARN": "bold yellow", "ERROR": "bold bright_red", "INFO": "grey70"}[self.level]
        return Text(f"{self.time} {self.level} {self.text}{suffix}", style=style)


class LogMessage(Message):
    def __init__(self, text, level):
        super().__init__()
        self.text, self.level = text, level


class DaPlusApp(App):
    BINDINGS = [Binding("m", "message_log", "Messages"), Binding("x", "clear_message_log", "Clear messages")]
    TITLE = "da-plus"
    SUB_TITLE = "per-user resource limits"
    NOTIFICATION_DEDUP_SECONDS = 5
    MAX_VISIBLE_NOTIFICATIONS = 3
    CSS = """
    $background: #1e1e1e;
    $surface: #1e1e1e;
    $panel: #262626;
    $foreground: #dedede;
    $accent: #ffb454;
    $error: #ff5555;
    $success: #55ff55;
    $warning: #ffb454;

    Screen {
        background: #1e1e1e;
        color: #dedede;
    }
    TopBar {
        dock: top;
        layout: horizontal;
        height: 3;
        padding: 0 1;
        background: #262626;
        color: #dedede;
        align-vertical: middle;
        border-bottom: ascii #666666;
    }
    TopBar > HeaderTitle, TopBar > HeaderIcon, TopBar > HeaderClockSpace {
        display: none;
    }
    TopBar .app-name {
        width: 10;
        height: 1;
        color: #ffb454;
        text-style: bold;
    }
    TopBar .top-tabs {
        width: auto;
        height: 1;
    }
    TopBar .tab {
        width: 9;
        height: 1;
        margin-right: 1;
        content-align: center middle;
        text-style: bold;
    }
    TopBar .tab.active {
        color: #245a96;
        background: #dedede;
        text-style: bold reverse;
    }
    TopBar .config-path {
        width: 1fr;
        height: 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
        content-align: right middle;
        text-style: dim;
    }
    .panel {
        height: 1fr;
        border: ascii #666666;
        border-title-color: #ffb454;
        border-title-style: bold;
        background: #1e1e1e;
        color: #dedede;
        margin: 0 1;
        padding: 0 1;
    }
    .panel:focus-within {
        border: ascii #ffb454;
    }
    #limits-layout {
        height: 1fr;
    }
    #limits-panel {
        width: 55%;
        margin-right: 0;
    }
    #overrides-panel {
        width: 1fr;
    }
    LimitsScreen .setting {
        height: 3;
        align-vertical: middle;
    }
    LimitsScreen .setting Static, LimitsScreen .setting Label {
        height: 3;
        content-align: left middle;
    }
    #override-heading {
        height: 4;
        text-style: bold;
    }
    #info {
        height: 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    #limits-state {
        height: 2;
        text-style: bold;
    }
    Footer, FooterKey, FooterLabel {
        background: #262626;
        color: #dedede;
        text-style: bold;
    }
    FooterKey .footer-key--key {
        background: #262626;
        color: #ffb454;
        text-style: bold;
    }
    FooterKey .footer-key--description {
        background: #262626;
        color: #dedede;
        text-style: bold;
    }
    FooterKey.-command-palette {
        border-left: ascii #666666;
    }
    .footer-hint {
        width: auto;
        height: 1;
        background: #262626;
    }
    DataTable {
        height: 1fr;
        background: #1e1e1e;
        color: #dedede;
        background-tint: transparent;
    }
    DataTable > .datatable--even-row {
        background: #1e1e1e;
        color: #dedede;
    }
    DataTable > .datatable--odd-row {
        background: #242424;
        color: #dedede;
    }
    DataTable > .datatable--header, DataTable > .datatable--fixed {
        background: #262626;
        color: #ffb454;
        text-style: bold;
    }
    DataTable > .datatable--cursor, DataTable > .datatable--fixed-cursor,
    DataTable > .datatable--header-cursor, DataTable > .datatable--hover,
    DataTable > .datatable--header-hover {
        background: #dedede;
        color: #245a96;
        text-style: bold reverse;
    }
    Button {
        background: #262626 !important;
        color: #dedede !important;
        border: ascii #666666;
        text-style: bold;
        text-opacity: 1;
        tint: transparent;
    }
    Button:hover, Button:focus, Button.-active {
        background: #dedede !important;
        color: #245a96 !important;
        border: ascii #ffb454;
        text-style: bold reverse;
        tint: transparent;
    }
    Input, Input:focus, TextArea {
        border: ascii #666666;
        background: #262626 !important;
        color: #dedede !important;
        background-tint: transparent;
    }
    Input:focus, TextArea:focus {
        border: ascii #ffb454;
    }
    Input > .input--placeholder, Input > .input--suggestion,
    TextArea > .text-area--placeholder, TextArea > .text-area--suggestion,
    TextArea > .text-area--gutter {
        background: #262626;
        color: #bcbcbc;
    }
    Input > .input--cursor, Input > .input--selection,
    TextArea > .text-area--cursor, TextArea > .text-area--selection,
    TextArea > .text-area--cursor-gutter, TextArea > .text-area--matching-bracket {
        background: #dedede;
        color: #245a96;
        text-style: bold reverse;
    }
    TextArea > .text-area--cursor-line {
        background: #242424;
        color: #dedede;
    }
    ConfirmScreen #dialog {
        width: 80%;
        height: 80%;
        max-height: 90%;
        background: #1e1e1e;
        color: #dedede;
    }
    ConfirmScreen #body {
        background: #1e1e1e;
        color: #dedede;
    }
    #message, #error, #status, #log, Toast {
        background: #1e1e1e;
        color: #dedede;
        text-style: bold;
        height: auto;
    }
    Toast.-information {
        color: #55ff55;
        border-left: ascii #55ff55;
        background: #262626;
    }
    Toast.-error {
        color: #ff5555;
        border-left: ascii #ff5555;
        background: #262626;
    }
    Toast.-warning {
        color: #ffb454;
        border-left: ascii #ffb454;
        background: #262626;
    }
    Toast.-information .toast--title { color: #55ff55; }
    Toast.-error .toast--title { color: #ff5555; }
    Toast.-warning .toast--title { color: #ffb454; }
    """

    def __init__(self, *, plain: bool = False, **kwargs) -> None:
        term = os.environ.get("TERM", "").lower()
        self.plain = plain or os.environ.get("DA_PLUS_TUI_PLAIN") == "1" or term in {"linux", "vt100", "dumb"}
        self._plain_filter = PlainFilter()
        self._chrome_filter = AsciiChromeFilter()
        self.message_log = deque(maxlen=200)
        super().__init__(**kwargs)
        self.no_color = self.no_color or self.plain

    def get_line_filters(self):
        filters = [*super().get_line_filters(), self._chrome_filter]
        return [*filters, self._plain_filter] if self.plain else filters

    def notify(self, message: str, *, title: str = "", severity="information",
               timeout: float | None = None, markup: bool = False) -> None:
        self.log_message(message, {"error": "ERROR", "warning": "WARN", "information": "OK"}.get(severity, "INFO"))

    def log_message(self, message, level="INFO"):
        text = Text.from_ansi(str(message)).plain.encode("ascii", "backslashreplace").decode("ascii")
        self.post_message(LogMessage(text, level))

    def on_log_message(self, event):
        if self.message_log and self.message_log[-1].text == event.text:
            self.message_log[-1].count += 1
        else:
            self.message_log.append(LogEntry(datetime.now().strftime("%H:%M:%S"), event.level, event.text))
        self._refresh_message_log()

    def _on_notify(self, event: Notify):
        event.prevent_default()
        self.notify(event.notification.message, severity=event.notification.severity)

    def _refresh_message_log(self):
        for screen in self.screen_stack:
            for dock in screen.query(MessageDock):
                dock.refresh_messages()
            if isinstance(screen, MessageLogScreen):
                screen.refresh_messages()

    def action_message_log(self):
        if not isinstance(self.screen, MessageLogScreen):
            self.push_screen(MessageLogScreen())

    def action_clear_message_log(self):
        self.message_log.clear()
        self._refresh_message_log()

    def format_title(self, title: str, sub_title: str) -> Content:
        title_content = Content(title.encode("ascii", "backslashreplace").decode("ascii"))
        sub_title_content = Content(sub_title.encode("ascii", "backslashreplace").decode("ascii"))
        if sub_title_content:
            return Content.assemble(title_content, " - ", sub_title_content)
        return title_content

    def on_mount(self) -> None:
        self.push_screen(UsersScreen())
