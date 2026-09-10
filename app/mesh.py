"""Owns the single serial link to the Heltec companion radio.

Only one process can hold /dev/ttyACM0, so every request in the app funnels
through the one MeshManager instance here. Radio commands are serialised with
a lock -- the firmware answers one request at a time and interleaving them
produces cross-matched replies.
"""
import asyncio
import contextlib
import io
import json
import os
import logging
import re
import shlex
import time
from typing import Any, Callable

from meshcore import MeshCore

log = logging.getLogger("meshweb.mesh")

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# meshcli verbs that block forever or need a real terminal; refused in the web console.
CLI_BLOCKLIST = {
    "wait_key", "wk", "script", "msgs_subscribe", "ms", "wait_msg", "wm",
    "wait_ack", "wa", "sleep", "s", "handler_attach", "handler_detach",
}

# MeshCore advert types.
ADV_CHAT, ADV_REPEATER, ADV_ROOM, ADV_SENSOR = 1, 2, 3, 4
INFRA_TYPES = {ADV_REPEATER, ADV_ROOM}


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def jsonable(obj):
    """Make radio payloads safe to serialise.

    Several MeshCore events carry raw bytes (packet blobs, pubkeys, LPP frames).
    Those are not valid UTF-8, so letting them reach the JSON encoder raises
    UnicodeDecodeError and takes out the whole response -- or the websocket.
    Bytes become hex, which is what you actually want to read anyway.
    """
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex()
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


class PasswordStore:
    """Repeater passwords, kept on disk so they survive a container restart.

    Stored in plaintext: the radio protocol needs the password itself, so a
    hash is useless here, and any key used to encrypt it would have to sit
    beside it. The file is 0600 and lives in a named volume; treat the Pi as
    the trust boundary.
    """

    def __init__(self, path: str):
        self.path = path
        self._data: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path) as f:
                self._data = json.load(f)
        except FileNotFoundError:
            self._data = {}
        except Exception:
            log.exception("could not read %s; starting empty", self.path)
            self._data = {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._data, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def get(self, pubkey: str) -> str | None:
        return self._data.get(pubkey)

    def set(self, pubkey: str, pwd: str) -> None:
        self._data[pubkey] = pwd
        self._save()

    def clear(self, pubkey: str) -> bool:
        if self._data.pop(pubkey, None) is None:
            return False
        self._save()
        return True


class MessageStore:
    """Chat history. The radio only holds unread messages, so keep our own."""

    def __init__(self, path: str, cap: int = 2000):
        self.path = path
        self.cap = cap
        self.items: list[dict] = []
        self.next_id = 1
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path) as f:
                self.items = json.load(f)
        except FileNotFoundError:
            self.items = []
        except Exception:
            log.exception("could not read %s; starting empty", self.path)
            self.items = []
        self.next_id = max((m.get("id", 0) for m in self.items), default=0) + 1

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.items, f)
            os.replace(tmp, self.path)
        except Exception:
            log.exception("could not persist messages")

    def add(self, rec: dict) -> dict:
        rec["id"] = self.next_id
        self.next_id += 1
        rec.setdefault("ts", time.time())
        self.items.append(rec)
        if len(self.items) > self.cap:
            del self.items[: len(self.items) - self.cap]
        self._save()
        return rec

    def since(self, after: int = 0) -> list[dict]:
        return [m for m in self.items if m["id"] > after]


