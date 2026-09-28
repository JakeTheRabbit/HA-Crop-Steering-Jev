# Batch-tank dosing

Crop Steering can make a batch of nutrient solution and dose single pumps into a room's batch tank:
fill the tank, mix it, dose each nutrient in order, mix again and stamp the fill. Each room has its
own dosing page (Equipment › Dosing) for its own tank, so a site with two tanks sees two separate
pages, one per room.

It drives dosing pumps whose firmware times each dose itself: Home Assistant sets a volume, presses
start, and the pump's own board switches the motor off. Crop Steering never switches a dosing motor
on and trusts itself to switch it off; a browser never switches anything.

This document is the contract between the three layers. Everything below is generic: no entity id,
pump name or recipe is assumed.

## Layers

| Layer | Owns | Never does |
|---|---|---|
| Integration | Each room's dosing configuration and its one pending request, in a revisioned store; the services to read, save and request; `sensor.crop_steering_<prefix>dosing_config` for the controller | Switch, press or set any hardware |
| Controller app | Every action: doses, batches, stops, the irrigation hold during a batch, recovery after a restart, alerts CS-801 to CS-806; `sensor.crop_steering_<prefix>dosing` with live status | Act on a request older than 120 s, or on a room whose configuration it cannot read |
| Dashboard | Equipment › Dosing: pumps, a dose form, the batch, the recipe, history, and the dosing setup | Write any hardware entity; everything goes through `dosing_request` |

## Configuration (integration store `crop_steering.dosing.<entry_id>`)

```jsonc
{
  "revision": 4,                       // bumped on every save; dosing_save requires expected_revision
  "pumps": [                           // 0..8, in the order the page shows them
    {
      "id": "balance",                 // [a-z0-9_]{1,24}, unique in the room
      "name": "Balance",               // 1..40 chars
      "volume_entity": "number.x",     // number.* | input_number.*   the firmware's dose volume (mL)
      "start_entity": "button.x",      // button.* | input_button.* | script.*   starts the on-device dose
      "dosing_entity": "sensor.x",     // binary_sensor.* (on = dosing) | sensor.* (state starts with dosing_prefix)
      "dosing_prefix": "Dosing",       // sensor type only; default "Dosing"
      "power_entity": "switch.x",      // switch.*   turning it off stops the motor (the stop and the overrun cut)
      "flow_entity": "number.x",       // number.* | input_number.* | sensor.*   calibrated flow, mL/s
      "max_ml": 2000,                  // 1..5000; a request above it is refused
      "restore_volume": true,          // put the volume number back after a single dose (firmware that
                                       // keeps its batch recipe in that number); default true
      "stock_tank": "balance"          // the id of a stock tank in this room's stock store, or null
                                       // (Stock tanks, below)
    }
  ],
  "batch": {
    "fill_valve": "switch.x",          // switch.* | null   null = the batch starts with the tank already filled
    "full_entity": "binary_sensor.x",  // binary_sensor.* | sensor.* | null   required when fill_valve is set
    "full_state": "on",                // the full_entity state that means full; default "on"
    "fill_timeout_min": 20,            // 1..60
    "mix_pump": "switch.x",            // switch.* | null
    "mix_valves": ["switch.x"],        // 0..4 switch.*, opened before the mix pump starts
    "mix_power_sensor": "sensor.x",    // sensor.* (W) | null
    "mix_min_w": 200,                  // 0 = no power check; else the mix pump must draw this within 20 s
    "premix_min": 2,                   // 0..30
    "postmix_min": 5,                  // 0..60
    "close_entities": ["switch.x"],    // 0..16 switch.*, switched off before filling (another room's feed path)
    "hold_entity": "input_boolean.x",  // input_boolean.* | null   on for the whole batch, for other automations
    "filled_at_entity": "input_datetime.x", // input_datetime.* | null   stamped with the end time of a finished batch
    "recipe": [                        // dose order = list order; each pump at most once
      {"pump": "balance", "ml": 300, "ml_entity": null} // ml_entity (number.*|input_number.*|sensor.*) is read
                                                        // at batch start and wins over ml; 0 skips the pump
    ]
  },
  "request": null,                     // see Requests
  "updated_at": "2026-09-28T12:00:00+13:00"
}
```

