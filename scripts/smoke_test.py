from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TypeVar

from arcam.fmj.client import Client, ClientContext
from arcam.fmj.codecs import PresetDetail, SourceCodes
from arcam.fmj.commands import (
    AUTO_SHUTDOWN_CONTROL,
    COMMANDS,
    CURRENT_SOURCE,
    DAB_SCAN,
    DISPLAY_BRIGHTNESS_WRITE_SUPPORTED,
    DISPLAY_INFO_TYPE,
    FM_SCAN,
    LIFTER_TEMPERATURE,
    MUTE,
    MUTE_WRITE_SUPPORTED,
    OUTPUT_TEMPERATURE,
    POWER,
    POWER_WRITE_SUPPORTED,
    PRESET_DETAIL,
    ReadCommand,
    ReadWriteCommand,
    SAVE_RESTORE_COPY_OF_SETTINGS,
    SOFTWARE_VERSION,
    SOURCE_WRITE_SUPPORTED,
    TUNER_PRESET,
    VOLUME,
    VOLUME_STEP_SUPPORTED,
)
from arcam.fmj.errors import (
    ArcamException,
    CommandInvalidAtThisTime,
    ResponseException,
)
from arcam.fmj.packets import ResponsePacket
from arcam.fmj.state import State


_T = TypeVar("_T")
Check = Callable[[], Awaitable[str | None]]


class CheckFailed(Exception):
    pass


class CheckSkipped(Exception):
    pass


@dataclass(frozen=True)
class Result:
    status: str
    name: str
    detail: str


class SmokeSuite:
    def __init__(self, state: State, timeout: float) -> None:
        self.state = state
        self.timeout = timeout
        self.results: list[Result] = []

    async def check(self, name: str, check: Check) -> None:
        try:
            detail = await check() or ""
        except CheckSkipped as exception:
            result = Result("SKIP", name, str(exception))
        except CheckFailed as exception:
            result = Result("FAIL", name, str(exception))
        except (ArcamException, TimeoutError, OSError, ValueError) as exception:
            result = Result("FAIL", name, f"{type(exception).__name__}: {exception}")
        else:
            result = Result("PASS", name, detail)
        self.results.append(result)
        suffix = f": {result.detail}" if result.detail else ""
        print(f"{result.status:4} {result.name}{suffix}")

    def failed(self) -> bool:
        return any(result.status == "FAIL" for result in self.results)


def require(condition: bool, detail: str) -> None:
    if not condition:
        raise CheckFailed(detail)


async def read_command(state: State, command: ReadCommand[_T]) -> _T | None:
    data = await state.request(command, b"\xf0")
    state.set_cached(command.cc, data)
    return command.read(data, state.model)


async def read_source(state: State) -> SourceCodes | None:
    data = await state.request(CURRENT_SOURCE, b"\xf0")
    state.set_cached(CURRENT_SOURCE.cc, data)
    return state.get_source()


async def wait_for_command(
    state: State,
    command: ReadCommand[_T],
    expected: _T,
    timeout: float,
) -> _T:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        actual = await read_command(state, command)
        if actual == expected:
            return actual
        if asyncio.get_running_loop().time() >= deadline:
            raise CheckFailed(
                f"{command.name} read back as {actual!r}, expected {expected!r}"
            )
        await asyncio.sleep(0.2)


async def wait_for_source(
    state: State, expected: SourceCodes, timeout: float
) -> SourceCodes:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        actual = await read_source(state)
        if actual == expected:
            return actual
        if asyncio.get_running_loop().time() >= deadline:
            raise CheckFailed(
                f"source read back as {actual!r}, expected {expected.name}"
            )
        await asyncio.sleep(0.2)


@asynccontextmanager
async def preserved_command(
    state: State,
    command: ReadWriteCommand[_T],
    timeout: float,
) -> AsyncIterator[_T]:
    original = await read_command(state, command)
    if original is None:
        raise CheckSkipped(f"{command.name} did not return a value")
    try:
        yield original
    finally:
        await state.set(command, original)
        await wait_for_command(state, command, original, timeout)


@asynccontextmanager
async def preserved_source(state: State, timeout: float) -> AsyncIterator[SourceCodes]:
    original = await read_source(state)
    if original is None:
        raise CheckSkipped("current source did not decode")
    try:
        yield original
    finally:
        await state.set_source(original)
        await wait_for_source(state, original, timeout)


