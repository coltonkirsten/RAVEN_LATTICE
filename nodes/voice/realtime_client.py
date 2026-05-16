"""Thin asyncio WebSocket client for the OpenAI Realtime API (gpt-realtime-2).

Wire protocol is a stream of JSON events documented at
    https://platform.openai.com/docs/api-reference/realtime
Audio format: 24 kHz mono PCM16, base64 encoded.
"""
from __future__ import annotations

import base64
import json
import logging
from typing import AsyncIterator, Optional

import aiohttp

log = logging.getLogger("voice.realtime")

REALTIME_URL = "wss://api.openai.com/v1/realtime"


class RealtimeClient:
    def __init__(self, api_key: str, model: str = "gpt-realtime-2",
                 session_config: Optional[dict] = None) -> None:
        self.api_key = api_key
        self.model = model
        self.session_config = session_config or {}
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed or self._ws is None or self._ws.closed

    async def connect(self) -> None:
        url = f"{REALTIME_URL}?model={self.model}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(url, headers=headers, heartbeat=20)
        except Exception:
            await self._session.close()
            self._session = None
            raise
        log.info("connected to %s", url)
        if self.session_config:
            await self.update_session(self.session_config)

    async def update_session(self, session: dict) -> None:
        await self._send({"type": "session.update", "session": session})

    async def append_audio(self, pcm16: bytes) -> None:
        if not pcm16:
            return
        b64 = base64.b64encode(pcm16).decode("ascii")
        await self._send({"type": "input_audio_buffer.append", "audio": b64})

    async def commit_audio(self) -> None:
        await self._send({"type": "input_audio_buffer.commit"})

    async def clear_audio_buffer(self) -> None:
        await self._send({"type": "input_audio_buffer.clear"})

    async def add_system_item(self, text: str) -> None:
        """Append a system-role conversation item carrying `text` verbatim."""
        item = {
            "type": "message",
            "role": "system",
            "content": [{"type": "input_text", "text": text}],
        }
        await self._send({"type": "conversation.item.create", "item": item})

    async def add_user_item(self, text: str) -> None:
        """Append a user-role conversation item with `text` content."""
        item = {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        }
        await self._send({"type": "conversation.item.create", "item": item})

    async def speak_verbatim(self, text: str, voice: Optional[str] = None) -> None:
        """Force the model to read `text` aloud verbatim."""
        instructions = (
            f"Read the following text verbatim, no additional commentary: {text}"
        )
        opts: dict = {
            "output_modalities": ["audio"],
            "instructions": instructions,
        }
        if voice:
            opts["audio"] = {"output": {"voice": voice}}
        await self.create_response(opts)

    async def create_response(self, options: Optional[dict] = None) -> None:
        evt: dict = {"type": "response.create"}
        if options:
            evt["response"] = options
        await self._send(evt)

    async def cancel_response(self) -> None:
        await self._send({"type": "response.cancel"})

    async def _send(self, evt: dict) -> None:
        if self.closed:
            raise RuntimeError("realtime websocket is closed")
        assert self._ws is not None
        await self._ws.send_str(json.dumps(evt))

    async def send_raw(self, evt: dict) -> None:
        await self._send(evt)

    async def events(self) -> AsyncIterator[dict]:
        if self._ws is None:
            return
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        yield json.loads(msg.data)
                    except json.JSONDecodeError:
                        log.warning("non-JSON text frame")
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    log.debug("unexpected binary frame, %d bytes", len(msg.data))
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE,
                                  aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                    log.info("ws closed (%s)", msg.type)
                    break
        finally:
            self._closed = True

    async def close(self) -> None:
        self._closed = True
        if self._ws is not None and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = None
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None


def decode_audio_delta(delta_b64: str) -> bytes:
    return base64.b64decode(delta_b64)
