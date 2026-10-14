"""Spool assignment (#6): PRINTLOG_SET_SPOOL's commands, and what a reader in a slot reports."""

from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest
from fake_moonraker import eventually
from fake_printlog_api import FakePrintLogApi

from printlog_bridge.core.api_client import ApiClient
from printlog_bridge.core.events import SpoolCommand, SpoolReading
from printlog_bridge.core.spools import (
    UNKNOWN_TAG_RETRY_S,
    Spools,
    color_distance,
    normalize_tag,
)


@contextlib.asynccontextmanager
async def spools_for(api: FakePrintLogApi, **options):
    base = await api.start()
    try:
        async with ApiClient(base, api.api_key) as client:
            yield Spools(client, {"u1": 42}, **options)
    finally:
        await api.close()


def run(api: FakePrintLogApi, scenario, **options):
    async def main():
        async with spools_for(api, **options) as spools:
            return await scenario(spools)

    return asyncio.run(main())


def command(slot: int = 0, **kwargs) -> SpoolCommand:
    return SpoolCommand("u1", slot, **kwargs)


class TestNormalizeTag:
    @pytest.mark.parametrize("uid", ["04A23F11", "04:a2:3f:11", "04-A2-3F-11", "04 a2 3f 11"])
    def test_any_common_format_is_the_same_tag(self, uid):
        assert normalize_tag(uid) == "04A23F11"

    @pytest.mark.parametrize("uid", ["", "04A23F1", "0x04A23F11", "zz", "0" * 66])
    def test_what_isnt_whole_hex_bytes_is_none(self, uid):
        assert normalize_tag(uid) is None


