"""Reading a job's 3MF from a Bambu Lab printer over FTPS. Read only: nothing is ever written.

The printer serves its storage (SD card or internal) over FTP with implicit TLS on port 990,
user `bblp` and the LAN access code, and wants the data connections protected (`PROT P`) and on
the control connection's TLS session (ha-bambulab's `ImplicitFTP_TLS`, which this follows).

Where the job's file is depends on how it was sent (ha-bambulab, `models.py`): a print from the
SD card or sent in LAN mode is `/<name>.gcode.3mf` or `/<name>.3mf`, a print sent through the
cloud or from MakerWorld is cached as `/cache/<name>.3mf`, where `<name>` is the report's
`subtask_name`. `gcode_file` names the file itself for some, and a plate inside it
(`/data/Metadata/plate_1.gcode`, on a RAM disk FTP can't see) for others.

The whole file is fetched (a few MB, at most `MAX_FILE_BYTES`) into a temporary file that is
deleted when the job ends: the printer's FTP server is a small one, and fetching ranges of a
file (REST) isn't known to work on it.
"""

from __future__ import annotations

import ftplib
import posixpath
import ssl
import tempfile
from collections.abc import Callable
from typing import IO, Any

from .tls import client_context

FTPS_PORT = 990
USER = "bblp"
TIMEOUT_S = 15.0
MAX_FILE_BYTES = 200 * 1024 * 1024
"""A 3MF this large is mostly toolpaths; the bridge gives up on it rather than fill a disk."""
SPOOL_IN_MEMORY_BYTES = 8 * 1024 * 1024
SEARCH_FOLDERS = ("/cache/", "/")


class FilesRefused(Exception):
    """The FTPS server refused the access code (530)."""


class FileTooLarge(Exception):
    pass


class ImplicitFTPS(ftplib.FTP_TLS):
    """FTP over TLS from the first byte (port 990), with the data connections on the control
    connection's TLS session, which the printer requires."""

    def __init__(self, context: ssl.SSLContext, timeout: float) -> None:
        super().__init__(context=context, timeout=timeout)
        self._sock: Any = None

    @property
    def sock(self) -> Any:
        return self._sock

    @sock.setter
    def sock(self, value: Any) -> None:
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=None)
        self._sock = value

    def ntransfercmd(self, cmd: str, rest: Any = None) -> tuple[Any, int | None]:
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:  # type: ignore[attr-defined]
            conn = self.context.wrap_socket(conn, server_hostname=None, session=self.sock.session)
        return conn, size


def candidates(subtask_name: str, gcode_file: str) -> list[str]:
    """Where the job's 3MF may be, most likely first."""
    names = []
    for name in (subtask_name, gcode_file):
        name = (name or "").strip()
        if not name or name.lower().startswith(("/data/", "data/")):
            continue
        if name.startswith("/") and name.lower().endswith(".3mf"):
            names.append(name)
            name = posixpath.basename(name)
        is_3mf = name.lower().endswith(".3mf")
        stems = [name] if is_3mf else [f"{name}.gcode.3mf", f"{name}.3mf"]
        names += [f"{folder}{stem}" for stem in stems for folder in SEARCH_FOLDERS]
    return list(dict.fromkeys(names))


class BambuFtps:
    """Blocking: run it in a thread."""

    def __init__(
        self,
        host: str,
        password: str,
        *,
        port: int = FTPS_PORT,
        verify: Callable[[bytes], None] | None = None,
        timeout: float = TIMEOUT_S,
    ) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.verify = verify
        self.timeout = timeout

    def login(self) -> ImplicitFTPS:
        """A logged-in session. Raises `FilesRefused` for a refused access code, and OSError or
        `ftplib.Error` otherwise. The certificate is checked before the access code is sent."""
        ftp = ImplicitFTPS(client_context(), self.timeout)
        try:
            ftp.connect(self.host, self.port, timeout=self.timeout)
            if self.verify is not None:
                self.verify(ftp.sock.getpeercert(binary_form=True) or b"")
            try:
                ftp.login(USER, self.password)
            except ftplib.error_perm as e:
                if str(e).startswith("530"):
                    raise FilesRefused(str(e)) from None
                raise
            ftp.prot_p()
        except BaseException:
            ftp.close()
            raise
        return ftp

    def probe(self) -> None:
        """Logs in and out, for `doctor`."""
        ftp = self.login()
        try:
            ftp.voidcmd("NOOP")
        finally:
            _quit(ftp)

    def fetch(
        self, paths: list[str], max_bytes: int = MAX_FILE_BYTES
    ) -> tuple[str, IO[bytes]] | None:
        """The first of `paths` that exists, as a temporary file (the caller closes it), or None
        when none does."""
        ftp = self.login()
        try:
            for path in paths:
                try:
                    size = ftp.size(path)
                except ftplib.error_perm:
                    continue  # 550: not there
                if size is not None and size > max_bytes:
                    raise FileTooLarge(f"{posixpath.basename(path)} is {size} bytes")
                return path, self._retrieve(ftp, path, max_bytes)
            return None
        finally:
            _quit(ftp)

    @staticmethod
    def _retrieve(ftp: ftplib.FTP, path: str, max_bytes: int) -> IO[bytes]:
        out = tempfile.SpooledTemporaryFile(max_size=SPOOL_IN_MEMORY_BYTES)  # noqa: SIM115
        written = 0

        def write(block: bytes) -> None:
            nonlocal written
            written += len(block)
            if written > max_bytes:
                raise FileTooLarge(f"{posixpath.basename(path)} is over {max_bytes} bytes")
            out.write(block)

        try:
            ftp.retrbinary(f"RETR {path}", write)
        except BaseException:
            out.close()
            raise
        out.seek(0)
        return out  # type: ignore[return-value]


def _quit(ftp: ftplib.FTP) -> None:
    try:
        ftp.quit()
    except (OSError, EOFError, ftplib.Error):
        ftp.close()
