"""Terminal UI channel — Textual-based rich TUI."""

from __future__ import annotations

import logging

import anyio
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.suggester import SuggestFromList
from textual.widgets import Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from ..models import IncomingMessage, MessageType, OutgoingMessage
from .base import BaseChannel

log = logging.getLogger(__name__)

_COMMANDS = ["/list", "/new-project", "/switch", "/quit", "/exit"]


class AgentBoxApp(App):
    """Textual app: RichLog for output, Input for prompt, OptionList for slash commands."""

    TITLE = "agent-box"
    CSS = """
    #log { height: 1fr; }
    #status { height: 1; dock: bottom; color: $text-muted; }
    #cmd-list { height: auto; max-height: 8; display: none; dock: bottom; }
    #cmd-list.visible { display: block; }
    #prompt-bar { dock: bottom; height: 3; layout: horizontal; border: hkey $accent; }
    #prompt-icon { width: 2; content-align-vertical: middle; color: $accent; }
    #prompt { border: none; width: 1fr; }
    """
    BINDINGS = [Binding("ctrl+c", "quit", "Quit")]

    def __init__(self, send_stream: anyio.abc.ObjectSendStream[IncomingMessage]) -> None:
        super().__init__()
        self._send_stream = send_stream

    def compose(self) -> ComposeResult:
        yield RichLog(id="log", markup=True, wrap=True, auto_scroll=True)
        yield Static("", id="status")
        yield OptionList(id="cmd-list")
        with Horizontal(id="prompt-bar"):
            yield Static("❯", id="prompt-icon")
            yield Input(
                id="prompt",
                placeholder="Type a message or / for commands",
                suggester=SuggestFromList(_COMMANDS, case_sensitive=False),
            )

    def on_mount(self) -> None:
        self.query_one("#log", RichLog).write("[bold]agent-box[/] — type a message or / for commands")
        self.query_one("#prompt", Input).focus()

    # --- slash command picker ---

    @on(Input.Changed, "#prompt")
    def _on_input_changed(self, event: Input.Changed) -> None:
        text = event.value
        cmd_list = self.query_one("#cmd-list", OptionList)
        if text.startswith("/") and not text.endswith(" "):
            matches = [c for c in _COMMANDS if c.startswith(text)]
            cmd_list.clear_options()
            if matches:
                cmd_list.add_options([Option(c) for c in matches])
                cmd_list.add_class("visible")
                return
        cmd_list.remove_class("visible")

    @on(OptionList.OptionSelected, "#cmd-list")
    def _on_cmd_selected(self, event: OptionList.OptionSelected) -> None:
        prompt = self.query_one("#prompt", Input)
        selected = str(event.option.prompt)
        prompt.value = selected + " "
        prompt.cursor_position = len(prompt.value)
        prompt.focus()
        self.query_one("#cmd-list", OptionList).remove_class("visible")

    # --- submit ---

    @on(Input.Submitted, "#prompt")
    def _on_submit(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.clear()
        self.query_one("#cmd-list", OptionList).remove_class("visible")

        if text in ("/quit", "/exit"):
            self.exit()
            return

        rich_log = self.query_one("#log", RichLog)
        rich_log.write(f"[bold green]❯[/] {text}")
        self._send_message(text)

    @work()
    async def _send_message(self, text: str) -> None:
        await self._send_stream.send(
            IncomingMessage(text=text, user_id="local", channel="tui"),
        )

    # --- receive outgoing messages ---

    def write_outgoing(self, msg: OutgoingMessage) -> None:
        """Thread-safe: schedule a write on the Textual event loop."""
        self.call_from_thread(self._render_outgoing, msg)

    def post_outgoing(self, msg: OutgoingMessage) -> None:
        """Async-safe: schedule a write from within the event loop."""
        self.call_later(self._render_outgoing, msg)

    def _render_outgoing(self, msg: OutgoingMessage) -> None:
        rich_log = self.query_one("#log", RichLog)
        status = self.query_one("#status", Static)

        if msg.type == MessageType.thinking:
            status.update("[dim magenta]💭 Thinking...[/]")
            return
        if msg.type == MessageType.tool_use:
            name = msg.data.get("name", msg.text) if msg.data else msg.text
            status.update(f"[bold yellow]🔧 {name}...[/]")
            return
        if msg.type == MessageType.tool_result:
            return
        if msg.type == MessageType.system:
            status.update(f"[dim]⚙ {msg.text}...[/]")
            return

        # text or result — clear status and write to log
        status.update("")
        if msg.type == MessageType.result:
            cost = msg.data.get("cost") if msg.data else None
            dur = msg.data.get("duration_ms") if msg.data else None
            meta = f" (${cost:.4f}, {dur}ms)" if cost is not None else ""
            rich_log.write(f"[bold green]✔ Done{meta}[/]")
        else:
            rich_log.write(f"[bold blue]◀[/] {msg.text}")


class TuiChannel(BaseChannel):
    """Textual-based TUI channel."""

    def __init__(self, send_stream: anyio.abc.ObjectSendStream[IncomingMessage]) -> None:
        super().__init__(send_stream)
        self._app = AgentBoxApp(send_stream)

    async def start(self) -> None:
        try:
            await self._app.run_async()
        finally:
            await self.send_stream.aclose()

    async def send_reply(self, msg: OutgoingMessage) -> None:
        if self._app.is_running:
            self._app.post_outgoing(msg)
