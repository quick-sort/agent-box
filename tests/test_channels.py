"""Tests for agent_box.channels."""

from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest

from agent_box.models import IncomingMessage, OutgoingMessage
from agent_box.channels.base import BaseChannel


# ── BaseChannel ──

def test_base_channel_is_abstract():
    with pytest.raises(TypeError):
        BaseChannel(MagicMock())


def test_base_channel_subclass():
    class Dummy(BaseChannel):
        async def start(self): pass
        async def send_reply(self, msg): pass

    d = Dummy(MagicMock())
    assert d.send_stream is not None


@pytest.mark.anyio
async def test_send_loop():
    """send_loop should call send_reply for each outgoing message."""
    replies = []

    class Collector(BaseChannel):
        async def start(self): pass
        async def send_reply(self, msg: OutgoingMessage):
            replies.append(msg)

    send, recv = anyio.create_memory_object_stream[OutgoingMessage](4)
    channel = Collector(MagicMock())

    async with anyio.create_task_group() as tg:
        tg.start_soon(channel.send_loop, recv)
        await send.send(OutgoingMessage(text="a", user_id="u1"))
        await send.send(OutgoingMessage(text="b", user_id="u1"))
        await send.aclose()

    assert len(replies) == 2
    assert replies[0].text == "a"
    assert replies[1].text == "b"


# ── WeixinChannel ──

@pytest.mark.anyio
async def test_weixin_start_polls_and_emits(tmp_path):
    """WeixinChannel.start() should poll and emit IncomingMessage."""
    from agent_box.channels.weixin import WeixinChannel

    fake_poll = MagicMock()
    fake_poll.messages = [
        {"from_user_id": "wx_user", "item_list": [{"type": 1, "text_item": {"text": "hi"}}]},
    ]

    fake_account = MagicMock()
    fake_account.poll_once = MagicMock(return_value=fake_poll)

    send, recv = anyio.create_memory_object_stream[IncomingMessage](4)

    with patch("agent_box.channels.weixin.AccountClient") as MockAC, \
         patch("agent_box.channels.weixin._resolve_account_id", return_value="fake_id"):
        MockAC.from_store.return_value = fake_account
        channel = WeixinChannel(send)
        channel.account = fake_account

        # Run start() but cancel after first poll
        async with anyio.create_task_group() as tg:
            async def run_then_cancel():
                await channel.start()

            async def stop_after_message():
                msg = await recv.receive()
                assert msg.text == "hi"
                assert msg.user_id == "wx_user"
                assert msg.channel == "weixin"
                tg.cancel_scope.cancel()

            tg.start_soon(run_then_cancel)
            tg.start_soon(stop_after_message)


@pytest.mark.anyio
async def test_weixin_start_skips_empty_text(tmp_path):
    """Messages with no text should be skipped."""
    from agent_box.channels.weixin import WeixinChannel

    fake_poll = MagicMock()
    fake_poll.messages = [
        {"from_user_id": "wx_user", "item_list": [{"type": 2, "image_item": {}}]},  # image, no text
    ]

    fake_account = MagicMock()
    # First poll returns image-only, then raise to stop
    fake_account.poll_once = MagicMock(side_effect=[fake_poll, TimeoutError()])

    send, recv = anyio.create_memory_object_stream[IncomingMessage](4)

    with patch("agent_box.channels.weixin.AccountClient"):
        channel = WeixinChannel.__new__(WeixinChannel)
        channel.send_stream = send
        channel._account_id = "fake-account"
        channel._store = MagicMock()
        channel.account = fake_account

        async with anyio.create_task_group() as tg:
            async def run_start():
                await channel.start()

            async def timeout_stop():
                await anyio.sleep(0.5)
                tg.cancel_scope.cancel()

            tg.start_soon(run_start)
            tg.start_soon(timeout_stop)

    # recv should be empty — no text messages were emitted
    with pytest.raises((anyio.WouldBlock, anyio.EndOfStream)):
        recv.receive_nowait()


