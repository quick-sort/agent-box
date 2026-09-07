"""Generic Agent Client Protocol driver backed by a persistent subprocess."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from acp.client.connection import ClientSideConnection
from acp.schema import (
    AgentMessageChunk,
    AllowedOutcome,
    ClientCapabilities,
    DeniedOutcome,
    Implementation,
    PermissionOption,
    RequestPermissionResponse,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    UsageUpdate,
)

from ..config import settings
from ..models import MessageType, OutgoingMessage, ProjectInfo
from .base import BaseAgent
from .delivery import SEND_FILE_INSTRUCTION, parse_send_file_markers

log = logging.getLogger(__name__)

try:
    _AGENT_BOX_VERSION = version("agent-box")
except PackageNotFoundError:
    _AGENT_BOX_VERSION = "unknown"

_TURN_DONE = object()
_PERMISSION_PAUSE = object()

_APPROVE_WORDS = {
    "yes", "y", "ok", "okay", "approve", "allow", "同意", "允许", "可以", "好的", "是", "行",
}

@dataclass(frozen=True, slots=True)
class ACPProvider:
    """Launch policy for one ACP-compatible agent implementation.

    Protocol, session, and event handling stay in :class:`ACPAgent`; provider
    wrappers only describe how to launch their ACP server.
    """

    name: str
    command: tuple[str, ...]
    model_flag: str | None = None
    instructions: str = SEND_FILE_INSTRUCTION

    def command_for(self, project: ProjectInfo) -> tuple[str, ...]:
        if self.model_flag and project.model:
            return (*self.command, self.model_flag, project.model)
        return self.command


@dataclass(slots=True)
class _PendingPermission:
    future: asyncio.Future[RequestPermissionResponse]
    options: list[PermissionOption]
    user_id: str
    channel: str


class _ACPClientAdapter:
    """ACP client callbacks delegated to the owning driver."""

    def __init__(self, owner: ACPAgent) -> None:
        self._owner = owner

    async def session_update(self, session_id: str, update: Any, **_: Any) -> None:
        await self._owner._handle_session_update(session_id, update)

    async def request_permission(
        self,
        session_id: str,
        tool_call: Any,
        options: list[PermissionOption],
        **_: Any,
    ) -> RequestPermissionResponse:
        return await self._owner._handle_permission_request(session_id, tool_call, options)

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        log.debug("ACP extension request ignored: %s(%s)", method, params)
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        log.debug("ACP extension notification ignored: %s(%s)", method, params)


class ACPAgent(BaseAgent):
    """Reusable ACP transport that keeps one process and session per project."""

    def __init__(self, project: ProjectInfo, provider: ACPProvider) -> None:
        super().__init__(project)
        self.provider = provider
        self._process: asyncio.subprocess.Process | None = None
        self._connection: ClientSideConnection | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._prompt_task: asyncio.Task[None] | None = None
        self._events: asyncio.Queue[OutgoingMessage | object] | None = None
        self._pending_permission: _PendingPermission | None = None
        self._active_user_id = ""
        self._active_channel = ""
        self._text_chunks: list[str] = []
        self._usage: dict[str, Any] | None = None
        self._instructions_sent = False

    @property
    def has_pending_question(self) -> bool:
        return self._pending_permission is not None

    async def _ensure_session(self) -> tuple[ClientSideConnection, str]:
        if (
            self._connection is not None
            and self._process is not None
            and self._process.returncode is None
            and self.project.session_id
        ):
            return self._connection, self.project.session_id

        if self._connection is not None or self._process is not None:
            await self._close_transport()

        command = self.provider.command_for(self.project)
        self._instructions_sent = False
        log.info("starting %s ACP agent for project %s: %s", self.provider.name, self.project.name, command)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self.project.path,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"{self.provider.name} agent executable not found: {command[0]!r}"
            ) from exc

        if process.stdin is None or process.stdout is None or process.stderr is None:
            process.kill()
            await process.wait()
            raise RuntimeError(f"{self.provider.name} ACP process did not expose stdio")

        self._process = process
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(process.stderr),
            name=f"{self.provider.name}-stderr-{self.project.name}",
        )
        connection = ClientSideConnection(
            _ACPClientAdapter(self),
            process.stdin,
            process.stdout,
        )
        self._connection = connection

        try:
            async with asyncio.timeout(settings.acp_startup_timeout):
                await connection.initialize(
                    protocol_version=1,
                    client_capabilities=ClientCapabilities(),
                    client_info=Implementation(
                        name="agent-box",
                        title="agent-box",
                        version=_AGENT_BOX_VERSION,
                    ),
                )

                stale_session = self.project.session_id
                if stale_session:
                    try:
                        await connection.load_session(
                            cwd=self.project.path,
                            session_id=stale_session,
                            mcp_servers=[],
                        )
                        log.info(
                            "resumed %s ACP session %s for project %s",
                            self.provider.name,
                            stale_session,
                            self.project.name,
                        )
                        return connection, stale_session
                    except Exception:  # noqa: BLE001 - stale providers fail with non-standard errors
                        log.warning(
                            "failed to resume %s ACP session %s for project %s; starting fresh",
                            self.provider.name,
                            stale_session,
                            self.project.name,
                        )
                        self.project.session_id = None

                response = await connection.new_session(
                    cwd=self.project.path,
                    mcp_servers=[],
                )
                self.project.session_id = response.session_id
                log.info(
                    "created %s ACP session %s for project %s",
                    self.provider.name,
                    response.session_id,
                    self.project.name,
                )
                return connection, response.session_id
        except Exception:
            await self._close_transport()
            raise

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        try:
            while line := await stream.readline():
                log.warning(
                    "%s stderr [%s]: %s",
                    self.provider.name,
                    self.project.name,
                    line.decode(errors="replace").rstrip(),
                )
        except asyncio.CancelledError:
            pass
        except Exception:
            log.debug("failed to drain ACP stderr", exc_info=True)

    async def run(
        self,
        prompt: str,
        user_id: str = "",
        channel: str = "",
    ) -> AsyncIterator[OutgoingMessage]:
        if self._pending_permission is not None:
            pending = self._pending_permission
            if pending.user_id and user_id and pending.user_id != user_id:
                yield OutgoingMessage(
                    text="❌ This agent is waiting for a reply from another user.",
                    user_id=user_id,
                    channel=channel,
                )
                return
            pending.future.set_result(self._permission_response(pending.options, prompt))
            async for message in self._consume_active_turn():
                yield message
            return

        if self._prompt_task is not None and not self._prompt_task.done():
            yield OutgoingMessage(
                text="❌ This project already has an ACP turn in progress.",
                user_id=user_id,
                channel=channel,
            )
            return

        connection, session_id = await self._ensure_session()
        self._active_user_id = user_id
        self._active_channel = channel
        self._events = asyncio.Queue()
        self._text_chunks = []
        self._usage = None

        effective_prompt = prompt
        if self.provider.instructions and not self._instructions_sent:
            effective_prompt = f"{self.provider.instructions}\n\nUser request:\n{prompt}"
            self._instructions_sent = True

        self._prompt_task = asyncio.create_task(
            self._execute_prompt(connection, session_id, effective_prompt),
            name=f"{self.provider.name}-prompt-{self.project.name}",
        )
        async for message in self._consume_active_turn():
            yield message

    async def _execute_prompt(
        self,
        connection: ClientSideConnection,
        session_id: str,
        prompt: str,
    ) -> None:
        assert self._events is not None
        try:
            response = await connection.prompt(
                session_id=session_id,
                prompt=[TextContentBlock(type="text", text=prompt)],
            )
            await self._flush_text()
            data: dict[str, Any] = {
                "session_id": session_id,
                "stop_reason": response.stop_reason,
            }
            if response.usage is not None:
                data["usage"] = response.usage.model_dump(mode="json", by_alias=True)
            if self._usage is not None:
                data["context"] = self._usage
            await self._events.put(
                OutgoingMessage(
                    text="",
                    user_id=self._active_user_id,
                    channel=self._active_channel,
                    type=MessageType.result,
                    data=data,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("%s ACP prompt failed for project %s", self.provider.name, self.project.name)
            await self._flush_text()
            await self._events.put(
                OutgoingMessage(
                    text=f"❌ {self.provider.name} agent error: {exc}",
                    user_id=self._active_user_id,
                    channel=self._active_channel,
                )
            )
        finally:
            await self._events.put(_TURN_DONE)

    async def _consume_active_turn(self) -> AsyncIterator[OutgoingMessage]:
        if self._events is None:
            return
        while True:
            event = await self._events.get()
            if event is _PERMISSION_PAUSE:
                return
            if event is _TURN_DONE:
                task = self._prompt_task
                self._prompt_task = None
                self._events = None
                self._active_user_id = ""
                self._active_channel = ""
                if task is not None:
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                return
            assert isinstance(event, OutgoingMessage)
            yield event

    async def _handle_session_update(self, session_id: str, update: Any) -> None:
        if session_id != self.project.session_id or self._events is None:
            return
        if isinstance(update, AgentMessageChunk):
            if isinstance(update.content, TextContentBlock):
                self._text_chunks.append(update.content.text)
            return
        if isinstance(update, ToolCallStart):
            await self._flush_text()
            await self._events.put(
                OutgoingMessage(
                    text=self._format_tool_call(update),
                    user_id=self._active_user_id,
                    channel=self._active_channel,
                    data={
                        "id": update.tool_call_id,
                        "kind": update.kind,
                        "input": update.raw_input,
                    },
                )
            )
            return
        if isinstance(update, ToolCallProgress) and update.status == "failed":
            await self._events.put(
                OutgoingMessage(
                    text=f"❌ {update.title or 'Tool call failed'}",
                    user_id=self._active_user_id,
                    channel=self._active_channel,
                )
            )
            return
        if isinstance(update, UsageUpdate):
            self._usage = update.model_dump(mode="json", by_alias=True)

    async def _flush_text(self) -> None:
        if not self._text_chunks or self._events is None:
            return
        text = "".join(self._text_chunks).strip()
        self._text_chunks.clear()
        if not text:
            return
        cleaned, file_paths = parse_send_file_markers(text)
        if cleaned:
            await self._events.put(
                OutgoingMessage(
                    text=cleaned,
                    user_id=self._active_user_id,
                    channel=self._active_channel,
                )
            )
        for path in file_paths:
            await self._events.put(
                OutgoingMessage(
                    text="",
                    user_id=self._active_user_id,
                    channel=self._active_channel,
                    data={"file_path": path},
                )
            )

    def _format_tool_call(self, update: ToolCallStart) -> str:
        icon = {
            "read": "📖",
            "edit": "✏️",
            "delete": "🗑️",
            "move": "📦",
            "search": "🔍",
            "execute": "🔧",
            "think": "🤔",
            "fetch": "🌐",
        }.get(update.kind or "", "⚙️")
        title = update.title or update.kind or "tool"
        title = title.replace(self.project.path.rstrip("/"), ".")
        if len(title) > 80:
            title = title[:77] + "..."
        return f"{icon} {title}"

    async def _handle_permission_request(
        self,
        session_id: str,
        tool_call: Any,
        options: Sequence[PermissionOption],
    ) -> RequestPermissionResponse:
        options = list(options)
        if settings.agent_permission_mode == "bypassPermissions":
            return self._select_permission(options, allow=True)
        if session_id != self.project.session_id or self._events is None:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

        loop = asyncio.get_running_loop()
        pending = _PendingPermission(
            future=loop.create_future(),
            options=options,
            user_id=self._active_user_id,
            channel=self._active_channel,
        )
        self._pending_permission = pending
        await self._flush_text()
        lines = [f"Permission required: {getattr(tool_call, 'title', None) or 'tool call'}"]
        for index, option in enumerate(options, 1):
            lines.append(f"  {index}. {option.name}")
        lines.append("Reply with an option number/name, Yes, or No.")
        await self._events.put(
            OutgoingMessage(
                text="\n".join(lines),
                user_id=self._active_user_id,
                channel=self._active_channel,
            )
        )
        await self._events.put(_PERMISSION_PAUSE)
        try:
            return await pending.future
        except asyncio.CancelledError:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        finally:
            if self._pending_permission is pending:
                self._pending_permission = None

    def _permission_response(
        self,
        options: Sequence[PermissionOption],
        reply: str,
    ) -> RequestPermissionResponse:
        value = reply.strip().lower()
        if value.isdigit():
            index = int(value) - 1
            if 0 <= index < len(options):
                return RequestPermissionResponse(
                    outcome=AllowedOutcome(outcome="selected", optionId=options[index].option_id)
                )
        for option in options:
            if value == option.name.strip().lower() or value == option.option_id.lower():
                return RequestPermissionResponse(
                    outcome=AllowedOutcome(outcome="selected", optionId=option.option_id)
                )
        decision_word = re.split(r"[\s,:;，：；]+", value, maxsplit=1)[0]
        if decision_word in _APPROVE_WORDS:
            return self._select_permission(options, allow=True)
        return self._select_permission(options, allow=False)

    @staticmethod
    def _select_permission(
        options: Sequence[PermissionOption],
        *,
        allow: bool,
    ) -> RequestPermissionResponse:
        desired = ("allow_once", "allow_always") if allow else ("reject_once", "reject_always")
        for kind in desired:
            for option in options:
                if option.kind == kind:
                    return RequestPermissionResponse(
                        outcome=AllowedOutcome(outcome="selected", optionId=option.option_id)
                    )
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    async def _close_transport(self) -> None:
        connection, self._connection = self._connection, None
        process, self._process = self._process, None
        stderr_task, self._stderr_task = self._stderr_task, None

        if connection is not None:
            with contextlib.suppress(Exception):
                await connection.close()

        if process is not None:
            if process.returncode is None:
                process.terminate()
                try:
                    async with asyncio.timeout(settings.acp_shutdown_timeout):
                        await process.wait()
                except TimeoutError:
                    process.kill()
                    await process.wait()
            # asyncio's subprocess transport may otherwise be finalized after
            # the event loop closes, producing a spurious destructor warning.
            transport = getattr(process, "_transport", None)
            if transport is not None:
                transport.close()

        if stderr_task is not None:
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task

    async def close(self) -> None:
        pending, self._pending_permission = self._pending_permission, None
        if pending is not None and not pending.future.done():
            pending.future.cancel()

        prompt_task, self._prompt_task = self._prompt_task, None
        if prompt_task is not None and not prompt_task.done():
            if self._connection is not None and self.project.session_id:
                with contextlib.suppress(Exception):
                    await self._connection.cancel(self.project.session_id)
            prompt_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await prompt_task

        self._events = None
        self._text_chunks.clear()
        await self._close_transport()