class TestSetSpool:
    def test_loads_the_spool_by_its_id(self):
        api = FakePrintLogApi()
        spool = api.add_spool("Galaxy Black PLA")

        answer = run(api, lambda s: s.spool_command(command(0, filament_id=spool)))

        assert api.slot_spools(42) == {0: spool}
        assert "Galaxy Black PLA" in answer
        assert "T0" in answer

    def test_loads_the_spool_with_the_tag(self):
        api = FakePrintLogApi()
        api.slot_counts[42] = 4
        spool = api.add_spool("Silk Gold", tags=("04A23F11",))

        answer = run(api, lambda s: s.spool_command(command(2, tag="04:a2:3f:11")))

        assert api.slot_spools(42) == {2: spool}
        assert "Silk Gold" in answer
        assert api.loaded[42][0]["slotLabel"] == "T2"

    def test_a_tag_no_spool_has_changes_nothing(self):
        api = FakePrintLogApi()
        spool = api.add_spool("Loaded")
        api.user_loads(42, 0, spool)

        answer = run(api, lambda s: s.spool_command(command(0, tag="DEADBEEF")))

        assert api.slot_spools(42) == {0: spool}
        assert "DEADBEEF" in answer
        assert "no spool" in answer.lower()

    def test_spool_number_loads_the_spool_it_names(self):
        # What a spool picked in Mainsail or Fluidd sends: Moonraker's spool id is the number.
        api = FakePrintLogApi()
        api.slot_counts[42] = 2
        api.add_spool("Galaxy Black PLA", number=11)
        spool = api.add_spool("Silk Gold", number=12)

        answer = run(api, lambda s: s.spool_command(command(1, spool_number=12)))

        assert api.slot_spools(42) == {1: spool}
        assert "Silk Gold" in answer
        assert ("GET", "/api/Filaments/by-number/12") in api.requests

    def test_unknown_spool_number_changes_nothing(self):
        api = FakePrintLogApi()
        spool = api.add_spool("Loaded", number=1)
        api.user_loads(42, 0, spool)

        answer = run(api, lambda s: s.spool_command(command(0, spool_number=99)))

        assert api.slot_spools(42) == {0: spool}
        assert answer == "no spool in 3D Print Log has number 99; nothing changed"

    def test_a_tag_that_isnt_hex_is_refused_without_asking_the_api(self):
        api = FakePrintLogApi()

        answer = run(api, lambda s: s.spool_command(command(0, tag="not-a-tag")))

        assert ("GET", "/api/Filaments/by-tag/not-a-tag") not in api.requests
        assert "not-a-tag" in answer

    def test_an_id_that_isnt_a_spool_id_is_refused_without_asking_the_api(self):
        api = FakePrintLogApi()

        answer = run(api, lambda s: s.spool_command(command(0, filament_id="7")))

        assert not [r for r in api.requests if "/slots/" in r[1]]
        assert "7" in answer

    def test_another_users_spool_is_refused(self):
        api = FakePrintLogApi()

        answer = run(
            api,
            lambda s: s.spool_command(
                command(0, filament_id="0e9c1d8e-0000-4000-8000-000000000000")
            ),
        )

        assert api.slot_spools(42) == {}
        assert "isn't one of yours" in answer

    def test_a_slot_beyond_the_printers_count_grows_it_first(self):
        api = FakePrintLogApi()
        kept = api.add_spool("Kept")
        api.user_loads(42, 0, kept)
        spool = api.add_spool("Orange PETG")

        run(api, lambda s: s.spool_command(command(3, filament_id=spool, slot_count=4)))

        assert api.slot_counts[42] == 4
        assert api.slot_spools(42) == {0: kept, 3: spool}
        [put] = api.printer_puts
        assert put["make"] == "Snapmaker"
        assert put["name"] == "Printer 42"
        assert put["category"] == "FDM"

    def test_without_the_printers_tool_count_it_grows_to_the_slot(self):
        api = FakePrintLogApi()
        spool = api.add_spool()

        run(api, lambda s: s.spool_command(command(2, filament_id=spool)))

        assert api.slot_counts[42] == 3
        assert api.slot_spools(42) == {2: spool}

    def test_a_slot_within_the_count_leaves_the_printer_alone(self):
        api = FakePrintLogApi()
        api.slot_counts[42] = 4
        spool = api.add_spool()

        run(api, lambda s: s.spool_command(command(1, filament_id=spool, slot_count=4)))

        assert api.printer_puts == []

    def test_clearing_unloads_the_slot(self):
        api = FakePrintLogApi()
        api.slot_counts[42] = 4
        a, b = api.add_spool("A"), api.add_spool("B")
        api.user_loads(42, 0, a)
        api.user_loads(42, 1, b)

        answer = run(api, lambda s: s.spool_command(command(1)))

        assert api.slot_spools(42) == {0: a}
        assert "T1" in answer

    def test_with_the_api_down_nothing_changes_and_it_says_so(self):
        api = FakePrintLogApi()
        spool = api.add_spool()
        api.offline = True

        answer = run(api, lambda s: s.spool_command(command(0, filament_id=spool)))

        assert "couldn't reach" in answer.lower()
        assert "again" in answer

    def test_an_unknown_printer_is_refused(self):
        api = FakePrintLogApi()

        answer = run(api, lambda s: s.spool_command(SpoolCommand("ender", 0, tag="04A23F11")))

        assert "ender" in answer
        assert api.requests == []


