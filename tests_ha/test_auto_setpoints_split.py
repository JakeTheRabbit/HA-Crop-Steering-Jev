"""Auto Setpoints under Jev's Setpoints judge, in a real Home Assistant, with the real controller.

4 Oct 2026, F2 zone 2: controller 3.8.0's learner wrote nothing at all once the judge ran, so nothing
wrote the P1 target. It sat at 29.5 from 26 Sep while the probe, moved up 15-20 points by a sump
flood, read 42-44 % at lights-on, and P1 never ramped. Each lever now has one writer: the judge its
P2 shot size and re-water threshold, the learner the P1 target and field capacity (and the rescue
floor only to keep it 3 points under the threshold). Upward, the learner moves a target at most 2
points a grow-day (zone 3's ratchet, 22-26 Sep), unless the zone entered P1 already above it.

It starts from a seeded old install (tests_ha/fixtures/entry_3_8_learned_tent.json): the state file
controller 3.8.0 wrote, whose learner block has none of the guard's keys. Every number the
controller writes over REST is replayed into the real entity, so it must be one Home Assistant
accepts, under the id the integration registered.
"""

import functools
import json
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

from test_recreated_room_controller import _see
from test_upgrade_in_place import _upgrade

KILL = "switch.crop_steering_engine_enabled"
# Jev on, so its Setpoints judge runs. Nothing here asks Jev anything.
JEV = {"enable_flag": KILL, "cf_account_id": "acc", "cf_api_token": "tok"}
NUMBER = "number.crop_steering_zone_1_"
SENSOR = "sensor.crop_steering_zone_1_auto_setpoints"


async def _learned_tent(hass, controller_for, monkeypatch):
    import jev_bridge
    from jev.client import Asker

    # Jev's asker without its background worker: nothing in this test asks Jev anything, and a
    # worker thread would outlive the test (Home Assistant's test harness fails on that).
    monkeypatch.setattr(jev_bridge, "Asker", functools.partial(Asker, threaded=False))
    _entry, seed = await _upgrade(hass, "entry_3_8_learned_tent.json")
    assert hass.states.get("switch.crop_steering_auto_setpoints").state == "on"
    c, fake, _clock = controller_for(JEV, saved_state=seed["controller_saved"])
    assert jev_bridge.owns_setpoints(c)
    room = c.rooms[0]
    assert room._setup_pending is None  # 3.8.0's state file is carried straight on
    c._refresh_lights(room)  # the room's 10:00 to 22:00, as every loop reads it
    return c, fake, room, seed


async def _day(hass, c, fake, room, entry_vwc):
    """4 Oct through the controller's own learner pass, a minute apart: lights-on in P0, P1 entered
    at `entry_vwc`, then P2. What it writes reaches the real entity before the next pass reads it.
    """
    now, sent = datetime(2026, 10, 4, 10, 0), []
    for phase, vwc, passes in (("P0", 44.0, 1), ("P1", entry_vwc, 1), ("P2", 52.0, 8)):
        for _ in range(passes):
            room.state[1]["phase"] = phase
            fake.calls.clear()
            snap = SimpleNamespace(vwc=vwc, ec=5.36, dryback_rate=0.0)
            c._auto_tick(room, 1, snap, c._params(room, 1), True, now)
            for domain, service, data in fake.calls:
                if (domain, service) == ("number", "set_value"):
                    sent.append((data["entity_id"], data["value"]))
                    await hass.services.async_call(domain, service, data, blocking=True)
            _see(hass, fake)
            now += timedelta(minutes=1)
    return sent


def _value(hass, suffix):
    return float(hass.states.get(NUMBER + suffix).state)


async def test_a_stale_p1_target_is_raised_to_the_learned_peak_and_the_judges_levers_are_left_alone(
    hass, controller_for, monkeypatch
):
    c, fake, room, seed = await _learned_tent(hass, controller_for, monkeypatch)
    learn = room.state[1]["learn"]
    # All that 3.8.0 had learned, and none of the guard's keys.
    assert learn["peak"] == 65.54 and learn["outcome"] == "reached"
    assert learn["day_ceiling"] is None and learn["stale_target"] is None

    sent = await _day(hass, c, fake, room, entry_vwc=44.0)

    # Lights-on: 2 points. P1 entered at 44 %, over its 31.5 % ceiling: stale, so the same day it
    # goes to the learned peak + 1, in the supervisor's 6-point steps.
    assert [v for e, v in sent if e == NUMBER + "p1_target_vwc"] == [
        31.5, 37.5, 43.5, 49.5, 55.5, 61.5, 66.5,
    ]  # fmt: skip
    written = {e for e, _v in sent}
    assert written == {NUMBER + "p1_target_vwc", NUMBER + "field_capacity"}
    assert _value(hass, "p1_target_vwc") == 66.5
    assert _value(hass, "field_capacity") == 68.5
    # The judge's levers, and the rescue floor that already sits 3 under the judge's threshold.
    assert _value(hass, "p2_vwc_threshold") == 27.5
    assert _value(hass, "p2_shot_size") == 6.0
    assert _value(hass, "p3_emergency_vwc_threshold") == 20.0

    judge = "Jev's Setpoints judge, one notch a night"
    owners = fake.sets[SENSOR][1]["managed_by"]
    assert owners[NUMBER + "p1_target_vwc"] == "Auto setpoints learner"
    assert {owners[NUMBER + s] for s in ("p2_vwc_threshold", "p2_shot_size")} == {judge}

    c._save_state()
    saved = json.loads(open(os.environ["F2_STATE_PATH"], encoding="utf-8").read())
    assert saved["default"]["1"]["learn"]["peak"] == 65.54
    assert saved["default"]["1"]["learn"]["day_ceiling"] == 29.5
    assert saved["default"]["1"]["learn"]["stale_target"] is True
    assert saved["default"]["_setup"] == seed["controller_saved"]["default"]["_setup"]


async def test_a_target_the_ramp_can_reach_rises_two_points_a_grow_day(
    hass, controller_for, monkeypatch
):
    c, fake, room, _seed = await _learned_tent(hass, controller_for, monkeypatch)
    # The operator's stopgap, 08:26 on 4 Oct.
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": NUMBER + "p1_target_vwc", "value": 48},
        blocking=True,
    )
    _see(hass, fake)

    # P1 entered under its 50 % ceiling: the ramp runs, so the target is not stale.
    sent = await _day(hass, c, fake, room, entry_vwc=40.5)

    assert sent == [(NUMBER + "p1_target_vwc", 50.0), (NUMBER + "field_capacity", 52.0)]
    assert _value(hass, "p1_target_vwc") == 50.0
    assert _value(hass, "field_capacity") == 52.0
