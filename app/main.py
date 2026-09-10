"""MeshCore web console -- REST + WebSocket over a single companion radio."""
import asyncio
import contextlib
import json
import logging
import os
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .mesh import MeshManager

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("meshweb")

PORT_DEV = os.environ.get("MESH_PORT", "/dev/ttyACM0")
BAUD = int(os.environ.get("MESH_BAUD", "115200"))
STATIC = Path(__file__).parent / "static"

mesh = MeshManager(PORT_DEV, BAUD)
app = FastAPI(title="MeshCore Console")


@app.on_event("startup")
async def _startup():
    Path(os.path.expanduser("~/.config/meshcore")).mkdir(parents=True, exist_ok=True)
    await mesh.start()


@app.on_event("shutdown")
async def _shutdown():
    await mesh.stop()


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------

class CliReq(BaseModel):
    line: str


class RunReq(BaseModel):
    args: list[str] = Field(min_length=1)
    json_output: bool = True
    timeout: float = 90.0


class RadioReq(BaseModel):
    freq: float
    bw: float
    sf: int
    cr: int


class NameReq(BaseModel):
    name: str


class TxReq(BaseModel):
    tx_power: int


class AutoAddReq(BaseModel):
    chat: bool = True
    repeater: bool = True
    room: bool = True
    sensor: bool = False
    overwrite: bool = False


class ImportReq(BaseModel):
    uri: str


class LoginReq(BaseModel):
    password: str


class PasswordReq(BaseModel):
    password: str
    remember: bool = True


class CmdReq(BaseModel):
    cmd: str


class ChanReq(BaseModel):
    name: str
    key: str | None = None      # 32 hex chars; omitted means the device picks one


class SendReq(BaseModel):
    text: str
    channel: int | None = None
    contact_key: str | None = None


def _maybe_json(text: str):
    """meshcli prints JSON for most commands; hand structure to the UI when we can."""
    t = text.strip()
    if not t:
        return None
    try:
        return json.loads(t)
    except Exception:
        return None


async def _run(args: list[str], json_output: bool = True, timeout: float = 90.0,
               login_contact: dict | None = None):
    try:
        raw = await mesh.run_argv(args, json_output=json_output, timeout=timeout,
                                  login_contact=login_contact)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"ok": True, "command": " ".join(args), "text": raw, "data": _maybe_json(raw),
            "auth": None if login_contact is None else {
                "has_password": mesh.has_password(login_contact["public_key"]),
                "logged_in": mesh.is_logged_in(login_contact["public_key"]),
            }}


def _contact_or_404(key: str) -> dict:
    for c in mesh.contacts():
        if c.get("public_key", "").startswith(key) or c.get("adv_name") == key:
            return c
    raise HTTPException(status_code=404, detail=f"no contact matching {key!r}")


# --------------------------------------------------------------------------
# state + events
# --------------------------------------------------------------------------

@app.get("/api/state")
async def get_state():
    return mesh.state()


@app.get("/api/events")
async def get_events(since: int = 0):
    return {"events": mesh.backlog(since), "log_seq": mesh._log_seq}


@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    queue: asyncio.Queue = asyncio.Queue(maxsize=500)

    def listener(rec):
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(rec)

    mesh.add_listener(listener)
    try:
        await sock.send_json({"type": "state", "state": mesh.state()})
        for rec in mesh.backlog(0)[-80:]:
            await sock.send_json({"type": "event", "event": rec})
        while True:
            try:
                rec = await asyncio.wait_for(queue.get(), timeout=20)
                await sock.send_json({"type": "event", "event": rec})
                # Connection changes alter the whole page; push fresh state.
                if rec["kind"] in {"connected", "disconnected", "CONTACTS",
                                   "NEW_CONTACT", "SELF_INFO", "CONTACT_DELETED",
                                   "contacts_updated"}:
                    await sock.send_json({"type": "state", "state": mesh.state()})
            except asyncio.TimeoutError:
                await sock.send_json({"type": "ping", "ts": time.time()})
    except WebSocketDisconnect:
        pass
    except Exception:
        log.debug("ws closed", exc_info=True)
    finally:
        mesh.remove_listener(listener)


# --------------------------------------------------------------------------
# local node actions
# --------------------------------------------------------------------------