@pytest.mark.anyio
async def test_weixin_send_reply():
    from agent_box.channels.weixin import WeixinChannel

    fake_account = MagicMock()
    fake_account.send_text = MagicMock()

    with patch("agent_box.channels.weixin.AccountClient"):
        channel = WeixinChannel.__new__(WeixinChannel)
        channel._account_id = "fake-account"
        channel._store = MagicMock()
        channel.account = fake_account

        await channel.send_reply(OutgoingMessage(text="reply", user_id="wx_user"))

    fake_account.send_text.assert_called_once()
    call_kwargs = fake_account.send_text.call_args
    assert call_kwargs.kwargs.get("to_user_id") == "wx_user" or "wx_user" in str(call_kwargs)


@pytest.mark.anyio
async def test_weixin_handles_poll_exception():
    """Exceptions during poll should be caught and not crash the loop."""
    from agent_box.channels.weixin import WeixinChannel

    fake_account = MagicMock()
    call_count = 0

    def poll_side_effect():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ConnectionError("network down")
        raise TimeoutError()  # stop loop

    fake_account.poll_once = MagicMock(side_effect=poll_side_effect)

    send, recv = anyio.create_memory_object_stream[IncomingMessage](4)

    with patch("agent_box.channels.weixin.AccountClient.from_store", return_value=fake_account):
        channel = WeixinChannel.__new__(WeixinChannel)
        channel.send_stream = send
        channel._account_id = "fake-account"
        channel._store = MagicMock()
        channel.account = fake_account

        async with anyio.create_task_group() as tg:
            tg.start_soon(channel.start)
            await anyio.sleep(2)
            tg.cancel_scope.cancel()

    assert call_count >= 2  # survived the first exception


# ── TuiChannel ──

@pytest.mark.anyio
async def test_tui_channel_has_app():
    """TuiChannel should create an AgentBoxApp instance."""
    from agent_box.channels.tui import TuiChannel, AgentBoxApp

    send, recv = anyio.create_memory_object_stream[IncomingMessage](4)
    channel = TuiChannel(send)
    assert isinstance(channel._app, AgentBoxApp)


@pytest.mark.anyio
async def test_tui_send_reply_when_not_running():
    """send_reply should not crash when app is not running."""
    from agent_box.channels.tui import TuiChannel

    send, _ = anyio.create_memory_object_stream[IncomingMessage](4)
    channel = TuiChannel(send)
    # App is not running, should silently skip
    await channel.send_reply(OutgoingMessage(text="hello back", user_id="local"))


# ── WeixinChannel file sending ──


@pytest.mark.anyio
async def test_weixin_send_reply_with_file_path():
    """send_reply with file_path should call MediaClient.send_file."""
    from agent_box.channels.weixin import WeixinChannel

    fake_account = MagicMock()
    fake_account.media = MagicMock()
    fake_account.media.send_file = MagicMock()

    with patch("agent_box.channels.weixin.AccountClient"):
        channel = WeixinChannel.__new__(WeixinChannel)
        channel.send_stream = MagicMock()
        channel._account_id = "fake-account"
        channel._store = MagicMock()
        channel.account = fake_account

        await channel.send_reply(OutgoingMessage(
            text="report",
            user_id="wx_user",
            data={"file_path": "/tmp/report.pdf"},
        ))

    fake_account.media.send_file.assert_called_once()
    call_kwargs = fake_account.media.send_file.call_args
    assert call_kwargs.kwargs.get("file_path") == "/tmp/report.pdf"
    assert call_kwargs.kwargs.get("to_user_id") == "wx_user"


@pytest.mark.anyio
async def test_weixin_send_reply_with_image_path():
    """send_reply with image_path should call send_file with forced_kind='image'."""
    from agent_box.channels.weixin import WeixinChannel

    fake_account = MagicMock()
    fake_account.media = MagicMock()
    fake_account.media.send_file = MagicMock()

    with patch("agent_box.channels.weixin.AccountClient"):
        channel = WeixinChannel.__new__(WeixinChannel)
        channel.send_stream = MagicMock()
        channel._account_id = "fake-account"
        channel._store = MagicMock()
        channel.account = fake_account

        await channel.send_reply(OutgoingMessage(
            text="photo",
            user_id="wx_user",
            data={"image_path": "/tmp/photo.jpg"},
        ))

    fake_account.media.send_file.assert_called_once()
    call_kwargs = fake_account.media.send_file.call_args
    assert call_kwargs.kwargs.get("file_path") == "/tmp/photo.jpg"
    assert call_kwargs.kwargs.get("forced_kind") == "image"


