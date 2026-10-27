"""`printlog-bridge pair`, and the pairing `run` starts on its own the first time (#2, #101).

`pair` runs the device flow (core/pairing.py), stores the device token, then links each
`[printer]` without a `printer_id` to a printer of the account: one picked from the list, or a
new one filled in from what the adapter knows. With `--yes`, or without a terminal (Docker), it
asks nothing and says what to set.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from printlog_bridge.core.adapter import PrinterDetails
from printlog_bridge.core.api_client import ApiClient, ApiError
from printlog_bridge.core.config import API_KEY_ENV, BridgeConfig, PrinterConfig, set_printer_ids
from printlog_bridge.core.device import DeviceToken
from printlog_bridge.core.doctor import API_KEYS_PAGE, PRINTERS_PAGE
from printlog_bridge.core.pairing import (
    CREATE_SCOPE,
    PairingError,
    default_name,
    instructions,
    pair,
)
from printlog_bridge.core.status import read_status

log = logging.getLogger(__name__)

Ask = Callable[[str], str]
DETAILS_TIMEOUT_S = 15
NEW, SKIP = "n", "s"


def pair_command(
    config: BridgeConfig,
    env: Mapping[str, str],
    *,
    name: str | None,
    yes: bool,
    switch: bool,
    ask: Ask | None = None,
    interactive: bool | None = None,
) -> int:
    """`printlog-bridge pair`. Returns the exit code."""
    if interactive is None:
        interactive = not yes and sys.stdin.isatty()
    refusal = _root_refusal(config.state_dir)
    if refusal:
        print(f"printlog-bridge: {refusal}", file=sys.stderr)
        return 1
    needs = [p for p in config.printers if switch or p.printer_id is None]
    if config.device is not None:
        print(
            f"This bridge is already paired{_as(config.device)}. Pairing again gives it a new "
            "token, under the same device in 3D Print Log; the old token stops working."
        )
    try:
        stored, repaired = asyncio.run(
            pair(
                config.api_url,
                config.state_dir,
                name=name or default_name(env),
                create=interactive and bool(needs),
                show=lambda a: print(instructions(a, sys.stdout), flush=True),
                say=print,
            )
        )
    except PairingError as e:
        print(f"printlog-bridge: pairing didn't work: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(
            f"printlog-bridge: can't store the pairing in {config.state_dir}: {e}", file=sys.stderr
        )
        return 1
    except KeyboardInterrupt:
        print("\nPairing canceled.", file=sys.stderr)
        return 1

    print(f"{'Paired again' if repaired else 'Paired'}{_as(stored)}.", end=" ")
    print(f"The device token is in {config.device_token_file}, which only you can read.")
    if repaired:
        print("The token it had before no longer works.")
    if config.api_key is not None:
        print(
            "From now on the bridge signs in with the device token, not the API key in "
            f"{config.describe_api_key()}. You can delete that key at {API_KEYS_PAGE}."
        )
    if env.get(API_KEY_ENV):
        print(f"{API_KEY_ENV} is set, but the device token wins over it.")

    code = asyncio.run(link_printers(config, stored, needs, interactive=interactive, ask=ask))
    print(
        "Restart printlog-bridge to start using the device token (a bridge that was waiting "
        "for a re-pair carries on by itself within a minute)."
    )
    return code


async def pair_on_first_run(config: BridgeConfig, env: Mapping[str, str]) -> str | None:
    """`run` without an API key or device token: pairs in the log (QR code and all), as long
    as it takes. Returns None once paired, or why it couldn't (pairing off on the server)."""
    log.info("No API key or device token yet, so this bridge pairs with 3D Print Log first.")
    try:
        stored, _ = await pair(
            config.api_url,
            config.state_dir,
            name=default_name(env),
            create=False,
            show=lambda a: log.info("%s", "\n" + instructions(a, sys.stderr)),
            say=lambda message: log.warning("%s", message),
            keep_trying=True,
        )
    except PairingError as e:
        return str(e)
    except OSError as e:
        return f"can't store the pairing in {config.state_dir}: {e}"
    log.info("Paired%s. The device token is in %s.", _as(stored), config.device_token_file)
    needs = [p for p in config.printers if p.printer_id is None]
    if needs:
        lines = await _guidance(config, stored, needs)
        log.warning("%s", "\n".join(lines))
    return None


async def link_printers(
    config: BridgeConfig,
    device: DeviceToken,
    needs: Sequence[PrinterConfig],
    *,
    interactive: bool,
    ask: Ask | None = None,
) -> int:
    """Links each printer in `needs` to one of the account's, and writes the ids chosen into the
    config. Without a terminal, says what to set instead."""
    if not needs:
        return 0
    if not interactive:
        print("\n".join(await _guidance(config, device, needs)))
        return 0
    ask = ask or input
    chosen: dict[str, int] = {}
    async with ApiClient(config.api_url, device=device) as api:
        try:
            account = await api.printers()
        except ApiError as e:
            print(f"Couldn't list your printers in 3D Print Log: {e}", file=sys.stderr)
            return 1
        can_create = CREATE_SCOPE in device.scopes
        for printer in needs:
            choice = _choose(printer, account, can_create, ask)
            if choice == SKIP:
                continue
            if choice == NEW:
                body = _new_printer(printer, await _details(config, printer), ask)
                try:
                    created = await api.create_printer(body)
                except ApiError as e:
                    print(f"Couldn't create the printer: {e}", file=sys.stderr)
                    continue
                print(f"Created printer {created.get('id')}, {created.get('name')}.")
                account.append(created)
                chosen[printer.name] = int(created["id"])
            else:
                chosen[printer.name] = int(choice)
    if not chosen:
        return 0
    try:
        missing = set_printer_ids(config.path, chosen)
    except OSError as e:
        print(
            f"Couldn't write {config.path} ({e.strerror or e}). Set these by hand:",
            file=sys.stderr,
        )
        for printer, printer_id in chosen.items():
            print(f"  [printer {printer}] printer_id = {printer_id}", file=sys.stderr)
        return 1
    for printer, printer_id in chosen.items():
        if printer not in missing:
            print(f"[printer {printer}] now logs to printer {printer_id} ({config.path}).")
    return 0


