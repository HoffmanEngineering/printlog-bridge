"""The running job's 3MF, fetched over FTPS once per job and kept until the next one, so its
thumbnail and settings can be read when the sink asks for them."""

from __future__ import annotations

import asyncio
import contextlib
import ftplib
import logging
import ssl
from collections.abc import Mapping
from typing import Any, Protocol

from printlog_bridge.core.status import PrinterReport

from .ftps import BambuFtps, FilesRefused, FileTooLarge, candidates
from .threemf import ThreeMF
from .tls import CertificateChanged

log = logging.getLogger(__name__)

FETCH_TIMEOUT_S = 30.0
WARNING = "files"
"""The `report.warn` topic for a printer that won't let the bridge read its files."""
REFUSED = (
    "the printer refused the access code for its files (FTPS), so usage is logged without "
    "the slicer's grams. Printers on firmware with Authorization Control serve files only with "
    "LAN Only Mode and Developer Mode on."
)


class JobFiles(Protocol):
    async def open(self, status: Mapping[str, Any]) -> ThreeMF | None:
        """The 3MF of the job `status` describes, or None when it can't be had."""

    def close(self) -> None:
        """Lets go of the file kept."""


class BambuFiles:
    def __init__(
        self, printer: str, ftps: BambuFtps, report: PrinterReport, timeout: float = FETCH_TIMEOUT_S
    ) -> None:
        self.printer = printer
        self.ftps = ftps
        self.report = report
        self.timeout = timeout
        self._key: tuple[str, ...] | None = None
        self._file: ThreeMF | None = None

    async def open(self, status: Mapping[str, Any]) -> ThreeMF | None:
        subtask = str(status.get("subtask_name") or "")
        gcode_file = str(status.get("gcode_file") or "")
        key = (subtask, gcode_file, str(status.get("gcode_start_time") or ""))
        if key == self._key and self._file is not None:
            return self._file
        self.close()
        paths = candidates(subtask, gcode_file)
        if not paths:
            return None
        loop = asyncio.get_running_loop()
        try:
            found = await asyncio.wait_for(
                loop.run_in_executor(None, self.ftps.fetch, paths), self.timeout
            )
        except FilesRefused:
            log.warning("[printer %s] %s", self.printer, REFUSED)
            self.report.warn(WARNING, REFUSED)
            return None
        except CertificateChanged as e:
            log.warning("[printer %s] can't read the job's file: %s", self.printer, e)
            self.report.warn(WARNING, str(e))
            return None
        except asyncio.TimeoutError:
            log.warning(
                "[printer %s] the job's file didn't arrive within %gs, so its usage is logged "
                "without the slicer's grams", self.printer, self.timeout,
            )  # fmt: skip
            return None
        except (OSError, EOFError, ftplib.Error, ssl.SSLError, FileTooLarge) as e:
            log.warning(
                "[printer %s] can't read the job's file over FTPS (%s), so its usage is logged "
                "without the slicer's grams", self.printer, e,
            )  # fmt: skip
            return None
        self.report.warn(WARNING, None)
        if found is None:
            log.info(
                "[printer %s] the job's file isn't on the printer's storage (looked for %s)",
                self.printer, ", ".join(paths),
            )  # fmt: skip
            return None
        path, file = found
        try:
            self._file = ThreeMF(file)
        except Exception as e:  # a broken ZIP, or not one
            file.close()
            log.warning("[printer %s] can't read %s as a 3MF: %s", self.printer, path, e)
            return None
        self._key = key
        return self._file

    def close(self) -> None:
        if self._file is not None:
            with contextlib.suppress(Exception):
                self._file.close()
        self._file = None
        self._key = None
