"""Active polling: the local radio's own counters, and repeater telemetry.

Two things separate this from the packet capture side. It transmits, so it
costs airtime on a shared duty-cycled band and the intervals are deliberately
slow. And it needs the radio's command channel, which the web console also
uses, so every call goes through MeshManager.run() and takes the same lock --
interleaving radio commands produces cross-matched replies.

Repeaters come from OwnedStore plus PasswordStore rather than a config list:
those already mean "repeaters you own, with credentials", and are what the web
console edits.
"""
import asyncio
import logging
import time

from .lineproto import point
from .rxlog import _float, _int

log = logging.getLogger("meshweb.telemetry.pollers")

# Ask for 6-byte key prefixes so a neighbour's id matches mc_node.pubkey
# (12 hex) exactly. The library defaults to 4 bytes, which would leave the
# topology graph unable to join to the node registry.
NEIGHBOUR_PREFIX_BYTES = 6

STATUS_FIELDS = (
    "bat", "tx_queue_len", "noise_floor", "last_rssi", "nb_recv", "nb_sent",
    "airtime", "uptime", "sent_flood", "sent_direct", "recv_flood",
    "recv_direct", "full_evts", "last_snr", "direct_dups", "flood_dups",
    "rx_airtime", "recv_errors",
)


class LocalStatsPoller:
    """The companion radio's own counters.

    Cheap and receive-only -- these are reads over USB, not over the air -- so
    this runs far more often than anything that transmits.
    """

    def __init__(self, mesh, writer, node: str, interval: float = 60.0):
        self.mesh = mesh
        self.writer = writer
        self.node = node
        self.interval = interval
        self.polls = 0
        self.errors = 0
        self.last_ok: float | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.interval)
                await self.poll()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.errors += 1
                log.debug("local stats poll failed", exc_info=True)

    async def poll(self) -> None:
        if not self.mesh.connected:
            return
        fields: dict = {}
        for name, call in (
            ("core", lambda mc: mc.commands.get_stats_core()),
            ("radio", lambda mc: mc.commands.get_stats_radio()),
            ("packets", lambda mc: mc.commands.get_stats_packets()),
        ):
            try:
                res = await self.mesh.run(call, timeout=20)
            except Exception:
                log.debug("stats %s unavailable", name, exc_info=True)
                continue
            if res is None or getattr(getattr(res, "type", None), "name", "") == "ERROR":
                continue
            payload = res.payload if isinstance(res.payload, dict) else {}
            for k, v in payload.items():
                fields[k] = _float(v) if isinstance(v, float) else _int(v)

        if not fields:
            self.errors += 1
            return
        ln = point("mc_local", {"node": self.node}, fields, time.time_ns())
        self.writer.write(ln)
        self.polls += 1
        self.last_ok = time.time()

    def stats(self) -> dict:
        return {"local_polls": self.polls, "local_errors": self.errors,
                "local_last_ok": self.last_ok}


