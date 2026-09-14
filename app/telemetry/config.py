"""Collector configuration.

Environment variables rather than the YAML file the plan sketched: the rest of
this app is already configured that way (MESH_PORT, MESH_PASSWORD_FILE, ...),
env vars need no new dependency, and the one thing YAML was going to carry --
which repeaters to poll -- turned out to be already modelled. OwnedStore and
PasswordStore hold exactly "repeaters you own, with credentials", and the web
console already edits both, so a second list in a config file would only be a
way for the two to disagree.
"""
import os
from dataclasses import dataclass


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass
class TelemetryConfig:
    enabled: bool
    url: str
    token: str
    org: str
    bucket: str
    node: str
    spool_dir: str
    spool_max_bytes: int
    batch_size: int
    flush_interval: float
    local_interval: float
    repeater_interval: float
    neighbour_interval: float
    repeater_stagger: float
    dedupe_ttl: float
    decrypt_channels: bool

    @classmethod
    def from_env(cls) -> "TelemetryConfig":
        token = os.environ.get("INFLUX_TOKEN", "")
        url = os.environ.get("INFLUX_URL", "")
        return cls(
            # No URL or token means "run the console without telemetry", which
            # is how this behaves before the VLAN rule for 8086 exists.
            enabled=bool(url and token)
            and os.environ.get("TELEMETRY_ENABLED", "1") not in ("0", "false", "no"),
            url=url,
            token=token,
            org=os.environ.get("INFLUX_ORG", "home"),
            bucket=os.environ.get("INFLUX_BUCKET", "meshcore"),
            node=os.environ.get("TELEMETRY_NODE", ""),
            spool_dir=os.environ.get("TELEMETRY_SPOOL_DIR", "/data/spool"),
            spool_max_bytes=int(_f("TELEMETRY_SPOOL_MAX_MB", 32) * 1024 * 1024),
            batch_size=int(_f("TELEMETRY_BATCH_SIZE", 500)),
            flush_interval=_f("TELEMETRY_FLUSH_INTERVAL", 5.0),
            local_interval=_f("TELEMETRY_LOCAL_INTERVAL", 60.0),
            # Polling transmits. These are floors as much as defaults: pulling
            # status from every repeater every minute would put real load on a
            # shared duty-cycled band for data that changes slowly.
            repeater_interval=max(300.0, _f("TELEMETRY_REPEATER_INTERVAL", 300.0)),
            neighbour_interval=max(900.0, _f("TELEMETRY_NEIGHBOUR_INTERVAL", 1800.0)),
            repeater_stagger=_f("TELEMETRY_REPEATER_STAGGER", 20.0),
            dedupe_ttl=_f("TELEMETRY_DEDUPE_TTL", 90.0),
            decrypt_channels=os.environ.get("TELEMETRY_DECRYPT_CHANNELS", "0")
            in ("1", "true", "yes"),
        )
