"""Mic capture + speaker playback for the voice node.

Both run as background asyncio tasks talking to sounddevice via thread-safe
queues. The Realtime API expects 24 kHz mono PCM16 in both directions.
"""
from __future__ import annotations

import asyncio
import logging
import queue as _queue
import threading
from typing import Optional

log = logging.getLogger("voice.audio")

SAMPLE_RATE = 24000
CHANNELS = 1
SAMPLE_WIDTH = 2
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * SAMPLE_WIDTH


class AudioUnavailable(Exception):
    """Raised when sounddevice can't open input/output devices."""


def _import_sounddevice():
    try:
        import sounddevice as sd  # type: ignore
        import numpy as np  # type: ignore
        return sd, np
    except Exception as e:
        raise AudioUnavailable(f"sounddevice/numpy import failed: {e}") from e


def _resolve_device(sd, name_or_index, kind: str) -> tuple[Optional[int], Optional[str]]:
    devs = sd.query_devices()
    if isinstance(name_or_index, int):
        if 0 <= name_or_index < len(devs):
            return name_or_index, devs[name_or_index]["name"]
        return None, None
    if isinstance(name_or_index, str) and name_or_index:
        needle = name_or_index.lower()
        ch_key = "max_input_channels" if kind == "input" else "max_output_channels"
        for i, d in enumerate(devs):
            if d[ch_key] > 0 and needle in d["name"].lower():
                return i, d["name"]
        return None, None
    default = sd.default.device
    idx = default[0 if kind == "input" else 1] if isinstance(default, (list, tuple)) else default
    if isinstance(idx, int) and 0 <= idx < len(devs):
        return idx, devs[idx]["name"]
    return None, None


