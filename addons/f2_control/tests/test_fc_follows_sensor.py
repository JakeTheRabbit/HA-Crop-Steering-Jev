"""Field capacity follows the sensor.

6 Oct 2026, the owner: "the FC needs to increase as the fucking sensor does". F2 zone 2 settled at 63-68 after every
shot on 5 Oct against a field capacity of 54, because field capacity was only ever the P1 target + 2, and the target
may rise 2 points a grow-day (the ratchet guard). Its probe was also set aside by Jev that evening, and a zone without
a trusted probe skipped the learner altogether. Now each zone's highest reading 20 minutes or more after a shot is
tracked from the probe every loop, and field capacity is that, never under the target + 2: in the learner's pass and,
for a zone watering without its probe, on its own.
"""
from datetime import date, timedelta

import auto_setpoints as a
from test_plan_hold_never_stops_rescues import Clock, probe, rig  # noqa: F401 (rig: a pytest fixture)

DAY = "2026-10-05T10:00:00+13:00"


def test_only_readings_past_the_spike_count():
    learn = a.fresh()
    a.track_full(learn, DAY, 72.0, minutes_since_shot=5)  # the free-water spike right after a shot
    assert a.measured_full(learn) is None
    a.track_full(learn, DAY, 67.5, minutes_since_shot=20)
    a.track_full(learn, DAY, 64.0, minutes_since_shot=60)  # drying: the day's highest settled reading stays
    assert a.measured_full(learn) == 67.5


def test_it_goes_up_the_same_day_and_comes_down_after_two_lower_days():
    learn = a.fresh()
    a.track_full(learn, DAY, 67.5, minutes_since_shot=30)
    a.track_full(learn, "2026-10-06T10:00:00+13:00", 60.0, minutes_since_shot=30)
    assert a.measured_full(learn) == 67.5  # yesterday's still holds it up
    a.track_full(learn, "2026-10-06T10:00:00+13:00", 69.0, minutes_since_shot=30)
    assert a.measured_full(learn) == 69.0  # the sensor read higher: up at once
    a.track_full(learn, "2026-10-07T10:00:00+13:00", 58.0, minutes_since_shot=30)
    a.track_full(learn, "2026-10-08T10:00:00+13:00", 58.0, minutes_since_shot=30)
    assert a.measured_full(learn) == 58.0


def test_field_capacity_is_the_sensor_never_under_the_target_plus_two():
    assert a.field_capacity(52.0, 67.98) == 68.0  # zone 2, 5 Oct
    assert a.field_capacity(52.0, 40.0) == 54.0
    assert a.field_capacity(52.0, None) == 54.0
    assert a.field_capacity(30.0, 20.0) == 40.0 and a.field_capacity(88.0, 99.0) == 90.0  # the number's bounds


def test_the_learner_wants_the_sensor_level_while_its_target_is_held():
    learn = a.fresh()
    learn.update(peak=65.54, outcome="reached", day_ceiling=50.0)
    current = {"p1_target_vwc": 52.0, "field_capacity": 54.0, "p2_vwc_threshold": 27.5,
               "p3_emergency_vwc_threshold": 20.0, "p2_shot_size": 6.0}
    want = a.wanted(learn, current, 40.0, "P2", None, others=("p2_shot_size", "p2_vwc_threshold"), full=67.98)
    assert want["p1_target_vwc"] == 52.0 and want["field_capacity"] == 68.0  # the target keeps its brake


def test_a_learner_block_saved_before_this_loads_and_changes_nothing():
    old = {k: v for k, v in a.fresh().items() if not k.startswith("full_")}
    old.update(peak=65.54, outcome="reached", day_ceiling=50.0)
    learn = a.restore(old)
    assert learn["full_day"] is None and learn["full_prev"] is None and a.measured_full(learn) is None
    assert a.restore({**old, "full_date": 7, "full_day": "68"})["full_date"] is None  # junk never lifts anything


def _zone(fake, room, now, zone=1):
    for key, value in {"p1_target_vwc": 52, "field_capacity": 54, "p2_vwc_threshold": 27.5,
                       "p3_emergency_vwc_threshold": 20}.items():  # zone 2's ladder on 5 Oct
        fake.set_state(f"number.crop_steering_zone_{zone}_{key}", str(value))
    room.state[zone].update(phase="P2", last_shot=now - timedelta(minutes=30), last_daily_reset=date(2026, 9, 23))


