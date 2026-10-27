"""The bridge's local page (#100, story 14d), which is also the Home Assistant app's ingress page
(#98): what `printlog-bridge status` says, the code to approve while the bridge pairs (with a QR
code), and `doctor`'s checks on request. Nothing else: printers are managed in 3D Print Log, so
the page has no form, takes no input and changes nothing.

It runs in a thread of its own (the standard library's HTTP server), so a slow or broken page
can never hold up or end the bridge's event loop, and reads what the bridge already writes down
(the status file, the config, the key) with the same functions `status` and `doctor` use.

Who may open it:

- It listens on this computer only (127.0.0.1) unless `status_page` says otherwise.
- `status_page_clients` limits it to those addresses (the Home Assistant app: the Supervisor's
  ingress proxy, 172.30.32.2).
- Otherwise the Host must be an IP address, a name without dots or a local name (`.local`,
  `.lan`, `.home.arpa`...), so a web page can't rebind its own name to the bridge and read it.
- It answers GET and HEAD only, and every answer forbids scripts, other origins' frames,
  caching and sniffing. Every value is escaped and scrubbed of the bridge's secrets.
"""

from __future__ import annotations

import asyncio
import html
import io
import ipaddress
import logging
import os
import re
import secrets
import socket
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

from printlog_bridge import __version__
from printlog_bridge.core import pairing, qr
from printlog_bridge.core.checks import CheckResult, Status
from printlog_bridge.core.config import API_KEY_ENV, BridgeConfig, ConfigError, PageAddress
from printlog_bridge.core.doctor import diagnose as run_diagnose
from printlog_bridge.core.doctor import format_report
from printlog_bridge.core.secrets import scrub
from printlog_bridge.core.status import (
    _ago,
    format_status,
    is_running,
    key_line,
    read_status,
    utc_now,
)
from printlog_bridge.core.support import known_secrets

log = logging.getLogger(__name__)
T = TypeVar("T")

DOCTOR_TIMEOUT_S = 60
DOCTOR_REUSE_S = 30
"""The checks reach the API and every printer, so a result is reused for this long."""
REFRESH_S, REFRESH_PAIRING_S, REFRESH_CHECKS_S = 15, 5, 3
REQUEST_TIMEOUT_S = 10
"""How long one read or write on a connection may take."""
REQUEST_DEADLINE_S = 10
"""How long a client has to send its whole request line and headers. A slow one is cut off, so
nobody can hold one of the MAX_REQUESTS slots by trickling bytes."""
MAX_REQUESTS = 8
"""Requests answered at once; more are dropped, so a flood can't start a thread each."""
CONFIG_ERROR = (
    "The config file has an error; see the bridge's log (`printlog-bridge doctor` prints it)."
)
"""Shown instead of the error itself, which can quote the line, and a line can hold a secret."""
FETCH_SITES = ("same-origin", "none")
"""`Sec-Fetch-Site` values answered: the page's own links and refreshes (Home Assistant's ingress
is the same origin), and an address typed or bookmarked. Another site, even a sibling one on the
same domain, can't make the bridge run its checks."""
FETCH_DESTS = ("document", "iframe")
"""`Sec-Fetch-Dest` values answered: a page, or Home Assistant's ingress frame. Never an image,
script or fetch another page embeds."""
LOCAL_SUFFIXES = (".local", ".lan", ".home", ".internal", ".localdomain", ".home.arpa")
_INGRESS_PATH = re.compile(r"/api/hassio_ingress/[A-Za-z0-9_-]+")
_HOST_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?")
APP_PAGE = "https://www.3dprintlog.com"


@dataclass
class Response:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


Diagnose = Callable[[], Sequence[CheckResult]]


