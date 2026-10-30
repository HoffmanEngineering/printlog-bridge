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
- **Superseded** (api#299): 403 `device_token_superseded` with no newer token stored (the API
  client already tried one) means another install of this device paired again. Check-ins and
  polls stop, said once in the log, `status` and `doctor`; prints and heartbeats go on during
  the grace. Every `desiredPollSeconds` the state folder is looked at again: a newer token there
  resumes the sync, and the 401 after the grace hands over to the re-pair path. It never pairs.

- **Sealed credentials** (story 14b; 3d-print-log-api `docs/bridge-credential-sealing.md`):
  every check-in sends the bridge's public key and its fingerprint (core/credentials.py), and,
  once a desired version has been applied, the complete list of credential issues for that
  version (`[]` when all is well). The answer says whether 3D Print Log uses the key, or holds it
  until the owner confirms it in the app: `status`, `doctor` and the log say so, with the
  fingerprint to compare. A changed set of issues brings the next check-in forward.

What it remembers is in `<state_dir>/bridge.json`: the last desired object (sealed values as
sent: only the key opens them) and its `ETag` (so a restart watches the app's printers before 3D
Print Log answers), the bridge's id in 3D Print Log (the associated data of every sealed value),
the last `scanRequestedAt` acted on, `lastSweepAt`, and the scope re-pair record.
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
    SUPERSEDED,
    ApiClient,
    ApiRejected,
    ApiUnavailable,
    AuthRejected,
    TokenSuperseded,
)
from printlog_bridge.core.app_printers import DesiredPrinter
from printlog_bridge.core.credentials import KeyStore, Opener, short_fingerprint
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
INVALID_PUBLIC_KEY = "invalid_public_key"
ISSUES_CHECK_IN_S = 30
"""The soonest a changed set of credential issues brings the next check-in forward."""

