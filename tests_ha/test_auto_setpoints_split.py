"""Auto Setpoints under Jev's Setpoints judge, in a real Home Assistant, with the real controller.

4 Oct 2026, F2 zone 2: controller 3.8.0's learner wrote nothing at all once the judge ran, so nothing
wrote the P1 target. It sat at 29.5 from 26 Sep while the probe, moved up 15-20 points by a sump
flood, read 42-44 % at lights-on, and P1 never ramped. Each lever now has one writer: the judge its
P2 shot size and re-water threshold, the learner the P1 target and field capacity (and the rescue
floor only to keep it 3 points under the threshold). Upward, the learner moves a target at most 2
points a grow-day, and only once that day's ramp reached it (zone 3's ratchet, 22-26 Sep). A target
the zone entered P1 already above goes, the same day, to 2 points over the reading P1 began at.

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


class _Clock(datetime):
    """The controller's wall clock, pinned to 4 Oct: its shot hook stamps each shot with it."""

    current = None

    @classmethod
    def now(cls, tz=None):
        return cls.current if tz is None else cls.current.astimezone(tz)


async def _learned_tent(hass, controller_for, monkeypatch):
    import controller
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
    monkeypatch.setattr(controller, "datetime", _Clock)
    return c, fake, room, seed


async def _day(hass, c, fake, room, entry_vwc, ramp=()):
    """4 Oct through the controller's own learner pass, a minute apart: lights-on in P0, P1 begun at
    `entry_vwc` with a 2 % ramp shot through the controller's own shot hook for each of `ramp` (what
    each retained, read 21 minutes on), then P2. What it writes reaches the real entity before the
    next pass reads it. Returns (phase, entity, value) for every write."""
    now, sent, vwc = datetime(2026, 10, 4, 10, 0), [], entry_vwc

    async def one_pass(phase, reading):
        room.state[1]["phase"], _Clock.current = phase, now
        fake.calls.clear()
        snap = SimpleNamespace(vwc=reading, ec=5.36, dryback_rate=0.0)
        c._auto_tick(room, 1, snap, c._params(room, 1), True, now)
        for domain, service, data in fake.calls:
            if (domain, service) == ("number", "set_value"):
                sent.append((phase, data["entity_id"], data["value"]))
                await hass.services.async_call(domain, service, data, blocking=True)
        _see(hass, fake)

    for phase, reading in (("P0", 44.0), ("P1", entry_vwc)):
        await one_pass(phase, reading)
        now += timedelta(minutes=1)
    for rise in ramp:
        c._advance_shot_counters(room, 1, 2.0)
        vwc, now = round(vwc + rise, 2), now + timedelta(minutes=21)
        await one_pass("P1", vwc)
    for _ in range(8):
        now += timedelta(minutes=1)
        await one_pass("P2", 52.0)
    return sent


def _value(hass, suffix):
    return float(hass.states.get(NUMBER + suffix).state)


def _written(sent, suffix):
    return [value for _phase, entity, value in sent if entity == NUMBER + suffix]


async def test_a_stale_p1_target_goes_two_over_where_p1_began_and_the_judges_levers_are_left_alone(
    hass, controller_for, monkeypatch
):
    c, fake, room, seed = await _learned_tent(hass, controller_for, monkeypatch)
    learn = room.state[1]["learn"]
    # All that 3.8.0 had learned, and none of the guard's keys.
    assert learn["peak"] == 65.54 and learn["outcome"] == "reached"
    assert learn["day_ceiling"] is None and learn["p1_entry"] is None

    sent = await _day(hass, c, fake, room, entry_vwc=44.0)

    # Lights-on: 29.5 holds, nothing has reached anything yet. P1 began at 44 %, over its 29.5 %
    # ceiling: stale, so the same day it goes to 44 + 2, in the supervisor's 6-point steps. Not to
    # the learned 65.54 + 1: a stale learned peak (zone 3's ratchet) must not be reached in a day.
    assert _written(sent, "p1_target_vwc") == [35.5, 41.5, 46.0]
    assert {entity for _phase, entity, _value in sent} == {
        NUMBER + "p1_target_vwc",
        NUMBER + "field_capacity",
    }
    assert _value(hass, "p1_target_vwc") == 46.0
    assert _value(hass, "field_capacity") == 48.0
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
    learned = saved["default"]["1"]["learn"]
    assert learned["peak"] == 65.54 and learned["day_ceiling"] == 29.5
    assert learned["p1_entry"] == 44.0 and learned["stale_target"] is True
    assert saved["default"]["_setup"] == seed["controller_saved"]["default"]["_setup"]


async def test_after_the_stopgap_the_target_holds_until_a_ramp_reaches_it_then_rises_two(
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

    # P1 begins at 44, under its 48 ceiling, and the ramp reaches 48.2 still taking water.
    sent = await _day(hass, c, fake, room, entry_vwc=44.0, ramp=(2.1, 2.1))

    assert room.state[1]["learn"]["outcome"] == "reached"
    # Nothing moved in P0 or P1: the 2 points came after the ramp had reached its 48.
    assert sent == [
        ("P2", NUMBER + "p1_target_vwc", 50.0),
        ("P2", NUMBER + "field_capacity", 52.0),
    ]
    assert _value(hass, "p1_target_vwc") == 50.0
    assert _value(hass, "field_capacity") == 52.0
