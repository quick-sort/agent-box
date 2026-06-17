"""GLM-ASR voice transcription client (ZhipuAI / BigModel).

API: POST https://open.bigmodel.cn/api/paas/v4/audio/transcriptions
  multipart/form-data: model, stream, file
  Header: Authorization: Bearer <api_key>
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

GLM_ASR_URL = "https://open.bigmodel.cn/api/paas/v4/audio/transcriptions"
_TIMEOUT_S = 120.0


class GlmASR:
    """Transcribe an audio file to text via the GLM-ASR API."""

    def __init__(self, api_key: str, model: str = "glm-asr-2512") -> None:
        self._api_key = api_key
        self._model = model

    async def transcribe(self, file_path: str) -> str | None:
        """Return the transcribed text, or None on any failure.

        Never raises — callers can rely on a None to mean "fall back gracefully".
        """
        p = Path(file_path)
        if not p.is_file():
            log.warning("GLM ASR: file not found: %s", file_path)
            return None

        try:
            data = p.read_bytes()
        except OSError:
            log.exception("GLM ASR: failed to read %s", file_path)
            return None
        if not data:
            log.warning("GLM ASR: empty file: %s", file_path)
            return None

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
                resp = await client.post(
                    GLM_ASR_URL,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    files={"file": (p.name, data)},
                    data={"model": self._model, "stream": "false"},
                )
        except Exception:
            log.exception("GLM ASR: request failed for %s", file_path)
            return None

        if not resp.is_success:
            log.error(
                "GLM ASR: %s returned %s: %s",
                file_path, resp.status_code, resp.text[:300],
            )
            return None

        try:
            payload = resp.json()
        except ValueError:
            log.error("GLM ASR: non-JSON response: %s", resp.text[:300])
            return None

        # API returns {"text": "...", ...} on success
        text = (payload.get("text") or "").strip()
        if not text:
            log.warning("GLM ASR: empty transcription for %s: %s", file_path, payload)
            return None
        log.info("GLM ASR: transcribed %s (%d chars)", file_path, len(text))
        return text