async def ask_yes_no(prompt: str) -> bool:
    response = await asyncio.to_thread(input, f"{prompt} [y/N] ")
    return response.strip().lower() in {"y", "yes"}


async def check_identity(state: State, expected_model: str) -> str:
    require(state.model is not None, "AMX discovery did not return a model")
    require(
        state.model == expected_model,
        f"connected to {state.model}, expected {expected_model}",
    )
    return f"model={state.model}, revision={state.revision}, api={state.api_model.name}"


async def check_initial_state(state: State) -> str:
    values = {
        POWER.name: state.get(POWER),
        VOLUME.name: state.get(VOLUME),
        MUTE.name: state.get(MUTE),
        SOFTWARE_VERSION.name: state.get(SOFTWARE_VERSION),
        CURRENT_SOURCE.name: state.get_source(),
    }
    missing = [name for name, value in values.items() if value is None]
    require(not missing, f"missing initial values: {', '.join(missing)}")
    rendered = ", ".join(f"{name}={value}" for name, value in values.items())
    return rendered


async def check_catalogue(state: State) -> str:
    supported = [
        command
        for command in COMMANDS
        if isinstance(command, ReadCommand) and state.is_command_supported(command)
    ]
    populated = [
        command for command in supported if state.get_cached(command.cc) is not None
    ]
    for command in populated:
        state.get(command)
    require(populated, "no readable commands were populated")
    return f"decoded {len(populated)} of {len(supported)} supported readable commands"


async def check_support_matrix(state: State) -> str:
    expected = {
        SOFTWARE_VERSION: True,
        SAVE_RESTORE_COPY_OF_SETTINGS: True,
        DISPLAY_INFO_TYPE: True,
        FM_SCAN: True,
        DAB_SCAN: True,
        AUTO_SHUTDOWN_CONTROL: False,
        LIFTER_TEMPERATURE: False,
        OUTPUT_TEMPERATURE: False,
    }
    mismatches = [
        f"{command.name}={state.is_command_supported(command)}"
        for command, supported in expected.items()
        if state.is_command_supported(command) is not supported
    ]
    require(not mismatches, f"unexpected AV41 support: {', '.join(mismatches)}")
    return "AV41 support gates match the command catalogue"


async def check_software_versions(state: State) -> str:
    names = ("RS232", "Host", "OSD", "DSP", "NET", "IAP")
    versions: dict[str, str] = {}
    unavailable: list[str] = []
    for offset, name in enumerate(names):
        try:
            data = await state.request(SOFTWARE_VERSION, bytes([0xF0 + offset]))
        except ResponseException:
            unavailable.append(name)
            continue
        version = SOFTWARE_VERSION.read(data, state.model)
        require(version is not None, f"{name} returned undecodable payload {data!r}")
        versions[name] = version
    require("RS232" in versions, "RS232 software version was unavailable")
    available = ", ".join(f"{name}={version}" for name, version in versions.items())
    missing = f"; unavailable={','.join(unavailable)}" if unavailable else ""
    return f"{available}{missing}"


async def check_volume_round_trip(state: State, timeout: float) -> str:
    power = await read_command(state, POWER)
    if power is not True:
        raise CheckSkipped("zone is not powered on")
    async with preserved_command(state, VOLUME, timeout) as original:
        if original <= 0:
            raise CheckSkipped("volume is already at its minimum")
        target = original - 1
        await state.set(VOLUME, target)
        await wait_for_command(state, VOLUME, target, timeout)
    return f"changed {original} -> {target} -> {original}"


async def check_mute_round_trip(state: State, timeout: float) -> str:
    power = await read_command(state, POWER)
    if power is not True:
        raise CheckSkipped("zone is not powered on")
    async with preserved_command(state, MUTE, timeout) as original:
        await state.set(MUTE, True)
        await wait_for_command(state, MUTE, True, timeout)
    return f"mute asserted and restored to {original}"


async def check_source_reselect(state: State, timeout: float) -> str:
    original = await read_source(state)
    if original is None:
        raise CheckSkipped("current source did not decode")
    await state.set_source(original)
    await wait_for_source(state, original, timeout)
    return f"reselected {original.name} through the AV41 RC5 fallback"


