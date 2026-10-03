"""The ratchet guard: a learned P1 target rises at most DAY_RISE_PTS (2 points) a grow-day.

22-26 Sep 2026, F2 zone 3: the learner took p1_target_vwc 40.9 -> 74.5, field_capacity 42.9 -> 76.5 and
p2_vwc_threshold 37.4 -> 69.6 in four days. Every "reached" day raised the learned peak to whatever the extra ramp
water achieved, the target followed at peak + PROBE_STEP_PTS, and nothing but the per-write step limits stood in the
way, with a write every loop. Down is never held back: that is the P1 fail-over. A target the zone entered P1 at or
above (4 Oct 2026, zone 2: 29.5 % under a probe reading 44 % after the 29 Sep sump flood) was stale, not learned: it
may go to the learned peak + PROBE_STEP_PTS the same grow-day.
"""
import json
from datetime import datetime, timedelta

import pytest
import test_auto_setpoints_runtime as asr

import auto_setpoints as au
import setpoint_supervisor as ss

T0 = 1_800_000_000.0
Z3 = dict(p1_target_vwc=40.9, field_capacity=42.9, p2_vwc_threshold=37.4, p3_emergency_vwc_threshold=22.0,
          p2_shot_size=3.0)  # F2 zone 3 on 22 Sep 2026
Z2 = dict(p1_target_vwc=29.5, field_capacity=50.0, p2_vwc_threshold=27.5, p3_emergency_vwc_threshold=20.0,
          p2_shot_size=6.0)  # F2 zone 2 on 4 Oct 2026, before the operator's stopgap
SAVED_BY_3_8_0 = {  # zone 2's learner block as controller 3.8.0 wrote it (tests_ha/fixtures/entry_3_8_learned_tent.json)
    "peak_adj": 0.0, "jev": {"day": None, "nudged": [], "asked": None, "last": None, "changed": None},
    "peak": 65.54, "gain": 1.368, "day_rate": 1.621, "night_rate": 0.815, "day_n": 8, "night_n": 8,
    "day_acc": [56.7, 35], "night_acc": [6.5, 8], "hold_days": 0, "day": "2026-10-03", "ramp_start": 44.0,
    "ramp": [{"pct": 2.0, "rise": 2.1, "settled": 46.1, "flat": False},
             {"pct": 2.0, "rise": 1.8, "settled": 47.9, "flat": False}],
    "pending": None, "outcome": "reached", "stalled_at": None, "last_change": "22:02 p2_shot_size 5.5 -> 6",
    "prev_peak": None, "prev_hold": 0, "veto": None,
}


def _passes(learn, current, vwc, phase, n=20):
    """The controller's loop, a pass a minute: what is wanted goes through the supervisor's step limits and
    Home Assistant reflects each write before the next pass."""
    for k in range(n):
        ceiling = min(current["p1_target_vwc"], current["field_capacity"])
        au.tick(learn, vwc, phase, T0 + k * 60, True, 0.0, 60, ceiling=ceiling)
        for suffix, value, _why in ss.writes(current, au.wanted(learn, current, vwc, phase, None)):
            current[suffix] = value
    return current


# ------------------------------------------------------------------ up: two points a grow-day
def test_zone_3s_four_days_now_move_its_target_eight_points_not_thirty_four():
    learn, current = au.fresh(), dict(Z3)
    learn.update(gain=1.9, outcome="reached")
    for day, peak in enumerate((49.3, 57.7, 66.1, 74.5)):  # each "reached" day left a peak ~8.4 points higher
        au.new_day(learn, f"2026-09-{23 + day}", 36.0)
        learn["peak"] = peak
        before = current["p1_target_vwc"]
        _passes(learn, current, 36.0, "P0")  # from lights-on...
        _passes(learn, current, 39.0, "P2")  # ...through the day
        assert current["p1_target_vwc"] <= before + au.DAY_RISE_PTS
        assert current["field_capacity"] == round(current["p1_target_vwc"] + 2.0, 1)
    assert au.p1_target(learn, 39.0, "P2") == 75.5  # what the learner wanted on day four...
    assert current["p1_target_vwc"] == 48.9  # ...and what it got: 40.9 + 4 x 2
    # While the target is held under the learned peak the re-water threshold is left where it was: its band hangs
    # off that peak, and following it was the other half of the ratchet (37.4 -> 69.6).
    assert current["p2_vwc_threshold"] == Z3["p2_vwc_threshold"]