class LocalPage:
    def __init__(
        self,
        address: PageAddress,
        clients: Sequence[str],
        load: Callable[[], BridgeConfig],
        *,
        env: Mapping[str, str],
        adapters: Mapping[str, Any] | None = None,
        home: Path | None = None,
        pending: pairing.PendingCodes | None = None,
        diagnose: Diagnose | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.address = address
        self.clients = {_ip(c) for c in clients}
        self.load = load
        self.env = dict(env)
        self.adapters = adapters
        self.home = home or Path.home()
        self.pending = pending or pairing.pending
        self.diagnose = diagnose or self._diagnose
        self.clock = clock
        self.request_deadline = REQUEST_DEADLINE_S
        self._doctor_lock = threading.Lock()
        self._doctor: tuple[float, list[CheckResult] | None] | None = None
        self._doctor_since: float | None = None
        """When the checks running now started, or None."""
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    # -- serving ---------------------------------------------------------------------------------

    def start(self) -> bool:
        """Listens in a thread of its own. False (and a warning) when it can't: the bridge
        carries on without the page."""
        try:
            self._server = _Server(self)
        except OSError as e:
            log.warning(
                "the local page can't listen on %s (%s); the bridge carries on without it. "
                "Set status_page in [bridge] to another port, or off.",
                self.address.url(), e.strerror or e,
            )  # fmt: skip
            return False
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="printlog-bridge local page", daemon=True
        )
        self._thread.start()
        log.info("local page (status, pairing, doctor): %s", self._where())
        return True

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(5)
        self._server = self._thread = None

    @property
    def port(self) -> int:
        return self._server.server_address[1] if self._server else self.address.port

    def _where(self) -> str:
        url = PageAddress(self.address.host, self.port).url()
        if self.clients:
            return f"{url} (only for {', '.join(sorted(str(c) for c in self.clients))})"
        if self.address.host in ("0.0.0.0", "::"):
            return f"port {self.port} on every network address of this computer"
        return url

    def allows(self, peer: str) -> bool:
        """Whether `peer` may open the page at all (`status_page_clients`). Checked as a
        connection is accepted, before it takes a slot or anything is read."""
        return not self.clients or _ip(peer) in self.clients

    # -- answering -------------------------------------------------------------------------------

    def respond(self, method: str, target: str, headers: Mapping[str, str], peer: str) -> Response:
        nonce = secrets.token_urlsafe(16)
        try:
            response = self._respond(method, target, headers, peer, nonce)
        except Exception:  # a bug in the page must never reach the bridge, nor its details
            log.exception("the local page failed")
            response = _plain(500, "The page failed; the bridge's log says why.")
        response.headers.update(_security_headers(nonce))
        if method == "HEAD":
            response.headers["Content-Length"] = str(len(response.body))
            response.body = b""
        return response

    def _respond(
        self, method: str, target: str, headers: Mapping[str, str], peer: str, nonce: str
    ) -> Response:
        if self.clients:
            if not self.allows(peer):
                return _plain(403, "Not for this address.")
        elif not _local_host(_header(headers, "Host")):
            return _plain(421, "Open the page with the bridge's IP address or local name.")
        if method not in ("GET", "HEAD"):
            response = _plain(405, "The page only shows; it changes nothing.")
            response.headers["Allow"] = "GET, HEAD"
            return response
        site = _header(headers, "Sec-Fetch-Site").strip().lower()
        dest = _header(headers, "Sec-Fetch-Dest").strip().lower()
        if (site and site not in FETCH_SITES) or (dest and dest not in FETCH_DESTS):
            return _plain(403, "Open the page itself: another site can't load it.")
        path = urlsplit(target).path
        prefix = _ingress_prefix(_header(headers, "X-Ingress-Path"))
        if path == "/":
            return self._status_page(prefix, nonce)
        if path == "/doctor":
            return self._doctor_page(prefix, nonce)
        return _plain(404, "Not found.")

    # -- the status page ------------------------------------------------------------------------

    def _status_page(self, prefix: str, nonce: str) -> Response:
        now = self.clock()
        code = self.pending.get()
        try:
            config = self.load()
        except ConfigError:
            text = _Text(self._env_secrets())
            body = _section("Config", f"<p class=bad>{text(CONFIG_ERROR)}</p>")
            return _html_page(_page(nonce, text, body, prefix, refresh=REFRESH_S, code=code))
        text = _Text(self._secrets(config))
        status = read_status(config.state_dir)
        running = status is not None and is_running(status, now)
        state = "pairing: waiting for approval (above)" if code else _state(status, running, now)
        summary = [("Bridge", state)]
        summary.append(("Pairing", _pairing(config, status, running, code)))
        if config.device is not None:
            reported = ((status or {}).get("bridge") or {}).get("key") if running else None
            summary.append(("Encryption key", key_line(config, reported).split(": ", 1)[-1]))
        check_in = ((status or {}).get("bridge") or {}).get("check_in") or {}
        if running and (check_in.get("at") or check_in.get("problem")):
            last = f"{_ago(now, check_in['at'])} ago" if check_in.get("at") else "none yet"
            if check_in.get("problem"):
                last += f"; {check_in['problem']}"
            summary.append(("Last check-in", last))
        rows = "".join(f"<dt>{text(k)}</dt><dd>{text(v)}</dd>" for k, v in summary)
        body = _section("Bridge", f"<dl>{rows}</dl>")
        body += _printers(config, status, running, now, text)
        details, _ = format_status(config, now)
        body += _section(
            "Details",
            f"<p>What <code>printlog-bridge status</code> says.</p><pre>{text(details)}</pre>",
        )
        body += _section(
            "Checks",
            f'<p><a href="{prefix}doctor">Run the checks</a> (<code>printlog-bridge doctor</code>'
            "): the config, the 3D Print Log API and each printer. They take a few seconds.</p>",
        )
        refresh = REFRESH_PAIRING_S if code is not None else REFRESH_S
        return _html_page(_page(nonce, text, body, prefix, refresh=refresh, code=code))

    # -- doctor ----------------------------------------------------------------------------------

    def _doctor_page(self, prefix: str, nonce: str) -> Response:
        back = f'<p><a href="{prefix or "./"}">Back to the status</a></p>'
        try:
            config = self.load()
        except ConfigError:
            # doctor would only say the same, quoting the line, which can hold a secret.
            text = _Text(self._env_secrets())
            body = _section("Checks", f"<p class=bad>{text(CONFIG_ERROR)}</p>") + back
            return _html_page(_page(nonce, text, body, prefix, refresh=None, code=None))
        text = _Text(self._secrets(config))
        running, results = self._checks()
        refresh = None
        if running:
            refresh = REFRESH_CHECKS_S
            body = _section(
                "Checks",
                "<p>The checks are running: the config, the 3D Print Log API and each printer. "
                "This page shows them in a few seconds.</p>",
            )
        elif results is None:
            body = _section(
                "Checks",
                "<p class=bad>The checks couldn't run; the bridge's log says why. Run "
                "<code>printlog-bridge doctor</code> where the bridge runs.</p>",
            )
        else:
            rows = "".join(
                f"<tr class={_css(r.status)}><td>{text(r.status.value)}</td>"
                f"<td>{text(r.name)}</td><td>{text(r.detail)}</td></tr>"
                for r in results
            )
            summary = format_report(results).splitlines()[-1]
            body = _section(
                "Checks",
                f"<p>{text(summary)}</p><table><thead><tr><th>Result</th><th>Check</th>"
                f"<th>Detail</th></tr></thead><tbody>{rows}</tbody></table>",
            )
        return _html_page(_page(nonce, text, body + back, prefix, refresh=refresh, code=None))

    def _checks(self) -> tuple[bool, list[CheckResult] | None]:
        """Whether the checks are running, else their last results (None when they failed).
        Never waits: a request starts one run in the background, unless one is running or ran
        less than DOCTOR_REUSE_S ago, and the page refreshes until it has finished."""
        with self._doctor_lock:
            now = time.monotonic()
            if self._doctor_since is not None and now - self._doctor_since < 2 * DOCTOR_TIMEOUT_S:
                return True, None
            if self._doctor is not None and now - self._doctor[0] < DOCTOR_REUSE_S:
                return False, self._doctor[1]
            self._doctor_since = now
        threading.Thread(
            target=self._run_checks, name="printlog-bridge local page checks", daemon=True
        ).start()
        return True, None

    def _run_checks(self) -> None:
        try:
            results: list[CheckResult] | None = list(self.diagnose())
        except Exception:
            log.warning("the local page couldn't run the checks", exc_info=True)
            results = None
        with self._doctor_lock:
            self._doctor = (time.monotonic(), results)
            self._doctor_since = None

    def _diagnose(self) -> list[CheckResult]:
        if self.adapters is None:
            raise RuntimeError("no adapters to check printers with")
        config = self.load()
        work = run_diagnose(config.path, adapters=self.adapters, env=self.env, home=self.home)
        return run_with_timeout(work, DOCTOR_TIMEOUT_S)

    # -- secrets ---------------------------------------------------------------------------------

    def _secrets(self, config: BridgeConfig) -> list[str]:
        found = known_secrets(config.path, config.state_dir, self.env)
        if config.api_key:
            found.append(config.api_key)
        if config.stored_device is not None:
            found.append(config.stored_device.token)
        return sorted(set(found) | set(self._env_secrets()), key=len, reverse=True)

    def _env_secrets(self) -> list[str]:
        key = (self.env.get(API_KEY_ENV) or "").strip()
        return [key] if key else []