class TestLoaded:
    def test_current_reads_each_slots_spool(self):
        api = FakePrintLogApi()
        api.slot_counts[42] = 4
        a, b = api.add_spool("A"), api.add_spool("B")
        api.user_loads(42, 0, a)
        api.user_loads(42, 3, b)

        assert run(api, lambda s: s.current("u1")) == {0: a, 3: b}

    def test_a_spool_loaded_without_a_slot_is_slot_0_on_a_single_tool_printer(self):
        api = FakePrintLogApi()
        spool = api.add_spool()
        api.user_loads(42, None, spool)

        assert run(api, lambda s: s.current("u1")) == {0: spool}

    def test_a_spool_loaded_without_a_slot_is_in_no_slot_on_a_multi_tool_printer(self):
        api = FakePrintLogApi()
        api.slot_counts[42] = 4
        spool = api.add_spool()
        api.user_loads(42, None, spool)

        assert run(api, lambda s: s.current("u1")) == {}

    def test_with_the_api_down_current_is_what_was_last_read(self):
        api = FakePrintLogApi()
        spool = api.add_spool()
        api.user_loads(42, 0, spool)

        async def scenario(spools):
            await spools.refresh_all()
            api.offline = True
            return await spools.current("u1")

        assert run(api, scenario) == {0: spool}

    def test_run_reads_again_every_interval(self):
        api = FakePrintLogApi()
        a, b = api.add_spool("A"), api.add_spool("B")
        api.user_loads(42, 0, a)

        async def scenario(spools):
            task = asyncio.ensure_future(spools.run())
            try:
                await eventually(lambda: spools.loaded("u1") == {0: a})
                api.user_loads(42, 0, b)  # changed in the app
                await eventually(lambda: spools.loaded("u1") == {0: b})
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        run(api, scenario, interval=0.05)

    def test_a_failed_read_is_logged_once_and_running_goes_on(self, caplog):
        api = FakePrintLogApi()
        api.offline = True

        async def scenario(spools):
            task = asyncio.ensure_future(spools.run())
            try:
                await asyncio.sleep(0.3)
                assert not task.done()
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        with caplog.at_level(logging.DEBUG, logger="printlog_bridge"):
            run(api, scenario, interval=0.05)

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1


def reading(slot: int = 0, **kwargs) -> SpoolReading:
    return SpoolReading("u1", slot, **kwargs)


class TestReadings:
    def test_a_tag_a_spool_has_assigns_it_to_the_slot(self):
        api = FakePrintLogApi()
        api.slot_counts[42] = 4
        spool = api.add_spool("Snapmaker PLA Red", tags=("04A23F11",))

        run(api, lambda s: s.spool_reading(reading(1, tag="04A23F11", material="PLA")))

        assert api.slot_spools(42) == {1: spool}

    def test_the_same_tag_again_asks_nothing(self):
        api = FakePrintLogApi()
        api.slot_counts[42] = 4
        api.add_spool(tags=("04A23F11",))

        async def scenario(spools):
            await spools.spool_reading(reading(1, tag="04A23F11"))
            before = len(api.requests)
            await spools.spool_reading(reading(1, tag="04A23F11"))
            return len(api.requests) - before

        assert run(api, scenario) == 0

    def test_a_spool_already_in_the_slot_isnt_loaded_again(self):
        api = FakePrintLogApi()
        api.slot_counts[42] = 4
        spool = api.add_spool(tags=("04A23F11",))
        api.user_loads(42, 1, spool)

        async def scenario(spools):
            await spools.refresh_all()
            await spools.spool_reading(reading(1, tag="04A23F11"))

        run(api, scenario)

        assert not [r for r in api.requests if "/slots/" in r[1]]

    def test_a_tag_no_spool_has_leaves_the_slot_alone(self):
        api = FakePrintLogApi()
        spool = api.add_spool()
        api.user_loads(42, 0, spool)

        run(api, lambda s: s.spool_reading(reading(0, tag="DEADBEEF", material="PLA")))

        assert api.slot_spools(42) == {0: spool}

    def test_a_reader_that_cant_reach_the_api_tries_the_tag_again_after_the_back_off(self):
        api = FakePrintLogApi()
        spool = api.add_spool(tags=("04A23F11",))
        clock = FakeClock()

        async def scenario(spools):
            api.offline = True
            await spools.spool_reading(reading(0, tag="04A23F11"))
            api.offline = False
            clock.now += UNKNOWN_TAG_RETRY_S
            await spools.refresh_all()

        run(api, scenario, clock=clock)

        assert api.slot_spools(42) == {0: spool}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def tag_lookups(api: FakePrintLogApi) -> int:
    return sum(1 for _, path in api.requests if "/by-tag/" in path)


def unknown_tag_logs(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "no spool in 3D Print Log" in r.getMessage()]