def test_the_two_points_are_counted_from_where_the_grow_day_began_not_from_the_last_write():
    learn, current = au.fresh(), dict(Z3)
    learn.update(peak=60.0, gain=1.9, outcome="reached")
    au.new_day(learn, "2026-09-23", 36.0)
    _passes(learn, current, 36.0, "P2", n=200)  # a whole day of passes
    assert current["p1_target_vwc"] == 42.9 and learn["day_ceiling"] == 40.9
    au.new_day(learn, "2026-09-24", 36.0)
    _passes(learn, current, 36.0, "P2", n=200)
    assert current["p1_target_vwc"] == 44.9 and learn["day_ceiling"] == 42.9


# ------------------------------------------------------------------ down: never held back
def test_the_p1_fail_over_drops_the_target_at_once_however_far():
    learn, current = au.fresh(), dict(p1_target_vwc=40.0, field_capacity=55.0, p2_vwc_threshold=34.0,
                                      p3_emergency_vwc_threshold=22.0, p2_shot_size=3.0)
    au.new_day(learn, "2026-09-20", 30.0)
    vwc = 30.0
    for k, rise in enumerate((1.8, 1.8, 0.1, 0.1)):  # a ramp that stops lifting the probe at 33.8
        au.shot(learn, "P1", 3.0, vwc, T0 + k * 1260)
        vwc += rise
        au.tick(learn, vwc, "P1", T0 + (k + 1) * 1260, True, 0.0, 21, ceiling=40.0)
    assert au.ramp_outcome(learn, "P1") == "plateau"
    _passes(learn, current, vwc, "P1", n=3)
    assert current["p1_target_vwc"] == 33.7  # 6.3 points down the same grow-day: a target the zone has met


# ------------------------------------------------------------------ a stale target: the bug on zone 2
def test_a_target_the_zone_entered_p1_above_is_stale_and_may_go_to_the_learned_peak_at_once():
    learn, current = au.restore(SAVED_BY_3_8_0), dict(Z2)
    au.new_day(learn, "2026-10-04", 44.0)
    _passes(learn, current, 44.0, "P0", n=1)  # lights-on: two points, as on any day
    assert current["p1_target_vwc"] == 31.5 and learn["stale_target"] is None
    _passes(learn, current, 44.0, "P1", n=1)  # P1 entered at 44 %, over its 31.5 % ceiling
    assert learn["stale_target"] is True
    assert au.wanted(learn, current, 44.0, "P2", None)["p1_target_vwc"] == 66.5  # not held to 31.5


def test_a_stale_target_still_never_goes_above_the_learned_peak_plus_one_probe_step():
    learn, current = au.restore(SAVED_BY_3_8_0), dict(Z2)
    learn["peak_adj"] = 2.0  # what the old hourly judge may have asked to hold it above
    au.new_day(learn, "2026-10-04", 44.0)
    _passes(learn, current, 44.0, "P0", n=1)
    _passes(learn, current, 44.0, "P1", n=1)
    assert au.wanted(learn, current, 44.0, "P2", None)["p1_target_vwc"] == round(65.54 + au.PROBE_STEP_PTS, 1)


