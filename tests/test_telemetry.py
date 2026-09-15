"""Collector tests. No radio, no InfluxDB, no network."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.telemetry.influx import InfluxWriter          # noqa: E402
from app.telemetry.lineproto import fmt_field, point   # noqa: E402
from app.telemetry.rxlog import (                      # noqa: E402
    Clock, Deduper, RxLogCollector, points_for_frame,
)


# --------------------------------------------------------------------------
# line protocol
# --------------------------------------------------------------------------

def test_tag_values_are_escaped():
    """Node names are operator-supplied and routinely contain separators."""
    ln = point("mc_node", {"name": "Hill Top, Repeater=2"}, {"heard": 1}, 1)
    assert ln == r"mc_node,name=Hill\ Top\,\ Repeater\=2 heard=1i 1"


def test_string_fields_quote_and_escape():
    ln = point("m", {}, {"s": 'say "hi"\\'}, 7)
    assert ln == r'm s="say \"hi\"\\" 7'


def test_control_characters_cannot_split_a_point():
    ln = point("m", {"t": "a\nb"}, {"v": "c\nd"}, 1)
    assert "\n" not in ln


def test_bool_is_not_written_as_an_integer():
    # bool subclasses int; getting this wrong changes the column type.
    assert fmt_field(True) == "true"
    assert fmt_field(1) == "1i"


def test_point_with_no_usable_fields_is_dropped():
    assert point("m", {"a": "b"}, {"x": None}, 1) is None


def test_empty_tag_values_are_omitted_not_written_empty():
    ln = point("m", {"a": "", "b": "  ", "c": "ok"}, {"v": 1}, 1)
    assert ln == "m,c=ok v=1i 1"


def test_nan_and_inf_fields_are_dropped():
    assert fmt_field(float("nan")) is None
    assert fmt_field(float("inf")) is None


# --------------------------------------------------------------------------
# dedupe and timestamps
# --------------------------------------------------------------------------

def test_repeated_packet_hash_is_flagged_as_duplicate():
    d = Deduper(ttl=60)
    assert d.check(1234, now=0.0) is False
    assert d.check(1234, now=5.0) is True


def test_duplicate_flag_expires_after_ttl():
    d = Deduper(ttl=60)
    d.check(1234, now=0.0)
    assert d.check(1234, now=61.0) is False


def test_missing_packet_hash_is_never_a_duplicate():
    d = Deduper()
    assert d.check(None) is False
    assert d.check(None) is False


def test_deduper_stays_bounded():
    d = Deduper(ttl=1, max_entries=100)
    for i in range(1000):
        d.check(i, now=float(i))
    assert len(d._seen) <= 100


def test_clock_never_repeats_a_timestamp_within_a_series():
    """Bursts share a tag set; equal timestamps would overwrite in InfluxDB."""
    c = Clock()
    stamps = [c.next("mc_rx", 1_000) for _ in range(5)]
    assert stamps == [1000, 1001, 1002, 1003, 1004]


def test_clock_keeps_series_independent():
    c = Clock()
    assert c.next("mc_rx", 500) == 500
    assert c.next("mc_node", 500) == 500


# --------------------------------------------------------------------------
# point building
# --------------------------------------------------------------------------

ADVERT = {
    "snr": 11.75, "rssi": -28, "payload_length": 129, "payload_ver": 0,
    "route_typename": "FLOOD", "payload_typename": "ADVERT", "payload_type": 0x04,
    "path_len": 2, "path": "981c", "pkt_hash": 42,
    "adv_key": "258e52d610f2aabbccdd", "adv_name": "Fischer Repeater",
    "adv_type": 2, "adv_lat": 29.9629, "adv_lon": -98.22414,
    "adv_timestamp": 1789455094,
}


def test_advert_yields_both_a_frame_and_a_node_registry_point():
    lines = points_for_frame(ADVERT, "obs", False, Clock(), 1)
    assert len(lines) == 2
    assert lines[0].startswith("mc_rx,")
    assert lines[1].startswith("mc_node,")


def test_node_point_is_keyed_on_pubkey_prefix_not_name():
    line = points_for_frame(ADVERT, "obs", False, Clock(), 1)[1]
    assert "pubkey=258e52d610f2," in line
    assert "type=REPEATER" in line
    assert "lat=29.9629" in line


def test_advert_without_a_key_yields_no_node_point():
    d = dict(ADVERT)
    del d["adv_key"]
    assert len(points_for_frame(d, "obs", False, Clock(), 1)) == 1


def test_high_cardinality_values_are_fields_not_tags():
    """pkt_hash and path would blow up series cardinality if indexed."""
    line = points_for_frame(ADVERT, "obs", False, Clock(), 1)[0]
    tagpart, fieldpart = line.split(" ")[0], line.split(" ")[1]
    assert "pkt_hash" not in tagpart and "pkt_hash=42i" in fieldpart
    assert "path=" not in tagpart and 'path="981c"' in fieldpart


def test_absurd_hop_counts_are_bucketed():
    d = dict(ADVERT, path_len=99)
    assert "hops=>12" in points_for_frame(d, "obs", False, Clock(), 1)[0]


def test_unparseable_hop_count_does_not_mint_a_series():
    d = dict(ADVERT, path_len=None)
    assert "hops=unk" in points_for_frame(d, "obs", False, Clock(), 1)[0]


def test_channel_frame_records_volume_without_message_text():
    d = {"payload_type": 0x05, "payload_typename": "GRP_TXT",
         "route_typename": "FLOOD", "path_len": 0, "snr": 12.0, "rssi": -9,
         "payload_length": 37, "pkt_hash": 7, "chan_hash": "ab",
         "chan_name": "tejas", "message": "secret text"}
    lines = points_for_frame(d, "obs", False, Clock(), 1)
    assert any(l.startswith("mc_chan,") for l in lines)
    assert not any("secret text" in l for l in lines)


def test_collector_counts_duplicates_and_adverts():
    class FakeWriter:
        def __init__(self): self.lines = []
        def write_many(self, lines): self.lines.extend(lines)

    w = FakeWriter()
    c = RxLogCollector(w, "obs")
    c.handle(dict(ADVERT))
    c.handle(dict(ADVERT))
    assert c.frames == 2 and c.dups == 1 and c.adverts == 2
    assert any("dup=1" in l for l in w.lines)


def test_collector_survives_a_malformed_frame():
    class FakeWriter:
        def write_many(self, lines): pass

    c = RxLogCollector(FakeWriter(), "obs")
    c.handle({"pkt_hash": "not-an-int", "path_len": object()})
    assert c.frames + c.errors == 1     # counted somewhere, did not raise


# --------------------------------------------------------------------------
# advert name registry
# --------------------------------------------------------------------------

def test_adverts_build_a_name_registry():
    class FakeWriter:
        def write_many(self, lines): pass

    c = RxLogCollector(FakeWriter(), "obs")
    c.handle(dict(ADVERT))
    assert c.names["258e52d610f2"] == "Fischer Repeater"


def test_name_registry_keys_on_the_same_prefix_width_as_neighbours():
    """Repeaters report neighbours as 6-byte prefixes; the map must match."""
    class FakeWriter:
        def write_many(self, lines): pass

    c = RxLogCollector(FakeWriter(), "obs")
    c.handle(dict(ADVERT))
    key = ADVERT["adv_key"][:12]
    assert len(key) == 12 and key in c.names


def test_name_registry_ignores_adverts_without_a_name():
    class FakeWriter:
        def write_many(self, lines): pass

    c = RxLogCollector(FakeWriter(), "obs")
    d = dict(ADVERT)
    del d["adv_name"]
    c.handle(d)
    assert c.names == {}


def test_name_registry_stays_bounded():
    class FakeWriter:
        def write_many(self, lines): pass

    c = RxLogCollector(FakeWriter(), "obs", max_names=10)
    for i in range(50):
        c.handle(dict(ADVERT, adv_key=f"{i:012d}aabb", adv_name=f"node{i}"))
    assert len(c.names) <= 10


def test_neighbour_points_carry_a_name_and_fall_back_to_the_key():
    from app.telemetry.pollers import RepeaterPoller

    written = []

    class FakeWriter:
        def write(self, line): written.append(line)

    class FakeMesh:
        connected = True
        def contacts(self): return []

    p = RepeaterPoller(FakeMesh(), FakeWriter(),
                       name_map=lambda: {"aaaabbbbcccc": "Hill Top"})
    res = {"neighbours": [{"pubkey": "aaaabbbbcccc", "snr": 9.0, "secs_ago": 5},
                          {"pubkey": "ddddeeeeffff", "snr": 3.0, "secs_ago": 9}]}

    async def fake_run(fn, timeout=0):
        return res

    p.mesh.run = fake_run
    asyncio.run(p._poll_neighbours({}, {"repeater": "R", "pubkey": "pk"}))

    assert any("neighbour_name=Hill\\ Top" in l for l in written)
    # unknown key still gets the tag, so the series does not split later
    assert any("neighbour_name=ddddeeeeffff" in l for l in written)


def test_neighbour_name_lookup_failure_is_not_fatal():
    from app.telemetry.pollers import RepeaterPoller

    written = []

    class FakeWriter:
        def write(self, line): written.append(line)

    class FakeMesh:
        connected = True

    def boom():
        raise RuntimeError("contacts unavailable")

    p = RepeaterPoller(FakeMesh(), FakeWriter(), name_map=boom)

    async def fake_run(fn, timeout=0):
        return {"neighbours": [{"pubkey": "aaaabbbbcccc", "snr": 9.0, "secs_ago": 5}]}

    p.mesh.run = fake_run
    asyncio.run(p._poll_neighbours({}, {"repeater": "R", "pubkey": "pk"}))
    assert any("mc_neighbour" in l for l in written)


# --------------------------------------------------------------------------
# writer: outage behaviour
# --------------------------------------------------------------------------

def make_writer(tmp_path, **kw):
    return InfluxWriter("http://influx.invalid", "tok", "org", "bucket",
                        spool_dir=str(tmp_path / "spool"), **kw)


def test_points_spool_when_influx_is_unreachable_then_drain(tmp_path):
    async def go():
        w = make_writer(tmp_path, batch_size=2)
        w.responses = [(False, True, "conn refused")]

        def fake_post(body, _w=w):
            return _w.responses[-1] if _w.responses else (True, False, None)

        w._post = fake_post
        w.write("m v=1i 1")
        w.write("m v=2i 2")
        await w._flush()
        assert w.spool_bytes() > 0, "outage should preserve points on disk"

        w.responses = []                       # influx comes back
        await w._drain_spool()
        assert w.spool_bytes() == 0
        assert w.points_written == 2
    asyncio.run(go())


def test_spool_respects_its_size_cap(tmp_path):
    async def go():
        w = make_writer(tmp_path, batch_size=1, spool_max_bytes=200)
        w._post = lambda body: (False, True, "down")
        for i in range(200):
            w.write(f"m v={i}i {i}")
            await w._flush()
        assert w.spool_bytes() <= 200
        assert w.points_dropped > 0
    asyncio.run(go())


def test_rejected_points_are_dropped_not_retried_forever(tmp_path):
    """A 400 never becomes acceptable; spooling it would wedge everything behind it."""
    async def go():
        w = make_writer(tmp_path, batch_size=10)
        w._post = lambda body: (False, False, "HTTP 400: bad")
        w.write("m v=1i 1")
        await w._flush()
        assert w.spool_bytes() == 0
        assert w.points_dropped == 1
    asyncio.run(go())


def test_producer_sheds_load_rather_than_growing_without_bound(tmp_path):
    w = make_writer(tmp_path, batch_size=10)
    for i in range(1000):
        w.write(f"m v={i}i {i}")
    assert len(w._buf) <= 40
    assert w.points_dropped > 0


def test_bulk_submit_applies_backpressure_instead_of_dropping(tmp_path):
    """A bulk load must not silently shed, the way the live path deliberately does."""
    async def go():
        w = make_writer(tmp_path, batch_size=100)
        w._post = lambda body: (True, False, None)
        for i in range(0, 5000, 10):
            await w.submit([f"m v={j}i {j}" for j in range(i, i + 10)])
        await w._flush()
        return w
    w = asyncio.run(go())
    assert w.points_dropped == 0
    assert w.points_written == 5000


def test_live_write_still_sheds_rather_than_blocking(tmp_path):
    w = make_writer(tmp_path, batch_size=10)
    for i in range(1000):
        w.write(f"m v={i}i {i}")
    assert w.points_dropped > 0


def test_write_ignores_none(tmp_path):
    w = make_writer(tmp_path)
    w.write(None)
    assert w._buf == []


# --------------------------------------------------------------------------
# wiring: manager -> hook -> collector -> writer
# --------------------------------------------------------------------------

def test_rx_log_reaches_the_collector_but_not_the_console_stream():
    """The hook must see every frame; the websocket ring must see none of them."""
    from app.mesh import MeshManager

    m = MeshManager.__new__(MeshManager)     # no serial port, no event loop
    m._listeners = set()
    m._event_hooks = []
    m._log, m._log_size, m._log_seq = [], 400, 0
    m.connected = False
    m.mc = None

    seen = []
    m.add_event_hook(lambda name, payload: seen.append(name))

    class Ev:
        def __init__(self, name, payload):
            self.type = type("T", (), {"name": name})
            self.payload = payload

    m._on_event(Ev("RX_LOG_DATA", dict(ADVERT)))
    m._on_event(Ev("BATTERY", {"level": 4100}))

    assert seen == ["RX_LOG_DATA", "BATTERY"]
    kinds = [r["kind"] for r in m._log]
    assert "RX_LOG_DATA" not in kinds, "frame firehose must not enter the console ring"
    assert "BATTERY" in kinds


def test_collector_is_inert_without_influx_credentials(monkeypatch):
    """The console must still run before the VLAN rule for 8086 exists."""
    from app.telemetry.collector import Collector
    from app.telemetry.config import TelemetryConfig

    monkeypatch.delenv("INFLUX_URL", raising=False)
    monkeypatch.delenv("INFLUX_TOKEN", raising=False)
    c = Collector(object(), TelemetryConfig.from_env())
    assert c.cfg.enabled is False
    asyncio.run(c.start())
    assert c.writer is None
    asyncio.run(c.stop())


def test_collector_end_to_end_without_a_radio(tmp_path):
    from app.mesh import MeshManager
    from app.telemetry.collector import Collector
    from app.telemetry.config import TelemetryConfig

    m = MeshManager.__new__(MeshManager)
    m._listeners, m._event_hooks = set(), []
    m._log, m._log_size, m._log_seq = [], 400, 0
    m.connected, m.mc = True, None

    cfg = TelemetryConfig(
        enabled=True, url="http://influx.invalid", token="t", org="o",
        bucket="b", node="bench", spool_dir=str(tmp_path / "spool"),
        spool_max_bytes=1 << 20, batch_size=500, flush_interval=60.0,
        local_interval=3600.0, repeater_interval=3600.0,
        neighbour_interval=3600.0, repeater_stagger=1.0, dedupe_ttl=90.0,
        decrypt_channels=False)

    async def go():
        c = Collector(m, cfg)
        await c.start()
        c.writer._post = lambda body: (True, False, None)

        class Ev:
            def __init__(self, payload):
                self.type = type("T", (), {"name": "RX_LOG_DATA"})
                self.payload = payload

        for _ in range(3):
            m._on_event(Ev(dict(ADVERT)))
        await c.writer._flush()
        s = c.stats()
        await c.stop()
        return s

    stats = asyncio.run(go())
    assert stats["frames_seen"] == 3
    assert stats["duplicate_frames"] == 2      # same pkt_hash three times
    assert stats["points_written"] == 6        # mc_rx + mc_node per frame
    assert stats["node"] == "bench"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