class TestUnknownTags:
    """A tag no spool has yet is looked up again (#35): after the user adds it in the app, or
    at once when the spool is pulled out and put back."""

    def test_a_tag_added_in_the_app_is_assigned_after_the_back_off(self):
        api = FakePrintLogApi()
        spool = api.add_spool()
        clock = FakeClock()

        async def scenario(spools):
            await spools.spool_reading(reading(0, tag="DEADBEEF"))
            api.filaments[spool]["tagUids"].append("DEADBEEF")  # the user adds it in the app
            clock.now += UNKNOWN_TAG_RETRY_S
            await spools.refresh_all()

        run(api, scenario, clock=clock)

        assert api.slot_spools(42) == {0: spool}

    def test_an_unknown_tag_is_looked_up_at_most_once_per_back_off(self):
        api = FakePrintLogApi()
        clock = FakeClock()

        async def scenario(spools):
            await spools.spool_reading(reading(0, tag="DEADBEEF"))
            counts = [tag_lookups(api)]
            clock.now += UNKNOWN_TAG_RETRY_S - 1
            await spools.refresh_all()
            await spools.spool_reading(reading(0, tag="DEADBEEF", material="PLA"))
            counts.append(tag_lookups(api))
            clock.now += 1
            await spools.refresh_all()
            await spools.refresh_all()
            counts.append(tag_lookups(api))
            return counts

        assert run(api, scenario, clock=clock) == [1, 1, 2]

    def test_removing_and_reinserting_the_spool_looks_it_up_again_at_once(self):
        api = FakePrintLogApi()
        spool = api.add_spool()

        async def scenario(spools):
            await spools.spool_reading(reading(0, tag="DEADBEEF"))
            api.filaments[spool]["tagUids"].append("DEADBEEF")
            await spools.spool_reading(reading(0))  # pulled out: the reader sees no tag
            await spools.spool_reading(reading(0, tag="DEADBEEF"))  # and put back

        run(api, scenario, clock=FakeClock())

        assert api.slot_spools(42) == {0: spool}

    def test_the_unknown_tag_is_logged_once_per_insertion(self, caplog):
        api = FakePrintLogApi()
        clock = FakeClock()

        async def scenario(spools):
            await spools.spool_reading(reading(0, tag="DEADBEEF"))
            for _ in range(3):
                clock.now += UNKNOWN_TAG_RETRY_S
                await spools.refresh_all()
            logged = len(unknown_tag_logs(caplog))
            await spools.spool_reading(reading(0))
            await spools.spool_reading(reading(0, tag="DEADBEEF"))
            return logged, tag_lookups(api)

        with caplog.at_level(logging.INFO, logger="printlog_bridge"):
            logged, lookups = run(api, scenario, clock=clock)

        assert (logged, lookups) == (1, 5)
        assert len(unknown_tag_logs(caplog)) == 2

    def test_a_tag_that_found_its_spool_stays_found(self):
        api = FakePrintLogApi()
        api.add_spool(tags=("04A23F11",))
        clock = FakeClock()

        async def scenario(spools):
            await spools.spool_reading(reading(0, tag="04A23F11"))
            clock.now += 10 * UNKNOWN_TAG_RETRY_S
            await spools.refresh_all()
            return tag_lookups(api)

        assert run(api, scenario, clock=clock) == 1

    def test_running_looks_an_unknown_tag_up_again_between_refreshes(self):
        # Readings only arrive when the reader's view changes, so the retry can't wait for one.
        api = FakePrintLogApi()
        spool = api.add_spool()

        async def scenario(spools):
            task = asyncio.ensure_future(spools.run())
            try:
                await eventually(lambda: tag_lookups(api) == 0 and len(api.requests) > 0)
                await spools.spool_reading(reading(0, tag="DEADBEEF"))
                api.filaments[spool]["tagUids"].append("DEADBEEF")
                await eventually(lambda: api.slot_spools(42) == {0: spool})
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        run(api, scenario, interval=60, retry_after=0.05)

    def test_a_lookup_that_cant_reach_the_api_waits_for_the_back_off(self):
        # The next try is scheduled before asking, so an error doesn't leave the tag due at once.
        api = FakePrintLogApi()
        clock = FakeClock()

        async def scenario(spools):
            api.fail_next(1000)  # 3D Print Log is down from the first lookup on
            await spools.spool_reading(reading(0, tag="DEADBEEF"))
            counts = [tag_lookups(api)]
            await spools.spool_reading(reading(0, tag="DEADBEEF", material="PLA"))
            await spools.retry_tags()
            counts.append(tag_lookups(api))
            clock.now += UNKNOWN_TAG_RETRY_S - 1
            await spools.retry_tags()
            counts.append(tag_lookups(api))
            clock.now += 1
            await spools.retry_tags()
            await spools.retry_tags()
            counts.append(tag_lookups(api))
            await spools.spool_reading(reading(0, tag="04A23F11"))  # another spool: asked at once
            counts.append(tag_lookups(api))
            return counts

        assert run(api, scenario, clock=clock) == [1, 1, 1, 2, 3]

    def test_a_failed_retry_isnt_a_warning(self, caplog):
        api = FakePrintLogApi()
        clock = FakeClock()

        async def scenario(spools):
            await spools.spool_reading(reading(0, tag="DEADBEEF"))
            api.fail_next(1000)
            for _ in range(3):
                clock.now += UNKNOWN_TAG_RETRY_S
                await spools.retry_tags()

        with caplog.at_level(logging.DEBUG, logger="printlog_bridge"):
            run(api, scenario, clock=clock)

        assert tag_lookups(api) == 4
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_a_spool_pulled_out_while_its_lookup_waits_isnt_looked_up(self):
        api = FakePrintLogApi()
        spool = api.add_spool(tags=("DEADBEEF",))

        async def scenario(spools):
            async with spools._lock("u1"):  # another slot's lookup holds it
                waiting = asyncio.ensure_future(spools.spool_reading(reading(0, tag="DEADBEEF")))
                await asyncio.sleep(0.01)
                await spools.spool_reading(reading(0))  # pulled out
            await waiting
            before = (tag_lookups(api), api.slot_spools(42))
            await spools.spool_reading(reading(0, tag="DEADBEEF"))  # and put back
            return before, tag_lookups(api), api.slot_spools(42)

        assert run(api, scenario, clock=FakeClock()) == ((0, {}), 1, {0: spool})

    def test_a_spool_pulled_out_during_its_lookup_isnt_assigned(self):
        api = FakePrintLogApi()
        spool = api.add_spool(tags=("DEADBEEF",))

        async def scenario(spools):
            gate = asyncio.Event()
            lookup = spools.api.filament_by_tag

            async def slow(uid):
                await gate.wait()
                return await lookup(uid)

            spools.api.filament_by_tag = slow
            asking = asyncio.ensure_future(spools.spool_reading(reading(0, tag="DEADBEEF")))
            await asyncio.sleep(0.01)
            await spools.spool_reading(reading(0))  # pulled out while 3D Print Log answers
            gate.set()
            await asking
            before = api.slot_spools(42)
            await spools.spool_reading(reading(0, tag="DEADBEEF"))  # and put back
            return before, tag_lookups(api), api.slot_spools(42)

        assert run(api, scenario, clock=FakeClock()) == ({}, 2, {0: spool})

    def test_a_retry_that_fell_due_isnt_dropped(self):
        # As when the refresh it was due at failed before retrying, or another lookup was slow.
        api = FakePrintLogApi()
        clock = FakeClock()

        async def scenario(spools):
            await spools.spool_reading(reading(0, tag="DEADBEEF"))
            clock.now += UNKNOWN_TAG_RETRY_S + 50
            return spools._next_wake(clock.now + 100)

        assert run(api, scenario, clock=clock) == clock.now

    def test_a_failed_lookup_after_another_spools_tag_in_the_slot_isnt_dropped(self):
        api = FakePrintLogApi()
        api.add_spool(tags=("04A23F11",))
        clock = FakeClock()

        async def scenario(spools):
            await spools.spool_reading(reading(0, tag="04A23F11"))
            api.fail_next(1)
            await spools.spool_reading(reading(0, tag="DEADBEEF"))  # swapped, with no gap
            return spools._next_wake(clock.now + 2 * UNKNOWN_TAG_RETRY_S)

        assert run(api, scenario, clock=clock) == clock.now + UNKNOWN_TAG_RETRY_S

    def test_a_retry_for_a_tag_no_longer_in_its_slot_is_dropped(self):
        api = FakePrintLogApi()
        clock = FakeClock()

        async def scenario(spools):
            spools._retry_at[("u1", 0)] = ("DEADBEEF", clock.now - 1)  # no reading has it
            return spools._next_wake(clock.now + 100), dict(spools._retry_at)

        assert run(api, scenario, clock=clock) == (clock.now + 100, {})

    def test_cancelling_run_as_a_retry_is_scheduled_stops_it(self):
        # Before Python 3.12, wait_for swallowed a cancel that came with its inner wait's result,
        # so the bridge never finished shutting down.
        api = FakePrintLogApi()

        async def scenario(spools):
            task = asyncio.ensure_future(spools.run())
            await eventually(lambda: len(api.requests) > 0)
            await asyncio.sleep(0.05)  # waiting for its next refresh
            spools._rescheduled.set()
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=1)
            if not done:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            return bool(done) and task.cancelled()

        assert run(api, scenario, interval=60)

    def test_running_against_an_api_that_is_down_doesnt_ask_over_and_over(self):
        api = FakePrintLogApi()

        async def scenario(spools):
            task = asyncio.ensure_future(spools.run())
            try:
                await spools.spool_reading(reading(0, tag="DEADBEEF"))
                api.fail_next(1000)  # 3D Print Log is down from here on
                await asyncio.sleep(0.3)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        run(api, scenario, interval=60, retry_after=0.05)

        assert tag_lookups(api) <= 0.3 / 0.05 + 2  # not as fast as it can

    def test_running_with_no_unknown_tag_only_refreshes(self):
        api = FakePrintLogApi()
        api.add_spool(tags=("04A23F11",))

        async def scenario(spools):
            task = asyncio.ensure_future(spools.run())
            try:
                await spools.spool_reading(reading(0, tag="04A23F11"))
                await asyncio.sleep(0.3)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        run(api, scenario, interval=60, retry_after=0.01)

        assert tag_lookups(api) == 1
        assert sum(1 for m, p in api.requests if m == "GET" and p == "/api/Printers/42") <= 3