# ── QQ Channel media helpers ──


def test_detect_file_type():
    from agent_box.channels.qq import _detect_file_type, _FILE_TYPE_IMAGE, _FILE_TYPE_VIDEO, _FILE_TYPE_VOICE, _FILE_TYPE_FILE

    assert _detect_file_type("photo.jpg") == _FILE_TYPE_IMAGE
    assert _detect_file_type("photo.png") == _FILE_TYPE_IMAGE
    assert _detect_file_type("anim.gif") == _FILE_TYPE_IMAGE
    assert _detect_file_type("clip.mp4") == _FILE_TYPE_VIDEO
    assert _detect_file_type("clip.mov") == _FILE_TYPE_VIDEO
    assert _detect_file_type("song.mp3") == _FILE_TYPE_VOICE
    assert _detect_file_type("voice.wav") == _FILE_TYPE_VOICE
    assert _detect_file_type("report.pdf") == _FILE_TYPE_FILE
    assert _detect_file_type("data.xlsx") == _FILE_TYPE_FILE
    assert _detect_file_type("archive.zip") == _FILE_TYPE_FILE


def test_detect_file_type_case_insensitive():
    from agent_box.channels.qq import _detect_file_type, _FILE_TYPE_IMAGE

    assert _detect_file_type("photo.JPG") == _FILE_TYPE_IMAGE
    assert _detect_file_type("photo.Png") == _FILE_TYPE_IMAGE


def test_format_size():
    from agent_box.channels.qq import _format_size

    assert _format_size(0) == "0B"
    assert _format_size(1023) == "1023B"
    assert _format_size(1024) == "1.0KB"
    assert _format_size(1024 * 1024) == "1.0MB"
    assert _format_size(1024 * 1024 * 100) == "100.0MB"


@pytest.mark.anyio
async def test_compute_file_hashes():
    from agent_box.channels.qq import QQChannel

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
        f.write(b"hello world test data for hashing")
        f.flush()
        path = f.name

    send, _ = anyio.create_memory_object_stream[IncomingMessage](4)
    channel = QQChannel.__new__(QQChannel)
    channel.send_stream = send

    hashes = await channel._compute_file_hashes(path)

    assert "md5" in hashes
    assert "sha1" in hashes
    assert "md5_10m" in hashes
    assert len(hashes["md5"]) == 32
    assert len(hashes["sha1"]) == 40
    # File is small, md5_10m should equal md5
    assert hashes["md5_10m"] == hashes["md5"]

    import os
    os.unlink(path)


@pytest.mark.anyio
async def test_read_chunk_and_hash():
    from agent_box.channels.qq import QQChannel

    import tempfile
    content = b"0123456789abcdef"  # 16 bytes
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
        f.write(content)
        f.flush()
        path = f.name

    send, _ = anyio.create_memory_object_stream[IncomingMessage](4)
    channel = QQChannel.__new__(QQChannel)
    channel.send_stream = send

    data, md5_hex = await channel._read_chunk_and_hash(path, 4, 8)
    assert data == b"456789ab"
    assert len(md5_hex) == 32

    import os
    os.unlink(path)


