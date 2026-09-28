"""Both layers of dosing, across the seam, in a real Home Assistant: a dose asked for through the real
dosing_request service is read by the REAL controller app from sensor.crop_steering_dosing_config (as
REST would send it) and dosed, what the controller publishes is what the integration reads to know
the request was handled, and the stock draw it sends is one the real stock_draw service takes
(docs/DOSING.md)."""

import json

from test_dosing import DEVICES, PUMP, _call, _devices
from test_setup_entry import _install

DOMAIN = "crop_steering"
STATUS = "sensor.crop_steering_dosing"
DOSING = PUMP["dosing_entity"]


async def test_the_controller_doses_what_the_integration_was_asked_for(
    hass, hass_admin_user, controller_for, monkeypatch
):
    import controller

    await _install(hass)
    _devices(hass)
    await _call(
        hass,
        hass_admin_user,
        "stock_save",
        expected_revision=0,
        tanks=[{"name": "Balance", "capacity_l": 10}],
    )
    await _call(
        hass,
        hass_admin_user,
        "dosing_save",
        expected_revision=0,
        pumps=[{**PUMP, "stock_tank": "balance"}],
        batch={},
    )
    asked = (
        await _call(
            hass, hass_admin_user, "dosing_request", action="dose", pump="balance", ml=22
        )
    )["request"]
    await hass.async_block_till_done()

    c, fake, clock = controller_for()
    runner = c.dosing
    runner._mono = clock.monotonic
    pressed = []

    def call(domain, service, **data):  # the pump's firmware, as the other tests script it
        fake.calls.append((domain, service, data))
        if service == "set_value":
            fake.set_state(data["entity_id"], f"{float(data['value']):g}")
        if (domain, service) == ("button", "press"):
            fake.set_state(DOSING, "on")
            pressed.append(clock.seconds)
        return True

    def sleep(seconds):  # 22 mL at 11.06 mL/s: about 2 s, then the firmware stops the motor
        clock.sleep(seconds)
        if pressed and clock.seconds - pressed[0] >= 2:
            fake.set_state(DOSING, "off")

    monkeypatch.setattr(controller, "ha_call", call)
    runner._sleep = sleep
    assert runner.recover()
    runner.poll()

    sent = list(fake.calls)
    assert ("number", "set_value", {"entity_id": PUMP["volume_entity"], "value": 22}) in sent
    assert ("button", "press", {"entity_id": PUMP["start_entity"]}) in sent
    assert (  # and the volume put back
        "number",
        "set_value",
        {"entity_id": PUMP["volume_entity"], "value": float(DEVICES[PUMP["volume_entity"]])},
    ) in sent
    [draw] = [data for domain, service, data in sent if (domain, service) == (DOMAIN, "stock_draw")]
    assert draw["draws"] == {"balance": 22} and draw["source"] == "dose"

    # What the controller sent, as REST carries it and without asking for the answer, is taken
    # by the real service; sent again (it never heard back), it counts once.
    for _attempt in range(2):
        await hass.services.async_call(
            DOMAIN, "stock_draw", json.loads(json.dumps(draw)), blocking=True
        )
    stock = await _call(hass, hass_admin_user, "stock_get")
    assert stock["tanks"][0]["level_l"] == 9.978
    assert stock["history"][0]["key"] == draw["key"]

    state, attributes = fake.sets[STATUS]
    attributes = json.loads(json.dumps(attributes))  # what REST carries
    assert (attributes["handled"], attributes["handled_result"]) == (asked["id"], "finished")
    assert attributes["pumps"]["balance"]["last"]["ml"] == 22

    # The integration reads the controller's status: the request is handled, the next one taken.
    hass.states.async_set(STATUS, state, attributes)
    again = await _call(
        hass, hass_admin_user, "dosing_request", action="dose", pump="balance", ml=5
    )
    assert again["error"] is None
