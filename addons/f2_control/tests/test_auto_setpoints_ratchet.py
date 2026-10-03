"""The ratchet guard: a learned P1 target rises at most DAY_RISE_PTS (2 points) a grow-day, and only after a ramp
that reached it.

22-26 Sep 2026, F2 zone 3: the learner took p1_target_vwc 40.9 -> 74.5, field_capacity 42.9 -> 76.5 and
p2_vwc_threshold 37.4 -> 69.6 in four days. Every "reached" day raised the learned peak to whatever the extra ramp
water achieved, the target followed at peak + PROBE_STEP_PTS, and nothing but the per-write step limits stood in the
way, with a write every loop. Up, a target now moves 2 points over the ceiling its grow-day began with, and only once
that day's ramp reached it (or plateaued, believed): after a stalled, short or suspect ramp it holds. Down is never
held back: that is the P1 fail-over. A target the zone entered P1 at or above (4 Oct 2026, zone 2: 29.5 % under a
probe reading 44 % after the 29 Sep sump flood) was stale, not learned: it may go the same grow-day to 2 points over
the reading P1 began at, never above the learned peak + PROBE_STEP_PTS. Never straight to that peak: zone 3's is
still the ratchet's 79.21.
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
Z3_RESET = dict(p1_target_vwc=41.0, field_capacity=43.0, p2_vwc_threshold=34.5, p3_emergency_vwc_threshold=22.0,
                p2_shot_size=4.0)  # F2 zone 3 on 4 Oct 2026, the operator's values since the ratchet
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
    """The controller's loop, a pass a minute: the learner ticks, the day's ramp is judged, and what is wanted
    goes through the supervisor's step limits. Home Assistant reflects each write before the next pass."""
    for k in range(n):
        ceiling = min(current["p1_target_vwc"], current["field_capacity"])
        au.tick(learn, vwc, phase, T0 + k * 60, True, 0.0, 60, ceiling=ceiling)
        au.ramp_outcome(learn, phase)
        for suffix, value, _why in ss.writes(current, au.wanted(learn, current, vwc, phase, None)):
            current[suffix] = value
    return current


def _ramp(learn, current, start, rises, t=T0 + 3600):
    """The morning's ramp from `start`, where P1 began: a 2 % shot every 21 minutes through the learner's own
    hooks, each retaining the next of `rises`. Returns the last reading."""
    vwc, ceiling = start, min(current["p1_target_vwc"], current["field_capacity"])
    for k, rise in enumerate(rises):
        au.shot(learn, "P1", 2.0, vwc, t + k * 1260)
        vwc = round(vwc + rise, 2)
        au.tick(learn, vwc, "P1", t + (k + 1) * 1260, True, 0.0, 21, ceiling=ceiling)
    return vwc


# ------------------------------------------------------------------ up: two points, after a ramp that reached
def test_zone_3s_four_days_now_move_its_target_eight_points_not_thirty_four():
    learn, current = au.fresh(), dict(Z3)
    learn["gain"] = 1.9
    for day, peak in enumerate((49.3, 57.7, 66.1, 74.5)):  # each "reached" day left a peak ~8.4 points higher
        au.new_day(learn, f"2026-09-{23 + day}", 36.0)
        learn["peak"] = peak
        before = current["p1_target_vwc"]
        _passes(learn, current, 36.0, "P0")  # lights-on: no ramp has reached anything yet today
        assert current["p1_target_vwc"] == before
        learn["outcome"] = "reached"  # the morning's ramp reached its target, as on every one of those days
        _passes(learn, current, 39.0, "P2")
        assert current["p1_target_vwc"] == round(before + au.DAY_RISE_PTS, 1)
        assert current["field_capacity"] == round(current["p1_target_vwc"] + 2.0, 1)
    assert au.p1_target(learn, 39.0, "P2") == 75.5  # what the learner wanted on day four...
    assert current["p1_target_vwc"] == 48.9  # ...and what it got: 40.9 + 4 x 2
    # While the target is held under the learned peak the re-water threshold is left where it was: its band hangs
    # off that peak, and following it was the other half of the ratchet (37.4 -> 69.6).
    assert current["p2_vwc_threshold"] == Z3["p2_vwc_threshold"]


def test_the_two_points_are_counted_from_where_the_grow_day_began_not_from_the_last_write():
    learn, current = au.fresh(), dict(Z3)
    learn.update(peak=60.0, gain=1.9)
    for day, began in (("2026-09-23", 40.9), ("2026-09-24", 42.9)):
        au.new_day(learn, day, 36.0)
        learn["outcome"] = "reached"
        _passes(learn, current, 36.0, "P2", n=200)  # a whole day of passes
        assert learn["day_ceiling"] == began and current["p1_target_vwc"] == round(began + 2.0, 1)


