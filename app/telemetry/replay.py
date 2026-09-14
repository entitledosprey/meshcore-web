"""Offline harness: replay stored frames through the live decode path.

There is one radio, and flashing it to companion firmware ends the MQTT
observer feed that currently supplies every MeshCore dashboard. So the decoder
has to be proven before the radio is touched, not after.

The lever is that the observer firmware stored the actual on-air bytes: the
`raw` field on the legacy `meshcore_packet` measurement is the frame itself.
Those frames can be pushed through the real MeshcorePacketParser and the same
point builders the live collector uses, with no hardware attached -- and
because the observer independently tagged every row with its own reading of
`packet_type` and `route`, each stored frame doubles as a labelled test case.
Disagreement means the decoder is wrong.

    python -m app.telemetry.replay --hours 24 --check
    python -m app.telemetry.replay --hours 24 --out-bucket meshcore_dev
"""
import argparse
import asyncio
import csv
import os
import sys
from collections import Counter

from meshcore.meshcore_parser import MeshcorePacketParser

from .influx import InfluxWriter
from .rxlog import Clock, Deduper, points_for_frame

# Route type -> the single letter the observer firmware used.
ROUTE_LETTER = {0: "F", 1: "F", 2: "D", 3: "D"}

FLUX = """
from(bucket: "{bucket}")
  |> range(start: -{hours}h)
  |> filter(fn: (r) => r._measurement == "{measurement}")
  |> filter(fn: (r) => r._field == "raw" or r._field == "SNR"
                    or r._field == "RSSI" or r._field == "len_bytes")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> limit(n: {limit})
"""


def parse_annotated_csv(text: str) -> list[dict]:
    """Parse InfluxDB annotated CSV.

    Not csv.DictReader: a Flux response is a sequence of blocks, each with its
    own header, and the schema genuinely changes between them. This bucket has
    rows predating the packet_type and route tags, so those blocks carry fewer
    columns -- feeding the whole response to a single DictReader silently
    misaligns every row after the first block.
    """
    rows: list[dict] = []
    header: list[str] | None = None
    for line in text.splitlines():
        if not line.strip():
            header = None          # blank line ends a block
            continue
        if line.startswith("#"):
            header = None          # annotation rows precede a new header
            continue
        cells = next(csv.reader([line]))
        if header is None:
            header = cells
            continue
        rows.append(dict(zip(header, cells)))
    return rows


def fetch_rows(url: str, token: str, org: str, bucket: str, hours: int,
               limit: int, measurement: str) -> list[dict]:
    import requests

    q = FLUX.format(bucket=bucket, hours=hours, limit=limit, measurement=measurement)
    r = requests.post(
        f"{url.rstrip('/')}/api/v2/query",
        params={"org": org},
        data=q.encode(),
        headers={
            "Authorization": f"Token {token}",
            "Content-Type": "application/vnd.flux",
            "Accept": "application/csv",
        },
        timeout=120,
    )
    r.raise_for_status()
    return [r for r in parse_annotated_csv(r.text) if r.get("raw")]


