"""voice — gpt-realtime-2 voice interface node for the LATTICE mesh.

Registers as 'voice' (interface kind), exposes:
  - tool surfaces: start_session, stop_session, session_status (request_response)
  - inbox surfaces: speak, tell (fire_and_forget)

Runs natively on the Mac mini because it needs PortAudio mic + speaker access
through sounddevice — Docker can't reach the host audio bus.

Surface semantics:
  voice.speak   — verbatim: model is instructed to read the text aloud
                  exactly, no commentary. Queued (max 50) if no session.
  voice.tell    — conversational: text is injected as a user-role turn
                  tagged `[from: <source>]`; the model reacts naturally.
                  Same queue behavior as speak.
  voice.start_session — open a Realtime API ws and start mic/speaker IO.
  voice.stop_session  — tear down current session.
  voice.session_status — report whether a session is live and basic stats.

Inspector UI: http://localhost:8807 — voice/device pickers, start/stop,
live transcript, queue view. SSE-driven, aiohttp app same as the
reference impl in raven-mesh-nodes/voice_actor.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import pathlib
import signal
import sys
import time
import uuid
from typing import Optional

import aiohttp
from aiohttp import web

from audio_io import (AudioUnavailable, MicCapture, SpeakerPlayback,
                      check_devices, list_devices)
from realtime_client import RealtimeClient, decode_audio_delta


log = logging.getLogger("voice")

NODE_ID = "voice"
CORE_URL = os.environ.get("CORE_URL", "http://127.0.0.1:8000").rstrip("/")
SECRET_RAW = os.environ.get("VOICE_SECRET")
if not SECRET_RAW:
    print("[voice] FATAL: VOICE_SECRET not set", file=sys.stderr)
    sys.exit(1)
SECRET = SECRET_RAW.encode()

DEFAULT_VOICE = "alloy"
DEFAULT_MODEL = os.environ.get("VOICE_MODEL", "gpt-realtime-2")
INSPECTOR_HOST = os.environ.get("VOICE_INSPECTOR_HOST", "127.0.0.1")
INSPECTOR_PORT = int(os.environ.get("VOICE_INSPECTOR_PORT", "8807"))
SUPPORTED_VOICES = [
    "alloy", "marin", "cedar", "shimmer",
    "ash", "coral", "sage", "verse",
]
TRANSCRIPT_MAX = 100
QUEUE_MAX = 50
METER_PUSH_HZ = 20

HTML_PATH = pathlib.Path(__file__).resolve().parent / "web" / "index.html"
API_KEYS_PATH = pathlib.Path.home() / "raven" / "config" / "api_keys.json"


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical(env: dict) -> bytes:
    body = {k: v for k, v in env.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def sign(env: dict) -> str:
    return hmac.new(SECRET, canonical(env), hashlib.sha256).hexdigest()


def load_openai_key() -> Optional[str]:
    """Return OpenAI API key from env or RAVEN api_keys.json. None if absent."""
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    try:
        with open(API_KEYS_PATH) as f:
            blob = json.load(f)
        # Accept either {"openai":{"api_key":...}} or flat {"openai_api_key":...}.
        if isinstance(blob, dict):
            v = blob.get("openai")
            if isinstance(v, dict):
                k = v.get("api_key")
                if isinstance(k, str) and k.strip():
                    return k.strip()
            v2 = blob.get("openai_api_key")
            if isinstance(v2, str) and v2.strip():
                return v2.strip()
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("api_keys.json read failed: %s", e)
    return None


# ---------------------------------------------------------------------
# Pending speak/tell queue (drained at next session start)
# ---------------------------------------------------------------------

class PendingQueue:
    """Bounded FIFO of {kind: 'speak'|'tell', text, source, ts}.

    Drop oldest on overflow. Drained when a session is started so the
    new session immediately handles whatever was waiting.
    """

    def __init__(self, maxlen: int = QUEUE_MAX) -> None:
        self._q: collections.deque = collections.deque(maxlen=maxlen)

    def push(self, kind: str, text: str, source: Optional[str]) -> None:
        self._q.append({
            "kind": kind,
            "text": text,
            "source": source,
            "ts": now_iso(),
        })

    def drain(self) -> list[dict]:
        items = list(self._q)
        self._q.clear()
        return items

    def snapshot(self) -> list[dict]:
        return list(self._q)

    def __len__(self) -> int:
        return len(self._q)


# ---------------------------------------------------------------------
# Realtime session
# ---------------------------------------------------------------------

class Session:
    def __init__(self, *, voice: str, system_prompt: Optional[str],
                 on_user_transcript_target: Optional[str],
                 api_key: str, model: str, owner: "VoiceNode",
                 audio_input_device: object = None,
                 audio_output_device: object = None) -> None:
        self.id = f"vs_{uuid.uuid4().hex[:12]}"
        self.voice = voice
        self.system_prompt = system_prompt
        self.on_user_transcript_target = on_user_transcript_target
        self.started_at = now_iso()
        self._started_monotonic = time.monotonic()
        self.last_user_transcript: Optional[str] = None
        self.last_assistant_transcript: Optional[str] = None
        self.error: Optional[str] = None
        self._owner = owner
        self.client = RealtimeClient(api_key=api_key, model=model)
        self.mic = MicCapture(device=audio_input_device)
        self.spk = SpeakerPlayback(device=audio_output_device)
        self._mic_task: Optional[asyncio.Task] = None
        self._evt_task: Optional[asyncio.Task] = None
        self._meter_task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()
        self.mesh_targets: dict = {}
        self.input_device_ok = False
        self.output_device_ok = False

    @property
    def uptime_seconds(self) -> float:
        return time.monotonic() - self._started_monotonic

    async def start(self) -> None:
        try:
            self.spk.start()
            self.output_device_ok = True
        except AudioUnavailable as e:
            log.warning("speaker unavailable: %s", e)
            self.output_device_ok = False
        try:
            self.mic.start()
            self.input_device_ok = True
        except AudioUnavailable as e:
            log.warning("mic unavailable: %s", e)
            self.input_device_ok = False

        self.mesh_targets, tools = await self._owner._build_mesh_tools()
        instructions = self._build_instructions(self.mesh_targets)

        cfg: dict = {
            "type": "realtime",
            "model": self.client.model,
            "output_modalities": ["audio"],
            "instructions": instructions,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "transcription": {"model": "gpt-4o-transcribe"},
                    "turn_detection": {"type": "server_vad"},
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "voice": self.voice,
                },
            },
        }
        if tools:
            cfg["tools"] = tools
            cfg["tool_choice"] = "auto"
        self.client.session_config = cfg
        await self.client.connect()

        self._evt_task = asyncio.create_task(self._event_loop())
        if self.input_device_ok:
            self._mic_task = asyncio.create_task(self._mic_loop())
        self._meter_task = asyncio.create_task(self._meter_loop())

    async def stop(self) -> None:
        self._stopped.set()
        try:
            await self.client.close()
        except Exception:
            pass
        for t in (self._mic_task, self._evt_task, self._meter_task):
            if t is not None and not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        self._mic_task = None
        self._evt_task = None
        self._meter_task = None
        try:
            await self.mic.stop()
        except Exception:
            pass
        try:
            await self.spk.stop()
        except Exception:
            pass

    async def speak_verbatim(self, text: str) -> None:
        """Have the model read `text` aloud exactly. Used by voice.speak."""
        await self.client.add_system_item(text)
        await self.client.speak_verbatim(text, voice=self.voice)

    async def tell_conversational(self, text: str, source: Optional[str]) -> None:
        """Inject `text` as a user-role turn; let the model react."""
        tagged = f"[from: {source}] {text}" if source else text
        await self.client.add_user_item(tagged)
        await self.client.create_response()

    def _build_instructions(self, targets: dict) -> str:
        lines = [
            f"You are the voice surface of LATTICE node `{NODE_ID}`. Colton "
            "speaks into a microphone and hears your replies through the "
            "Mac mini speakers. Talk naturally — concise, friendly, dry. "
            "Do not narrate what you are doing.",
            "",
            "You are part of a larger mesh of cooperating nodes. Other nodes "
            "can do things you cannot — run code, edit files, drive UIs, ask "
            "humans for approval. When a request needs more than conversation, "
            "hand it off to the right node by calling the matching tool below. "
            "After dispatching, briefly tell the user what you sent and to "
            "whom. Don't fabricate results — wait for the node to respond "
            "(it may come back as a follow-up turn injected by the system).",
        ]
        if targets:
            lines.append("")
            lines.append("Available mesh tools:")
            for name, info in targets.items():
                if info["type"] == "inbox":
                    lines.append(
                        f"  - {name}(text): hand off to "
                        f"{info['node']}.{info['surface']} (fire-and-forget)."
                    )
                else:
                    lines.append(
                        f"  - {name}(payload): invoke "
                        f"{info['node']}.{info['surface']} and use the response."
                    )
        else:
            lines.append("")
            lines.append("(No mesh tools available — conversational only.)")
        if self.system_prompt:
            lines.append("")
            lines.append("Operator-supplied instructions:")
            lines.append(self.system_prompt)
        return "\n".join(lines)

    # ---------------- event handling ----------------

    async def _meter_loop(self) -> None:
        period = 1.0 / METER_PUSH_HZ
        last_mic = -1.0
        last_spk = -1.0
        try:
            while not self._stopped.is_set():
                await asyncio.sleep(period)
                mic = float(getattr(self.mic, "last_rms", 0.0))
                spk = float(getattr(self.spk, "last_rms", 0.0))
                if abs(mic - last_mic) > 0.005 or abs(spk - last_spk) > 0.005:
                    last_mic, last_spk = mic, spk
                    await self._owner.push()
        except asyncio.CancelledError:
            return

    async def _mic_loop(self) -> None:
        try:
            while not self._stopped.is_set() and not self.client.closed:
                buf = await self.mic.get()
                if not buf:
                    continue
                try:
                    await self.client.append_audio(buf)
                except Exception as e:
                    log.warning("append_audio failed: %s", e)
                    return
        except asyncio.CancelledError:
            return

    async def _event_loop(self) -> None:
        try:
            async for evt in self.client.events():
                if self._stopped.is_set():
                    return
                await self._handle_event(evt)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("realtime event loop crashed")
            self.error = "event_loop_crashed"
        finally:
            await self._owner._on_session_ended(self)

    async def _handle_event(self, evt: dict) -> None:
        et = evt.get("type", "")
        if et in ("response.audio.delta", "response.output_audio.delta"):
            delta = evt.get("delta")
            if delta:
                try:
                    self.spk.play(decode_audio_delta(delta))
                except Exception as e:
                    log.debug("audio decode failed: %s", e)
        elif et == "conversation.item.input_audio_transcription.completed":
            transcript = (evt.get("transcript") or "").strip()
            if transcript:
                self.last_user_transcript = transcript
                self._owner._record("user", transcript)
                await self._forward_user_transcript(transcript)
        elif et in ("response.audio_transcript.done", "response.output_audio_transcript.done"):
            transcript = (evt.get("transcript") or "").strip()
            if transcript:
                self.last_assistant_transcript = transcript
                self._owner._record("assistant", transcript)
        elif et == "input_audio_buffer.speech_started":
            self.spk.clear()  # barge-in
            self._owner._set_status("listening")
        elif et == "input_audio_buffer.speech_stopped":
            self._owner._set_status("processing")
        elif et in ("response.created", "response.output_item.added"):
            self._owner._set_status("speaking")
        elif et == "response.done":
            self._owner._set_status("listening" if self.input_device_ok else "idle")
        elif et == "response.function_call_arguments.done":
            call_id = evt.get("call_id") or ""
            name = evt.get("name") or ""
            args = evt.get("arguments") or "{}"
            log.info("fn call: %s args=%s", name, args[:160])
            asyncio.create_task(self._handle_function_call(call_id, name, args))
        elif et == "error":
            err = evt.get("error", {})
            log.warning("realtime error: %s", err)
            self.error = err.get("message") or json.dumps(err)[:200]
            self._owner._set_status("error")
        else:
            log.debug("realtime event: %s", et)

    async def _forward_user_transcript(self, text: str) -> None:
        target = self.on_user_transcript_target
        if not target:
            return
        payload = {
            "from": NODE_ID,
            "kind": "voice_transcript",
            "role": "user",
            "text": text,
            "session_id": self.id,
            "timestamp": now_iso(),
        }
        ok, body = await self._owner.mesh_invoke(target, payload)
        if not ok:
            log.warning("forward user transcript -> %s failed: %s", target, body[:200])

    async def _handle_function_call(self, call_id: str, name: str,
                                    arguments_json: str) -> None:
        info = self.mesh_targets.get(name)
        if not info:
            output = {"error": f"unknown tool: {name}"}
        else:
            try:
                args = json.loads(arguments_json) if arguments_json else {}
            except json.JSONDecodeError:
                args = {}
            try:
                if info["type"] == "inbox":
                    payload = {
                        "from": NODE_ID,
                        "kind": "voice_handoff",
                        "message": args.get("text", ""),
                        "text": args.get("text", ""),
                        "session_id": self.id,
                        "timestamp": now_iso(),
                    }
                    ok, body = await self._owner.mesh_invoke(info["target"], payload)
                    output = {"ok": ok, "delivered_to": info["target"], "ack": body[:200]}
                else:
                    payload = args.get("payload") or {}
                    ok, body = await self._owner.mesh_invoke(info["target"], payload)
                    output = {"ok": ok, "delivered_to": info["target"], "ack": body[:200]}
            except Exception as e:  # noqa: BLE001
                output = {"error": True, "message": str(e)}

        self._owner._record("tool", f"{name} -> {json.dumps(output, default=str)[:200]}")
        try:
            await self.client.send_raw({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(output, default=str),
                },
            })
            await self.client.create_response()
        except Exception as e:
            log.warning("posting function_call_output failed: %s", e)


# ---------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------

class VoiceNode:
    def __init__(self, http: aiohttp.ClientSession, api_key: Optional[str]) -> None:
        self.http = http
        self.api_key = api_key
        self.model = DEFAULT_MODEL
        self.session: Optional[Session] = None
        self.status = "idle"
        self.transcript_log: list[dict] = []
        self.pending = PendingQueue(maxlen=QUEUE_MAX)
        self.subscribers: set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()

    # ---------- mesh wiring ----------

    async def mesh_invoke(self, to: str, payload: dict) -> tuple[bool, str]:
        msg_id = str(uuid.uuid4())
        env = {
            "id": msg_id,
            # Pre-fill correlation_id BEFORE signing — Core's _route_invocation
            # calls env.setdefault("correlation_id", id) pre-verify; if we omit
            # it Core mutates the body and HMAC mismatches -> 401 bad_signature.
            "correlation_id": msg_id,
            "from": NODE_ID,
            "to": to,
            "kind": "invocation",
            "payload": payload,
            "timestamp": now_iso(),
        }
        env["signature"] = sign(env)
        try:
            async with self.http.post(f"{CORE_URL}/v0/invoke", json=env) as r:
                body = await r.text()
                return r.status in (200, 202), body
        except Exception as e:  # noqa: BLE001
            return False, f"mesh_invoke crash: {e!r}"

    async def send_response(self, env_in: dict, payload: dict, kind: str = "response") -> None:
        """Reply to a request_response invocation via /v0/respond."""
        corr = env_in.get("correlation_id") or env_in.get("id")
        resp = {
            "id": str(uuid.uuid4()),
            "correlation_id": corr,
            "from": NODE_ID,
            "to": env_in.get("from"),
            "kind": kind,
            "payload": payload,
            "timestamp": now_iso(),
        }
        resp["signature"] = sign(resp)
        try:
            async with self.http.post(f"{CORE_URL}/v0/respond", json=resp) as r:
                body = await r.text()
                if r.status not in (200, 202):
                    log.warning("respond corr=%s status=%s body=%s",
                                str(corr)[:8], r.status, body[:200])
        except Exception as e:  # noqa: BLE001
            log.warning("send_response crash: %s", e)

    async def _build_mesh_tools(self) -> tuple[dict, list[dict]]:
        """Query Core /v0/introspect and build Realtime function tools."""
        try:
            async with self.http.get(f"{CORE_URL}/v0/introspect") as r:
                data = await r.json()
        except Exception as e:
            log.warning("introspect failed; running tool-less: %s", e)
            return {}, []
        node_index = {n["id"]: n for n in data.get("nodes", [])}
        targets: dict = {}
        tools: list[dict] = []
        for edge in data.get("relationships", []):
            if edge.get("from") != NODE_ID:
                continue
            target = edge.get("to", "")
            target_node, _, surface_name = target.partition(".")
            if target_node == NODE_ID:
                continue
            ndecl = node_index.get(target_node, {})
            sdecl = next(
                (s for s in ndecl.get("surfaces", []) if s["name"] == surface_name),
                {},
            )
            stype = sdecl.get("type")
            mode = sdecl.get("invocation_mode")
            tool_name = f"send_{target_node}_{surface_name}"
            if stype == "inbox":
                desc = (
                    f"Send a free-form text message to {target}'s inbox "
                    f"({ndecl.get('kind','node')}). Fire-and-forget — no "
                    "immediate reply. Use this to hand off a task that "
                    "needs reasoning, code, or external action beyond what "
                    "you can do as the voice."
                )
                params = {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string",
                                 "description": "Message body — phrase as a task or question."},
                    },
                    "required": ["text"],
                }
            elif stype == "tool" and mode == "request_response":
                desc = (
                    f"Invoke the {target} tool surface and return its "
                    "response. Use only when the user explicitly asks "
                    "for that capability."
                )
                params = {
                    "type": "object",
                    "properties": {
                        "payload": {"type": "object",
                                    "description": "Surface input."},
                    },
                    "required": ["payload"],
                }
            else:
                continue
            targets[tool_name] = {
                "target": target, "mode": mode, "type": stype,
                "node": target_node, "surface": surface_name,
            }
            tools.append({"type": "function", "name": tool_name,
                          "description": desc, "parameters": params})
        return targets, tools

    # ---------- surface dispatch ----------

    async def dispatch(self, env: dict) -> None:
        to = env.get("to", "")
        _, _, surface = to.partition(".")
        if surface == "start_session":
            result = await self.handle_start_session(env)
            await self.send_response(env, result)
        elif surface == "stop_session":
            result = await self.handle_stop_session(env)
            await self.send_response(env, result)
        elif surface == "session_status":
            result = await self.handle_session_status(env)
            await self.send_response(env, result)
        elif surface == "speak":
            await self.handle_speak(env)
        elif surface == "tell":
            await self.handle_tell(env)
        else:
            log.warning("dispatch: unknown surface %r", surface)

    # ---------- handlers ----------

    async def handle_start_session(self, env: dict) -> dict:
        if not self.api_key:
            return {"error": "openai_key_missing",
                    "detail": "set OPENAI_API_KEY or populate ~/raven/config/api_keys.json"}
        body = env.get("payload") or {}
        voice = body.get("voice") or DEFAULT_VOICE
        if voice not in SUPPORTED_VOICES:
            log.warning("voice %r not in canonical list; passing through anyway", voice)
        system_prompt = body.get("system_prompt")
        user_transcript_target = body.get("on_user_transcript_target")
        in_dev = body.get("audio_input_device")
        out_dev = body.get("audio_output_device")
        if isinstance(in_dev, str) and in_dev.lstrip("-").isdigit():
            in_dev = int(in_dev)
        if isinstance(out_dev, str) and out_dev.lstrip("-").isdigit():
            out_dev = int(out_dev)

        async with self._lock:
            if self.session is not None:
                log.info("replacing active session %s", self.session.id)
                old = self.session
                self.session = None
                try:
                    await old.stop()
                except Exception:
                    log.exception("old session stop raised")

            sess = Session(
                voice=voice,
                system_prompt=system_prompt,
                on_user_transcript_target=user_transcript_target,
                api_key=self.api_key,
                model=self.model,
                owner=self,
                audio_input_device=in_dev,
                audio_output_device=out_dev,
            )
            try:
                await sess.start()
            except Exception as e:
                log.exception("session start failed")
                try:
                    await sess.stop()
                except Exception:
                    pass
                return {"error": "session_start_failed", "detail": str(e)[:300]}
            self.session = sess
            self._set_status("listening" if sess.input_device_ok else "idle")
            await self.push()

        # Flush any queued speak/tell items.
        pending = self.pending.drain()
        if pending:
            asyncio.create_task(self._flush_pending(sess, pending))

        return {
            "session_id": sess.id,
            "voice": sess.voice,
            "model": self.model,
            "mic_device": sess.mic.device_name,
            "speaker_device": sess.spk.device_name,
            "input_device_ok": sess.input_device_ok,
            "output_device_ok": sess.output_device_ok,
        }

    async def _flush_pending(self, sess: Session, items: list[dict]) -> None:
        # Tiny stagger so the session has a chance to finish session.update
        # before the first response.create lands.
        await asyncio.sleep(0.25)
        for item in items:
            try:
                if item["kind"] == "speak":
                    await sess.speak_verbatim(item["text"])
                else:
                    await sess.tell_conversational(item["text"], item.get("source"))
            except Exception:
                log.exception("flushing pending %s failed", item.get("kind"))

    async def handle_stop_session(self, env: dict) -> dict:
        async with self._lock:
            sess = self.session
            if sess is None:
                return {"stopped": False, "reason": "no_active_session"}
            self.session = None
            try:
                await sess.stop()
            except Exception:
                log.exception("stop_session: stop raised")
            self._set_status("idle")
            await self.push()
            return {"stopped": True, "session_id": sess.id}

    async def handle_session_status(self, env: dict) -> dict:
        s = self.session
        return {
            "active": s is not None,
            "status": self.status,
            "key_present": bool(self.api_key),
            "session_id": s.id if s else None,
            "voice": s.voice if s else None,
            "model": self.model,
            "mic_device": s.mic.device_name if s else None,
            "speaker_device": s.spk.device_name if s else None,
            "uptime_seconds": (s.uptime_seconds if s else None),
            "pending_queue_size": len(self.pending),
            "last_user_transcript": s.last_user_transcript if s else None,
            "last_assistant_transcript": s.last_assistant_transcript if s else None,
        }

    async def handle_speak(self, env: dict) -> None:
        payload = env.get("payload") or {}
        text = payload.get("text") or payload.get("message") or ""
        source = payload.get("source") or env.get("from")
        if not isinstance(text, str) or not text.strip():
            log.warning("speak: empty text from %r", env.get("from"))
            return
        sess = self.session
        if sess is None:
            self.pending.push("speak", text, source)
            log.info("speak queued (no session): %r", text[:80])
            await self.push()
            return
        try:
            await sess.speak_verbatim(text)
        except Exception:
            log.exception("speak failed; re-queueing")
            self.pending.push("speak", text, source)
            await self.push()

    async def handle_tell(self, env: dict) -> None:
        payload = env.get("payload") or {}
        text = payload.get("text") or payload.get("message") or ""
        source = payload.get("source") or env.get("from")
        if not isinstance(text, str) or not text.strip():
            log.warning("tell: empty text from %r", env.get("from"))
            return
        sess = self.session
        if sess is None:
            self.pending.push("tell", text, source)
            log.info("tell queued (no session): %r from=%s", text[:80], source)
            await self.push()
            return
        try:
            await sess.tell_conversational(text, source)
        except Exception:
            log.exception("tell failed; re-queueing")
            self.pending.push("tell", text, source)
            await self.push()

    # ---------- state / SSE ----------

    def state(self) -> dict:
        s = self.session
        return {
            "node_id": NODE_ID,
            "model": self.model,
            "key_present": bool(self.api_key),
            "devices": check_devices(),
            "status": self.status,
            "active": s is not None,
            "supported_voices": SUPPORTED_VOICES,
            "session": {
                "id": s.id,
                "voice": s.voice,
                "system_prompt": s.system_prompt,
                "on_user_transcript_target": s.on_user_transcript_target,
                "started_at": s.started_at,
                "uptime_seconds": s.uptime_seconds,
                "last_user_transcript": s.last_user_transcript,
                "last_assistant_transcript": s.last_assistant_transcript,
                "input_device_ok": s.input_device_ok,
                "output_device_ok": s.output_device_ok,
                "input_device_name": getattr(s.mic, "device_name", None),
                "output_device_name": getattr(s.spk, "device_name", None),
                "error": s.error,
                "mic_rms": getattr(s.mic, "last_rms", 0.0),
                "spk_rms": getattr(s.spk, "last_rms", 0.0),
            } if s else None,
            "transcript": list(self.transcript_log),
            "pending_queue": self.pending.snapshot(),
        }

    async def push(self) -> None:
        snap = self.state()
        for q in list(self.subscribers):
            try:
                q.put_nowait(snap)
            except asyncio.QueueFull:
                pass

    def _set_status(self, status: str) -> None:
        if self.status != status:
            self.status = status
            asyncio.create_task(self.push())

    def _record(self, role: str, text: str) -> None:
        entry = {"role": role, "text": text, "timestamp": now_iso()}
        self.transcript_log.append(entry)
        del self.transcript_log[: max(0, len(self.transcript_log) - TRANSCRIPT_MAX)]
        asyncio.create_task(self.push())

    async def _on_session_ended(self, sess: Session) -> None:
        if self.session is sess:
            log.info("session %s ended", sess.id)
            self.session = None
            self._set_status("idle")
            await self.push()


# ---------------------------------------------------------------------
# Inspector web app
# ---------------------------------------------------------------------

def make_web_app(node: VoiceNode) -> web.Application:
    app = web.Application()

    async def index(request: web.Request) -> web.Response:
        try:
            return web.Response(text=HTML_PATH.read_text(), content_type="text/html")
        except FileNotFoundError:
            return web.Response(text="inspector html missing", status=500)

    async def state(request: web.Request) -> web.Response:
        return web.json_response(node.state())

    async def events(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(status=200, headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
        })
        await response.prepare(request)
        queue: asyncio.Queue = asyncio.Queue()
        node.subscribers.add(queue)
        try:
            await response.write(
                f"event: state\ndata: {json.dumps(node.state())}\n\n".encode()
            )
            while True:
                try:
                    snap = await asyncio.wait_for(queue.get(), timeout=2)
                except asyncio.TimeoutError:
                    snap = node.state()
                try:
                    await response.write(
                        f"event: state\ndata: {json.dumps(snap)}\n\n".encode()
                    )
                except (ConnectionResetError, BrokenPipeError):
                    break
        finally:
            node.subscribers.discard(queue)
        return response

    async def http_start(request: web.Request) -> web.Response:
        body = await request.json()
        env = {"payload": body, "from": "inspector"}
        result = await node.handle_start_session(env)
        return web.json_response(result)

    async def http_stop(request: web.Request) -> web.Response:
        result = await node.handle_stop_session({"payload": {}, "from": "inspector"})
        return web.json_response(result)

    async def http_status(request: web.Request) -> web.Response:
        result = await node.handle_session_status({"payload": {}, "from": "inspector"})
        return web.json_response(result)

    async def http_devices(request: web.Request) -> web.Response:
        return web.json_response(list_devices())

    app.router.add_get("/", index)
    app.router.add_get("/state", state)
    app.router.add_get("/events", events)
    app.router.add_get("/api/devices", http_devices)
    app.router.add_post("/api/start", http_start)
    app.router.add_post("/api/stop", http_stop)
    app.router.add_get("/api/status", http_status)
    return app


# ---------------------------------------------------------------------
# Main loop: register, then drain the SSE deliver stream
# ---------------------------------------------------------------------

async def main_async() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )
    api_key = load_openai_key()
    if not api_key:
        log.warning("OPENAI_API_KEY not set; node will register but tools will return openai_key_missing")

    async with aiohttp.ClientSession() as http:
        node = VoiceNode(http=http, api_key=api_key)

        reg = {"node_id": NODE_ID, "timestamp": now_iso()}
        reg["signature"] = sign(reg)
        async with http.post(f"{CORE_URL}/v0/register", json=reg) as r:
            if r.status != 200:
                body = await r.text()
                print(f"[voice] register failed: {r.status} {body}",
                      file=sys.stderr, flush=True)
                sys.exit(1)
            reg_resp = await r.json()
        session_id = reg_resp["session_id"]
        print(
            f"[voice] registered session={session_id[:8]} model={DEFAULT_MODEL} "
            f"key_present={bool(api_key)} inspector=http://{INSPECTOR_HOST}:{INSPECTOR_PORT}",
            flush=True,
        )

        # Spin up the inspector web app.
        web_app = make_web_app(node)
        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, INSPECTOR_HOST, INSPECTOR_PORT)
        await site.start()

        stop_evt = asyncio.Event()
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_evt.set)
            except NotImplementedError:
                pass

        stream_task = asyncio.create_task(_drain_stream(http, node, session_id))
        await stop_evt.wait()

        stream_task.cancel()
        try:
            await stream_task
        except (asyncio.CancelledError, Exception):
            pass
        if node.session is not None:
            try:
                await node.session.stop()
            except Exception:
                pass
        await runner.cleanup()


async def _drain_stream(http: aiohttp.ClientSession, node: VoiceNode,
                        session_id: str) -> None:
    async with http.get(
        f"{CORE_URL}/v0/stream",
        params={"session": session_id},
        timeout=aiohttp.ClientTimeout(total=None),
    ) as r:
        event_type: Optional[str] = None
        buf: list[str] = []
        async for raw in r.content:
            line = raw.decode().rstrip("\r\n")
            if line == "":
                if event_type == "deliver" and buf:
                    try:
                        data = json.loads("\n".join(buf))
                        await node.dispatch(data)
                    except Exception as e:  # noqa: BLE001
                        print(f"[voice] dispatch crashed: {e!r}",
                              file=sys.stderr, flush=True)
                event_type, buf = None, []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                buf.append(line[5:].lstrip())


def main() -> int:
    global CORE_URL, INSPECTOR_HOST, INSPECTOR_PORT
    p = argparse.ArgumentParser()
    p.add_argument("--core-url", default=CORE_URL)
    p.add_argument("--inspector-host", default=INSPECTOR_HOST)
    p.add_argument("--inspector-port", type=int, default=INSPECTOR_PORT)
    args = p.parse_args()
    CORE_URL = args.core_url.rstrip("/")
    INSPECTOR_HOST = args.inspector_host
    INSPECTOR_PORT = args.inspector_port
    asyncio.run(main_async())
    return 0


if __name__ == "__main__":
    sys.exit(main())