def run_with_timeout(work: Awaitable[T], timeout: float) -> T:
    """Runs `work` in an event loop of this thread's own, for at most `timeout` seconds. Unlike
    asyncio.run, it never waits for the loop's executor threads when it ends (Python 3.9 to 3.11
    wait for good), so a lookup stuck in one can't keep the checks from finishing."""
    loop = asyncio.new_event_loop()
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="printlog-bridge checks")
    loop.set_default_executor(executor)
    try:
        return loop.run_until_complete(asyncio.wait_for(work, timeout))
    finally:
        try:
            left = asyncio.all_tasks(loop)
            for task in left:
                task.cancel()
            if left:
                loop.run_until_complete(asyncio.gather(*left, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            executor.shutdown(wait=False)
            loop.close()


class _Text:
    """Escapes a value for the page, with the bridge's secrets removed first."""

    def __init__(self, secrets: Sequence[str]) -> None:
        self.secrets = secrets

    def __call__(self, value: object) -> str:
        return html.escape(scrub(str(value), self.secrets), quote=True)


# -- what the page says ---------------------------------------------------------------------------


def _state(status: Mapping[str, Any] | None, running: bool, now: datetime) -> str:
    if status is None:
        return "hasn't run yet"
    if not running:
        return "not running" + (" (stopped)" if status.get("stopped_at") else "; check its log")
    if status.get("credentials_problem"):
        return str(status["credentials_problem"])
    if status.get("setup_problem"):
        return f"waiting for printers: {status['setup_problem']}"
    return f"running for {_ago(now, status['started_at'])}"


def _pairing(
    config: BridgeConfig,
    status: Mapping[str, Any] | None,
    running: bool,
    code: pairing.PendingCode | None,
) -> str:
    if code is not None:
        return "pairing again" if config.device is not None else "not paired yet"
    if config.device is not None:
        name = config.device.name
        return f"Paired as {name}" if name else "Paired"
    if config.api_key is not None:
        return f"not paired; signs in with the API key from {config.describe_api_key()}"
    return "not paired"


def _printers(
    config: BridgeConfig,
    status: Mapping[str, Any] | None,
    running: bool,
    now: datetime,
    text: _Text,
) -> str:
    known = (status or {}).get("printers") or {}
    apps = ((status or {}).get("bridge") or {}).get("app_printers") or {}
    adapters = {p.name: p.adapter.describe() for p in config.printers}
    for entry, app in apps.items():
        adapters[f"app-{entry}"] = f"{app.get('kind')} at {app.get('host')}, from the app"
    names = list(dict.fromkeys([*adapters, *known]))
    if not names:
        return _section("Printers", "<p>None yet: connect them in 3D Print Log (Bridges).</p>")
    rows = ""
    for name in names:
        printer = known.get(name)
        if printer is None or not running:
            connection, since, detail = ("not running" if not running else "not watched"), "", ""
        else:
            connection = printer.get("connection") or ""
            since = (
                _ago(now, printer["connection_since"]) if printer.get("connection_since") else ""
            )
            detail = printer.get("connection_detail") or printer.get("printer_state") or ""
        last = (printer or {}).get("last_event") or ""
        rows += (
            f"<tr><td>{text(name)}</td><td>{text(adapters.get(name, ''))}</td>"
            f"<td class={_css_connection(connection)}>{text(connection)}</td><td>{text(since)}</td>"
            f"<td>{text(detail)}</td><td>{text(last)}</td></tr>"
        )
    return _section(
        "Printers",
        "<table><thead><tr><th>Printer</th><th>Adapter</th><th>Connection</th><th>For</th>"
        f"<th>Detail</th><th>Last event</th></tr></thead><tbody>{rows}</tbody></table>",
    )


def _pairing_section(code: pairing.PendingCode, text: _Text) -> str:
    target = code.verification_uri_complete or code.verification_uri
    minutes = max(1, round((code.expires_at - time.monotonic()) / 60))
    try:
        image = qr.svg(target) if _linkable(target) else ""
    except ValueError:
        image = ""

    def link(url: str) -> str:
        if _linkable(url):
            return (
                f'<a href="{text(url)}" rel="noreferrer noopener" target="_blank">{text(url)}</a>'
            )
        return f"<code>{text(url)}</code>"

    complete = (
        f"<p>Or open {link(code.verification_uri_complete)}</p>"
        if code.verification_uri_complete
        else ""
    )
    return _section(
        "Pair this bridge",
        f"<div class=pair><div class=qr>{image}</div><div><p>Scan the QR code with your phone, "
        f"or open {link(code.verification_uri)} and enter the code</p>"
        f"<p class=code>{text(code.user_code)}</p>{complete}"
        f"<p>The code works for {minutes} minute{'' if minutes == 1 else 's'}; the bridge "
        "shows a new one when it runs out. This page updates itself once you approve it.</p>"
        "</div></div>",
    )


def _page(
    nonce: str,
    text: _Text,
    body: str,
    prefix: str,
    *,
    refresh: int | None,
    code: pairing.PendingCode | None,
) -> str:
    if code is not None:
        body = _pairing_section(code, text) + body
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta name="referrer" content="no-referrer">{meta}'
        f'<title>3D Print Log Bridge</title><style nonce="{nonce}">{_CSS}</style></head><body>'
        f'<header><h1><a href="{prefix or "./"}">3D Print Log Bridge</a></h1>'
        f"<span>version {text(__version__)}</span></header><main>{body}</main>"
        f"<footer>This page only shows the bridge's status. Manage printers in "
        f'<a href="{APP_PAGE}" rel="noreferrer noopener" target="_blank">3D Print Log</a>.'
        "</footer></body></html>"
    )


def _section(title: str, inner: str) -> str:
    return f"<section><h2>{html.escape(title)}</h2>{inner}</section>"


def _css(status: Status) -> str:
    return {Status.OK: "ok", Status.WARN: "warn"}.get(status, "bad")


def _css_connection(connection: str) -> str:
    return {"connected": "ok", "starting": "warn"}.get(connection, "bad" if connection else "")


_CSS = """
:root{color-scheme:light dark;--bg:#f6f7f9;--fg:#1d2330;--muted:#5b6475;--card:#fff;
--line:#dde1e8;--ok:#1f7a3d;--warn:#9a6700;--bad:#b42318;--accent:#0b63ce}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--fg:#e6e8ec;--muted:#9aa3b2;
--card:#1a1f26;--line:#2c333d;--ok:#4ac26b;--warn:#d4a72c;--bad:#f47067;--accent:#58a6ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header,main,footer{max-width:960px;margin:0 auto;padding:12px 16px}
header{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
h1{font-size:20px;margin:8px 0}h1 a{color:inherit;text-decoration:none}
header span,footer{color:var(--muted);font-size:13px}
h2{font-size:16px;margin:0 0 8px}a{color:var(--accent)}
section{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px;
margin:0 0 14px;overflow-x:auto}
dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px;margin:0}dt{color:var(--muted)}
dd{margin:0;overflow-wrap:anywhere}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:6px 8px;border-top:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;border-top:0}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;margin:0}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.pair{display:flex;gap:20px;flex-wrap:wrap;align-items:center}.pair>div{flex:1 1 280px}
.pair>.qr{flex:0 0 220px}
.qr svg{width:220px;height:220px;display:block;border-radius:4px}
.code{font:600 28px/1.2 ui-monospace,Menlo,Consolas,monospace;letter-spacing:2px;margin:8px 0}
@media (max-width:600px){dl{grid-template-columns:1fr}dt{margin-top:6px}}
"""


# -- requests -------------------------------------------------------------------------------------


def _security_headers(nonce: str) -> dict[str, str]:
    return {
        "Content-Security-Policy": (
            f"default-src 'none'; style-src 'nonce-{nonce}'; img-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
        ),
        "X-Frame-Options": "SAMEORIGIN",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
    }


def _html_page(text: str) -> Response:
    return Response(200, text.encode("utf-8"), {"Content-Type": "text/html; charset=utf-8"})


def _plain(status: int, text: str) -> Response:
    return Response(status, text.encode("utf-8"), {"Content-Type": "text/plain; charset=utf-8"})


def _header(headers: Mapping[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return ""


def _ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _local_host(host: str) -> bool:
    """Whether a Host header names this bridge the way a user would on a LAN: an IP address,
    `localhost`, a name without dots, or a local name. Anything else (a public name) is how a
    DNS rebinding attack reaches a LAN page."""
    host = host.strip().lower()
    if host.startswith("["):
        literal, _, port = host[1:].partition("]")
        return _ip(literal) is not None and (
            port == "" or re.fullmatch(r":\d{1,5}", port) is not None
        )
    name, _, port = host.partition(":")
    if port and not port.isdigit():
        return False
    if not name or not _HOST_NAME.fullmatch(name):
        return False
    if _ip(name) is not None or name == "localhost" or "." not in name:
        return True
    return name.rstrip(".").endswith(LOCAL_SUFFIXES)


def _ingress_prefix(value: str) -> str:
    """Home Assistant's ingress path, for links, as `<path>/`; empty (relative links) when
    there's none or it isn't one."""
    value = value.strip().rstrip("/")
    return f"{value}/" if _INGRESS_PATH.fullmatch(value) else ""


def _linkable(url: str) -> bool:
    """Only https (or http to this computer, a test sign-in service) becomes a link."""
    return pairing._secure(url)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # So a restarted bridge can listen again at once (connections the last one closed linger in
    # TIME_WAIT on Linux). Not on Windows, where it would let a second server take the port.
    allow_reuse_address = os.name != "nt"

    def __init__(self, page: LocalPage) -> None:
        self.page = page
        self.slots = threading.BoundedSemaphore(MAX_REQUESTS)
        if ":" in page.address.host:
            self.address_family = socket.AF_INET6
        super().__init__((page.address.host, page.address.port), _Handler)

    def verify_request(self, request: Any, client_address: Any) -> bool:
        """A peer `status_page_clients` doesn't list is closed at once, before it takes a slot
        or a byte is read (the Home Assistant app's port is open on the LAN)."""
        return self.page.allows(client_address[0])

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        log.debug("the local page dropped a request", exc_info=True)


class _DeadlineReads(io.RawIOBase):
    """A connection's reads, each given only the time left until `deadline` (when set), so the
    whole request line and headers must arrive in time however slowly they trickle in."""

    def __init__(self, connection: Any) -> None:
        """The accepted connection (it never opens one: tests/test_privacy.py)."""
        self.connection = connection
        self.deadline: float | None = None

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        if self.deadline is not None:
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise socket.timeout("the request took too long")
            self.connection.settimeout(min(left, REQUEST_TIMEOUT_S))
        return self.connection.recv_into(buffer)


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    timeout = REQUEST_TIMEOUT_S
    server_version = "printlog-bridge"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.rfile.close()  # the socket stays open; reads go through the deadline instead
        self._reads = _DeadlineReads(self.connection)
        self.rfile = io.BufferedReader(self._reads)

    def handle_one_request(self) -> None:
        # The request line and headers must all come within the deadline: a client that
        # trickles them (or keeps an idle connection) is cut off and its slot freed.
        self._reads.deadline = time.monotonic() + self.server.page.request_deadline
        super().handle_one_request()

    def parse_request(self) -> bool:
        parsed = super().parse_request()  # the headers have been read
        self._reads.deadline = None
        self.connection.settimeout(REQUEST_TIMEOUT_S)
        return parsed

    def _answer(self) -> None:
        response = self.server.page.respond(
            self.command, self.path, dict(self.headers.items()), self.client_address[0]
        )
        self.send_response(response.status)
        for name, value in response.headers.items():
            self.send_header(name, value)
        if "Content-Length" not in response.headers:
            self.send_header("Content-Length", str(len(response.body)))
        self.end_headers()
        if response.body:
            self.wfile.write(response.body)

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _answer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base's name
        log.debug("local page: %s", format % args)