@app.post("/api/node/advert")
async def advert(flood: bool = False):
    return await _run(["floodadv"] if flood else ["advert"], json_output=False, timeout=30)


@app.post("/api/node/reboot")
async def reboot():
    return await _run(["reboot"], json_output=False, timeout=20)


@app.get("/api/node/clock")
async def clock_status():
    drift = await mesh.clock_drift()
    return {"ok": True, "drift_seconds": drift,
            "stale": drift is not None and abs(drift) > 60}


@app.post("/api/node/clock-sync")
async def clock_sync():
    return await _run(["clock", "sync"], json_output=False, timeout=20)


@app.get("/api/node/telemetry")
async def self_telemetry():
    return await _run(["self_telemetry"], timeout=30)


@app.get("/api/node/card")
async def node_card():
    return await _run(["card"], json_output=False, timeout=20)


@app.post("/api/node/radio")
async def set_radio(r: RadioReq):
    return await _run(["set", "radio", f"{r.freq},{r.bw},{r.sf},{r.cr}"],
                      json_output=False, timeout=30)


@app.post("/api/node/name")
async def set_name(r: NameReq):
    return await _run(["set", "name", r.name], json_output=False, timeout=20)


@app.post("/api/node/tx-power")
async def set_tx(r: TxReq):
    # meshcli's parameter is "tx"; "tx_power" falls through to its custom-var
    # branch and the firmware rejects it with ERR_CODE_ILLEGAL_ARG.
    return await _run(["set", "tx", str(r.tx_power)], json_output=False, timeout=20)


@app.get("/api/node/autoadd")
async def get_autoadd():
    """Which advert types the node will turn into contacts automatically."""
    r = await _run(["get", "autoadd_config"], json_output=False, timeout=20)
    try:
        flags = int((r.get("text") or "0").strip(), 0)
    except ValueError:
        flags = 0
    return {"ok": True, "flags": flags, "hex": f"0x{flags:02x}",
            "overwrite": bool(flags & 0x01), "chat": bool(flags & 0x02),
            "repeater": bool(flags & 0x04), "room": bool(flags & 0x08),
            "sensor": bool(flags & 0x10)}


@app.post("/api/node/autoadd")
async def set_autoadd(r: AutoAddReq):
    """A node that adds nobody cannot decrypt direct messages from them:
    the shared secret needs the sender's public key, which arrives by advert."""
    flags = ((0x01 if r.overwrite else 0) | (0x02 if r.chat else 0)
             | (0x04 if r.repeater else 0) | (0x08 if r.room else 0)
             | (0x10 if r.sensor else 0))
    res = await _run(["set", "autoadd_config", f"0x{flags:02x}"],
                     json_output=False, timeout=20)
    return {**res, "flags": flags, "hex": f"0x{flags:02x}"}


@app.post("/api/node/contacts/refresh")
async def refresh_contacts():
    return await _run(["reload_contacts"], json_output=False, timeout=60)


@app.post("/api/node/import")
async def import_contact(r: ImportReq):
    return await _run(["import_contact", r.uri], json_output=False, timeout=30)


@app.post("/api/node/discover")
async def node_discover(filter: str = "2"):
    """Ask the mesh for nodes of a type (2 = repeaters)."""
    return await _run(["node_discover", filter], json_output=False, timeout=60)