Validation (`dosing.py`, pure): every entity exists and is in its allowed domain; pump ids unique;
recipe pumps exist; a pump appears in the recipe at most once; `full_entity` set whenever
`fill_valve` is; no entity is both a dosing pump's `power_entity` and batch hardware; a `mix_pump`,
`mix_valves` or `close_entities` switch may be a room's irrigation pump or main line (the batch
holds that room's watering); a pump's `stock_tank` is one of the room's stock tanks, and a stock
tank is linked to at most one pump. A room with no pumps is valid and does nothing. "Exists" is checked
when a setup is saved; a stored setup read back at start-up is kept although a device has not come
back yet.

## Services

| Service | Who | Data | Response |
|---|---|---|---|
| `crop_steering.dosing_get` | any user | `room_id` | `{schema_version: 1, room_id, config, candidates[], can_edit, error}` |
| `crop_steering.dosing_save` | admin | `room_id, expected_revision, pumps, batch` | the same as `dosing_get`, or `error` |
| `crop_steering.dosing_request` | admin | `room_id, action ("dose" \| "batch" \| "stop"), pump?, ml?` | `{request, error}` |

- `room_id` is `"room:<prefix>"`, as for the stock services.
- `candidates` lists entities of every domain the configuration can use (`number`, `input_number`,
  `button`, `input_button`, `script`, `binary_sensor`, `sensor`, `switch`, `input_boolean`,
  `input_datetime`): `{entity_id, name, domain, state, unit, device_class}`.
