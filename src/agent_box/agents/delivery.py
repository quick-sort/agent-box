"""Provider-neutral helpers for delivering agent-generated files."""

from __future__ import annotations

import re

SEND_FILE_INSTRUCTION = (
    "When you generate a file that the user should receive (images, charts, "
    "PDFs, documents), include a marker on its own line:\n"
    "[SEND_FILE:/absolute/path/to/file]\n"
    "You can include multiple markers for multiple files. The markers will be "
    "automatically removed from your response and the files will be sent to "
    "the user. Only use this for files the user explicitly asked for or that "
    "are final deliverables — not intermediate temporary files."
)

_SEND_FILE_RE = re.compile(r"\[SEND_FILE:([^\]]+)\]")


def parse_send_file_markers(text: str) -> tuple[str, list[str]]:
    """Return response text with delivery markers removed and their paths."""
    paths = _SEND_FILE_RE.findall(text)
    if not paths:
        return text, []
    return _SEND_FILE_RE.sub("", text).strip(), paths