def test_a_zone_that_entered_p1_under_its_ceiling_is_held_to_two_points():
    learn, current = au.restore(SAVED_BY_3_8_0), dict(Z2, p1_target_vwc=48.0)  # after the operator's stopgap
    au.new_day(learn, "2026-10-05", 42.0)
    _passes(learn, current, 42.0, "P0", n=1)
    _passes(learn, current, 40.5, "P1", n=1)  # under its 50 % ceiling: the ramp runs, the target is not stale
    assert learn["stale_target"] is False
    assert au.wanted(learn, current, 52.0, "P2", None)["p1_target_vwc"] == 50.0


def test_only_the_first_look_at_p1_counts_as_p1_entry():
    learn = au.restore(SAVED_BY_3_8_0)
    au.new_day(learn, "2026-10-04", 44.0)
    au.tick(learn, 30.0, "P1", T0, True, 0.0, 60, ceiling=40.0)
    au.tick(learn, 45.0, "P1", T0 + 1260, True, 0.0, 60, ceiling=40.0)  # the ramp has reached it since
    assert learn["stale_target"] is False
    au.new_day(learn, "2026-10-05", 44.0)
    assert learn["stale_target"] is None and learn["day_ceiling"] is None


# ------------------------------------------------------------------ an old install
def test_a_learner_block_saved_before_the_guard_loads_and_the_guard_starts_from_the_first_pass():
    learn = au.restore(json.loads(json.dumps(SAVED_BY_3_8_0)))
    assert learn["day_ceiling"] is None and learn["stale_target"] is None
    assert {k: learn[k] for k in SAVED_BY_3_8_0} == SAVED_BY_3_8_0  # everything it learned, unchanged
    current = dict(Z2, p1_target_vwc=48.0)  # the release lands mid-day on zone 2, after the stopgap
    au.tick(learn, 52.0, "P2", T0, True, 0.0, 60, ceiling=48.0)
    assert learn["day_ceiling"] == 48.0
    assert au.wanted(learn, current, 52.0, "P2", None)["p1_target_vwc"] == 50.0
    assert au.restore(json.loads(json.dumps(learn))) == learn  # and the new keys survive a restart


def test_junk_in_the_new_keys_is_handled_like_any_other_junk():
    saved = dict(SAVED_BY_3_8_0, day_ceiling="high", stale_target=None)
    assert au.restore(saved) == au.fresh()  # a corrupt file never crashes the engine
    assert au.restore(dict(SAVED_BY_3_8_0, day_ceiling=48, stale_target=1))["stale_target"] is True


# ------------------------------------------------------------------ in the controller
@pytest.fixture
def ramp_clock(monkeypatch):
    asr._Clock.current = datetime(2026, 9, 22, 10, 0)
    monkeypatch.setattr(asr.controller, "datetime", asr._Clock)


def test_zone_3s_four_days_through_the_controller(ramp_clock):
    numbers = {f"number.crop_steering_zone_1_{s}": (str(v), {}) for s, v in Z3.items()}
    c, fake, room = asr._rig(numbers=numbers)  # no Jev: the learner owns every target, as it did on 22 Sep
    room.state[1]["learn"].update(gain=1.9, outcome="reached")
    for day, peak in enumerate((49.3, 57.7, 66.1, 74.5)):
        now = datetime(2026, 9, 23 + day, 10, 0)
        room.state[1]["learn"]["peak"] = peak  # what the day before's "reached" ramp left it at
        for minute in range(30):  # lights-on, then the morning, a pass a minute
            asr._Clock.current = now + timedelta(minutes=minute)
            room.state[1]["phase"] = "P0" if minute == 0 else "P2"
            asr._tick(c, fake, room, 38.0, asr._Clock.current)
    number = {s: float(fake.states[f"number.crop_steering_zone_1_{s}"][0]) for s in Z3}
    # 40.9 + 4 x 2, counted from the engine's own P1 ceiling, which the controller reads every pass (was 75.5)
    assert number["p1_target_vwc"] == 48.9 and number["field_capacity"] == 50.9
    assert number["p2_vwc_threshold"] == 37.4  # was 68.8
