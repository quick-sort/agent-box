"""Tests for agent_box.asr.glm."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from agent_box.asr.glm import GlmASR, GLM_ASR_URL


def _make_response(status: int, json_body: dict | None = None, text: str = "") -> httpx.Response:
    kwargs: dict = {"request": httpx.Request("POST", GLM_ASR_URL)}
    if json_body is not None:
        kwargs["json"] = json_body
    else:
        kwargs["text"] = text
    return httpx.Response(status, **kwargs)


@pytest.mark.anyio
async def test_transcribe_success(tmp_path):
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"fake-audio-bytes")

    asr = GlmASR(api_key="key-123", model="glm-asr-2512")

    with patch("agent_box.asr.glm.httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=None)
        instance.post = AsyncMock(
            return_value=_make_response(200, {"text": "你好世界"})
        )
        result = await asr.transcribe(str(audio))

    assert result == "你好世界"
    instance.post.assert_awaited_once()
    kwargs = instance.post.await_args.kwargs
    assert kwargs["headers"]["Authorization"] == "Bearer key-123"
    assert kwargs["data"]["model"] == "glm-asr-2512"
    assert kwargs["data"]["stream"] == "false"
    assert "file" in kwargs["files"]


@pytest.mark.anyio
async def test_transcribe_http_error_returns_none(tmp_path):
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"audio")

    asr = GlmASR(api_key="key", model="glm-asr-2512")

    with patch("agent_box.asr.glm.httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=None)
        instance.post = AsyncMock(return_value=_make_response(400, text="bad request"))
        result = await asr.transcribe(str(audio))

    assert result is None


@pytest.mark.anyio
async def test_transcribe_empty_text_returns_none(tmp_path):
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"audio")

    asr = GlmASR(api_key="key")

    with patch("agent_box.asr.glm.httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=None)
        instance.post = AsyncMock(return_value=_make_response(200, {"text": "  "}))
        result = await asr.transcribe(str(audio))

    assert result is None


@pytest.mark.anyio
async def test_transcribe_network_error_returns_none(tmp_path):
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"audio")

    asr = GlmASR(api_key="key")

    with patch("agent_box.asr.glm.httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=None)
        instance.post = AsyncMock(side_effect=httpx.ConnectError("nope"))
        result = await asr.transcribe(str(audio))

    assert result is None


@pytest.mark.anyio
async def test_transcribe_missing_file_returns_none():
    asr = GlmASR(api_key="key")
    result = await asr.transcribe("/nonexistent/voice.wav")
    assert result is None


@pytest.mark.anyio
async def test_transcribe_empty_file_returns_none(tmp_path):
    audio = tmp_path / "empty.wav"
    audio.write_bytes(b"")
    asr = GlmASR(api_key="key")
    result = await asr.transcribe(str(audio))
    assert result is None