class MicCapture:
    def __init__(self, sample_rate: int = SAMPLE_RATE,
                 device: object = None) -> None:
        self.sample_rate = sample_rate
        self._device_spec = device
        self.device_index: Optional[int] = None
        self.device_name: Optional[str] = None
        self._sd = None
        self._np = None
        self._thread_q: _queue.Queue[bytes] = _queue.Queue(maxsize=200)
        self._async_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self._stream = None
        self._reader_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._closed = False
        self.last_rms: float = 0.0

    def start(self) -> None:
        if self._stream is not None:
            return
        self._sd, self._np = _import_sounddevice()
        self.device_index, self.device_name = _resolve_device(
            self._sd, self._device_spec, "input")
        try:
            self._stream = self._sd.RawInputStream(
                samplerate=self.sample_rate,
                channels=CHANNELS,
                dtype="int16",
                blocksize=FRAME_SAMPLES,
                callback=self._cb,
                device=self.device_index,
            )
            self._stream.start()
        except Exception as e:
            raise AudioUnavailable(f"could not open input stream: {e}") from e
        self._loop = asyncio.get_running_loop()
        self._reader_task = asyncio.create_task(self._reader())
        log.info("mic started @ %d Hz device=%s idx=%s",
                 self.sample_rate, self.device_name, self.device_index)

    def _cb(self, indata, frames, time_info, status) -> None:
        if status:
            log.debug("mic status: %s", status)
        try:
            buf = bytes(indata)
            try:
                arr = self._np.frombuffer(buf, dtype=self._np.int16).astype(self._np.float32)
                if arr.size:
                    rms = float((arr * arr).mean()) ** 0.5
                    self.last_rms = min(1.0, rms / 32768.0)
            except Exception:
                pass
            try:
                self._thread_q.put_nowait(buf)
            except _queue.Full:
                pass
        except Exception:
            log.exception("mic callback raised")

    async def _reader(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            while not self._closed:
                buf = await loop.run_in_executor(None, self._blocking_get)
                if buf is None:
                    return
                try:
                    self._async_q.put_nowait(buf)
                except asyncio.QueueFull:
                    try:
                        self._async_q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        self._async_q.put_nowait(buf)
                    except asyncio.QueueFull:
                        pass
        except asyncio.CancelledError:
            return

    def _blocking_get(self) -> Optional[bytes]:
        try:
            return self._thread_q.get(timeout=0.5)
        except _queue.Empty:
            return b""

    async def get(self) -> bytes:
        return await self._async_q.get()

    async def stop(self) -> None:
        self._closed = True
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        log.info("mic stopped")


class SpeakerPlayback:
    def __init__(self, sample_rate: int = SAMPLE_RATE,
                 device: object = None) -> None:
        self.sample_rate = sample_rate
        self._device_spec = device
        self.device_index: Optional[int] = None
        self.device_name: Optional[str] = None
        self._sd = None
        self._np = None
        self._thread_q: _queue.Queue[bytes] = _queue.Queue(maxsize=400)
        self._stream = None
        self._buffer = bytearray()
        self._buffer_lock = threading.Lock()
        self._closed = False
        self._is_speaking = False
        self.last_rms: float = 0.0

    def start(self) -> None:
        if self._stream is not None:
            return
        self._sd, self._np = _import_sounddevice()
        self.device_index, self.device_name = _resolve_device(
            self._sd, self._device_spec, "output")
        try:
            self._stream = self._sd.RawOutputStream(
                samplerate=self.sample_rate,
                channels=CHANNELS,
                dtype="int16",
                blocksize=FRAME_SAMPLES,
                callback=self._cb,
                device=self.device_index,
            )
            self._stream.start()
        except Exception as e:
            raise AudioUnavailable(f"could not open output stream: {e}") from e
        log.info("speaker started @ %d Hz device=%s idx=%s",
                 self.sample_rate, self.device_name, self.device_index)

    def _cb(self, outdata, frames, time_info, status) -> None:
        if status:
            log.debug("spk status: %s", status)
        need = frames * SAMPLE_WIDTH
        with self._buffer_lock:
            while len(self._buffer) < need:
                try:
                    chunk = self._thread_q.get_nowait()
                except _queue.Empty:
                    break
                self._buffer.extend(chunk)
            if len(self._buffer) >= need:
                chunk = bytes(self._buffer[:need])
                outdata[:need] = chunk
                del self._buffer[:need]
                self._is_speaking = True
            else:
                give = len(self._buffer)
                if give:
                    chunk = bytes(self._buffer[:give])
                    outdata[:give] = chunk
                    self._buffer.clear()
                else:
                    chunk = b""
                outdata[give:need] = b"\x00" * (need - give)
                self._is_speaking = give > 0
        if self._np is not None:
            try:
                if chunk:
                    arr = self._np.frombuffer(chunk, dtype=self._np.int16).astype(
                        self._np.float32)
                    if arr.size:
                        rms = float((arr * arr).mean()) ** 0.5
                        self.last_rms = min(1.0, rms / 32768.0)
                    else:
                        self.last_rms = 0.0
                else:
                    self.last_rms = 0.0
            except Exception:
                pass

    def play(self, pcm16: bytes) -> None:
        if not pcm16:
            return
        try:
            self._thread_q.put_nowait(pcm16)
        except _queue.Full:
            try:
                self._thread_q.get_nowait()
            except _queue.Empty:
                pass
            try:
                self._thread_q.put_nowait(pcm16)
            except _queue.Full:
                pass

    def clear(self) -> None:
        with self._buffer_lock:
            self._buffer.clear()
        try:
            while True:
                self._thread_q.get_nowait()
        except _queue.Empty:
            pass

    @property
    def is_speaking(self) -> bool:
        return self._is_speaking

    async def stop(self) -> None:
        self._closed = True
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        log.info("speaker stopped")


def list_devices() -> dict:
    out: dict = {"inputs": [], "outputs": [], "default_input_idx": None,
                 "default_output_idx": None, "error": None}
    try:
        sd, _ = _import_sounddevice()
    except AudioUnavailable as e:
        out["error"] = str(e)
        return out
    try:
        devs = sd.query_devices()
    except Exception as e:
        out["error"] = str(e)
        return out
    for i, d in enumerate(devs):
        if d["max_input_channels"] > 0:
            out["inputs"].append({
                "index": i, "name": d["name"],
                "channels": d["max_input_channels"],
                "default_samplerate": d.get("default_samplerate"),
            })
        if d["max_output_channels"] > 0:
            out["outputs"].append({
                "index": i, "name": d["name"],
                "channels": d["max_output_channels"],
                "default_samplerate": d.get("default_samplerate"),
            })
    default = sd.default.device
    if isinstance(default, (list, tuple)) and len(default) >= 2:
        out["default_input_idx"] = default[0] if isinstance(default[0], int) else None
        out["default_output_idx"] = default[1] if isinstance(default[1], int) else None
    elif isinstance(default, int):
        out["default_input_idx"] = default
        out["default_output_idx"] = default
    return out


def check_devices() -> dict:
    out = {"input_ok": False, "output_ok": False}
    try:
        sd, _ = _import_sounddevice()
    except AudioUnavailable as e:
        out["error"] = str(e)
        return out
    try:
        sd.check_input_settings(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16")
        out["input_ok"] = True
    except Exception as e:
        out["input_error"] = str(e)
    try:
        sd.check_output_settings(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16")
        out["output_ok"] = True
    except Exception as e:
        out["output_error"] = str(e)
    return out