class TestMismatch:
    def scenario(self, api: FakePrintLogApi, seen: SpoolReading, slots=(0,)):
        async def main(spools):
            await spools.refresh_all()
            await spools.spool_reading(seen)
            return spools.mismatches("u1", slots)

        return run(api, main)

    def test_another_material_is_a_note(self):
        api = FakePrintLogApi()
        api.user_loads(42, 0, api.add_spool("Basic PLA", material="PLA", color_hex="#FF0000"))

        [note] = self.scenario(api, reading(0, material="PETG", color_hex="#FF0000"))

        assert "PETG" in note
        assert "PLA" in note
        assert "Basic PLA" in note
        assert "T0" in note

    def test_another_color_is_a_note(self):
        api = FakePrintLogApi()
        api.user_loads(42, 0, api.add_spool("Red", material="PLA", color_hex="#FF0000"))

        [note] = self.scenario(api, reading(0, material="PLA", color_hex="#0000FF"))

        assert "#0000FF" in note
        assert "#FF0000" in note

    def test_the_same_material_and_a_close_color_is_no_note(self):
        api = FakePrintLogApi()
        api.user_loads(42, 0, api.add_spool(material="PLA+", color_hex="ff0000"))

        assert self.scenario(api, reading(0, material="PLA", color_hex="#F80808")) == []

    def test_a_variant_of_the_spools_material_is_no_note(self):
        api = FakePrintLogApi()
        api.user_loads(42, 0, api.add_spool(material="PETG-CF"))

        assert self.scenario(api, reading(0, material="PETG")) == []

    def test_a_slot_the_print_doesnt_use_is_no_note(self):
        api = FakePrintLogApi()
        api.slot_counts[42] = 4
        api.user_loads(42, 2, api.add_spool(material="PLA"))

        assert self.scenario(api, reading(2, material="TPU"), slots=(0,)) == []

    def test_a_slot_with_no_spool_is_no_note(self):
        api = FakePrintLogApi()

        assert self.scenario(api, reading(0, material="TPU")) == []

    def test_a_tag_that_assigned_its_spool_is_no_note(self):
        api = FakePrintLogApi()
        api.user_loads(42, 0, api.add_spool("Old", material="PLA"))
        api.add_spool("New", material="TPU", tags=("04A23F11",))

        assert self.scenario(api, reading(0, tag="04A23F11", material="TPU")) == []


