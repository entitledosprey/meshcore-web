#!/usr/bin/env python3
"""Run every Flux query in the dashboard against InfluxDB and report on it.

Grafana shows a broken query as an empty panel, which looks exactly like a
panel whose data has not arrived yet -- so a dashboard can ship with several
dead panels and appear merely idle. This substitutes the dashboard variables
and executes each query for real.

    INFLUX_URL=... INFLUX_TOKEN=... INFLUX_ORG=... \
        python3 grafana/check_queries.py grafana/meshcore-platform.json
"""
import json
import os
import sys

import requests

SUBS = {
    "${bucket}": "meshcore",
    "${node:regex}": ".*",
    "v.timeRangeStart": "-72h",
    "v.timeRangeStop": "now()",
    "v.windowPeriod": "5m",
}


def resolve(q: str) -> str:
    for k, v in SUBS.items():
        q = q.replace(k, v)
    return q


def group_columns(text: str) -> list[str]:
    """Columns in the Flux group key, minus the ones Grafana handles itself.

    This is what Grafana turns into a series name, so it is the direct
    predictor of the legend. A stray _start/_stop/_measurement/_field here is
    what produces legends like `_value {_start="2026-09-14T..."}`.
    """
    cols: list[str] = []
    flags = header = None
    for line in text.splitlines():
        if line.startswith("#group"):
            flags = line.split(",")[1:]
            header = None
        elif flags and header is None and line.startswith(","):
            header = line.split(",")[1:]
            for f, name in zip(flags, header):
                if f == "true" and name not in ("_start", "_stop") and name not in cols:
                    cols.append(name)
            for f, name in zip(flags, header):
                if f == "true" and name in ("_start", "_stop") and name not in cols:
                    cols.append(name)
            flags = None
    return [c for c in cols if c not in ("result", "table", "_time", "_value")]


def run(url, token, org, q):
    # Posted as JSON rather than raw Flux so the dialect can ask for the
    # #group annotation. Without it InfluxDB returns a bare header and the
    # group key -- the thing that becomes the Grafana series name -- is
    # invisible, which makes the legend check silently report nothing.
    r = requests.post(
        f"{url.rstrip('/')}/api/v2/query", params={"org": org},
        json={"query": resolve(q),
              "dialect": {"annotations": ["group", "datatype", "default"]}},
        headers={"Authorization": f"Token {token}",
                 "Content-Type": "application/json",
                 "Accept": "application/csv"},
        timeout=90)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:300]}"
    rows = [l for l in r.text.splitlines()
            if l.strip() and not l.startswith("#") and not l.startswith(",result")]
    return (len(rows), group_columns(r.text)), None


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "grafana/meshcore-platform.json"
    url, token, org = (os.environ.get("INFLUX_URL", ""),
                       os.environ.get("INFLUX_TOKEN", ""),
                       os.environ.get("INFLUX_ORG", "home"))
    dash = json.load(open(path))

    checks = []
    for p in dash["panels"]:
        for t in p.get("targets", []):
            checks.append((f"{p['type']:11s} {p['title']}", t["query"]))
    for v in dash["templating"]["list"]:
        if v.get("type") == "query":
            checks.append((f"variable    ${v['name']}", v["query"]))

    NOISE = {"_start", "_stop", "_measurement", "_field"}
    bad = 0
    empty = []
    noisy = []
    for name, q in checks:
        res, err = run(url, token, org, q)
        if err:
            bad += 1
            print(f"FAIL  {name}\n      {err}")
            continue
        rows, cols = res
        label = ("legend: " + "+".join(cols)) if cols else "legend: (none)"
        if cols and NOISE & set(cols):
            noisy.append(name)
            label += "   <-- noise in series name"
        if rows == 0:
            empty.append(name)
            print(f"EMPTY {name}")
        else:
            print(f"ok    {name}  ({rows} rows)  {label}")
    print(f"\n{len(checks)} queries: {len(checks) - bad - len(empty)} ok, "
          f"{len(empty)} empty, {bad} failed, {len(noisy)} with noisy legends")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