@app.post("/api/node/discover-add")
async def discover_and_add(filter: int = 255, wait: float = 12.0):
    """Discover nodes and add any new ones to the contact list.

    A repeater normally only becomes a contact when it happens to advertise,
    which can be hours away. A discovery request with prefix_only=False returns
    the full 32-byte public key, which is everything needed to write the
    contact ourselves. The name is a placeholder until the node does advertise.
    """
    from meshcore import EventType

    async def job(mc):
        found: dict[str, dict] = {}

        def on_disc(ev):
            p = ev.payload or {}
            pk = p.get("pubkey")
            if isinstance(pk, str) and len(pk) == 64:
                found[pk] = p

        sub = mc.subscribe(EventType.DISCOVER_RESPONSE, on_disc)
        try:
            await mc.commands.send_node_discover_req(filter, prefix_only=False)
            await asyncio.sleep(wait)
        finally:
            mc.unsubscribe(sub)

        known = {c.get("public_key") for c in mesh.contacts()}
        added, skipped, failed = [], [], []
        for pk, p in found.items():
            if pk in known:
                skipped.append(pk[:12])
                continue
            contact = {
                "public_key": pk,
                "type": int(p.get("node_type") or 2),
                "flags": 0,
                "out_path": "",
                "out_path_len": -1,      # flood until a path is discovered
                "out_path_hash_mode": 0,
                "adv_name": f"node-{pk[:8]}",
                "last_advert": 0,
                "adv_lat": 0.0,
                "adv_lon": 0.0,
            }
            res = await mc.commands.add_contact(contact)
            if getattr(res, "type", None) == EventType.ERROR:
                failed.append({"key": pk[:12], "error": str(res.payload)})
            else:
                added.append({"key": pk[:12], "name": contact["adv_name"],
                              "type": contact["type"],
                              "snr": p.get("SNR"), "rssi": p.get("RSSI")})
        if added:
            await mc.commands.get_contacts()
        return {"ok": True, "discovered": len(found), "added": added,
                "already_known": skipped, "failed": failed}

    try:
        return await mesh.run(job, timeout=wait + 45)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


# --------------------------------------------------------------------------
# per-repeater actions
# --------------------------------------------------------------------------

@app.post("/api/contact/{key}/neighbours")
async def neighbours(key: str):
    c = _contact_or_404(key)
    return await _run(["req_neighbours", c["adv_name"]], timeout=120, login_contact=c)


@app.post("/api/contact/{key}/status")
async def status(key: str):
    c = _contact_or_404(key)
    return await _run(["req_status", c["adv_name"]], timeout=90, login_contact=c)


@app.post("/api/contact/{key}/telemetry")
async def telemetry(key: str):
    c = _contact_or_404(key)
    return await _run(["req_telemetry", c["adv_name"]], timeout=90, login_contact=c)


@app.post("/api/contact/{key}/owner")
async def owner(key: str):
    c = _contact_or_404(key)
    return await _run(["req_owner", c["adv_name"]], timeout=90, login_contact=c)


@app.post("/api/contact/{key}/path/discover")
async def path_discover(key: str):
    c = _contact_or_404(key)
    return await _run(["disc_path", c["adv_name"]], json_output=False, timeout=120)


@app.post("/api/contact/{key}/path/reset")
async def path_reset(key: str):
    c = _contact_or_404(key)
    return await _run(["reset_path", c["adv_name"]], json_output=False, timeout=30)


@app.post("/api/contact/{key}/login")
async def login(key: str, r: LoginReq):
    """Log in once with a supplied password, without storing it."""
    c = _contact_or_404(key)
    return await _run(["login", c["adv_name"], r.password], json_output=False,
                      timeout=60)


@app.put("/api/contact/{key}/password")
async def set_password(key: str, r: PasswordReq):
    """Verify a repeater password by logging in, and keep it if asked to.

    The password is only written to disk once the repeater has accepted it, so
    a typo never gets stored and silently breaks later auto-logins.
    """
    c = _contact_or_404(key)
    pk = c["public_key"]
    previous = mesh.passwords.get(pk)

    mesh.passwords.set(pk, r.password)   # login() reads it from the store
    mesh.forget_login(pk)
    ok, msg = await mesh.login(c)

    if not ok:
        # A stale node clock fails timestamp-signed logins and looks exactly
        # like a bad password. Checked here, outside the command lock.
        drift = await mesh.clock_drift()
        if drift is not None and abs(drift) > 60:
            msg += (f" — but this node's clock is off by {int(abs(drift))}s, "
                    f"which breaks logins regardless of the password. "
                    f"Sync the clock and try again.")

    keep = ok and r.remember
    if not keep:
        if previous is None:
            mesh.passwords.clear(pk)
        else:
            mesh.passwords.set(pk, previous)

    return {"ok": ok, "message": msg, "remembered": keep,
            "logged_in": mesh.is_logged_in(pk)}


@app.delete("/api/contact/{key}/password")
async def forget_password(key: str):
    c = _contact_or_404(key)
    pk = c["public_key"]
    removed = mesh.passwords.clear(pk)
    mesh.forget_login(pk)
    return {"ok": True, "removed": removed}