def test_color_distance_is_zero_for_the_same_color_in_any_format():
    assert color_distance("#ff0000", "FF0000") == 0
    assert color_distance("#FF0000", "#00FF00") > 300
    assert color_distance("#FF0000", "not a color") is None


class TestWatchLoaded:
    """How an adapter hears that a printer's spools changed: Moonraker's active spool follows."""

    def test_a_watcher_hears_each_change_with_each_slots_spool_number(self):
        api = FakePrintLogApi()
        api.slot_counts[42] = 2
        first = api.add_spool("Galaxy Black PLA", number=4)
        second = api.add_spool("Silk Gold", number=7)
        api.user_loads(42, 0, first)
        heard: list[dict[int, int | None]] = []

        def numbers(loaded):
            heard.append({slot: f.get("spoolNumber") for slot, f in loaded.items()})

        async def scenario(spools: Spools):
            spools.watch_loaded("u1", numbers)
            await spools.refresh("u1")
            await spools.refresh("u1")  # nothing changed: nothing to hear
            await spools.spool_command(command(1, filament_id=second))
            await spools.spool_command(command(0))  # cleared
            await spools.refresh("u1")

        run(api, scenario)

        assert heard == [{0: 4}, {0: 4, 1: 7}, {1: 7}]

    def test_a_new_watcher_hears_what_is_already_known(self):
        api = FakePrintLogApi()
        api.user_loads(42, 0, api.add_spool("Loaded", number=4))
        heard = []

        async def scenario(spools: Spools):
            spools.watch_loaded("u1", heard.append)  # nothing read yet: nothing to hear
            await spools.refresh("u1")
            spools.watch_loaded("u1", heard.append)

        run(api, scenario)

        assert [{s: f["spoolNumber"] for s, f in loaded.items()} for loaded in heard] == [
            {0: 4},
            {0: 4},
        ]

    def test_a_watcher_hears_only_its_printer_and_not_once_unwatched(self):
        api = FakePrintLogApi()
        api.user_loads(42, 0, api.add_spool("Loaded", number=4))
        other, mine = [], []

        async def scenario(spools: Spools):
            spools.watch_loaded("voron", other.append)
            unwatch = spools.watch_loaded("u1", mine.append)
            unwatch()
            await spools.refresh("u1")

        run(api, scenario)

        assert other == [] and mine == []

    def test_a_watcher_that_fails_breaks_nothing(self, caplog):
        api = FakePrintLogApi()
        spool = api.add_spool("Loaded", number=4)

        def broken(loaded):
            raise RuntimeError("boom")

        async def scenario(spools: Spools):
            spools.watch_loaded("u1", broken)
            return await spools.spool_command(command(0, filament_id=spool))

        answer = run(api, scenario)

        assert "is now Loaded" in answer
        assert api.slot_spools(42) == {0: spool}
