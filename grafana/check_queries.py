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


def run(url, token, org, q):
    r = requests.post(
        f"{url.rstrip('/')}/api/v2/query", params={"org": org},
        data=resolve(q).encode(),
        headers={"Authorization": f"Token {token}",
                 "Content-Type": "application/vnd.flux",
                 "Accept": "application/csv"},
        timeout=90)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:300]}"
    rows = [l for l in r.text.splitlines()
            if l.strip() and not l.startswith("#") and not l.startswith(",result")]
    return len(rows), None


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

    bad = 0
    empty = []
    for name, q in checks:
        rows, err = run(url, token, org, q)
        if err:
            bad += 1
            print(f"FAIL  {name}\n      {err}")
        elif rows == 0:
            empty.append(name)
            print(f"EMPTY {name}")
        else:
            print(f"ok    {name}  ({rows} rows)")
    print(f"\n{len(checks)} queries: {len(checks) - bad - len(empty)} ok, "
          f"{len(empty)} empty, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
