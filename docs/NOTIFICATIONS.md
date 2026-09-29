# Notifications: who gets which alerts

Every alert still becomes a Home Assistant notification card, as before. What this adds is **who gets a
phone push for what**: each phone has a row of checkboxes (stock tanks, emergencies, sensors and drift,
phase changes, hardware lockouts, watering stopped, and so on), and a push goes only to the phones whose
row ticks that kind of alert. It also adds the events a person can now subscribe to that were never
alerts: phase changes, Jev's setting changes, and "no watering for a while".

This document is the contract between the three layers. Nothing in it names a site's phones or people.

## Layers

| Layer | Owns | Never does |
|---|---|---|
| Integration | The site's notification setup (one store for the whole site, not per room); the list of phones; the services to read, save, test and send; routing each push to the phones that want it | Push anything nobody ticked (except the fallback below) |
| Controller app | Raising alerts (unchanged) and the new events; handing each push to `crop_steering.notify`; the fallback to its old `notify_service` option | Decide who gets what |
| Dashboard | Settings & help › Notifications: the checkbox grid, each phone's rooms, a test push per phone, the watering-stopped threshold | Call a notify service itself |

## The kinds of alert (the checkboxes)

A code can be in more than one kind (CS-301 is an emergency and a hardware lockout); a phone that ticks
either gets it once.

| Id | Checkbox | What it covers |
|---|---|---|
| `emergency` | Emergencies | Every alert whose severity is critical in docs/error-codes.json (CS-201 to 204, 207, 301, 308, 402, 601, 606, 801) |
| `hardware` | Hardware lockouts | CS-301 to CS-309 (a pump or valve that did not do what it was told, a hardware hold, a shot cut) and CS-701 (water isn't reaching a zone) |
| `sensors` | Sensors and drift | CS-101 to CS-104, CS-603, CS-604, CS-703, CS-704 |
| `watering` | Watering stopped | CS-202 to CS-208, CS-602, and **CS-209** (new: no watering for a while) |
| `phases` | Phase changes | A zone moving P0 → P1 → P2 → P3 (new event, no card) |
| `stock` | Stock tanks | CS-608, CS-807 |
| `dosing` | Dosing | CS-801 to CS-806 |
| `jev` | Jev | CS-501, CS-702, CS-705, CS-404, and Jev moving a setting (new event, no card) |
| `setup` | Setup and settings | CS-201, CS-401 to CS-405 (except 404), CS-601, CS-605, CS-606, CS-607 |

The mapping lives in one place in the integration (`notify_catalog.py`) and is returned by `notify_get`,
so the page shows exactly what each checkbox covers. A code the catalog does not know goes to
`emergency` when its severity is critical, else to `setup`; a test fails when a code in
docs/error-codes.json is in no kind at all.

## New events

- **Phase change** (`event: "phase"`): the controller sends one when a zone's phase changes, with the room,
  zone, from and to (for example "F2 · Zone 2: P1 → P2, ramp target reached"). No card; a push only.
- **Jev setting change** (`event: "jev_setpoint"`): when Jev moves a zone's P2 shot size or re-water point
  (docs/JEV.md). No card; a push only.
- **CS-209 No watering for a while** (a card and a push): a room that is active, with lights on, has watered
  nothing for `idle_hours` (default 3, set on the page, 1 to 12): no shot fired in any of its zones, whether
  because watering is switched off, a hold, or nothing called for water. Raised once per stretch and again
  every `idle_hours` while it lasts; cleared by the next shot. A zone in P0 (the morning dry-back) or in
  P3 does not start the clock; the clock runs only while at least one zone is in P1 or P2.

## Configuration (integration store `crop_steering.notify`, one for the site)

```jsonc
{
  "revision": 3,
  "idle_hours": 3,
  "recipients": [
    {
      "service": "notify.mobile_app_pixel_7",   // any notify.* service; the page offers the site's phones first
      "name": "Callum's phone",                   // shown on the page; defaults to the device name
      "user_id": "7f21…",                         // the Home Assistant user the phone belongs to, when known
      "kinds": ["stock", "dosing"],               // ids from the table above
      "rooms": [],                                // room prefixes ("" = the default room, "f1_"); empty = every room
      "urgent_high_priority": true                // emergencies go out as high-priority / time-sensitive pushes
    }
  ]
}
```

A phone is a `notify.mobile_app_*` service. The integration lists them with their device name and the
Home Assistant user that registered the app (the mobile_app config entry's user), so each row shows whose
phone it is. Other notify services (a group, Supernotify) can be added as rows by their service name.

## Services

| Service | Who | Data | Response |
|---|---|---|---|
| `crop_steering.notify_get` | any signed-in user | none | `{schema_version: 1, config, kinds[{id, name, detail, codes[], events[]}], phones[{service, name, user_id, user_name}], can_edit_all, user_id}` |
| `crop_steering.notify_save` | admin: any row; a non-admin: only rows whose `user_id` is their own (they cannot add or remove rows) | `expected_revision, recipients, idle_hours?` | as `notify_get`, or `error` ("revision", "not allowed: …", a readable message) |
| `crop_steering.notify_test` | admin, or the phone's own user | `service` | `{sent: true}` or `error` |
| `crop_steering.notify` | admin (the controller) | `key, code?, event?, room?, zone?, title, message, urgent?` | `{sent_to[], error}` |

- `notify` works out the push's kinds from `code` (the catalog) or `event`, then sends to every recipient
  that ticks one of them and whose `rooms` is empty or includes `room`, once per service. `urgent` (the
  controller sets it for critical codes) adds the high-priority data for a recipient with
  `urgent_high_priority`: Android `{"priority": "high", "ttl": 0, "channel": "Crop Steering urgent"}`, iOS
  `{"push": {"interruption-level": "time-sensitive"}}` (`data` is sent to mobile_app services only).
  Every push carries `data.tag = key`, so a repeat replaces the earlier one on the phone instead of piling up.
- A failed send to one phone never stops the others; failures are listed in the response and logged.

## The controller

- `_alert` still creates the card first, exactly as now, and Jev's Alerts judge still decides whether a
  repeat pushes at all (docs/JEV.md). When it pushes:
  - with at least one recipient saved (the integration publishes `sensor.crop_steering_notify_config`,
    state = the revision, attribute `recipients` = the count), it calls `crop_steering.notify` and sends
    nothing itself;
  - with no recipients saved, or when that call fails, it pushes to its `notify_service` option as it
    always has, so an install that never opens the page behaves exactly as before, and a push is never lost.
- Phase changes and Jev's setting changes are sent through `crop_steering.notify` only (never the fallback:
  they are not alerts).
- CS-209 is raised by the controller like any alert (key `idle_<room>`), with its code written out.

## The page (Settings & help › Notifications)

- A grid: a row per phone (the owner's name and the device), a column per kind with a checkbox, the rooms
  it covers (all, or ticked rooms), high priority for emergencies, and **Send a test**. On a phone the grid
  becomes a card per phone with its checkboxes.
- Each column's heading opens what it covers: its codes with their titles, and its events.
- **Add a phone** (admins): from the site's phones not yet listed, or a notify service by name.
- The watering-stopped threshold: "Tell me when a room has watered nothing for [3] hours with the lights on".
- A non-admin sees every row but can tick only their own phones'; an admin edits everything. Changes are a
  draft until **Save**, with a review of what changes, like the other pages.
- A room of the site with no row ticking `emergency` shows a warning: "Nobody gets emergencies for F1".