- `can_edit` (in `dosing_get`, and in `dosing_save`'s response): the calling user is an
  administrator (`call.context.user_id` → `hass.auth.async_get_user(...).is_admin`; false when
  there is no user).
- A request is **pending** while `request` is set AND its `id` differs from the `handled` attribute
  of `sensor.crop_steering_<prefix>dosing` (read with `hass.states.get`; Status, below) AND it is
  less than 120 s old. The stored request is not cleared by the integration; it simply stops being
  pending (the controller never acts on one older than 120 s).
- `dosing_request` stores one request `{id, action, pump, ml, at, by}` (`id` a uuid4 hex, `by` the
  user's name, null for an automation). A `stop` always replaces what is pending. A `dose` or a
  `batch` returns `error: "busy"` while one is pending, and a `dose` is refused when the pump is
  unknown or `ml` is not in 0 < ml ≤ `max_ml` (`error` then says which). A request does not move
  the revision.
- `dosing_save` returns `error: "busy"` too (the same word) while a request is pending,
  `error: "revision"` when `expected_revision` is stale, and an `error` naming the field when the
  setup breaks a rule under Validation; a refused save answers with the stored setup, unchanged. A
  pump saved without an `id` gets one made from its name.

`sensor.crop_steering_<prefix>dosing_config`: state = the revision; attributes `pumps`, `batch`,
`request`. This is what the controller reads (every 2 s).

## Requests and the controller

The controller runs one dosing thread for all rooms. Every 2 s it reads each room's
`dosing_config`. A request is taken once (its `id` is remembered, and published as `handled`).

- Taken only when it is less than 120 s old. An older one is published as `handled` with the result
  "too old to act on" and does nothing, so a request left while the controller was down never fires later.
- `stop`: switches off every pump's `power_entity` in the room, and ends a batch (below).
- `dose`: one pump, `ml` mL, into the tank as it is. Refused while a batch runs in the room.
- `batch`: the batch sequence. Refused while a dose or a batch runs, while `hold_entity` is already
  on (something else is dosing), or while the room has a latched hardware fault.

### One dose (`ml` into a pump)

1. Refuse unless the pump's `flow_entity` reads a number > 0 (a firmware that divides by a zero
   flow runs its motor for ever), `ml` ≤ `max_ml`, and `dosing_entity` does not already read dosing.
2. Write the dose to `/data/dosing_state.json` (atomic) before anything moves.
3. Remember the volume number, set it to `ml`, and read it back (within 0.5 mL).
4. Press `start_entity` (`button.press`, `input_button.press` or `script.turn_on`).
5. Expected time `t = ml / flow`. Wait (reading every second) until the pump has been seen dosing
   and then reads not dosing. Deadline `t × 1.25 + 20 s`.
   - Still dosing at the deadline: switch `power_entity` off, confirm it reads off (re-sent once),
     result "ran past its time", alert **CS-801**.
   - Never seen dosing by `t + 15 s`: result "not confirmed", alert **CS-802** (a very short dose
     can finish between two reads: seeing the pump's state change to dosing and back within the window counts).
6. Put the volume number back when `restore_volume` is on.
7. Clear the dose from `dosing_state.json`; publish the result.

### A batch

The room's watering is held for the whole batch (`_blocked`: "making a batch"), and so is every
room whose pump, main line or valves are among the batch's `mix_pump`, `mix_valves`,
`fill_valve` or `close_entities`.

| Step | What happens | Ends the batch (with its code) when |
|---|---|---|
| Hold | `hold_entity` on; wait for any shot in flight in a held room to finish (10 min at most) | a shot is still running after 10 min |
| Close | switch off each held room's valves, main line and pump, and `close_entities`; read back | a switch does not read off (CS-806) |
| Fill | skipped when `full_entity` already reads full or there is no `fill_valve`; else open it and wait for full; always close it and read back | not full within `fill_timeout_min` (**CS-804**) |
| Mix | open `mix_valves`, start `mix_pump`; with `mix_min_w`, wait up to 20 s for the power | the pump does not draw its power (**CS-805**) |
| Premix | `premix_min` minutes | |
| Dose | each recipe pump in order, as "One dose"; `ml_entity` read now; 0 skips | a dose ends any other way than finished (CS-801/802) |
| Postmix | `postmix_min` minutes | |
| Finish | stop `mix_pump`, close `mix_valves`, read back; stamp `filled_at_entity`; `hold_entity` off | |

- A `stop` at any point ends the batch: every dosing pump off, the fill valve closed, the mix pump
  off, the mix valves closed, `hold_entity` off. The result names the step it stopped in.
- Every end other than finishing raises **CS-806** with the step and the reason, after its own code.
- The whole batch has a watchdog: fill timeout + premix + postmix + every dose's deadline + 10 min.
- Stamping `filled_at_entity` is what the stock tanks count batches by, so a stock tank whose per-batch
  dose points at the same recipe number is drawn down by exactly what was dosed.

### After a restart

If `/data/dosing_state.json` holds a dose or a batch in progress, the controller at start-up switches
off every pump's `power_entity`, the fill valve, the mix pump and the mix valves of that room, turns
`hold_entity` off, clears the record and raises **CS-803**. It never resumes a batch.

### Status: `sensor.crop_steering_<prefix>dosing`

State: `idle` | `dosing` | `batch` | `unavailable` (the room's configuration can't be read).

```jsonc
{
  "pumps": {"balance": {"state": "idle" | "dosing" | "unavailable", "flow_ml_s": 11.06,
                        "target_ml": 25, "started_at": "…", "expected_s": 2.3,
                        "last": {"ml": 25, "at": "…", "result": "finished" | "ran past its time" | "not confirmed" | "stopped"}}},
  "batch": {"step": "idle" | "hold" | "close" | "fill" | "mix" | "premix" | "dose" | "postmix" | "finish",
            "pump": "bloom" | null, "started_at": "…", "step_started_at": "…",
            "steps": [{"step": "fill", "state": "done" | "running" | "skipped" | "failed", "at": "…", "note": "…"}],
            "result": null | "finished" | "stopped: <reason>", "ended_at": null | "…"},
  "handled": "<request id>", "handled_result": "…",
  "history": [{"kind": "dose" | "batch", "at": "…", "ended_at": "…", "result": "…",
               "doses": {"balance": 300, "bloom": 1800}, "by": "…"}]   // newest first, 20 kept
}
```

## Alerts (controller, group CS-8xx "Dosing")

| Code | Title | Watering |
|---|---|---|
| CS-801 | A dosing pump ran past its time and was switched off | carries on (a batch stops) |
| CS-802 | A dose could not be confirmed | carries on (a batch stops) |
| CS-803 | A dose or batch was interrupted by a restart | carries on; dosing hardware was switched off |
| CS-804 | The batch tank did not fill in time | held rooms water again; the fill valve was closed |
| CS-805 | The mixing pump did not start | held rooms water again |
| CS-806 | A batch was stopped | held rooms water again once the hardware reads off |

## Stock tanks

A pump may be linked to one of the room's stock tanks (`stock_tank`: the id of a tank in the room's
stock store, `crop_steering.stock.<entry_id>`, or null). A stock tank is linked to at most one pump,
and its level then follows what that pump has actually dosed:

- When a dose ends, the controller draws it from the pump's stock tank: "finished" → the requested
  mL; "ran past its time" or "stopped" → the requested mL or the seconds since the start press ×
  the flow, whichever is less; "not confirmed" → nothing, and the result says so
  ("not confirmed; nothing drawn from its stock tank"). The doses of a batch draw the same way, one
  by one.
- It draws through `crop_steering.stock_draw` with the key `<prefix>:<pump>:<the dose's
  started_at>`. Undelivered draws are kept in `/data/dosing_state.json` and sent again every tick
  until the service answers, so a draw survives Home Assistant being down or a restart; the key
  means it is never counted twice.
- The stock store's fill-based batch draw (a newer time on the room's tank last-fill entity takes
  each tank's per-batch dose) skips the tanks linked to a pump: their doses draw them, so a batch
  that stamps `filled_at_entity` never counts them twice.
- `sensor.crop_steering_<prefix>stock_low` gives each entry of its `tanks` attribute an `id`, and a
  `pump`: the id of the pump linked to it, or null.

| Service | Who | Data | Response |
|---|---|---|---|
| `crop_steering.stock_draw` | admin; automations and the controller app (Home Assistant's Supervisor user) too, as for `stock_record_batch` | `room_id, key, draws: {tank_id: ml}, source ("dose" \| "batch"), note?` | the `stock_get` answer, with `counted`, `duplicate` and `skipped` (the tank ids it does not know) |

`stock_draw` is idempotent on `key` (the stock document keeps the last 100 keys). It takes
ml / 1000 from each named tank's `level_l` (never below 0), appends a history entry
`{at, source, draw_ml, key}` and commits a new revision, which raises the low-stock Repairs card
(CS-608) as usual. Unknown tank ids are skipped and listed in the response. It answers only when
asked (`SupportsResponse.OPTIONAL`): the controller calls it over REST without asking.

## The page (Equipment › Dosing)

For the selected room:

- **The tank**: level, full, EC, pH and temperature from the room's tank mapping, and the batch
  state in one line.
- **Pumps**: a card per pump with a peristaltic-pump drawing (the rotor turns while it doses;
  still under reduced motion), its name, a status pill (Idle, Dosing, Unavailable, Not calibrated),
  its flow in mL/s, its last dose (mL, when, result), its stock tank's level when a stock tank has
  the same name, and a progress bar during a dose (elapsed against expected).
- **Dose a pump**: the pump, the mL (up to its `max_ml`), the expected seconds, a review, then the
  request; while it runs, the target, the elapsed time, the estimated mL so far, and **Stop**.
- **Make a batch**: the steps with their state, the recipe (mL per pump, the total, the expected
  time), **Make a batch** through a review, and **Stop** while it runs.
- **History**: the last doses and batches.
- **Dosing setup**: the pumps and the batch hardware, from `dosing_get` candidates, saved with
  `dosing_save`. Admins only; everyone else sees it read-only.

A room without pumps shows what dosing does and **Set up dosing**.