async def decode(raw_hex: str, parser: MeshcorePacketParser) -> dict | None:
    """Decode one stored frame exactly as reader.py would a live one.

    reader.py peels SNR and RSSI off the front of the 0x88 frame before handing
    the rest to the parser, and what the observer stored is that remainder --
    so the payload goes in as-is.
    """
    try:
        payload = bytes.fromhex(raw_hex.strip())
    except ValueError:
        return None
    if len(payload) < 2:
        return None
    return await parser.parsePacketPayload(payload, {"payload_length": len(payload)})


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=os.environ.get("INFLUX_URL", ""))
    ap.add_argument("--token", default=os.environ.get("INFLUX_TOKEN", ""))
    ap.add_argument("--org", default=os.environ.get("INFLUX_ORG", "home"))
    ap.add_argument("--bucket", default=os.environ.get("INFLUX_BUCKET", "meshcore"),
                    help="bucket holding the legacy observer data")
    ap.add_argument("--measurement", default="meshcore_packet")
    ap.add_argument("--csv", default=None,
                    help="read frames from an annotated-CSV export instead of querying")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--limit", type=int, default=20000)
    ap.add_argument("--node", default="replay")
    ap.add_argument("--check", action="store_true",
                    help="compare decoded type/route against the observer's own tags")
    ap.add_argument("--out-bucket", default=None,
                    help="write replayed points here (omit to decode only)")
    ap.add_argument("--sample", type=int, default=3,
                    help="print this many generated lines")
    args = ap.parse_args()

    if args.csv:
        rows = [r for r in parse_annotated_csv(open(args.csv).read()) if r.get("raw")]
        source = args.csv
    else:
        if not args.url or not args.token:
            print("need --csv, or --url and --token (INFLUX_URL / INFLUX_TOKEN)",
                  file=sys.stderr)
            return 2
        rows = fetch_rows(args.url, args.token, args.org, args.bucket,
                          args.hours, args.limit, args.measurement)
        source = args.bucket
    print(f"fetched {len(rows)} stored frames from {source}")
    if not rows:
        return 1

    parser = MeshcorePacketParser()
    clock, deduper = Clock(), Deduper()
    writer = None
    if args.out_bucket:
        writer = InfluxWriter(args.url, args.token, args.org, args.out_bucket,
                              spool_dir="/tmp/replay-spool")
        await writer.start()

    ptype_ok = ptype_bad = route_ok = route_bad = 0
    undecodable = 0
    dups = 0
    mismatches: Counter = Counter()
    types: Counter = Counter()
    lines_out = 0
    samples = []

    for rec in rows:
        d = await decode(rec["raw"], parser)
        if d is None:
            undecodable += 1
            continue

        # Restore the readings reader.py would have supplied from the frame header.
        for src, dst, cast in (("SNR", "snr", float), ("RSSI", "rssi", int)):
            try:
                d[dst] = cast(float(rec[src]))
            except (KeyError, TypeError, ValueError):
                pass

        types[d.get("payload_typename", "UNK")] += 1

        if args.check:
            want_type = rec.get("packet_type")
            if want_type not in (None, ""):
                if str(d.get("payload_type")) == str(want_type):
                    ptype_ok += 1
                else:
                    ptype_bad += 1
                    mismatches[f"ptype got={d.get('payload_type')} want={want_type}"] += 1
            want_route = rec.get("route")
            if want_route:
                if ROUTE_LETTER.get(d.get("route_type")) == want_route:
                    route_ok += 1
                else:
                    route_bad += 1
                    mismatches[
                        f"route got={d.get('route_typename')} want={want_route}"] += 1

        dup = deduper.check(d.get("pkt_hash"))
        dups += int(dup)
        ts_ns = _ts_ns(rec.get("_time"))
        lines = points_for_frame(d, args.node, dup, clock, ts_ns)
        lines_out += len(lines)
        if len(samples) < args.sample:
            samples.extend(lines[: args.sample - len(samples)])
        if writer:
            await writer.submit(lines)

    if writer:
        await writer.stop()

    print(f"decoded {len(rows) - undecodable}, undecodable {undecodable}, "
          f"duplicates {dups} ({100 * dups / max(1, len(rows)):.1f}%)")
    print("payload types: " + ", ".join(f"{k}={v}" for k, v in types.most_common()))
    if args.check:
        tot_t, tot_r = ptype_ok + ptype_bad, route_ok + route_bad
        print(f"payload_type agreement: {ptype_ok}/{tot_t}"
              f" ({100 * ptype_ok / max(1, tot_t):.2f}%)")
        print(f"route agreement:        {route_ok}/{tot_r}"
              f" ({100 * route_ok / max(1, tot_r):.2f}%)")
        for k, v in mismatches.most_common(10):
            print(f"  mismatch {k}: {v}")
    print(f"generated {lines_out} points")
    for s in samples:
        print("  " + s)
    if writer:
        print("writer:", writer.stats())

    if args.check and (ptype_bad or route_bad):
        return 1
    return 0


def _ts_ns(t: str | None) -> int | None:
    """RFC3339 from Flux -> epoch nanoseconds, keeping sub-second precision."""
    if not t:
        return None
    from datetime import datetime
    s = t.replace("Z", "+00:00")
    if "." in s:
        head, rest = s.split(".", 1)
        frac, tz = rest[:-6], rest[-6:]
        frac = (frac + "000000000")[:9]
        base = datetime.fromisoformat(head + tz)
        return int(base.timestamp()) * 1_000_000_000 + int(frac)
    return int(datetime.fromisoformat(s).timestamp() * 1_000_000_000)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