# ------------------------------------------------------------------ down: never held back
def test_the_p1_fail_over_drops_the_target_at_once_however_far():
    learn, current = au.fresh(), dict(p1_target_vwc=40.0, field_capacity=55.0, p2_vwc_threshold=34.0,
                                      p3_emergency_vwc_threshold=22.0, p2_shot_size=3.0)
    au.new_day(learn, "2026-09-20", 30.0)
    _passes(learn, current, 30.0, "P1", n=1)
    vwc = _ramp(learn, current, 30.0, (1.8, 1.8, 0.1, 0.1))  # stops lifting the probe at 33.8, under its 40
    _passes(learn, current, vwc, "P1", n=3)
    assert learn["outcome"] == "plateau"
    assert current["p1_target_vwc"] == 33.7  # 6.3 points down the same grow-day: a target the zone has met


# ------------------------------------------------------------------ zone 3: what the ratchet left behind
def _zone_3_stale_day():
    """4 Oct 2026: zone 3 still 'knows' the ratchet's 79.21, the operator has it at 41, and it enters P1 at 43."""
    learn, current = au.fresh(), dict(Z3_RESET)
    learn.update(peak=79.21, gain=1.909)
    au.new_day(learn, "2026-10-04", 43.5)
    _passes(learn, current, 43.5, "P0", n=1)  # lights-on: holds 41
    assert current["p1_target_vwc"] == 41.0
    _passes(learn, current, 43.0, "P1", n=1)  # P1 begins at 43, over its 41 ceiling: the target was stale
    assert (learn["p1_entry"], learn["stale_target"]) == (43.0, True)
    _passes(learn, current, 44.0, "P2")
    return learn, current


def test_zone_3_entering_p1_over_its_target_goes_two_points_over_that_not_to_its_ratchet_peak():
    learn, current = _zone_3_stale_day()
    assert au.p1_target(learn, 44.0, "P2") == 80.2  # what the learner wants...
    assert (current["p1_target_vwc"], current["field_capacity"]) == (45.0, 47.0)  # ...and what it gets


@pytest.mark.parametrize("rises, outcome, target", [
    ((2.0, 2.0, 2.0), "reached", 47.0),  # 40 -> 46: it reached its 45, so 2 more
    ((2.0, 2.0, 0.4), "short", 45.0),  # the engine ended the ramp under 45 while the probe had stopped
    ((2.0, 0.4, 0.2), "suspect", 45.0),  # it flattened 36 points under the 79.21 it 'knows'
])
def test_the_next_day_zone_3_climbs_only_if_its_ramp_reached_the_new_target(rises, outcome, target):
    learn, current = _zone_3_stale_day()
    au.new_day(learn, "2026-10-05", 41.0)
    _passes(learn, current, 41.0, "P0", n=1)
    _passes(learn, current, 40.0, "P1", n=1)  # under its 45: not stale
    vwc = _ramp(learn, current, 40.0, rises)
    _passes(learn, current, vwc, "P1", n=1)  # a plateau is judged in P1...
    _passes(learn, current, vwc, "P2")  # ...the engine's own hand-over in P2
    assert learn["outcome"] == outcome and current["p1_target_vwc"] == target


# ------------------------------------------------------------------ zone 2: the bug, and after the stopgap
def test_zone_2s_stale_29_5_goes_two_points_over_where_p1_began():
    learn, current = au.restore(SAVED_BY_3_8_0), dict(Z2)
    au.new_day(learn, "2026-10-04", 44.0)
    _passes(learn, current, 44.0, "P0", n=1)
    assert current["p1_target_vwc"] == 29.5
    _passes(learn, current, 44.0, "P1", n=1)  # P1 begins at 44, over its 29.5 ceiling
    assert (learn["p1_entry"], learn["stale_target"]) == (44.0, True)
    _passes(learn, current, 46.0, "P2")
    assert (current["p1_target_vwc"], current["field_capacity"]) == (46.0, 48.0)


def test_after_the_stopgap_zone_2_holds_48_until_its_ramp_reaches_it_then_goes_to_50():
    learn, current = au.restore(SAVED_BY_3_8_0), dict(Z2, p1_target_vwc=48.0)
    au.new_day(learn, "2026-10-05", 44.0)
    _passes(learn, current, 44.0, "P0", n=1)
    _passes(learn, current, 44.0, "P1", n=1)  # under its 48 ceiling: not stale (and 44 + 2 is under 48 anyway)
    assert learn["stale_target"] is False and current["p1_target_vwc"] == 48.0
    vwc = _ramp(learn, current, 44.0, (2.1, 2.1))  # 44 -> 48.2: it reached its 48 still taking water
    _passes(learn, current, vwc, "P2")
    assert learn["outcome"] == "reached"
    assert (current["p1_target_vwc"], current["field_capacity"]) == (50.0, 52.0)


def test_a_stale_target_never_goes_above_the_learned_peak_plus_one():
    learn, current = au.restore(SAVED_BY_3_8_0), dict(Z2)
    learn["peak_adj"] = 2.0  # what the old hourly judge may have asked to hold it above
    au.new_day(learn, "2026-10-04", 65.0)
    _passes(learn, current, 65.0, "P0", n=1)
    _passes(learn, current, 65.0, "P1", n=1)  # 65 + 2 would be over the learned 65.54 + 1
    _passes(learn, current, 65.0, "P2")
    assert current["p1_target_vwc"] == round(65.54 + au.PROBE_STEP_PTS, 1)