LADDER = ("field_capacity", "p1_target_vwc", "p2_vwc_threshold", "p3_emergency_vwc_threshold")


def _writes(fake, zone=1):
    """{suffix: [values written]} for the zone's four VWC setpoints."""
    out = {s: [] for s in LADDER}
    for dom, svc, d in fake.calls:
        for s in LADDER:
            if (dom, svc) == ("number", "set_value") and d.get("entity_id") == f"number.crop_steering_zone_{zone}_{s}":
                out[s].append(d["value"])
    return out


def _fc_writes(fake, zone=1):
    return _writes(fake, zone)["field_capacity"]


def test_the_loop_raises_field_capacity_and_moves_everything_under_it(rig):  # noqa: F811
    c, fake, room = rig
    fake.set_state("switch.crop_steering_auto_setpoints", "on")
    now = Clock.instant = Clock(2026, 9, 23, 14, 0)
    for zone in (1, 2):
        _zone(fake, room, now, zone)
        probe(fake, zone, 67.5)  # 30 minutes after its last shot: settled, and 13.5 over field capacity
    c._loop_room(room, now)
    # the P1 target, the P2 re-water threshold and the rescue (P3 emergency) floor move the same 13.5 points
    assert _writes(fake, 1) == {"field_capacity": [67.5], "p1_target_vwc": [65.5], "p2_vwc_threshold": [41.0],
                                "p3_emergency_vwc_threshold": [33.5]}


def test_before_anything_is_tracked_the_last_plateau_seeds_it(rig):  # noqa: F811
    c, fake, room = rig
    fake.set_state("switch.crop_steering_auto_setpoints", "on")
    now = Clock.instant = Clock(2026, 9, 23, 14, 0)
    for zone in (1, 2):
        _zone(fake, room, now, zone)
        room.state[zone]["last_shot"] = now - timedelta(minutes=5)  # mid-spike: nothing settled to track yet
        probe(fake, zone, 45.0)
    room.state[1]["plateau_hist"] = [{"date": "2026-09-22", "value": 67.98, "how": "engine", "shots": 2}]
    c._loop_room(room, now)
    # zone 2 on 6 Oct: 54 -> 68, so 52 -> 66, 27.5 -> 41.5, 20 -> 34
    assert _writes(fake, 1) == {"field_capacity": [68.0], "p1_target_vwc": [66.0], "p2_vwc_threshold": [41.5],
                                "p3_emergency_vwc_threshold": [34.0]}


def test_installed_at_night_the_plateau_still_stands_in_for_yesterday(rig):  # noqa: F811
    # The update lands at night: the first readings tracked are the night's (zone 2, 39.6 at 04:00 on 6 Oct). They
    # must not hide yesterday's plateau until a whole grow-day has been tracked.
    c, fake, room = rig
    fake.set_state("switch.crop_steering_auto_setpoints", "on")
    now = Clock.instant = Clock(2026, 9, 23, 14, 0)
    for zone in (1, 2):
        _zone(fake, room, now, zone)
        room.state[zone]["last_shot"] = now - timedelta(hours=7)  # settled: tracked tonight
        probe(fake, zone, 39.6)
    room.state[1]["plateau_hist"] = [{"date": "2026-09-22", "value": 67.98, "how": "engine", "shots": 2}]
    c._loop_room(room, now)
    assert room.state[1]["learn"]["full_day"] == 39.6
    assert _writes(fake, 1)["field_capacity"] == [68.0]


def test_a_zone_watering_without_its_probe_still_follows_the_sensor(rig):  # noqa: F811
    c, fake, room = rig
    fake.set_state("switch.crop_steering_auto_setpoints", "on")
    now = Clock.instant = Clock(2026, 9, 23, 14, 0)
    for zone in (1, 2):
        _zone(fake, room, now, zone)
        probe(fake, zone, 45.0)
    room.state[1]["learn"]["full_prev"] = 67.98  # what the probe settled at before it was set aside
    fake.set_state("sensor.crop_steering_vwc_zone_1", "unavailable")  # no reading: the blind path
    c._loop_room(room, now)
    assert _writes(fake, 1) == {"field_capacity": [68.0], "p1_target_vwc": [66.0], "p2_vwc_threshold": [41.5],
                                "p3_emergency_vwc_threshold": [34.0]}