async def check_display_write(state: State, timeout: float) -> str:
    async with preserved_command(state, DISPLAY_INFO_TYPE, timeout) as original:
        await state.set(DISPLAY_INFO_TYPE, original)
        await wait_for_command(state, DISPLAY_INFO_TYPE, original, timeout)
    return f"wrote and read back display type 0x{original:02X}"


async def check_direct_write_scope(state: State) -> str:
    direct = {
        "power": state.api_model in POWER_WRITE_SUPPORTED,
        "mute": state.api_model in MUTE_WRITE_SUPPORTED,
        "source": state.api_model in SOURCE_WRITE_SUPPORTED,
        "display brightness": state.api_model in DISPLAY_BRIGHTNESS_WRITE_SUPPORTED,
        "volume step": state.api_model in VOLUME_STEP_SUPPORTED,
    }
    enabled = [name for name, supported in direct.items() if supported]
    if not enabled:
        raise CheckSkipped("new direct-write paths require an SA or ST model")
    return f"direct paths available: {', '.join(enabled)}"


async def check_preset_formatting(state: State) -> str:
    presets = state.get_preset_details()
    if not presets:
        raise CheckSkipped("no tuner presets were populated on the current source")
    rendered = ", ".join(
        f"{index}={detail.name}" for index, detail in sorted(presets.items())
    )
    return rendered


async def check_persistent_setting_scope() -> str:
    raise CheckSkipped("settings backup/restore and auto-shutdown writes are not sent")


async def run_autonomous(suite: SmokeSuite, expected_model: str) -> None:
    state = suite.state
    await suite.check("device identity", lambda: check_identity(state, expected_model))
    await suite.check("initial data-driven update", lambda: check_initial_state(state))
    await suite.check("command catalogue decoding", lambda: check_catalogue(state))
    await suite.check("AV41 command support", lambda: check_support_matrix(state))
    await suite.check(
        "software version variants", lambda: check_software_versions(state)
    )
    await suite.check(
        "volume write/read/restore",
        lambda: check_volume_round_trip(state, suite.timeout),
    )
    await suite.check(
        "mute write/read/restore",
        lambda: check_mute_round_trip(state, suite.timeout),
    )
    await suite.check(
        "source fallback reselect",
        lambda: check_source_reselect(state, suite.timeout),
    )
    await suite.check(
        "display information write/read",
        lambda: check_display_write(state, suite.timeout),
    )
    await suite.check(
        "direct-write device coverage", lambda: check_direct_write_scope(state)
    )
    await suite.check(
        "preset frequency formatting", lambda: check_preset_formatting(state)
    )
    await suite.check("persistent-setting coverage", check_persistent_setting_scope)


async def check_physical_volume_update(state: State, timeout: float) -> str:
    power = await read_command(state, POWER)
    if power is not True:
        raise CheckSkipped("zone is not powered on")
    async with preserved_command(state, VOLUME, timeout) as original:
        packets: list[ResponsePacket] = []

        def collect(packet: object) -> None:
            if isinstance(packet, ResponsePacket) and packet.cc == VOLUME.cc:
                packets.append(packet)

        with state.client.listen(collect):
            await asyncio.to_thread(
                input,
                "Press a physical volume button once, then press Enter here: ",
            )
            await asyncio.sleep(0.5)
        actual = await read_command(state, VOLUME)
        require(actual != original, f"volume remained at {original}")
        require(packets, "no unsolicited volume status packet was observed")
    return f"observed {original} -> {actual}; restored {original}"


async def check_front_panel_display(state: State, timeout: float) -> str:
    async with preserved_command(state, DISPLAY_INFO_TYPE, timeout):
        await state.set(DISPLAY_INFO_TYPE, 0xE0)
        observed = await ask_yes_no("Did the front-panel information cycle?")
        require(observed, "front-panel change was not observed")
    return "display cycle was observed and the original type was restored"


async def read_fm_presets(state: State) -> dict[int, PresetDetail]:
    presets: dict[int, PresetDetail] = {}
    for index in range(1, 51):
        try:
            data = await state.request(PRESET_DETAIL, bytes([index]))
        except CommandInvalidAtThisTime:
            break
        if data != b"\x00":
            presets[index] = PresetDetail.from_bytes(data)
    return presets