@app.post("/api/contact/{key}/logout")
async def logout(key: str):
    c = _contact_or_404(key)
    return await _run(["logout", c["adv_name"]], json_output=False, timeout=30)


@app.post("/api/contact/{key}/cmd")
async def repeater_cmd(key: str, r: CmdReq):
    """Send a CLI command to a repeater and wait for its reply."""
    c = _contact_or_404(key)
    try:
        res = await mesh.repeater_command(c, r.cmd)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"ok": res["ok"], "command": r.cmd, "text": res["text"],
            "data": _maybe_json(res["text"]),
            "auth": {"has_password": mesh.has_password(c["public_key"]),
                     "logged_in": mesh.is_logged_in(c["public_key"])}}


@app.post("/api/contact/{key}/reboot")
async def repeater_reboot(key: str):
    """A rebooting repeater will not answer, so don't wait long for a reply."""
    c = _contact_or_404(key)
    try:
        res = await mesh.repeater_command(c, "reboot", timeout=8)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    text = res["text"] if res["ok"] else "reboot sent (no reply expected)"
    return {"ok": True, "command": "reboot", "text": text, "data": None}


@app.delete("/api/contact/{key}")
async def remove_contact(key: str):
    c = _contact_or_404(key)
    return await _run(["remove_contact", c["adv_name"]], json_output=False, timeout=30)


@app.get("/api/contact/{key}/export")
async def export_contact(key: str):
    c = _contact_or_404(key)
    return await _run(["export_contact", c["adv_name"]], json_output=False, timeout=30)


# --------------------------------------------------------------------------
# chat
# --------------------------------------------------------------------------

@app.get("/api/chat/channels")
async def list_channels():
    r = await _run(["get_channels"], json_output=True, timeout=60)
    chans = r.get("data")
    return {"ok": True, "channels": chans if isinstance(chans, list) else []}


@app.post("/api/chat/channels")
async def add_channel(r: ChanReq):
    """Add a channel in the first free slot."""
    listing = await _run(["get_channels"], json_output=True, timeout=60)
    used = {c.get("channel_idx") for c in (listing.get("data") or [])}
    limit = int(mesh.device_info.get("max_channels") or 40)
    slot = next((i for i in range(limit) if i not in used), None)
    if slot is None:
        raise HTTPException(status_code=409, detail="no free channel slots")

    args = ["set_channel", str(slot), r.name] + ([r.key] if r.key else [])
    res = await _run(args, json_output=False, timeout=45)
    await _run(["get_channels"], json_output=True, timeout=60)
    return {**res, "channel_idx": slot}


@app.delete("/api/chat/channels/{idx}")
async def remove_channel(idx: int):
    if idx == 0:
        raise HTTPException(status_code=400,
                            detail="channel 0 (Public) cannot be removed")
    return await _run(["remove_channel", str(idx)], json_output=False, timeout=45)


@app.get("/api/chat/messages")
async def get_messages(since: int = 0):
    return {"ok": True, "messages": mesh.messages.since(since),
            "last_id": mesh.messages.next_id - 1}


@app.post("/api/chat/send")
async def send_message(r: SendReq):
    text = r.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty message")

    if r.contact_key:
        c = _contact_or_404(r.contact_key)
        res = await _run(["msg", c["adv_name"], text], json_output=False, timeout=60)
        mesh.record_outgoing(contact_key=c["public_key"],
                             contact_name=c.get("adv_name"), text=text)
        return res

    chan = 0 if r.channel is None else r.channel
    res = await _run(["chan", str(chan), text], json_output=False, timeout=60)
    mesh.record_outgoing(channel=chan, text=text)
    return res


# --------------------------------------------------------------------------
# console
# --------------------------------------------------------------------------

@app.post("/api/cli")
async def cli(r: CliReq):
    try:
        text = await mesh.run_cli(r.line)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"ok": True, "line": r.line, "text": text}


@app.post("/api/run")
async def run_cmd(r: RunReq):
    return await _run(r.args, json_output=r.json_output, timeout=r.timeout)


@app.get("/healthz")
async def healthz():
    return JSONResponse({"ok": True, "connected": mesh.connected})


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")