def test_the_learners_brake_moves_with_the_scale(rig):  # noqa: F811
    c, fake, room = rig
    fake.set_state("switch.crop_steering_auto_setpoints", "on")
    now = Clock.instant = Clock(2026, 9, 23, 14, 0)
    for zone in (1, 2):
        _zone(fake, room, now, zone)
        probe(fake, zone, 45.0)
    learn = room.state[1]["learn"]
    learn.update(full_prev=67.98, day_ceiling=50.0, p1_entry=48.1, peak=65.54)
    fake.set_state("sensor.crop_steering_vwc_zone_1", "unavailable")
    c._loop_room(room, now)
    # never above the peak the learner has seen, so it does not pull the target back the next pass
    assert _writes(fake, 1)["p1_target_vwc"] == [65.5]
    assert learn["day_ceiling"] == 63.5 and learn["p1_entry"] == 61.6  # the 13.5 points the target moved


def test_a_drop_moves_everything_down(rig):  # noqa: F811
    c, fake, room = rig
    fake.set_state("switch.crop_steering_auto_setpoints", "on")
    now = Clock.instant = Clock(2026, 9, 23, 14, 0)
    for zone in (1, 2):
        _zone(fake, room, now, zone)
        fake.set_state(f"number.crop_steering_zone_{zone}_field_capacity", "70")
        probe(fake, zone, 45.0)
    room.state[1]["learn"]["full_prev"] = 65.0  # the probe now settles 5 points lower (reseated)
    fake.set_state("sensor.crop_steering_vwc_zone_1", "unavailable")
    c._loop_room(room, now)
    assert _writes(fake, 1) == {"field_capacity": [65.0], "p1_target_vwc": [47.0], "p2_vwc_threshold": [22.5],
                                "p3_emergency_vwc_threshold": [15.0]}


def test_a_small_move_or_one_from_the_target_floor_moves_field_capacity_only(rig):  # noqa: F811
    c, fake, room = rig
    fake.set_state("switch.crop_steering_auto_setpoints", "on")
    now = Clock.instant = Clock(2026, 9, 23, 14, 0)
    for zone in (1, 2):
        _zone(fake, room, now, zone)
        probe(fake, zone, 45.0)
    fake.set_state("sensor.crop_steering_vwc_zone_1", "unavailable")
    fake.set_state("sensor.crop_steering_vwc_zone_2", "unavailable")
    room.state[1]["learn"]["full_prev"] = 54.6  # 0.6 over: field capacity only
    fake.set_state("number.crop_steering_zone_2_field_capacity", "56")
    room.state[2]["learn"]["full_prev"] = 45.0  # zone 1 on 6 Oct: the sensor under the target + 2, nothing to follow
    c._loop_room(room, now)
    assert _writes(fake, 1) == {"field_capacity": [54.6], "p1_target_vwc": [], "p2_vwc_threshold": [],
                                "p3_emergency_vwc_threshold": []}
    assert _writes(fake, 2) == {s: [] for s in LADDER}  # field capacity is not pulled down to the target + 2


def test_a_move_that_would_invert_the_ladder_moves_field_capacity_only(rig):  # noqa: F811
    c, fake, room = rig
    fake.set_state("switch.crop_steering_auto_setpoints", "on")
    now = Clock.instant = Clock(2026, 9, 23, 14, 0)
    for zone in (1, 2):
        _zone(fake, room, now, zone)
        probe(fake, zone, 45.0)
    fake.set_state("number.crop_steering_zone_1_p2_vwc_threshold", "68")  # already above the target
    room.state[1]["learn"]["full_prev"] = 67.98
    fake.set_state("sensor.crop_steering_vwc_zone_1", "unavailable")
    c._loop_room(room, now)
    assert _writes(fake, 1) == {"field_capacity": [68.0], "p1_target_vwc": [], "p2_vwc_threshold": [],
                                "p3_emergency_vwc_threshold": []}


def test_nothing_is_written_with_auto_setpoints_off(rig):  # noqa: F811
    c, fake, room = rig
    now = Clock.instant = Clock(2026, 9, 23, 14, 0)
    for zone in (1, 2):
        _zone(fake, room, now, zone)
        probe(fake, zone, 67.5)
    room.state[1]["learn"]["full_prev"] = 67.98
    fake.set_state("sensor.crop_steering_vwc_zone_1", "unavailable")
    c._loop_room(room, now)
    assert _fc_writes(fake, 1) == [] and _fc_writes(fake, 2) == []
