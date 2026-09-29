"""The controller app hands its pushes to the integration (docs/NOTIFICATIONS.md), across the real seam: the REAL
controller reads the REAL sensor.crop_steering_notify_config, and what it asks crop_steering.notify for is taken
by the REAL service, whose phones get it with its tag and its priority. The two layers meet only through that
sensor and that call, and each has tests against its own idea of the other."""

from test_notify import STAFF_PHONE, TABLET, URGENT, _call, _set_up, _staff
from test_setup_entry import _install

DOMAIN = "crop_steering"


def _asked(fake):
    """What the controller asked crop_steering.notify for, as it goes over REST."""
    return [data for domain, service, data in fake.calls if (domain, service) == (DOMAIN, "notify")]


async def test_the_controllers_pushes_reach_the_phones_through_the_integration(
    hass, hass_admin_user, controller_for
):
    await _install(hass)
    staff = await _staff(hass)
    received, doc = await _set_up(hass, hass_admin_user, staff)
    phone, tablet = doc["config"]["recipients"]
    await _call(  # the staff phone ticks phase changes too, and it is two hours without watering
        hass,
        hass_admin_user,
        "notify_save",
        expected_revision=1,
        recipients=[{**phone, "kinds": ["stock", "phases"]}, tablet],
        idle_hours=2,
    )
    await hass.async_block_till_done()
    c, fake, _clock = controller_for({"notify_service": "notify/mobile_app_old_phone"})
    assert c._notify_config() == (2, 2.0)

    room = c.rooms[0]
    assert c._alert(
        "hw_default_z1",
        "CS-301",
        "CRITICAL hardware fault, watering stopped",
        "The valve did not close.",
        room=room,
        zone=1,
    )
    c._notify_event("phase", room, 1, "P1", "P2", "P1 recovered 42>=40 EC ok 2.5")
    c._send_events()
    alert, phase = _asked(fake)
    assert [call for call in fake.calls if call[0] == "notify"] == []  # nothing to the option
    assert (alert["code"], alert["urgent"], alert["room"], alert["zone"]) == (
        "CS-301",
        True,
        "",
        1,
    )

    answer = await hass.services.async_call(
        DOMAIN, "notify", alert, blocking=True, return_response=True
    )
    assert answer == {"sent_to": [TABLET], "error": None}
    assert received[TABLET][-1]["message"].startswith("The valve did not close.")
    assert received[TABLET][-1]["data"] == {"tag": "hw_default_z1", **URGENT}

    answer = await hass.services.async_call(
        DOMAIN, "notify", phase, blocking=True, return_response=True
    )
    assert answer == {"sent_to": [STAFF_PHONE], "error": None}
    assert received[STAFF_PHONE][-1] == {
        "title": phase["title"],
        "message": "P1 recovered 42>=40 EC ok 2.5",
        "data": {"tag": "phase_default_z1"},
    }
    assert phase["title"].endswith(": P1 → P2")


async def test_with_nobody_set_up_the_controller_pushes_to_its_option_as_before(
    hass, controller_for
):
    await _install(hass)
    c, fake, _clock = controller_for({"notify_service": "notify/mobile_app_old_phone"})
    assert c._notify_config() == (0, 3.0)  # the real sensor: nobody set up
    assert c._alert("k", "CS-102", "moisture sensor not reporting", "m", room=c.rooms[0], zone=1)
    c._notify_event("phase", c.rooms[0], 1, "P1", "P2", "why")
    c._send_events()
    assert _asked(fake) == []
    assert [(dom, svc) for dom, svc, _d in fake.calls if dom == "notify"] == [
        ("notify", "mobile_app_old_phone")
    ]
