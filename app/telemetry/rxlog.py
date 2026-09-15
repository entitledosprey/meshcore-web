"""Turn heard RF frames into InfluxDB points.

Companion firmware reports every frame the radio demodulates -- `logRxRaw()` in
Dispatcher.cpp runs before parsing, before the addressing check, before CRC --
so this sees the whole channel, not just traffic for us. The meshcore library
decodes the MeshCore header for us and, for ADVERT payloads, the sender's
public key, name, type and coordinates. That last part is why this exists: the
MQTT observer firmware this replaces could only ever report itself as the
origin, so the mesh was visible as traffic volume but not as nodes.

The point builders are module-level functions taking a decoded payload dict so
that replay.py can drive the exact same code from stored frames, with no radio
attached. If the live path and the replay path could drift, validating the
decoder offline would prove nothing.
"""
import logging
import time
from typing import Any

from .lineproto import point

log = logging.getLogger("meshweb.telemetry.rxlog")

# meshcore/packets.py AdvType
ADV_TYPES = {0: "NONE", 1: "CHAT", 2: "REPEATER", 3: "ROOM", 4: "SENSOR"}

PAYLOAD_TYPE_ADVERT = 0x04
PAYLOAD_TYPE_GRP_TXT = 0x05

# Hop counts beyond this are almost certainly a misparse; bucketing them keeps
# a corrupt frame from minting a new series per bogus value.
MAX_HOPS_TAG = 12