class MeshManager:
    def __init__(self, port: str, baudrate: int = 115200, log_size: int = 400):
        self.port = port
        self.baudrate = baudrate
        self.mc: MeshCore | None = None
        self.connected = False
        self.last_error: str | None = None
        self.device_info: dict[str, Any] = {}
        self.connected_at: float | None = None

        self._lock = asyncio.Lock()
        self._log: list[dict] = []
        self._log_size = log_size
        self._log_seq = 0
        self._listeners: set[Callable[[dict], None]] = set()
        self._run_task: asyncio.Task | None = None
        self._stopping = False

        self.messages = MessageStore(
            os.environ.get("MESH_MESSAGE_FILE", "/data/messages.json"))
        self._refresh_task: asyncio.Task | None = None

        self.passwords = PasswordStore(
            os.environ.get("MESH_PASSWORD_FILE", "/data/passwords.json"))
        # pubkey -> monotonic time of last successful login
        self._logins: dict[str, float] = {}
        self.login_ttl = float(os.environ.get("MESH_LOGIN_TTL", "900"))

    # ---------- event fan-out ----------

    def add_listener(self, fn: Callable[[dict], None]) -> None:
        self._listeners.add(fn)

    def remove_listener(self, fn: Callable[[dict], None]) -> None:
        self._listeners.discard(fn)

    def emit(self, kind: str, payload: Any = None, **extra) -> dict:
        self._log_seq += 1
        rec = {"seq": self._log_seq, "ts": time.time(), "kind": kind,
               "payload": jsonable(payload), **extra}
        self._log.append(rec)
        if len(self._log) > self._log_size:
            del self._log[: len(self._log) - self._log_size]
        for fn in list(self._listeners):
            try:
                fn(rec)
            except Exception:
                log.exception("listener failed")
        return rec

    def backlog(self, since: int = 0) -> list[dict]:
        return [r for r in self._log if r["seq"] > since]

    # ---------- connection lifecycle ----------

    async def start(self) -> None:
        self._run_task = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        self._stopping = True
        if self._run_task:
            self._run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._run_task
        await self._teardown()

    async def _teardown(self) -> None:
        if self.mc is not None:
            with contextlib.suppress(Exception):
                await self.mc.disconnect()
        self.mc = None
        self.connected = False

    async def _supervise(self) -> None:
        """Reconnect forever -- the Pi may reboot the USB link out from under us."""
        delay = 2
        while not self._stopping:
            try:
                await self._connect()
                delay = 2
                ticks = 0
                while self.connected and not self._stopping:
                    await asyncio.sleep(2)
                    if self.mc is None or not self.mc.is_connected:
                        self.connected = False
                        self.emit("disconnected", {"reason": "link dropped"})
                        break
                    ticks += 1
                    if ticks % 1800 == 0:      # roughly hourly
                        with contextlib.suppress(Exception):
                            await self._sync_clock(self.mc)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = str(e)
                self.emit("error", {"error": f"connect failed: {e}"})
                log.warning("connect failed: %s", e)
            await self._teardown()
            if self._stopping:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def _connect(self) -> None:
        log.info("opening %s", self.port)
        mc = await MeshCore.create_serial(self.port, self.baudrate, default_timeout=20)
        self.mc = mc
        self.connected = True
        self.connected_at = time.time()
        self.last_error = None

        mc.subscribe(None, self._on_event)
        with contextlib.suppress(Exception):
            res = await mc.commands.send_device_query()
            self.device_info = res.payload or {}
        with contextlib.suppress(Exception):
            await self._sync_clock(mc)
        with contextlib.suppress(Exception):
            await mc.ensure_contacts()
        await mc.start_auto_message_fetching()
        self.emit("connected", {"port": self.port, **self.device_info})

    async def _sync_clock(self, mc, max_drift: float = 30.0) -> float | None:
        """Match the node's clock to ours when it has drifted.

        The board has no battery-backed RTC, so every power cycle puts it back
        at its build date. Repeater logins are timestamp-signed, so a stale
        clock makes a correct password look rejected. Caller must not hold the
        lock; this takes it.
        """
        async with self._lock:
            res = await asyncio.wait_for(mc.commands.get_time(), timeout=20)
            dev = (res.payload or {}).get("time") if res else None
            if not dev:
                return None
            now = int(time.time())
            drift = now - int(dev)
            if abs(drift) <= max_drift:
                return drift
            await asyncio.wait_for(mc.commands.set_time(now), timeout=20)
        self.emit("clock_synced", {"drift_seconds": drift, "set_to": now})
        log.info("node clock was off by %ss; synced", drift)
        return drift

    async def clock_drift(self) -> float | None:
        """Seconds the node's clock is behind ours, or None if unreadable."""
        try:
            async with self._lock:
                res = await asyncio.wait_for(
                    self.require().commands.get_time(), timeout=15)
            dev = (res.payload or {}).get("time") if res else None
            return None if not dev else time.time() - int(dev)
        except Exception:
            return None

    def _on_event(self, ev) -> None:
        name = getattr(ev.type, "name", str(ev.type))
        # These fire constantly and carry no operator value.
        if name in {"OK", "NO_MORE_MSGS"}:
            return
        if name in {"LOGIN_SUCCESS", "LOGIN_FAILED"}:
            self._note_login(name, ev.payload)
        if name in {"CHANNEL_MSG_RECV", "CONTACT_MSG_RECV"}:
            self._record_incoming(name, ev.payload)
        if name in {"ADVERTISEMENT", "NEW_CONTACT", "PATH_UPDATE"}:
            # An advert for a contact we already know updates the name and path
            # on the device, but the library's cache only changes when we ask
            # for the list again -- otherwise a renamed node shows the old name.
            self.schedule_contact_refresh()
        self.emit(name, ev.payload)

    def schedule_contact_refresh(self, delay: float = 2.0) -> None:
        """Debounced contact reload; a burst of adverts triggers one fetch."""
        if self._refresh_task and not self._refresh_task.done():
            return

        async def run():
            try:
                await asyncio.sleep(delay)
                async with self._lock:
                    mc = self.require()
                    await asyncio.wait_for(mc.commands.get_contacts(), timeout=30)
                self.emit("contacts_updated", {})
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("contact refresh failed: %s", e)

        self._refresh_task = asyncio.create_task(run())

    def _contact_name_for_prefix(self, prefix: str | None) -> tuple[str | None, str | None]:
        if not prefix:
            return None, None
        for c in self.contacts():
            if c["public_key"].startswith(prefix):
                return c["public_key"], c.get("adv_name")
        return None, None

    def _record_incoming(self, kind: str, payload) -> None:
        p = payload if isinstance(payload, dict) else {}
        text = p.get("text")
        if text is None:
            return
        if kind == "CHANNEL_MSG_RECV":
            rec = {"dir": "in", "kind": "channel",
                   "channel": p.get("channel_idx"), "contact_key": None,
                   "sender": None}
        else:
            key, nm = self._contact_name_for_prefix(p.get("pubkey_prefix"))
            rec = {"dir": "in", "kind": "dm", "channel": None,
                   "contact_key": key or p.get("pubkey_prefix"),
                   "sender": nm or p.get("pubkey_prefix")}
        rec.update({"text": text, "snr": p.get("SNR"),
                    "path_len": p.get("path_len"),
                    "sender_timestamp": p.get("sender_timestamp")})
        saved = self.messages.add(rec)
        self.emit("chat", saved)

    def record_outgoing(self, *, channel=None, contact_key=None, contact_name=None,
                        text: str = "") -> dict:
        rec = self.messages.add({
            "dir": "out", "kind": "channel" if channel is not None else "dm",
            "channel": channel, "contact_key": contact_key,
            "sender": contact_name, "text": text, "snr": None,
            "path_len": None, "sender_timestamp": int(time.time()),
        })
        self.emit("chat", rec)
        return rec

    def _note_login(self, name: str, payload) -> None:
        """Keep the session cache honest when the radio reports a login result."""
        pre = (payload or {}).get("pubkey_pre") if isinstance(payload, dict) else None
        if not pre:
            return
        for pk in list(self._logins) + [c["public_key"] for c in self.contacts()]:
            if pk.startswith(pre):
                if name == "LOGIN_SUCCESS":
                    self._logins[pk] = time.monotonic()
                else:
                    self._logins.pop(pk, None)
                break

    # ---------- command execution ----------

    def require(self) -> MeshCore:
        if not self.connected or self.mc is None:
            raise RuntimeError("radio not connected")
        return self.mc

    async def run(self, fn, *, timeout: float = 45.0):
        """Serialise one radio operation. fn receives the live MeshCore."""
        async with self._lock:
            mc = self.require()
            return await asyncio.wait_for(fn(mc), timeout=timeout)

    async def run_cli(self, line: str, timeout: float = 90.0) -> str:
        """Execute a meshcli command line and capture what it prints."""
        line = line.strip()
        if not line:
            return ""
        try:
            argv = shlex.split(line, posix=True)
        except ValueError as e:
            return f"parse error: {e}"
        return await self.run_argv(argv, json_output=False, timeout=timeout)

    def has_password(self, pubkey: str) -> bool:
        return self.passwords.get(pubkey) is not None

    def is_logged_in(self, pubkey: str) -> bool:
        t = self._logins.get(pubkey)
        return t is not None and (time.monotonic() - t) < self.login_ttl

    def forget_login(self, pubkey: str) -> None:
        self._logins.pop(pubkey, None)

    async def _login_locked(self, mc, contact: dict) -> tuple[bool, str]:
        """Log into a repeater. Caller must already hold the command lock."""
        pk = contact["public_key"]
        pwd = self.passwords.get(pk)
        if pwd is None:
            return False, "no password saved for this repeater"
        try:
            res = await asyncio.wait_for(
                mc.commands.send_login_sync(contact, pwd), timeout=45)
        except asyncio.TimeoutError:
            return False, "login timed out (repeater did not answer)"
        except Exception as e:
            return False, f"login error: {type(e).__name__}: {e}"
        name = getattr(getattr(res, "type", None), "name", "")
        if res is None:
            return False, "no reply from repeater"
        if name in {"ERROR", "LOGIN_FAILED"}:
            self._logins.pop(pk, None)
            return False, "repeater rejected the password"
        self._logins[pk] = time.monotonic()
        return True, "logged in"

    async def login(self, contact: dict) -> tuple[bool, str]:
        async with self._lock:
            mc = self.require()
            return await self._login_locked(mc, contact)

    async def run_argv(self, argv: list[str], json_output: bool = False,
                       timeout: float = 90.0, login_contact: dict | None = None) -> str:
        """Run one meshcli command given as argv.

        Reuses meshcore_cli's own dispatcher so the app accepts exactly the
        syntax the CLI does instead of a reimplementation that drifts. Taking
        argv (not a string) means the UI never has to quote repeater names.
        """
        if not argv:
            return ""
        if argv[0] in CLI_BLOCKLIST:
            return (f"'{argv[0]}' is not available here "
                    f"(it blocks waiting on a terminal).")

        from meshcore_cli.meshcore_cli import process_cmds

        sink = io.StringIO()
        async with self._lock:
            mc = self.require()
            # Management commands need an authenticated session. Renew it under
            # the same lock so nothing can interleave between login and command.
            if login_contact is not None:
                pk = login_contact["public_key"]
                if self.has_password(pk) and not self.is_logged_in(pk):
                    ok, msg = await self._login_locked(mc, login_contact)
                    if not ok:
                        return f"[{msg}]\n"
            try:
                await asyncio.wait_for(
                    process_cmds(mc, list(argv), json_output=json_output, sink=sink),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                sink.write(f"\n[timed out after {timeout:.0f}s]\n")
            except Exception as e:
                sink.write(f"\n[error: {type(e).__name__}: {e}]\n")
        return strip_ansi(sink.getvalue())

    # ---------- derived state ----------

    def contacts(self) -> list[dict]:
        mc = self.mc
        raw = dict(getattr(mc, "contacts", None) or {}) if mc else {}
        out = []
        for key, c in raw.items():
            c = dict(c)
            c.setdefault("public_key", key)
            c["key_prefix"] = (c.get("public_key") or "")[:12]
            c["is_infra"] = c.get("type") in INFRA_TYPES
            c["has_password"] = self.has_password(c["public_key"])
            c["logged_in"] = self.is_logged_in(c["public_key"])
            out.append(c)
        out.sort(key=lambda c: (not c["is_infra"], (c.get("adv_name") or "").lower()))
        return out

    def state(self) -> dict:
        mc = self.mc
        self_info = jsonable(dict(getattr(mc, "self_info", None) or {}) if mc else {})
        contacts = jsonable(self.contacts())
        return {
            "connected": self.connected,
            "port": self.port,
            "error": self.last_error,
            "connected_at": self.connected_at,
            "device": jsonable(self.device_info),
            "self": self_info,
            "contacts": contacts,
            "counts": {
                "total": len(contacts),
                "infra": sum(1 for c in contacts if c["is_infra"]),
            },
            "log_seq": self._log_seq,
        }