async def check_fm_scans(state: State, timeout: float) -> str:
    if not await ask_yes_no(
        "FM scans can change the remembered tuner frequency. Run both directions?"
    ):
        raise CheckSkipped("declined")
    async with preserved_source(state, timeout):
        await state.set_source(SourceCodes.FM)
        try:
            await wait_for_source(state, SourceCodes.FM, timeout)
        except CheckFailed as exception:
            raise CheckSkipped("FM source was unavailable") from exception
        original_preset = await read_command(state, TUNER_PRESET)
        await state.fm_scan(True)
        scan_up = await ask_yes_no("Did the front panel show an upward FM scan?")
        await state.fm_scan(False)
        scan_down = await ask_yes_no("Did the front panel show a downward FM scan?")
        presets = await read_fm_presets(state)
        if original_preset is not None:
            await state.set(TUNER_PRESET, original_preset)
            await wait_for_command(state, TUNER_PRESET, original_preset, timeout)
        require(scan_up and scan_down, "one or both scan directions were not observed")
    rendered = ", ".join(
        f"{index}={detail.name}" for index, detail in sorted(presets.items())
    )
    preset_detail = rendered or "no presets reported"
    restore_detail = (
        f"preset {original_preset} restored"
        if original_preset is not None
        else "no selected preset was available to restore"
    )
    return f"{preset_detail}; {restore_detail}"


async def check_dab_scan(state: State, timeout: float) -> str:
    if not await ask_yes_no(
        "A DAB scan may update stored station data. Attempt it on this US AV41?"
    ):
        raise CheckSkipped("declined")
    async with preserved_source(state, timeout):
        await state.set_source(SourceCodes.DAB)
        try:
            await wait_for_source(state, SourceCodes.DAB, timeout)
        except CheckFailed as exception:
            raise CheckSkipped("DAB source was unavailable") from exception
        await state.dab_scan()
        observed = await ask_yes_no("Did the front panel show a DAB scan?")
        require(observed, "DAB scan was not observed")
    return "DAB scan was accepted; original source restored"


async def run_interactive(suite: SmokeSuite) -> None:
    state = suite.state
    await suite.check(
        "physical-button status update",
        lambda: check_physical_volume_update(state, suite.timeout),
    )
    await suite.check(
        "front-panel display cycle",
        lambda: check_front_panel_display(state, suite.timeout),
    )
    await suite.check(
        "FM scan and preset decoding", lambda: check_fm_scans(state, suite.timeout)
    )
    await suite.check("DAB scan", lambda: check_dab_scan(state, suite.timeout))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run non-persistent live-device smoke checks against an AV41."
    )
    parser.add_argument("host", help="receiver hostname or IP address")
    parser.add_argument(
        "mode",
        choices=("autonomous", "interactive", "all"),
        nargs="?",
        default="autonomous",
        help="test group to run (default: autonomous)",
    )
    parser.add_argument("--port", type=int, default=50000, help="IP control port")
    parser.add_argument("--zone", type=int, default=1, help="zone to exercise")
    parser.add_argument(
        "--expected-model",
        default="AV41",
        help="required AMX model response",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="seconds allowed for state readback",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


async def async_main(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    client = Client(args.host, args.port)
    try:
        async with ClientContext(client), State(client, args.zone) as state:
            async with asyncio.timeout(max(30.0, args.timeout)):
                await state.update()
            suite = SmokeSuite(state, args.timeout)
            if args.mode in {"autonomous", "all"}:
                print("Autonomous checks")
                await run_autonomous(suite, args.expected_model)
            if args.mode in {"interactive", "all"}:
                print("Interactive checks")
                await run_interactive(suite)
    except (ArcamException, TimeoutError, OSError) as exception:
        print(f"Connection failed: {type(exception).__name__}: {exception}")
        return 2
    passed = sum(result.status == "PASS" for result in suite.results)
    skipped = sum(result.status == "SKIP" for result in suite.results)
    failed = sum(result.status == "FAIL" for result in suite.results)
    print(f"Summary: {passed} passed, {skipped} skipped, {failed} failed")
    return 1 if suite.failed() else 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