@pytest.mark.anyio
async def test_chunked_upload_success():
    """Full chunked upload flow with mocked API and HTTP."""
    from agent_box.channels.qq import QQChannel, _FILE_TYPE_FILE
    from unittest.mock import AsyncMock, patch

    import tempfile
    content = b"A" * 1000
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        f.write(content)
        f.flush()
        path = f.name

    send, _ = anyio.create_memory_object_stream[IncomingMessage](4)
    channel = QQChannel.__new__(QQChannel)
    channel.send_stream = send

    api_responses = [
        # upload_prepare
        {"upload_id": "uid-123", "block_size": 500, "parts": [
            {"index": 1, "presigned_url": "https://cos.example.com/part1"},
            {"index": 2, "presigned_url": "https://cos.example.com/part2"},
        ]},
        # upload_part_finish for part 1
        {},
        # upload_part_finish for part 2
        {},
        # complete_upload
        {"file_info": "serialized_file_info_abc", "file_uuid": "f-uuid"},
    ]
    call_count = 0

    async def mock_api_post(path, body):
        nonlocal call_count
        resp = api_responses[call_count]
        call_count += 1
        return resp

    channel._api_post = mock_api_post
    channel._put_chunk = AsyncMock(return_value=True)
    channel._compute_file_hashes = AsyncMock(return_value={
        "md5": "a" * 32, "sha1": "b" * 40, "md5_10m": "a" * 32,
    })

    file_info = await channel._chunked_upload("user-123", "c2c", path, _FILE_TYPE_FILE)

    assert file_info == "serialized_file_info_abc"
    assert channel._put_chunk.call_count == 2
    assert call_count == 4

    import os
    os.unlink(path)


@pytest.mark.anyio
async def test_chunked_upload_prepare_fails():
    """chunked_upload returns None when upload_prepare fails."""
    from agent_box.channels.qq import QQChannel, _FILE_TYPE_FILE

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        f.write(b"test data")
        f.flush()
        path = f.name

    send, _ = anyio.create_memory_object_stream[IncomingMessage](4)
    channel = QQChannel.__new__(QQChannel)
    channel.send_stream = send

    channel._api_post = AsyncMock(return_value={})  # no upload_id
    channel._compute_file_hashes = AsyncMock(return_value={
        "md5": "a" * 32, "sha1": "b" * 40, "md5_10m": "a" * 32,
    })

    file_info = await channel._chunked_upload("user-123", "c2c", path, _FILE_TYPE_FILE)
    assert file_info is None

    import os
    os.unlink(path)


@pytest.mark.anyio
async def test_chunked_upload_file_not_found():
    from agent_box.channels.qq import QQChannel, _FILE_TYPE_FILE

    send, _ = anyio.create_memory_object_stream[IncomingMessage](4)
    channel = QQChannel.__new__(QQChannel)
    channel.send_stream = send

    file_info = await channel._chunked_upload("user-123", "c2c", "/nonexistent/file.txt", _FILE_TYPE_FILE)
    assert file_info is None


@pytest.mark.anyio
async def test_send_media_calls_chunked_upload():
    """_send_media should detect file type and send media message."""
    from agent_box.channels.qq import QQChannel

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(b"pdf content")
        f.flush()
        path = f.name

    send, _ = anyio.create_memory_object_stream[IncomingMessage](4)
    channel = QQChannel.__new__(QQChannel)
    channel.send_stream = send

    api_calls: list[tuple[str, dict]] = []

    async def mock_api_post(path, body):
        api_calls.append((path, body))
        if "upload_prepare" in path:
            return {"upload_id": "uid", "block_size": 1024, "parts": [
                {"index": 1, "presigned_url": "https://cos.example.com/part1"},
            ]}
        if "files" in path and "upload" not in path:
            return {"file_info": "fi-123"}
        if "upload_part_finish" in path:
            return {}
        if path.endswith("/messages"):
            return {"id": "msg-1", "timestamp": "0"}
        return {}

    channel._api_post = mock_api_post
    channel._put_chunk = AsyncMock(return_value=True)

    await channel._send_media("user-123", "c2c", path, msg_id="msg-0")

    # Last API call should be sending the media message
    last_path, last_body = api_calls[-1]
    assert last_path == "/v2/users/user-123/messages"
    assert last_body["msg_type"] == 7
    assert last_body["media"]["file_info"] == "fi-123"
    assert last_body["msg_id"] == "msg-0"

    import os
    os.unlink(path)


@pytest.mark.anyio
async def test_send_reply_with_file_path():
    """send_reply with file_path in data should call _send_media."""
    from agent_box.channels.qq import QQChannel

    send, _ = anyio.create_memory_object_stream[IncomingMessage](4)
    channel = QQChannel.__new__(QQChannel)
    channel.send_stream = send
    channel._last_msg_id = {"c2c:user-1": "msg-0"}

    called_with = {}

    async def mock_send_media(target_id, target_type, file_path, *, msg_id=None):
        called_with["target_id"] = target_id
        called_with["target_type"] = target_type
        called_with["file_path"] = file_path
        called_with["msg_id"] = msg_id

    channel._send_media = mock_send_media

    await channel.send_reply(OutgoingMessage(
        text="report",
        user_id="c2c:user-1",
        data={"file_path": "/tmp/report.pdf"},
    ))

    assert called_with["target_id"] == "user-1"
    assert called_with["target_type"] == "c2c"
    assert called_with["file_path"] == "/tmp/report.pdf"
    assert called_with["msg_id"] == "msg-0"