def _choose(printer: PrinterConfig, account: list[dict[str, Any]], can_create: bool, ask: Ask):
    """The id picked for `printer`, NEW or SKIP."""
    print()
    current = f" (now printer {printer.printer_id})" if printer.printer_id is not None else ""
    print(
        f"Which printer in 3D Print Log should [printer {printer.name}]{current} log its prints "
        f"to? It is {printer.adapter.describe()}."
    )
    for i, found in enumerate(account, 1):
        print(f"  {i}) {_describe(found)}")
    if can_create:
        print(f"  {NEW}) a new printer")
    print(f"  {SKIP}) skip it for now")
    default = _default_choice(printer, account, can_create)
    while True:
        answer = ask(f"Choice [{default}]: ").strip().lower() or default
        if answer == SKIP or (answer == NEW and can_create):
            return answer
        if answer.isdigit() and 1 <= int(answer) <= len(account):
            return int(account[int(answer) - 1]["id"])
        print("That isn't one of the choices.")


def _default_choice(printer: PrinterConfig, account: list[dict[str, Any]], can_create: bool) -> str:
    for i, found in enumerate(account, 1):
        if printer.printer_id is not None and found.get("id") == printer.printer_id:
            return str(i)
    for i, found in enumerate(account, 1):
        if str(found.get("name") or "").strip().lower() == printer.name.lower():
            return str(i)
    return NEW if can_create else SKIP


def _new_printer(printer: PrinterConfig, details: PrinterDetails, ask: Ask) -> dict[str, Any]:
    print("The new printer (press Enter to keep what's in brackets):")
    name = _text(ask, "Name", details.name or printer.name)
    make = _text(ask, "Make", details.make)
    model = _text(ask, "Model", details.model)
    slots = details.slot_count or 1
    while True:
        answer = ask(f"Filament slots (tools, MMU or AMS slots) [{slots}]: ").strip()
        if not answer:
            break
        if answer.isdigit() and 1 <= int(answer) <= 64:
            slots = int(answer)
            break
        print("A number from 1 to 64.")
    body: dict[str, Any] = {
        "name": name[:100],
        "make": make[:50],
        "model": model[:50],
        "isActive": True,
        "slotCount": slots,
    }
    if details.description:
        body["description"] = details.description[:1000]
    return body


def _text(ask: Ask, label: str, default: str | None) -> str:
    while True:
        answer = ask(f"{label}{f' [{default}]' if default else ''}: ").strip() or (default or "")
        if answer:
            return answer
        print(f"{label} is needed.")


async def _details(config: BridgeConfig, printer: PrinterConfig) -> PrinterDetails:
    """What the adapter knows, with the model the slicer last named for it (from `status`)."""
    try:
        details = await asyncio.wait_for(printer.adapter.printer_details(), DETAILS_TIMEOUT_S)
    except Exception:  # an adapter bug or a slow printer must not stop pairing
        details = PrinterDetails(name=printer.name)
    if details.model is None:
        known = ((read_status(config.state_dir) or {}).get("printers") or {}).get(printer.name)
        model = (known or {}).get("printer_model")
        if isinstance(model, str) and model:
            details = PrinterDetails(
                details.name, details.make, model, details.slot_count, details.description
            )
    return details


async def _guidance(
    config: BridgeConfig, device: DeviceToken, needs: Sequence[PrinterConfig]
) -> list[str]:
    """What to set for each printer without a printer_id, with the account's printers."""
    try:
        async with ApiClient(config.api_url, device=device) as api:
            account = await api.printers()
    except ApiError:
        account = None
    lines = []
    for printer in needs:
        lines.append(
            f"[printer {printer.name}] has no printer_id, so its prints have nowhere to go yet."
        )
    if account:
        lines.append("Your printers in 3D Print Log:")
        lines.extend(f"  {p.get('id')}: {_describe(p)}" for p in account)
    elif account is not None:
        lines.append(f"Your account has no printers yet: add one at {PRINTERS_PAGE[:-1]}.")
    lines.append(
        f"Set printer_id = <number> in each of those sections of {config.path} (the number "
        f"at the end of the printer's page, {PRINTERS_PAGE}<number>), or run "
        "`printlog-bridge pair` in a terminal to choose or create one, then restart the bridge."
    )
    return lines


def _describe(printer: Mapping[str, Any]) -> str:
    kind = " ".join(str(printer.get(k) or "").strip() for k in ("make", "model")).strip()
    text = str(printer.get("name") or f"printer {printer.get('id')}")
    return f"{text} ({kind})" if kind and kind != text else text


def _as(device: DeviceToken) -> str:
    return f" as {device.name!r}" if device.name else ""


def _root_refusal(state_dir: Path) -> str | None:
    """Root pairing into another user's state directory would leave files the bridge can't
    read."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return None
    try:
        owner = state_dir.stat().st_uid
    except OSError:
        return None
    if owner == 0:
        return None
    return (
        f"don't run pair as root (or with sudo): {state_dir} belongs to uid {owner}, the user "
        "the bridge runs as, and the token file would be root's. Run it as that user."
    )
