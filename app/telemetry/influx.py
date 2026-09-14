"""Batching InfluxDB v2 writer with an on-disk spool for outages.

Two design points are load-bearing on this box:

* The Pi writes to an SD card and has already destroyed two of them, so the
  steady-state path never touches disk. Spooling happens only when InfluxDB is
  unreachable, and drains back to memory as soon as it returns.
* InfluxDB lives across an isolated VLAN boundary, so "unreachable" is a normal
  operating state, not an exception. Losing the link must cost points, not the
  collector process.
"""
import asyncio
import gzip
import logging
import os
import time
from pathlib import Path

import requests

log = logging.getLogger("meshweb.telemetry.influx")


class InfluxWriter:
    def __init__(self, url: str, token: str, org: str, bucket: str, *,
                 batch_size: int = 500, flush_interval: float = 5.0,
                 spool_dir: str = "/data/spool", spool_max_bytes: int = 32 * 1024 * 1024,
                 timeout: float = 10.0):
        self.url = url.rstrip("/")
        self.token = token
        self.org = org
        self.bucket = bucket
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.spool_dir = Path(spool_dir)
        self.spool_max_bytes = spool_max_bytes
        self.timeout = timeout

        self._buf: list[str] = []
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._session: requests.Session | None = None

        self.points_written = 0
        self.points_dropped = 0
        self.write_errors = 0
        self.last_error: str | None = None
        self.last_write_ok: float | None = None

    # ---------- lifecycle ----------

    async def start(self) -> None:
        self._session = requests.Session()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # Best effort: a clean shutdown should not silently bin the last batch.
        if self._buf:
            await self._flush()
        if self._session:
            self._session.close()

    # ---------- producer side ----------

    def write(self, line: str | None) -> None:
        """Queue one line. None is accepted so callers can pass point() straight through."""
        if not line:
            return
        self._buf.append(line)
        if len(self._buf) >= self.batch_size * 4:
            # The flusher is not keeping up (Influx down and spool full). Shed
            # the oldest rather than growing until the process is OOM-killed.
            over = len(self._buf) - self.batch_size * 4
            del self._buf[:over]
            self.points_dropped += over

    def write_many(self, lines) -> None:
        for ln in lines:
            self.write(ln)

    async def submit(self, lines) -> None:
        """Bulk-load entry point, with backpressure instead of shedding.

        write() must never block -- it is called from the radio event handler,
        where stalling would back up the serial reader -- so it drops on
        overflow. A bulk loader has the opposite requirement: it can afford to
        wait and must not silently lose points, so it flushes as it fills
        rather than letting the buffer hit the shedding threshold.
        """
        for ln in lines:
            if ln:
                self._buf.append(ln)
        if len(self._buf) >= self.batch_size:
            await self._flush()

    # ---------- flush loop ----------

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(self.flush_interval)
                await self._flush()
                await self._drain_spool()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("flush loop error")

    async def _flush(self) -> None:
        while self._buf:
            batch = self._buf[: self.batch_size]
            del self._buf[: len(batch)]
            body = "\n".join(batch)
            ok, retryable, err = await asyncio.to_thread(self._post, body)
            if ok:
                self.points_written += len(batch)
                self.last_write_ok = time.time()
                continue
            self.write_errors += 1
            self.last_error = err
            if retryable:
                self._spool(body)
            else:
                # A 400 means these lines are malformed; retrying forever would
                # wedge the spool behind data that can never be accepted.
                self.points_dropped += len(batch)
                log.error("influx rejected %d points (%s); sample: %s",
                          len(batch), err, batch[0][:200])
            return   # stop after the first failure; the loop retries later

    def _post(self, body: str) -> tuple[bool, bool, str | None]:
        """Returns (ok, retryable, error). Runs off the event loop."""
        assert self._session is not None
        try:
            r = self._session.post(
                f"{self.url}/api/v2/write",
                params={"org": self.org, "bucket": self.bucket, "precision": "ns"},
                data=gzip.compress(body.encode()),
                headers={
                    "Authorization": f"Token {self.token}",
                    "Content-Type": "text/plain; charset=utf-8",
                    "Content-Encoding": "gzip",
                },
                timeout=self.timeout,
            )
        except Exception as e:
            return False, True, f"{type(e).__name__}: {e}"
        if r.status_code in (200, 204):
            return True, False, None
        # 429/503 carry backpressure, 5xx is transient, 401/404 are config
        # errors that will resolve when someone fixes them -- all worth keeping.
        retryable = r.status_code in (401, 403, 404, 408, 429) or r.status_code >= 500
        return False, retryable, f"HTTP {r.status_code}: {r.text[:200]}"

    # ---------- spool ----------

    def _spool_files(self) -> list[Path]:
        if not self.spool_dir.is_dir():
            return []
        return sorted(self.spool_dir.glob("*.lp"))

    def spool_bytes(self) -> int:
        return sum(f.stat().st_size for f in self._spool_files() if f.exists())

    def _spool(self, body: str) -> None:
        try:
            self.spool_dir.mkdir(parents=True, exist_ok=True)
            blob = body.encode()
            # Trim oldest first: during a long outage recent mesh activity is
            # more useful than the packets that were heard when it started.
            files = self._spool_files()
            total = sum(f.stat().st_size for f in files)
            while files and total + len(blob) > self.spool_max_bytes:
                victim = files.pop(0)
                total -= victim.stat().st_size
                self.points_dropped += victim.read_text().count("\n") + 1
                victim.unlink(missing_ok=True)
            if len(blob) > self.spool_max_bytes:
                self.points_dropped += body.count("\n") + 1
                return
            path = self.spool_dir / f"{time.time_ns()}.lp"
            path.write_bytes(blob)
        except Exception as e:
            self.points_dropped += body.count("\n") + 1
            log.warning("could not spool points: %s", e)

    async def _drain_spool(self) -> None:
        for path in self._spool_files()[:4]:      # a few per cycle, not a stampede
            try:
                body = path.read_text()
            except OSError:
                continue
            if not body.strip():
                path.unlink(missing_ok=True)
                continue
            ok, retryable, err = await asyncio.to_thread(self._post, body)
            if ok:
                self.points_written += body.count("\n") + 1
                self.last_write_ok = time.time()
                path.unlink(missing_ok=True)
            elif not retryable:
                self.points_dropped += body.count("\n") + 1
                path.unlink(missing_ok=True)
                log.error("dropping unacceptable spool file %s (%s)", path.name, err)
            else:
                self.last_error = err
                return       # still down; leave the rest alone

    # ---------- introspection ----------

    def stats(self) -> dict:
        return {
            "points_written": self.points_written,
            "points_dropped": self.points_dropped,
            "write_errors": self.write_errors,
            "queued": len(self._buf),
            "spool_bytes": self.spool_bytes(),
            "last_error": self.last_error,
            "last_write_ok": self.last_write_ok,
        }