KEY_CONFIRMED = "confirmed"
KEY_PENDING = "pending"
KEY_OTHER = "other"
KEY_REJECTED = "rejected"
KEY_UNSUPPORTED = "unsupported"
KEY_UNAVAILABLE = "unavailable"
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
Issues = Callable[[], Sequence[dict]]
Refresh = Callable[[], Awaitable[None]]


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
        keys: KeyStore | None = None,
        opener: Opener | None = None,
        issues: Issues = lambda: [],
        refresh: Refresh | None = None,
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
        self.held: str | None = None
        """Why check-ins and polls wait (another pairing replaced the token), or None."""
        self._wake: asyncio.Event | None = None
        self._registered = False
        """Whether a check-in got through in this run."""
        self.keys = keys
        self.opener = opener
        self.issues = issues
        self.refresh = refresh
        """Opens the desired list's sealed values again (a new key or bridge id)."""
        self._sent_issues: Any = None
        """The issues the last check-in carried, with their version."""
        self._last_check_in = float("-inf")
        self._key_said: tuple[str, str] | None = None
        if self.opener is not None and isinstance(self.state.get("bridge_id"), int):
            self.opener.bridge_id = self.state["bridge_id"]

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
        if self.keys is not None:
            self.keys.load()
            self._key_report(None)
        stored = self.state.get("desired")
        if isinstance(stored, dict):
            await self._apply(stored)
        self._report()
        try:
            while self.stopped is None:
                self._issues_changed()
                now = self.clock()
                if self.held is not None:
                    if now >= self._next_poll:
                        self._check_hold()
                elif now >= self._next_check_in:
                    await self.check_in()
                elif now >= self._next_poll:
                    await self.poll()
                if self.stopped is not None:
                    break
                if self._scan is not None and self.discovery is not None:
                    await self._sweep()
                wake = (
                    self._next_poll
                    if self.held is not None
                    else min(self._next_check_in, self._next_poll)
                )
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
        key = self.keys.key if self.keys is not None else None
        if key is not None:
            body["publicKey"] = key.public_key
            body["publicKeyFingerprint"] = key.fingerprint
        issues = self._current_issues()
        if issues is not None:
            body["credentialIssuesVersion"], body["credentialIssues"] = issues
        return body

    def _current_issues(self) -> tuple[str, list[dict]] | None:
        """The desired version applied and the complete list of its credential issues, or None
        before there is one to describe (no version applied, or the bridge's id not known yet,
        so nothing could be opened: a missing list keeps what 3D Print Log has)."""
        version = self.desired_version
        if version is None or (self.opener is not None and self.opener.bridge_id is None):
            return None
        return version, list(self.issues())

    def _issues_changed(self) -> None:
        """Brings the next check-in forward when the issues changed since the last one."""
        if not self._registered:
            return
        if self._current_issues() != self._sent_issues:
            soonest = self._last_check_in + ISSUES_CHECK_IN_S
            self._next_check_in = min(self._next_check_in, max(self.clock(), soonest))

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
        if self.keys is not None and self.keys.load():  # rotate-key while running
            await self._refresh()
        body = self.body()
        self._last_check_in = now
        try:
            answer = await self.api.check_in(body)
        except TokenSuperseded:
            self._hold()
            return
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
        if "credentialIssues" in body:
            self._sent_issues = (body["credentialIssuesVersion"], body["credentialIssues"])
        self._timings(answer)
        self.state["last_check_in_at"] = _iso(self.now())
        self.board.bridge(check_in={"at": self.state["last_check_in_at"], "problem": None})
        rejected = [item for item in answer.get("rejected") or [] if isinstance(item, dict)]
        for item in rejected:
            self._log_rejected(item)
        self._key_report(answer, rejected)
        bridge_id = answer.get("bridgeId")
        if isinstance(bridge_id, int) and bridge_id != self.state.get("bridge_id"):
            self.state["bridge_id"] = bridge_id
            if self.opener is not None:
                self.opener.bridge_id = bridge_id
            await self._refresh()
        desired = answer.get("desired")
        if isinstance(desired, dict):
            await self._desired(desired, None)
        self._save()
        self._start_discovery()
        self._report()

    async def _refresh(self) -> None:
        if self.refresh is not None and isinstance(self.state.get("desired"), dict):
            try:
                await self.refresh()
            except Exception:  # never stops the sync
                log.exception("couldn't apply the desired printers again")

    def _key_report(
        self, answer: dict[str, Any] | None, rejected: Sequence[dict[str, Any]] = ()
    ) -> None:
        """Where the key stands with 3D Print Log, for `status` and `doctor`, and the log
        when it changes. `answer` is None before the first check-in."""
        if self.keys is None:
            return
        key = self.keys.key
        if key is None:
            state, detail = KEY_UNAVAILABLE, self.keys.problem or "no key"
        elif answer is None:
            state, detail = None, None
        elif any(i.get("code") == INVALID_PUBLIC_KEY for i in rejected):
            state = KEY_REJECTED
            detail = "3D Print Log refused this bridge's key as malformed (invalid_public_key)"
        elif "publicKeyFingerprint" not in answer and "pendingPublicKeyFingerprint" not in answer:
            state = KEY_UNSUPPORTED
            detail = "this 3D Print Log doesn't take encryption keys yet"
        elif answer.get("publicKeyFingerprint") == key.fingerprint:
            state, detail = KEY_CONFIRMED, "in use: 3D Print Log seals credentials to it"
        elif answer.get("pendingPublicKeyFingerprint") == key.fingerprint:
            state = KEY_PENDING
            short = short_fingerprint(key.fingerprint)
            detail = (
                "waiting for confirmation in the app: open Bridges in 3D Print Log, check that "
                f"the fingerprint it shows for this bridge's new key is {short}, and confirm it"
            )
        else:
            state = KEY_OTHER
            detail = (
                "3D Print Log uses another key for this bridge and isn't holding this one for "
                "confirmation (it may have changed keys too often today): it's reported again "
                "at every check-in"
            )
        fingerprint = key.fingerprint if key is not None else None
        self.board.bridge(key={"fingerprint": fingerprint, "state": state, "detail": detail})
        said = (str(state), str(fingerprint))
        if state is None or said == self._key_said:
            return
        self._key_said = said
        if state in (KEY_PENDING, KEY_OTHER, KEY_REJECTED, KEY_UNAVAILABLE):
            short = f" ({short_fingerprint(fingerprint)})" if fingerprint else ""
            log.warning("this bridge's encryption key%s: %s", short, detail)
        elif state == KEY_CONFIRMED:
            log.info(
                "3D Print Log uses this bridge's encryption key (%s)",
                short_fingerprint(fingerprint),
            )

    def _timings(self, answer: dict[str, Any]) -> None:
        check_in = answer.get("checkInSeconds")
        poll = answer.get("desiredPollSeconds")
        if isinstance(check_in, (int, float)) and check_in > 0:
            self.check_in_s = max(MIN_CHECK_IN_S, float(check_in))
            self._next_check_in = self.clock() + self.check_in_s
        if isinstance(poll, (int, float)) and poll > 0:
            self.poll_s = max(MIN_POLL_S, float(poll))
        self._next_poll = self.clock() + self.poll_s

    def _log_rejected(self, item: dict[str, Any]) -> None:
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

    def _hold(self) -> None:
        """Another pairing replaced the token: no check-ins or polls until that changes."""
        self._next_poll = self.clock() + self.poll_s
        if self.held is not None:
            return
        self.held = SUPERSEDED
        log.warning("%s.", SUPERSEDED)
        self._problem(SUPERSEDED)

    def _check_hold(self) -> None:
        """Ends the hold once the token isn't superseded any more: a newer one was stored here,
        or the grace ended (then the check-in says a re-pair is needed)."""
        self._next_poll = self.clock() + self.poll_s
        if self.api.still_superseded():
            return
        self.held = None
        self.device = self.api.device or self.device
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
        except TokenSuperseded:
            self._hold()
            return
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