def test_only_the_first_look_at_p1_counts_as_p1_entry():
    learn = au.restore(SAVED_BY_3_8_0)
    au.new_day(learn, "2026-10-04", 44.0)
    au.tick(learn, 30.0, "P1", T0, True, 0.0, 60, ceiling=40.0)
    au.tick(learn, 45.0, "P1", T0 + 1260, True, 0.0, 60, ceiling=40.0)  # the ramp has reached it since
    assert (learn["p1_entry"], learn["stale_target"]) == (30.0, False)
    au.new_day(learn, "2026-10-05", 44.0)
    assert learn["p1_entry"] is None and learn["stale_target"] is None and learn["day_ceiling"] is None


# ------------------------------------------------------------------ an old install
def test_a_learner_block_saved_before_the_guard_loads_and_the_guard_starts_from_the_first_pass():
    learn = au.restore(json.loads(json.dumps(SAVED_BY_3_8_0)))
    assert learn["day_ceiling"] is None and learn["p1_entry"] is None and learn["stale_target"] is None
    assert {k: learn[k] for k in SAVED_BY_3_8_0} == SAVED_BY_3_8_0  # everything it learned, unchanged
    current = dict(Z2, p1_target_vwc=48.0)  # the release lands at night on zone 2, after the stopgap
    au.tick(learn, 52.0, "P3", T0, False, 0.0, 60, ceiling=48.0)
    assert learn["day_ceiling"] == 48.0
    # 3.8.0 had judged that grow-day's ramp reached: 2 points, counted from where it is now
    assert au.wanted(learn, current, 52.0, "P3", None)["p1_target_vwc"] == 50.0
    assert au.restore(json.loads(json.dumps(learn))) == learn  # and the new keys survive a restart


def test_junk_in_the_new_keys_is_handled_like_any_other_junk():
    for junk in ({"day_ceiling": "high"}, {"p1_entry": "low"}):
        assert au.restore(dict(SAVED_BY_3_8_0, **junk)) == au.fresh()  # a corrupt file never crashes the engine
    # stale only as saved, and only beside the reading P1 began at: junk never lifts a target
    assert au.restore(dict(SAVED_BY_3_8_0, p1_entry=44, stale_target=1))["stale_target"] is False
    assert au.restore(dict(SAVED_BY_3_8_0, stale_target=True))["stale_target"] is None
    assert au.restore(dict(SAVED_BY_3_8_0, p1_entry=44, stale_target=True))["stale_target"] is True


# ------------------------------------------------------------------ in the controller
@pytest.fixture
def ramp_clock(monkeypatch):
    asr._Clock.current = datetime(2026, 9, 22, 10, 0)
    monkeypatch.setattr(asr.controller, "datetime", asr._Clock)


def _morning(c, fake, room, now, start):
    """Lights-on in P0 and P1 begun at `start`, then the engine's ramp: a 2 % shot through the controller's own
    shot hook every 21 minutes, each retaining 2.5 points, until the reading reaches the P1 ceiling Home Assistant
    holds; then P2. A pass a minute, and Home Assistant reflects every write before the next."""
    def number(suffix):
        return float(fake.states[f"number.crop_steering_zone_1_{suffix}"][0])

    st, vwc = room.state[1], start
    for phase in ("P0", "P1"):
        st["phase"], asr._Clock.current = phase, now
        asr._tick(c, fake, room, vwc, now)
        now += timedelta(minutes=1)
    while vwc < min(number("p1_target_vwc"), number("field_capacity")):
        c._advance_shot_counters(room, 1, 2.0)
        vwc, now = vwc + 2.5, now + timedelta(minutes=21)
        asr._Clock.current = now
        asr._tick(c, fake, room, vwc, now)
    st["phase"] = "P2"
    for _ in range(30):
        now += timedelta(minutes=1)
        asr._Clock.current = now
        asr._tick(c, fake, room, vwc, now)
    return {s: number(s) for s in Z3}


def test_zone_3s_four_days_through_the_controller(ramp_clock):
    numbers = {f"number.crop_steering_zone_1_{s}": (str(v), {}) for s, v in Z3.items()}
    c, fake, room = asr._rig(numbers=numbers)  # no Jev: the learner owns every target, as it did on 22 Sep
    room.state[1]["learn"]["gain"] = 1.9
    for day, peak in enumerate((49.3, 57.7, 66.1, 74.5)):
        room.state[1]["learn"]["peak"] = peak  # what the day before's "reached" ramp left it at
        number = _morning(c, fake, room, datetime(2026, 9, 23 + day, 10, 0), start=36.0)
        assert room.state[1]["learn"]["outcome"] == "reached"
        assert number["p1_target_vwc"] == round(Z3["p1_target_vwc"] + 2 * (day + 1), 1)
    # 40.9 + 4 x 2, counted from the engine's own P1 ceiling, which the controller reads every pass (was 75.5)
    assert number["p1_target_vwc"] == 48.9 and number["field_capacity"] == 50.9
    assert number["p2_vwc_threshold"] == 37.4  # was 68.8