@pytest.mark.anyio
async def test_send_reply_with_image_path_fallback():
    """send_reply with image_path (legacy) should still use _send_image."""
    from agent_box.channels.qq import QQChannel

    send, _ = anyio.create_memory_object_stream[IncomingMessage](4)
    channel = QQChannel.__new__(QQChannel)
    channel.send_stream = send
    channel._last_msg_id = {"c2c:user-1": "msg-0"}

    called_with = {}

    async def mock_send_image(target_id, target_type, *, image_url=None, image_path=None, msg_id=None):
        called_with["target_id"] = target_id
        called_with["image_path"] = image_path

    channel._send_image = mock_send_image

    await channel.send_reply(OutgoingMessage(
        text="photo",
        user_id="c2c:user-1",
        data={"image_path": "/tmp/photo.jpg"},
    ))

    assert called_with["target_id"] == "user-1"
    assert called_with["image_path"] == "/tmp/photo.jpg"


# ── QQ Channel voice transcription (GLM ASR) ──


@pytest.mark.anyio
async def test_atts_text_with_voice_transcription():
    """A transcribed voice attachment renders its content instead of the file path."""
    from agent_box.channels.qq import QQChannel

    send, _ = anyio.create_memory_object_stream[IncomingMessage](4)
    channel = QQChannel.__new__(QQChannel)
    channel.send_stream = send

    text = channel._atts_text([("语音", "/path/voice.wav", "你好世界")])
    assert text == "用户发送了一段语音，内容: 你好世界"


@pytest.mark.anyio
async def test_atts_text_voice_without_transcription_falls_back_to_path():
    """When transcription is None, voice shows the file path (current behavior)."""
    from agent_box.channels.qq import QQChannel

    send, _ = anyio.create_memory_object_stream[IncomingMessage](4)
    channel = QQChannel.__new__(QQChannel)
    channel.send_stream = send

    text = channel._atts_text([("语音", "/path/voice.wav", None)])
    assert text == "用户发送了一个语音，文件路径: /path/voice.wav"


@pytest.mark.anyio
async def test_atts_text_mixed_image_and_voice():
    """Non-audio attachments keep the path format; voice uses transcription."""
    from agent_box.channels.qq import QQChannel

    send, _ = anyio.create_memory_object_stream[IncomingMessage](4)
    channel = QQChannel.__new__(QQChannel)
    channel.send_stream = send

    text = channel._atts_text([
        ("图片", "/path/img.jpg", None),
        ("语音", "/path/voice.wav", "转写文本"),
    ])
    assert "用户发送了一个图片，文件路径: /path/img.jpg" in text
    assert "用户发送了一段语音，内容: 转写文本" in text


@pytest.mark.anyio
async def test_download_atts_transcribes_audio_when_asr_configured(tmp_path):
    """Audio attachments get transcribed when self._asr is set."""
    from agent_box.channels.qq import QQChannel

    audio_path = tmp_path / "voice.wav"
    audio_path.write_bytes(b"audio")

    send, _ = anyio.create_memory_object_stream[IncomingMessage](4)
    channel = QQChannel.__new__(QQChannel)
    channel.send_stream = send
    channel._download_dir = tmp_path

    channel._asr = MagicMock()
    channel._asr.transcribe = AsyncMock(return_value="识别出的文字")

    channel._download_image = AsyncMock(return_value=str(audio_path))

    atts = [{"content_type": "audio/wav", "url": "https://x/voice.wav", "filename": "voice.wav"}]
    results = await channel._download_atts(atts)

    assert results == [("语音", str(audio_path), "识别出的文字")]
    channel._asr.transcribe.assert_awaited_once_with(str(audio_path))


