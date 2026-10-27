"""The bridge's check-in with 3D Print Log and its desired printers (#100; the contract is
3d-print-log-api PR #288, "Contract for story 12").

- **Check-in**, `PUT /api/bridges/me`, at start, every `checkInSeconds` and right after a sweep:
  the bridge's version, mode and host name, the private networks it sees, when it last swept,
  and (with discovery on) the printers it recognized, those in the config file as
  `configured: true`. The answer carries the desired list.
- **Poll**, `GET /api/bridges/me/desired` every `desiredPollSeconds` with the last `ETag`: 304 is
  unchanged; 404 `bridge_not_registered` means check in first.
- **Desired list**: when its `version` (opaque, compared for equality) changes, the printers it
  names are applied (core/app_printers.py). When `scanRequestedAt` is set and differs from the
  last value acted on (never compared with this clock), one sweep runs, then a check-in.
- **Independent of printing**: this runs beside the printers' watchers, and nothing it meets
  stops them. 429 backs the check-in off (the poll goes on: reads aren't budgeted).
- **Scope**: a device token paired before `bridge:sync` existed gets 403 `insufficient_scope`.
  The bridge pairs again once (the code is in the log), only when 3D Print Log offers the scope;
  if the scope is still missing, it says so and stops syncing. It never loops re-pairing, also
  across restarts: `bridge.json` records the token that was refused.

What it remembers is in `<state_dir>/bridge.json`: the last desired object and its `ETag` (so a
restart watches the app's printers before 3D Print Log answers), the last `scanRequestedAt` acted
on, `lastSweepAt`, and the scope re-pair record.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import logging
import re
import socket
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from printlog_bridge import __version__
from printlog_bridge.core.api_client import (
    ApiClient,
    ApiRejected,
    ApiUnavailable,
    AuthRejected,
)
from printlog_bridge.core.app_printers import DesiredPrinter
from printlog_bridge.core.device import DeviceToken, write_private_file
from printlog_bridge.core.discovery import MAX_FOUND, DiscoveryService, Found
from printlog_bridge.core.status import StatusBoard

log = logging.getLogger(__name__)

STATE_FILE = "bridge.json"
CHECK_IN_S = 300
POLL_S = 60
MIN_CHECK_IN_S = 30
MIN_POLL_S = 10
FAILED_RETRY_S = 60
MAX_BACKOFF_S = 30 * 60
MAX_NETWORKS = 16
INSUFFICIENT_SCOPE = "insufficient_scope"
NOT_SUPPORTED = ("device_endpoint_not_allowed", "device_token_required")

NO_SCOPE = (
    "3D Print Log doesn't let this bridge check in: it was paired before bridges could "
    "(the bridge:sync permission). Printers in the config file are still logged. Run "
    "`printlog-bridge pair` to pair it again and allow it; printers connected in the app "
    'and "Found on your network" need it'
)
UNSUPPORTED = (
    "this 3D Print Log doesn't take bridge check-ins yet, so printers connected in the app "
    'and "Found on your network" aren\'t available. Printers in the config file are still logged'
)

Repair = Callable[[], Awaitable[Optional[DeviceToken]]]
Apply = Callable[[Sequence[DesiredPrinter]], Awaitable[None]]


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def host_name() -> str | None:
    name = re.sub(r"[\x00-\x1f\x7f]", "", socket.gethostname()).strip()[:100]
    return name or None


class BridgeSync:
    def __init__(
        self,
        api: ApiClient,
        state_dir: Path,
        board: StatusBoard,
        apply: Apply,
        *,
        device: DeviceToken | None,
        discovery: DiscoveryService | None = None,
        networks: Callable[[], Sequence[str]] = lambda: [],
        configured: Callable[[], Sequence[Found]] = lambda: [],
        is_configured: Callable[[Found], bool] = lambda found: False,
        mode: str = "remote",
        repair: Repair | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.api = api
        self.path = state_dir / STATE_FILE
        self.board = board
        self.apply = apply
        self.device = device
        self.discovery = discovery
        self.networks = networks
        self.configured = configured
        self.is_configured = is_configured
        self.mode = mode
        self.repair = repair
        self.clock = clock
        self.now = now
        self.check_in_s: float = CHECK_IN_S
        self.poll_s: float = POLL_S
        self.state = self._load()
        self._next_check_in = 0.0
        self._next_poll = 0.0
        self._backoff = 0.0
        self._scan: str | None = None
        """A `scanRequestedAt` waiting for its sweep."""
        self._rejected_logged: set[tuple[str, str]] = set()
        self._discovering: asyncio.Task | None = None
        self.stopped: str | None = None
        """Why syncing stopped for good (a missing scope), or None."""
        self._wake: asyncio.Event | None = None
        self._registered = False
        """Whether a check-in got through in this run."""

    # -- the state file ------------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self) -> None:
        try:
            write_private_file(self.path, json.dumps(self.state, indent=1))
        except OSError as e:
            log.warning("couldn't write %s: %s", self.path, e)

    @property
    def desired_version(self) -> str | None:
        desired = self.state.get("desired")
        return desired.get("version") if isinstance(desired, dict) else None

    # -- running -------------------------------------------------------------------------------

    async def run(self) -> None:
        """Checks in and polls until cancelled, or until it can't for good (a missing scope)."""
        self._wake = asyncio.Event()
        stored = self.state.get("desired")
        if isinstance(stored, dict):
            await self._apply(stored)
        self._report()
        try:
            while self.stopped is None:
                now = self.clock()
                if now >= self._next_check_in:
                    await self.check_in()
                elif now >= self._next_poll:
                    await self.poll()
                if self.stopped is not None:
                    break
                if self._scan is not None and self.discovery is not None:
                    await self._sweep()
                wake = min(self._next_check_in, self._next_poll)
                if self._scan is not None:
                    wake = min(wake, self.clock() + 5)
                self._wake.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), max(0.0, wake - self.clock()))
        finally:
            await self._stop_discovery()

    def wake(self) -> None:
        """Checks in now (a test, or a config change)."""
        self._next_check_in = 0.0
        if self._wake is not None:
            self._wake.set()

    async def _stop_discovery(self) -> None:
        if self._discovering is not None:
            self._discovering.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._discovering
            self._discovering = None

    def _start_discovery(self) -> None:
        if self.discovery is not None and self._discovering is None:
            log.info("discovery: looking for printers on the network (discovery = false stops it)")
            self._discovering = asyncio.ensure_future(self.discovery.run())

    # -- check-in ------------------------------------------------------------------------------

    def body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"agentVersion": __version__, "mode": self.mode}
        name = host_name()
        if name:
            body["hostName"] = name
        body["networks"] = list(dict.fromkeys(self.networks()))[:MAX_NETWORKS]
        if self.state.get("last_sweep_at"):
            body["lastSweepAt"] = self.state["last_sweep_at"]
        if self.discovery is not None:
            body["discovered"] = self.discovered()
        return body

    def discovered(self) -> list[dict[str, Any]]:
        """What discovery found, plus the config file's printers, each once, configured ones
        first (they are what the app must never offer to connect)."""
        configured: list[Found] = []
        for entry in self.configured():
            cleaned = entry.cleaned()
            if cleaned is not None and not any(c.same_printer(cleaned) for c in configured):
                configured.append(cleaned)
        out = []
        found = self.discovery.found() if self.discovery is not None else []
        for f in found:
            match = next((c for c in configured if c.same_printer(f)), None)
            if match is not None:
                configured.remove(match)
                out.append((True, f))
            else:
                out.append((self.is_configured(f), f))
        out.extend((True, c) for c in configured)
        out.sort(key=lambda pair: not pair[0])
        seen: set[str] = set()
        result = []
        for is_configured, f in out:
            if f.identity in seen:
                continue
            seen.add(f.identity)
            result.append(f.to_json(is_configured))
        return result[:MAX_FOUND]

    async def check_in(self) -> None:
        now = self.clock()
        self._next_check_in = now + self.check_in_s
        try:
            answer = await self.api.check_in(self.body())
        except ApiRejected as e:
            await self._refused(e)
            return
        except ApiUnavailable as e:
            if e.status == 429:
                self._backoff = min(MAX_BACKOFF_S, max(self._backoff * 2, self.check_in_s))
                wait = max(e.retry_after or 0.0, self._backoff)
                self._next_check_in = now + wait
                self._problem(f"3D Print Log is limiting check-ins; the next in {wait:.0f}s")
            else:
                self._next_check_in = now + min(self.check_in_s, FAILED_RETRY_S)
                self._problem(f"couldn't check in: {e}")
            return
        except AuthRejected as e:  # a re-pair needed: the API client says so already
            self._problem(str(e))
            return
        self._backoff = 0.0
        self._registered = True
        self._timings(answer)
        self.state["last_check_in_at"] = _iso(self.now())
        self.board.bridge(check_in={"at": self.state["last_check_in_at"], "problem": None})
        for item in answer.get("rejected") or []:
            self._log_rejected(item)
        desired = answer.get("desired")
        if isinstance(desired, dict):
            await self._desired(desired, None)
        self._save()
        self._start_discovery()
        self._report()

    def _timings(self, answer: dict[str, Any]) -> None:
        check_in = answer.get("checkInSeconds")
        poll = answer.get("desiredPollSeconds")
        if isinstance(check_in, (int, float)) and check_in > 0:
            self.check_in_s = max(MIN_CHECK_IN_S, float(check_in))
            self._next_check_in = self.clock() + self.check_in_s
        if isinstance(poll, (int, float)) and poll > 0:
            self.poll_s = max(MIN_POLL_S, float(poll))
        self._next_poll = self.clock() + self.poll_s

    def _log_rejected(self, item: Any) -> None:
        if not isinstance(item, dict):
            return
        key = (str(item.get("field")), str(item.get("code")))
        if key in self._rejected_logged:
            return
        self._rejected_logged.add(key)
        log.warning("3D Print Log didn't take %s of the check-in (%s)", key[0], key[1])

    async def _refused(self, error: ApiRejected) -> None:
        if error.code == INSUFFICIENT_SCOPE:
            await self._scope_missing()
        elif error.code in NOT_SUPPORTED or error.status == 404:
            self._stop(UNSUPPORTED)
        else:
            self._problem(f"3D Print Log refused the check-in: {error}")

    async def _scope_missing(self) -> None:
        """Pairs again, once, to get `bridge:sync`; otherwise stops."""
        token = self.device.token if self.device is not None else ""
        record = self.state.get("scope_repair") or {}
        sha = _sha(token)
        if self.repair is None or sha in (record.get("refused"), record.get("result")):
            self._stop(NO_SCOPE)
            return
        self.state["scope_repair"] = {"refused": sha}
        self._save()
        log.warning(
            "3D Print Log answered that this bridge was paired without the bridge:sync "
            "permission. Pairing it again, once, to ask for it: approve the code below. Printers "
            "keep being logged meanwhile."
        )
        self._problem("pairing again to get the bridge:sync permission: see the log for the code")
        try:
            stored = await self.repair()
        except Exception as e:
            log.warning("pairing again didn't work: %s", e)
            stored = None
        if stored is None:
            self._stop(NO_SCOPE)
            return
        self.device = stored
        self.state["scope_repair"] = {"refused": sha, "result": _sha(stored.token)}
        self._save()
        self._next_check_in = 0.0

    def _stop(self, why: str) -> None:
        if self.stopped is None:
            log.error("%s.", why)
        self.stopped = why
        self._problem(why)

    def _problem(self, problem: str) -> None:
        self.board.bridge(check_in={"at": self.state.get("last_check_in_at"), "problem": problem})

    # -- the desired list ----------------------------------------------------------------------

    async def poll(self) -> None:
        self._next_poll = self.clock() + self.poll_s
        try:
            status, desired, etag = await self.api.desired(self.state.get("etag"))
        except ApiRejected as e:
            if e.code == INSUFFICIENT_SCOPE:
                await self._scope_missing()
            else:
                log.debug("polling the desired printers was refused: %s", e)
            return
        except (ApiUnavailable, AuthRejected) as e:
            log.debug("couldn't poll the desired printers: %s", e)
            return
        if status == 404:
            if self._registered:  # bridge_not_registered: 3D Print Log lost it; check in again
                self._registered = False
                self._next_check_in = 0.0
            return
        if status == 200 and desired is not None:
            await self._desired(desired, etag)
            self._save()

    async def _desired(self, desired: dict[str, Any], etag: str | None) -> None:
        version = desired.get("version")
        if etag:
            self.state["etag"] = etag
        if version != self.desired_version:
            self.state["desired"] = desired
            if etag is None:
                self.state.pop("etag", None)  # from a check-in: the poll learns the new one
            log.info("3D Print Log's desired printers changed (version %s)", version)
            await self._apply(desired)
            self._report()
        scan = desired.get("scanRequestedAt")
        if scan is not None and scan != self.state.get("scan_acted") and scan != self._scan:
            if self.discovery is None:
                log.info("a scan was asked for in 3D Print Log, but discovery = false here")
                self.state["scan_acted"] = scan
            else:
                self._scan = scan

    async def _apply(self, desired: dict[str, Any]) -> None:
        entries = [DesiredPrinter.from_json(p) for p in desired.get("printers") or []]
        try:
            await self.apply([e for e in entries if e is not None])
        except Exception:  # applying must never stop the sync
            log.exception("couldn't apply the desired printers")

    async def _sweep(self) -> None:
        assert self.discovery is not None
        networks = []
        for text in self.networks():
            with contextlib.suppress(ValueError):
                network = ipaddress.ip_network(text)
                if network.version == 4:
                    networks.append(network)
        if not await self.discovery.sweep(networks):
            return  # too soon after the last: tried again in a few seconds
        self.state["scan_acted"] = self._scan
        self.state["last_sweep_at"] = _iso(self.now())
        self._scan = None
        self._save()
        await self.check_in()

    def _report(self) -> None:
        self.board.bridge(
            desired_version=self.desired_version,
            discovery=(self.discovery.status() if self.discovery is not None else None),
            last_sweep_at=self.state.get("last_sweep_at"),
        )
