"""Injectable simulated-time contract for text and native environments."""
from datetime import datetime, timezone

import pytest

from pico import Clock, EnvironmentProfile, SimulatedClock, SystemClock
from pico.providers.native_tools import NativeToolConversation
from pico.prompt_prefix import build_prompt_prefix
from pico.workspace import WorkspaceContext

EPOCH = 1728975600.0  # 2024-10-15 07:00 UTC


def test_simulated_clock_formats_and_advances():
    clock = SimulatedClock(EPOCH, step_seconds=60)
    assert clock.now() == EPOCH
    assert clock.iso() == "2024-10-15 07:00"
    clock.tick()
    assert clock.iso() == "2024-10-15 07:01"
    clock.advance(120)
    assert clock.iso() == "2024-10-15 07:03"


def test_simulated_clock_rejects_bad_values():
    with pytest.raises(ValueError, match="epoch"):
        SimulatedClock("not-a-number")
    with pytest.raises(ValueError, match="step_seconds"):
        SimulatedClock(EPOCH, step_seconds=0)


def test_system_clock_is_close_to_real_utc():
    clock = SystemClock()
    assert abs(clock.now() - datetime.now(timezone.utc).timestamp()) < 5


def test_profile_without_clock_has_no_time_line(tmp_path):
    profile = EnvironmentProfile(name="external", role="Operate the environment.")
    assert profile.time_line() == ""
    prefix = build_prompt_prefix(WorkspaceContext.build(tmp_path), {}, environment=profile)
    assert "Current simulated time" not in prefix.text
    conversation = NativeToolConversation({}, "workspace", "request", None,
                                         require_reasoning=False, environment=profile)
    assert "Current simulated time" not in conversation.messages[0]["content"]


def test_profile_with_clock_surfaces_time_in_both_wires(tmp_path):
    profile = EnvironmentProfile(name="external", role="Operate the environment.",
                                 clock=SimulatedClock(EPOCH))
    prefix = build_prompt_prefix(WorkspaceContext.build(tmp_path), {}, environment=profile)
    assert "Current simulated time: 2024-10-15 07:00" in prefix.text
    conversation = NativeToolConversation({}, "workspace", "request", None,
                                         require_reasoning=False, environment=profile)
    assert "Current simulated time: 2024-10-15 07:00" in conversation.messages[0]["content"]


def test_profile_rejects_clock_without_interface():
    with pytest.raises(ValueError, match="now\\(\\) and iso\\(\\)"):
        EnvironmentProfile(name="external", role="role", clock=object())


def test_clock_interface_is_stable():
    assert isinstance(SimulatedClock(EPOCH), Clock)
    assert isinstance(SystemClock(), Clock)