@pytest.mark.anyio
async def test_download_atts_skips_transcription_when_asr_none(tmp_path):
    """When ASR is not configured, transcription stays None."""
    from agent_box.channels.qq import QQChannel

    audio_path = tmp_path / "voice.wav"
    audio_path.write_bytes(b"audio")

    send, _ = anyio.create_memory_object_stream[IncomingMessage](4)
    channel = QQChannel.__new__(QQChannel)
    channel.send_stream = send
    channel._download_dir = tmp_path
    channel._asr = None
    channel._download_image = AsyncMock(return_value=str(audio_path))

    atts = [{"content_type": "audio/wav", "url": "https://x/voice.wav", "filename": "voice.wav"}]
    results = await channel._download_atts(atts)

    assert results == [("语音", str(audio_path), None)]


@pytest.mark.anyio
async def test_download_atts_transcription_failure_falls_back(tmp_path):
    """When transcription returns None, the tuple keeps None (renders as path)."""
    from agent_box.channels.qq import QQChannel

    audio_path = tmp_path / "voice.wav"
    audio_path.write_bytes(b"audio")

    send, _ = anyio.create_memory_object_stream[IncomingMessage](4)
    channel = QQChannel.__new__(QQChannel)
    channel.send_stream = send
    channel._download_dir = tmp_path

    channel._asr = MagicMock()
    channel._asr.transcribe = AsyncMock(return_value=None)
    channel._download_image = AsyncMock(return_value=str(audio_path))

    atts = [{"content_type": "audio/wav", "url": "https://x/voice.wav", "filename": "voice.wav"}]
    results = await channel._download_atts(atts)

    assert results == [("语音", str(audio_path), None)]


@pytest.mark.anyio
async def test_download_atts_non_audio_not_transcribed(tmp_path):
    """Image/video attachments never trigger transcription."""
    from agent_box.channels.qq import QQChannel

    send, _ = anyio.create_memory_object_stream[IncomingMessage](4)
    channel = QQChannel.__new__(QQChannel)
    channel.send_stream = send
    channel._download_dir = tmp_path

    channel._asr = MagicMock()
    channel._asr.transcribe = AsyncMock(return_value="should-not-be-called")
    channel._download_image = AsyncMock(return_value=str(tmp_path / "img.jpg"))

    atts = [{"content_type": "image/jpeg", "url": "https://x/img.jpg", "filename": "img.jpg"}]
    results = await channel._download_atts(atts)

    assert results == [("图片", str(tmp_path / "img.jpg"), None)]
    channel._asr.transcribe.assert_not_awaited()


@pytest.mark.anyio
async def test_qqchannel_asr_disabled_when_no_key():
    """QQChannel._asr is None when glm_api_key is empty."""
    from agent_box.channels.qq import QQChannel

    send, _ = anyio.create_memory_object_stream[IncomingMessage](4)
    with patch("agent_box.channels.qq.settings") as mock_settings:
        mock_settings.glm_api_key = ""
        mock_settings.glm_asr_model = "glm-asr-2512"
        channel = QQChannel(send)
    assert channel._asr is None


@pytest.mark.anyio
async def test_qqchannel_asr_enabled_when_key_set():
    """QQChannel._asr is a GlmASR instance when glm_api_key is set."""
    from agent_box.channels.qq import QQChannel
    from agent_box.asr.glm import GlmASR

    send, _ = anyio.create_memory_object_stream[IncomingMessage](4)
    with patch("agent_box.channels.qq.settings") as mock_settings:
        mock_settings.glm_api_key = "key-abc"
        mock_settings.glm_asr_model = "glm-asr-2512"
        channel = QQChannel(send)
    assert isinstance(channel._asr, GlmASR)


# ── OdooChannel ──


def _make_odoo_channel(send_stream=None):
    from agent_box.channels.odoo import OdooChannel

    if send_stream is None:
        send_stream, _ = anyio.create_memory_object_stream[IncomingMessage](4)
    with patch("agent_box.channels.odoo.settings") as mock_settings:
        mock_settings.odoo_url = "https://odoo.example.com"
        mock_settings.odoo_db = "mydb"
        mock_settings.odoo_login = "bot@example.com"
        mock_settings.odoo_password = "secret"
        mock_settings.odoo_channel_id = 42
        channel = OdooChannel(send_stream)
    return channel