class Deduper:
    """Marks frames whose packet we have already heard.

    Flood routing means one packet arrives several times, once per repeater
    that rebroadcasts it. Both readings matter -- frames-on-air is the airtime
    cost, unique packets is the actual traffic -- so nothing is discarded; the
    frame is only labelled. pkt_hash covers the payload and not the path, so a
    rebroadcast with an extra hop still matches.
    """

    def __init__(self, ttl: float = 90.0, max_entries: int = 20000):
        self.ttl = ttl
        self.max_entries = max_entries
        self._seen: dict[int, float] = {}

    def check(self, pkt_hash: int | None, now: float | None = None) -> bool:
        if pkt_hash is None:
            return False
        now = time.monotonic() if now is None else now
        prev = self._seen.get(pkt_hash)
        self._seen[pkt_hash] = now
        if len(self._seen) > self.max_entries:
            self._prune(now)
        return prev is not None and (now - prev) < self.ttl

    def _prune(self, now: float) -> None:
        cutoff = now - self.ttl
        self._seen = {k: v for k, v in self._seen.items() if v >= cutoff}
        if len(self._seen) > self.max_entries:
            # Still oversized: the mesh is busier than the cap assumed. Keep the
            # newest half rather than unbounded growth.
            newest = sorted(self._seen.items(), key=lambda kv: kv[1])
            self._seen = dict(newest[len(newest) // 2:])


class Clock:
    """Strictly increasing nanosecond timestamps, per series.

    InfluxDB overwrites a point when measurement, tag set and timestamp all
    match. Frames arrive in bursts -- a flood and its rebroadcasts land inside
    the same millisecond -- and they share a tag set by design, so without this
    the repeats would quietly overwrite each other and every "frames on air"
    panel would under-report.
    """

    def __init__(self):
        self._last: dict[str, int] = {}

    def next(self, key: str, ts_ns: int | None = None) -> int:
        ts_ns = time.time_ns() if ts_ns is None else int(ts_ns)
        prev = self._last.get(key)
        if prev is not None and ts_ns <= prev:
            ts_ns = prev + 1
        self._last[key] = ts_ns
        return ts_ns


def _hops_tag(path_len: Any) -> str:
    try:
        n = int(path_len)
    except (TypeError, ValueError):
        return "unk"
    if n < 0:
        return "unk"
    return str(n) if n <= MAX_HOPS_TAG else f">{MAX_HOPS_TAG}"


def rx_point(d: dict, node: str, dup: bool, ts_ns: int) -> str | None:
    return point(
        "mc_rx",
        {
            "node": node,
            "route": d.get("route_typename") or "UNK",
            "ptype": d.get("payload_typename") or "UNK",
            "hops": _hops_tag(d.get("path_len")),
            "dup": "1" if dup else "0",
        },
        {
            "snr": _float(d.get("snr")),
            "rssi": _int(d.get("rssi")),
            "len": _int(d.get("payload_length")),
            "pver": _int(d.get("payload_ver")),
            "pkt_hash": _int(d.get("pkt_hash")),
            "path": d.get("path") or None,
        },
        ts_ns,
    )


def node_point(d: dict, node: str, ts_ns: int) -> str | None:
    """A node registry entry, built from an ADVERT we overheard.

    Keyed on a 12-hex prefix of the advertised public key rather than the name:
    names are not unique and their owners change them, and a series that moves
    when someone renames their node is worse than useless for "when did I last
    hear this node".
    """
    key = d.get("adv_key")
    if not key:
        return None
    return point(
        "mc_node",
        {
            "pubkey": str(key)[:12],
            "name": d.get("adv_name") or None,
            "type": ADV_TYPES.get(d.get("adv_type"), "UNK"),
            "heard_by": node,
        },
        {
            "snr": _float(d.get("snr")),
            "rssi": _int(d.get("rssi")),
            "hops": _int(d.get("path_len")),
            "lat": _float(d.get("adv_lat")),
            "lon": _float(d.get("adv_lon")),
            "adv_ts": _int(d.get("adv_timestamp")),
            "heard": 1,
        },
        ts_ns,
    )


def chan_point(d: dict, node: str, ts_ns: int) -> str | None:
    """Channel traffic volume. Message text is never written, only that one passed."""
    if not d.get("chan_hash"):
        return None
    return point(
        "mc_chan",
        {
            "node": node,
            "chan_hash": str(d.get("chan_hash")),
            "chan_name": d.get("chan_name") or None,
        },
        {
            "snr": _float(d.get("snr")),
            "rssi": _int(d.get("rssi")),
            "len": _int(d.get("payload_length")),
            "hops": _int(d.get("path_len")),
        },
        ts_ns,
    )


def points_for_frame(d: dict, node: str, dup: bool, clock: Clock,
                     ts_ns: int | None = None) -> list[str]:
    """Every point one heard frame produces."""
    out = []
    ln = rx_point(d, node, dup, clock.next("mc_rx", ts_ns))
    if ln:
        out.append(ln)
    ptype = d.get("payload_type")
    if ptype == PAYLOAD_TYPE_ADVERT:
        ln = node_point(d, node, clock.next("mc_node", ts_ns))
        if ln:
            out.append(ln)
    elif ptype == PAYLOAD_TYPE_GRP_TXT:
        ln = chan_point(d, node, clock.next("mc_chan", ts_ns))
        if ln:
            out.append(ln)
    return out


def _int(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


class RxLogCollector:
    """Live path: RX_LOG_DATA events in, points out."""

    def __init__(self, writer, node: str, dedupe_ttl: float = 90.0,
                 max_names: int = 5000):
        self.writer = writer
        self.node = node
        self.deduper = Deduper(dedupe_ttl)
        self.clock = Clock()
        # pubkey prefix -> advertised name, harvested from ADVERTs as they are
        # overheard. Repeaters report their neighbours as bare key prefixes, so
        # this is what turns a neighbour table into readable names.
        self.names: dict[str, str] = {}
        self.max_names = max_names
        self.frames = 0
        self.dups = 0
        self.adverts = 0
        self.errors = 0
        self.last_frame: float | None = None

    def handle(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        try:
            dup = self.deduper.check(payload.get("pkt_hash"))
            lines = points_for_frame(payload, self.node, dup, self.clock)
            self.writer.write_many(lines)
            self.frames += 1
            self.last_frame = time.time()
            if dup:
                self.dups += 1
            if payload.get("payload_type") == PAYLOAD_TYPE_ADVERT:
                self.adverts += 1
                self._remember_name(payload)
        except Exception:
            self.errors += 1
            log.exception("could not build points for frame")

    def _remember_name(self, payload: dict) -> None:
        key, name = payload.get("adv_key"), payload.get("adv_name")
        if not key or not name:
            return
        if len(self.names) >= self.max_names and str(key)[:12] not in self.names:
            # Unbounded growth would be a slow leak on a busy mesh. Names change
            # rarely, so dropping an arbitrary entry costs at most one advert
            # interval before it is learned again.
            self.names.pop(next(iter(self.names)), None)
        self.names[str(key)[:12].lower()] = str(name)

    def stats(self) -> dict:
        return {
            "frames_seen": self.frames,
            "duplicate_frames": self.dups,
            "adverts_seen": self.adverts,
            "decode_errors": self.errors,
            "names_known": len(self.names),
            "last_frame": self.last_frame,
        }
