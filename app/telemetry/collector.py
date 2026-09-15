"""Ties the collector together: one writer, the RX tap, and the pollers."""
import asyncio
import logging
import time

from .config import TelemetryConfig
from .influx import InfluxWriter
from .lineproto import point
from .pollers import LocalStatsPoller, RepeaterPoller
from .rxlog import RxLogCollector

log = logging.getLogger("meshweb.telemetry")

HEARTBEAT = 30.0


class Collector:
    def __init__(self, mesh, cfg: TelemetryConfig | None = None):
        self.mesh = mesh
        self.cfg = cfg or TelemetryConfig.from_env()
        self.writer: InfluxWriter | None = None
        self.rx: RxLogCollector | None = None
        self.local: LocalStatsPoller | None = None
        self.repeaters: RepeaterPoller | None = None
        self.started_at: float | None = None
        self._task: asyncio.Task | None = None

    # ---------- identity ----------

    def node_name(self) -> str:
        """Tag value identifying this receiver.

        Configured name wins; otherwise the radio's own advertised name, which
        is only known after the link is up. Falling back to a constant matters:
        an empty tag value would be dropped and the points would lose the one
        dimension that says where they were heard.
        """
        if self.cfg.node:
            return self.cfg.node
        mc = self.mesh.mc
        name = (getattr(mc, "self_info", None) or {}).get("name") if mc else None
        return name or "unknown"

    # ---------- lifecycle ----------

    async def start(self) -> None:
        if not self.cfg.enabled:
            log.info("telemetry disabled (no INFLUX_URL/INFLUX_TOKEN); "
                     "console runs without it")
            return
        self.writer = InfluxWriter(
            self.cfg.url, self.cfg.token, self.cfg.org, self.cfg.bucket,
            batch_size=self.cfg.batch_size, flush_interval=self.cfg.flush_interval,
            spool_dir=self.cfg.spool_dir, spool_max_bytes=self.cfg.spool_max_bytes)
        await self.writer.start()

        self.rx = RxLogCollector(self.writer, self.node_name(), self.cfg.dedupe_ttl)
        self.mesh.add_event_hook(self._on_event)

        self.local = LocalStatsPoller(self.mesh, self.writer, self.node_name(),
                                      self.cfg.local_interval)
        await self.local.start()
        self.repeaters = RepeaterPoller(
            self.mesh, self.writer, interval=self.cfg.repeater_interval,
            neighbour_interval=self.cfg.neighbour_interval,
            stagger=self.cfg.repeater_stagger, name_map=self.name_map)
        await self.repeaters.start()

        self._task = asyncio.create_task(self._heartbeat())
        self.started_at = time.time()
        log.info("telemetry collecting into %s/%s as node %r",
                 self.cfg.url, self.cfg.bucket, self.node_name())

    async def stop(self) -> None:
        for comp in (self.local, self.repeaters):
            if comp:
                await comp.stop()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self.writer:
            await self.writer.stop()

    # ---------- event tap ----------

    def _on_event(self, name: str, payload) -> None:
        if name == "RX_LOG_DATA" and self.rx is not None:
            self.rx.handle(payload if isinstance(payload, dict) else {})
        elif name == "CONNECTED":
            self._refresh_node()

    def _refresh_node(self) -> None:
        node = self.node_name()
        for comp in (self.rx, self.local):
            if comp is not None and getattr(comp, "node", None) != node:
                comp.node = node

    def name_map(self) -> dict[str, str]:
        """Public key prefix -> name, for resolving repeater neighbour tables.

        Two sources, because neither is complete: the radio's contact list is
        authoritative but only holds what autoadd accepted, while the advert
        registry covers everything overheard including nodes that never became
        contacts. Contacts win where both know a key.
        """
        out = dict(self.rx.names) if self.rx else {}
        try:
            for c in self.mesh.contacts():
                key = (c.get("public_key") or "").lower()[:12]
                name = c.get("adv_name")
                if key and name:
                    out[key] = name
        except Exception:
            log.debug("contact list unavailable for name map", exc_info=True)
        return out

    # ---------- heartbeat ----------

    async def _heartbeat(self) -> None:
        while True:
            try:
                await asyncio.sleep(HEARTBEAT)
                self._refresh_node()
                if self.writer is None:
                    continue
                s = self.stats()
                self.writer.write(point(
                    "mc_collector",
                    {"node": self.node_name()},
                    {
                        "serial_up": bool(self.mesh.connected),
                        "frames_seen": s.get("frames_seen"),
                        "duplicate_frames": s.get("duplicate_frames"),
                        "adverts_seen": s.get("adverts_seen"),
                        "decode_errors": s.get("decode_errors"),
                        "points_written": s.get("points_written"),
                        "points_dropped": s.get("points_dropped"),
                        "write_errors": s.get("write_errors"),
                        "queued": s.get("queued"),
                        "spool_bytes": s.get("spool_bytes"),
                        "repeater_failures": s.get("repeater_failures"),
                        "uptime_secs": int(time.time() - (self.started_at or time.time())),
                    },
                    time.time_ns()))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("heartbeat failed")

    # ---------- introspection ----------

    def stats(self) -> dict:
        out: dict = {
            "enabled": self.cfg.enabled,
            "node": self.node_name(),
            "bucket": self.cfg.bucket if self.cfg.enabled else None,
            "started_at": self.started_at,
        }
        for comp in (self.rx, self.local, self.repeaters, self.writer):
            if comp is not None:
                out.update(comp.stats())
        return out