def test_html_to_text_strips_tags_and_entities():
    from agent_box.channels.odoo import _html_to_text

    assert _html_to_text("<p>Hello&nbsp;<b>world</b></p>") == "Hello world"
    assert _html_to_text(False) == ""
    assert _html_to_text(None) == ""
    assert _html_to_text("") == ""


@pytest.mark.anyio
async def test_odoo_login_session_sets_self_partner_id():
    channel = _make_odoo_channel()

    async def fake_call(route, params):
        assert route == "/web/session/authenticate"
        assert params == {"db": "mydb", "login": "bot@example.com", "password": "secret"}
        return {"uid": 5, "partner_id": 7}

    channel._call = fake_call
    await channel._login_session()

    assert channel._self_partner_id == 7


@pytest.mark.anyio
async def test_odoo_login_session_raises_on_failure():
    channel = _make_odoo_channel()

    async def fake_call(route, params):
        return {"uid": None}

    channel._call = fake_call
    with pytest.raises(RuntimeError):
        await channel._login_session()


@pytest.mark.anyio
async def test_odoo_handle_notification_emits_incoming_message():
    send, recv = anyio.create_memory_object_stream[IncomingMessage](4)
    channel = _make_odoo_channel(send)
    channel._self_partner_id = 99  # "our" account, should be filtered out

    notif = {
        "type": "discuss.channel/new_message",
        "payload": {
            "data": {
                "mail.message": [
                    {"id": 1, "author_id": 7, "body": "<p>Hello there</p>"},
                    {"id": 2, "author_id": 99, "body": "<p>our own echo</p>"},
                ]
            }
        },
    }
    await channel._handle_notification(notif)

    msg = recv.receive_nowait()
    assert msg.text == "Hello there"
    assert msg.user_id == "7"
    assert msg.channel == "odoo"
    with pytest.raises((anyio.WouldBlock, anyio.EndOfStream)):
        recv.receive_nowait()


@pytest.mark.anyio
async def test_odoo_handle_notification_ignores_other_types():
    send, recv = anyio.create_memory_object_stream[IncomingMessage](4)
    channel = _make_odoo_channel(send)

    await channel._handle_notification({"type": "bus.presence", "payload": {}})

    with pytest.raises((anyio.WouldBlock, anyio.EndOfStream)):
        recv.receive_nowait()


@pytest.mark.anyio
async def test_odoo_send_reply_posts_message():
    channel = _make_odoo_channel()

    calls = []

    async def fake_call(route, params):
        calls.append((route, params))
        return {}

    channel._client = MagicMock()  # just needs to be non-None
    channel._call = fake_call

    await channel.send_reply(OutgoingMessage(text="hello back", user_id="7"))

    assert len(calls) == 1
    route, params = calls[0]
    assert route == "/mail/message/post"
    assert params["thread_model"] == "discuss.channel"
    assert params["thread_id"] == 42
    assert params["post_data"] == {"body": "hello back", "message_type": "comment"}


@pytest.mark.anyio
async def test_odoo_send_reply_skips_non_text_messages():
    from agent_box.models import MessageType

    channel = _make_odoo_channel()
    channel._client = MagicMock()
    channel._call = AsyncMock()

    await channel.send_reply(OutgoingMessage(text="x", user_id="7", type=MessageType.result))

    channel._call.assert_not_called()


@pytest.mark.anyio
async def test_odoo_start_noop_when_not_configured():
    """start() should warn and return immediately when required settings are missing."""
    from agent_box.channels.odoo import OdooChannel

    send, _ = anyio.create_memory_object_stream[IncomingMessage](4)
    with patch("agent_box.channels.odoo.settings") as mock_settings:
        mock_settings.odoo_url = ""
        mock_settings.odoo_db = ""
        mock_settings.odoo_login = ""
        mock_settings.odoo_password = ""
        mock_settings.odoo_channel_id = 0
        channel = OdooChannel(send)

    # Should return without ever creating an httpx client.
    await channel.start()
    assert channel._client is None