class RepeaterPoller:
    """Status, telemetry and neighbour tables from repeaters you own."""

    def __init__(self, mesh, writer, *, interval: float = 300.0,
                 neighbour_interval: float = 1800.0, stagger: float = 20.0,
                 name_map=None):
        self.mesh = mesh
        self.writer = writer
        # Callable returning {pubkey prefix: name}. Repeaters report neighbours
        # as bare key prefixes; resolving them here rather than joining in the
        # dashboard keeps the stored data readable on its own.
        self.name_map = name_map
        self.interval = interval
        self.neighbour_interval = neighbour_interval
        self.stagger = stagger
        self.polls = 0
        self.failures = 0
        self.last_ok: float | None = None
        self._last_neighbours: dict[str, float] = {}
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def targets(self) -> list[dict]:
        return [c for c in self.mesh.contacts()
                if c.get("owned") and c.get("has_password")]

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.interval)
                if not self.mesh.connected:
                    continue
                for i, contact in enumerate(self.targets()):
                    if i:
                        # Spread transmissions out. Polling every repeater at
                        # once would put a burst of directed traffic on the
                        # band each cycle, and they all answer at once too.
                        await asyncio.sleep(self.stagger)
                    await self._poll_one(contact)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("repeater poll cycle failed")

    async def _poll_one(self, contact: dict) -> None:
        name = contact.get("adv_name") or contact.get("public_key", "")[:12]
        pk = contact.get("public_key") or ""
        tags = {"repeater": name, "pubkey": pk[:12]}
        started = time.monotonic()

        try:
            if not self.mesh.is_logged_in(pk):
                ok, msg = await self.mesh.login(contact)
                if not ok:
                    self._write_failure(tags, msg)
                    return
            status = await self.mesh.run(
                lambda mc: mc.commands.req_status_sync(contact, min_timeout=10),
                timeout=90)
        except Exception as e:
            self._write_failure(tags, f"{type(e).__name__}: {e}")
            return

        if not isinstance(status, dict):
            # A session can lapse on the repeater side; drop ours so the next
            # cycle logs in again rather than retrying into a dead session.
            self.mesh.forget_login(pk)
            self._write_failure(tags, "no status reply")
            return

        fields = {k: _int(status.get(k)) for k in STATUS_FIELDS}
        fields["last_snr"] = _float(status.get("last_snr"))
        fields["rtt_ms"] = _int((time.monotonic() - started) * 1000)
        fields["ok"] = True
        self.writer.write(point("mc_repeater", tags, fields, time.time_ns()))
        self.polls += 1
        self.last_ok = time.time()

        await self._poll_telemetry(contact, tags)
        if time.monotonic() - self._last_neighbours.get(pk, 0) > self.neighbour_interval:
            self._last_neighbours[pk] = time.monotonic()
            await self._poll_neighbours(contact, tags)

    async def _poll_telemetry(self, contact: dict, tags: dict) -> None:
        try:
            lpp = await self.mesh.run(
                lambda mc: mc.commands.req_telemetry_sync(contact, min_timeout=10),
                timeout=90)
        except Exception:
            log.debug("telemetry request failed for %s", tags["repeater"], exc_info=True)
            return
        if not isinstance(lpp, list):
            return
        now = time.time_ns()
        for i, entry in enumerate(lpp):
            if not isinstance(entry, dict):
                continue
            value = entry.get("value")
            if isinstance(value, dict):
                # Composite readings (gps, accelerometer) arrive as one entry
                # with named components; each becomes its own series.
                for comp, v in value.items():
                    self.writer.write(point(
                        "mc_repeater_telem",
                        {**tags, "channel": str(entry.get("channel")),
                         "type": f"{entry.get('type')}.{comp}"},
                        {"value": _float(v)}, now + i))
            else:
                self.writer.write(point(
                    "mc_repeater_telem",
                    {**tags, "channel": str(entry.get("channel")),
                     "type": str(entry.get("type"))},
                    {"value": _float(value)}, now + i))

    async def _poll_neighbours(self, contact: dict, tags: dict) -> None:
        try:
            res = await self.mesh.run(
                lambda mc: mc.commands.fetch_all_neighbours(
                    contact, pubkey_prefix_length=NEIGHBOUR_PREFIX_BYTES,
                    min_timeout=15),
                timeout=240)
        except Exception:
            log.debug("neighbours request failed for %s", tags["repeater"], exc_info=True)
            return
        if not isinstance(res, dict):
            return
        names = {}
        if self.name_map is not None:
            try:
                names = self.name_map() or {}
            except Exception:
                log.debug("name lookup failed", exc_info=True)
        now = time.time_ns()
        for i, n in enumerate(res.get("neighbours") or []):
            key = str(n.get("pubkey", ""))[:12].lower()
            self.writer.write(point(
                "mc_neighbour",
                {"repeater": tags["repeater"], "pubkey": tags["pubkey"],
                 "neighbour": key,
                 # Always tagged, falling back to the key, so a neighbour whose
                 # name is not known yet does not start a second series that
                 # later splits away from the named one.
                 "neighbour_name": names.get(key) or key},
                {"snr": _float(n.get("snr")), "secs_ago": _int(n.get("secs_ago"))},
                now + i))
        self.writer.write(point(
            "mc_repeater", tags,
            {"neighbours_count": _int(res.get("neighbours_count")),
             "neighbours_seen": _int(res.get("results_count"))},
            time.time_ns()))

    def _write_failure(self, tags: dict, reason: str) -> None:
        """Record the miss.

        A repeater that stops answering is exactly what you want a dashboard to
        show, so an unreachable poll has to produce a point. Without this, the
        series simply stops and looks identical to the collector being down.
        """
        self.failures += 1
        self.writer.write(point("mc_repeater", tags,
                                {"ok": False, "error": reason}, time.time_ns()))
        log.info("repeater %s poll failed: %s", tags.get("repeater"), reason)

    def stats(self) -> dict:
        return {"repeater_polls": self.polls, "repeater_failures": self.failures,
                "repeater_last_ok": self.last_ok,
                "repeater_targets": len(self.targets()) if self.mesh.connected else 0}
