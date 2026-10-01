# Dryback planner and Jev line steering design

Date: 2026-10-02

Status: design approved by the owner on 2 Oct 2026 (explainer page, version 5). Next step: implementation plan.

Athena citations are to the Athena Pro Line handbook, metric, A01.002, by printed page: pp.33-41 are the sensor irrigation program (P0-P3), pp.42-47 are hand watering. Numbers and rules that are not from the handbook are marked "ours".

## Outcome and scope

Each morning P1 finds the zone's plateau. From it the controller draws a line, the planned VWC and pore EC for every minute until the next morning's first shot, and Jev steers the zone along that line live, every 15 minutes. In the afternoon Jev also sets where P2 ends (the time of the last shot and the VWC it leaves the block at), so the P3 dryback runs clean through the night and P0 to the next first shot with no night shot. Code keeps the arithmetic and the safety locks.

Everything is behind a per-zone switch, off by default, so a generic install sees no change. F2 zone 1 is the first live zone.

Out of scope: measuring runoff (an open issue below), room climate control, automatic stage changes (a later Jev job), F1 and veg rooms.

## Why

F2 history, 22 Sep - 1 Oct 2026:

- P1 stops at a fixed target before the block is full. Zone 1 reached a plateau on 3 of 9 days. Zone 3 never did: it was still gaining 2-5 points a shot when P1 ended at its target of 41, and it reads 67-74 when allowed to fill.
- The plateau sinks after over-drying and recovers when the block is re-wet: zone 1 was 37.2 on 22 Sep, 32.9 on 27 Sep after the dry days, and about 36 now. Zone 2, re-wet by the 28-30 Sep flood from below, holds 44-57 (it was 21-30).
- P2 never ends before lights-off. Predictive P3 cannot fire on a 12/12 day because `hours_to_lights_on` is always over 12 at 3 h before lights-off (`core.py:229-236`), and the envelope refuses a Dusk advance in vegetative steering. The overnight dryback is whatever the night does.
- Forecasting the night drying rate misses by about 0.25 points an hour, about 3 points over 12 h (zone 1: 0.06-0.15). A forecast alone cannot land the dryback.
- Zone 1's pore EC rose from 3.2 to 4.8 during P1 on 1 Oct while the feed was 3.3: a rinse still coming through, which a VWC-only test cannot see.
- On 26 Sep Jev's per-day setpoint notches walked zone 1's shot down to 1 % and zone 3's threshold up to 74.5 with nothing pulling them back. New authority must act on today's actions against a line that is re-measured every morning, never on persistent setpoints.

## Terms

- **Plateau** (ours): where VWC stops rising in P1, two shots in a row each adding less than 0.6 points and less than 25 % of the first shot's rise. It replaces the three "peaks" in use today (the running max, the learned ceiling and the planned peak).
- **Full saturation** (Athena p.33): the block can hold no more water and peak VWC can no longer increase. Athena states the dryback as a share of it: a sensor at 70 % when fully saturated, dried 30 %, reads 50 % (p.43). A plateau is not always a full reading. Zone 1's 36 is not; zone 3's 67-74 is.
- **Field capacity** (Athena p.33): the maximum VWC before runoff. Not measured here (there is no runoff measurement) and never used as a name for the plateau.
- **Landing**: VWC at the next morning's first shot. Athena's P3 runs until the first irrigation of the following day (p.39), and the dryback after lights-on before that shot is part of P3 (p.33).
- **P2 end**: the last shot of the day, its time and the VWC it leaves the block at.
- **Clean night** (ours): the zone lands within 0.5 points of target at the first shot, with no night shot.
- **The line**: the planned VWC and pore EC for every minute, from the plateau call to the next first shot.

## Design

### Who does what

Code measures, builds the line, works out gaps and forecasts, carries out every action, and holds the locks. Jev judges what a gap means and what to do about it. Jev answers typed questions with a confidence, each asked in two phrasings that must agree, as Jev does today. On zones with the switch on, the only limits on Jev's steering answers are the safety locks; agronomic choices inside them are Jev's. The code's own fallback rule acts whenever Jev is offline, unsure (confidence under 0.7) or benched.

### P0

- Window (ours): the first shot no earlier than 30 min and no later than 2 h after lights-on, matching Athena's 30 min to 2 h (p.39). Page 36 says 1-2 h.
- The morning dryback is measured from yesterday's plateau, the one peak.
- Athena's 1-5 % additional dryback (p.39) may be raw points: on the same page "2 %/hour" means 50 % falling to 48 % VWC. The handbook calls only the 30-40 and 40-50 targets relative. It is not used as a relative target here.

### P1: finding the plateau

1. Ramp shots every 20 min, inside Athena's 15-30 (p.36), growing by the existing increment. The grow-plan stage sets the minimum number of shots.
2. Each shot is scored: rise = VWC about 10 min after the shot minus VWC just before it, and the direction pore EC moved.
3. Plateau (code): two consecutive rises below max(0.6 points, 25 % of the first rise).
4. After each shot Jev answers: full / keep going / one bigger test shot / accept a lower peak / probe lagging.
   - On a vegetative day "keep going" includes rinsing: keep shooting until pore EC turns down. On a generative day stop at fullness, because waiting for EC to fall pushes the block through the salt and into runoff, the opposite of EC stacking (Athena p.33, p.34).
   - Vegetative day: today's plateau may not be lower than yesterday's unless last night's dryback went past the cell maximum, which code verifies. Otherwise one test shot at 1.5 times the size; if VWC is still flat, accept the lower plateau and alert the owner.
   - Generative day: Athena sets the peak on purpose at or below field capacity (p.34), so a lower peak is allowed without a big dryback.
   - "Probe lagging": the zone falls back to its fixed P1 target for the day.
5. Locks: 8 shots or 150 min, the zone's plateau ceiling, flood protection.
6. Jev offline: the code's plateau rule decides.
7. The plateau is saved to history and drives the line. It is passed to the engine in memory as the field-capacity parameter. The operator's `field_capacity` number is not written.

Athena ends P1 when VWC is on target and runoff is in range: 2-6 % shots every 15-30 min until 2-7 % runoff when establishing veg (p.36). Without a runoff measurement the plateau stands in for that.

### The line

Built at the plateau call, and rebuilt whenever the plan changes (a Jev call, a new P2 end, a stage change).

- **Landing** = plateau × (1 - dryback). The dryback is the Athena p.40 cell for the zone's stage and steering:

  | Stage (p.40) | Weeks | Steering | Dryback | Substrate EC |
  | --- | --- | --- | --- | --- |
  | Veg | - | vegetative | 50 % once at transplant, then 25 % | - |
  | Stretch | flower 1-4 | generative | 40-50 % | 4-10 |
  | Bulk | flower 5-7 | vegetative | 30-40 % | 3.5-6 |
  | Finish | flower 8-9 (p.34 says 8-10) | vegetative EC, generative dryback | 40-50 % | 3-4 |

  The p.39 summary card (30-40 % vegetative, 40-50 % generative) and the hand-water targets (pp.42-43: veg and bulk 30-40 %, stretch weeks 2-3 and the last week 50-60 %) are not used.
- **Start point in the cell**: the low end (bulk 30 %). Jev may move within the cell once 3 nights have landed.
- **Landing lock** (ours): never below emergency floor + 2. A cell that needs a deeper landing is capped. Zone 1 on a 36.0 plateau with a 20.7 floor reaches at most 36.9 % (36.0 down to 22.7), so the 40-50 % cells do not fit until the morning plateau is a full reading. Half of a 70 reading is 35, which clears 22.7.
- **P2 hold** (ours): top up when VWC drops below the plateau minus one shot's measured rise, plus the existing EC offset. It is not tied to runoff, so it can sit either side of field capacity (an open issue).
- **P2 end**: Jev's choice (below). The default is a shot back to the plateau at the formula time.
- **After the P2 end**: today's measured day rate to lights-off, the planned night rate to lights-on, and the zone's measured morning rate through P0.
- **Pore EC line**: the p.40 substrate EC cell for the stage is the band, and the operator's EC target (existing entities) is a point inside it. Pore EC is compared with substrate EC, not runoff EC: runoff reads slightly lower than substrate EC (p.41). The "runoff 1-2 EC over input" note (p.47) belongs to the hand-water chart (p.42).

### The tracker (code, every minute)

For each zone: the gap to the line for VWC and pore EC; rates over the last 30 and 60 min; forecasts at the P2 end, at lights-off and at the first shot; the sibling zones' gaps; the room (stage 2); and how the probe answered its last shot (rise against what the zone's gain predicts).

### Jev steering

Asked every 15 min with lights on and every 30 min at night (ours). Also asked straight away when VWC is 1.0 point or pore EC is 0.5 off the line, or the phase changes (ours).

The question carries the tracker's facts in plain words, the owner's doctrine and the zone's Athena cell. Answers: on track / shoot now (small, normal or big) / skip the next shot / set the P2 end / flush (a big shot for runoff) / let EC build / dilute / tonight deeper or softer inside the cell / probe suspect / call the owner.

Acting: with confidence 0.7 or more (ours) and both phrasings agreeing, the answer goes to the lock check and then runs. Otherwise the code's fallback rule acts (ours): shoot when VWC is more than the drift threshold (1.0 point) below the line, skip the next shot when it is more than that above, and flush when pore EC is 20 % over its target.

Marking (ours): every call that acted is graded 30-60 min later (did the gap shrink?). Three worse calls in a row bench Jev for the rest of the day: the code's rule steers live and the owner gets a push. There are no shadow or advisor runs at any stage.

On switched zones the Steer question covers the moments of today's narrow judges: Dawn, Ramp, Salt, Dusk and Night are not asked. Probe, Shot, Zones and Stage stay as they are. The Setpoints judge is retired on these zones; its persistent notches are the path that drifted on 26 Sep.

### The P2 end

From mid-afternoon, every 30 min until the last shot, the code builds a menu. For each end VWC from the hold level up to the plateau in 0.2-point steps it gives:

- the last-shot time that lands on target at the first shot;
- the lights-off VWC;
- the landing on the planned night rate, and on each of the last few nights' actual rates.

Jev picks one, or "plateau at the formula time", and names the night it planned for. Choosing which night to plan for and how long to hold the peak is the judgement; bulk is a vegetative cell, where the held peak keeps runoff going and pore EC down (p.34).

Constraints (code):

- the end VWC is at most the plateau;
- the landing is at least floor + 2;
- the minimum daily water is in by the last shot (the minimum wins, and the planner front-loads P2 to meet it);
- the last shot comes after P1 and no later than lights-off.

The final shot is sized from the zone's gain to reach the chosen end VWC. If Jev is offline, the final shot goes back to the plateau at the formula time on the planned night rate.

Worked example (zone 1 in the explainer): the default plateau shot at 16:13, planned on a 0.45 points an hour forecast, lands at 23.4 on a 0.60 night and needs a night shot. Jev plans for 0.60 (the last two nights in reheat ran 0.58 and 0.62) and ends with the 18:30 top-up sized to 35.8. That leaves 32.8 at lights-off and lands on 25.2 at the 10:30 first shot.

### P3 and the night watch

There are no routine shots (Athena p.39). Jev checks the pace every 30 min.

Safety net (ours): if the zone is heading more than 0.5 under target with more than 1 h left, one correction shot sized to land on target. Skipping it as a probe fault needs two agreeing "probe fault" answers plus code evidence; if Jev is offline the shot fires. One per night, capped in size. The emergency floor and shot are unchanged and always on.

Each morning the controller records: the landing against target at the first shot, the night rate, the P0 drop, whether the night was clean, and whether the net fired. The record feeds the next P2 end menu.

### Room conditions (stage 2)

Room temperature, RH, VPD and AC and dehumidifier state, compared with last night, go into the P2 end and night questions. Jev may scale the night rate it plans for by up to 20 % (ours). This needs the environment entities in the room descriptor and a deliberate change to the guard in `tests/test_translations.py:141-158`.

The Athena p.33 climate card and its warning also go into Jev's evidence:

- climate card: veg 22.2-27.7 °C, stretch 25.5-27.7, bulk 23.8-26.6, finish 18.3-22.2;
- warning: a high substrate EC may burn plants when the environment is out of range.

This design sets no room target.

### Locks (all ours, none from Athena)

- **Emergency floor and shot**: per zone, unchanged.
- **Landing**: at least floor + 2.
- **Machine-gun guard** outside P1: at least 15 min between shots, at most 4 an hour, shot sizes inside the zone's range. Emergency shots are exempt.
- **Daily cap and daily minimum**: 2000 mL a plant on F2. The minimum is unchecked until the block is matched to Athena's shot table (p.40), in mL per 1 % shot: Hugo 15 cm 35 mL; 15 cm slab 100 mL; Uni-slab 50 mL; Delta 6.5 6.5 mL; Delta 10 10 mL.
- **P1 caps and plateau ceiling**: 8 shots and 150 min. The ceiling sits within the engine range 40-90; F2 zone 3 is at 70.
- **Night correction**: one at most.
- **Flood protection**: on controller-opened shots only, never a blanket valve timer.
- **Other locks**: feed EC/pH gate, dose guard, hardware-fault latch, kill switch, and Jev's daily call budget.

## Settings (owner-approved defaults)

| # | Setting | Default | Where |
| --- | --- | --- | --- |
| 1 | P1 minimum shots by stage | veg 3, stretch 3, bulk 3, finish 2 | grow-plan stage parameter `p1_minimum_shots` |
| 2 | P1 caps | 8 shots, 150 min | existing `p1_maximum_shots`; new per-zone number `p1_max_minutes` |
| 3 | Plateau ceiling | 90 generic; F2 zone 3 at 70 | new per-zone number `plateau_ceiling` |
| 4 | Minimum daily water against the dryback | the minimum wins; the planner front-loads P2 | rule |
| 5 | Start point in the cell | the low end (bulk 30 %); 40-50 % only where floor + 2 allows | rule |
| 6 | Steer cadence and drift | 15 min with lights on, 30 at night; VWC 1.0, pore EC 0.5 | add-on options |
| 7 | When Jev is overruled | confidence 0.7; bench after 3 worse calls in a row | add-on options |
| 8 | Machine-gun guard | at least 15 min apart, at most 4 an hour | engine parameter |
| 9 | P0 window | first shot 30 min to 2 h after lights-on | add-on options |
| 10 | Clean night | within 0.5 at the first shot, no night shot | add-on option |

Per-zone switch: `switch.crop_steering_{prefix}zone_{n}_line_steering`, default off, following the existing `zone_{n}_enabled` key pattern. Add-on options are read with `o.get("key", default)` so an old `options.json` still works.

## Architecture and invariants

- **Engine** (`crop-steering-engine/src/crop_steering_engine/core.py` and its vendored copy, kept identical). New inputs, each neutral by default:
  - `p1_done`: ends P1 on the controller's plateau call. The ceiling and shot-count exits (`core.py:208-226`) stay for zones with the switch off.
  - `p2_end_at` and `final_shot_pct`: at `p2_end_at`, fire the final shot and enter P3 with the lights on. This replaces predictive P3 (`core.py:227-237`) on switched zones.
  - `night_correction_pct`: one P3 shot when set, cleared after it fires.
  - The machine-gun guard: a minimum gap and an hourly cap for non-P1, non-emergency shots.

  `decide()` stays pure. A golden test proves that with every new input at its default, every decision equals today's.
- **Controller** (`addons/f2_control/f2_control/`). New pure modules:
  - `plateau.py`: P1 scoring and the plateau rule.
  - `line.py`: builds the line.
  - `tracker.py`: gaps and forecasts.
  - `p2_end.py`: the menu, grown out of `curve_tracker.plan_day` (`curve_tracker.py:64-90`).
  - `night.py`: the watch and the net.

  Jev gets a `steer` judge with P1, P2-end and night variants, in `jev/judges/steer.py`; marking and the bench live in `jev_bridge.py`. On switched zones the plateau is passed to the engine in memory and the operator's `field_capacity` number is left alone.
- **Integration**: the per-zone switch and two per-zone numbers, all additive. Each zone also publishes, as a sensor or attributes on the existing zone sensor: the line, the P2 end plan, the last landing, the clean-night streak and Jev's running score.
- **Dashboard**: a line layer and a P2-end marker on the day chart, using the layer toggles from #29, and Jev's calls on the existing Jev layer.
- **Doctrine** (`jev/doctrine.py`): on switched zones the target is the Athena p.40 cell as a share of the plateau (owner, 1 Oct 2026). The rule that "the owner's stage arc leads" where Athena's relative drybacks and the owner's point drybacks differ no longer sets it. The three Athena copies (the frontend `athenaDryback`, `curve_tracker` ATHENA and the doctrine) collapse into one table from p.40.
- **State** (`/data/state.json`, per zone, read with `.get()` and seeded in `_fresh_zone`):
  - plateau history: date, value, how it was found, shots;
  - night history: date, night rate, P0 drop, target, landing, clean, net fired;
  - bench-until.

  Jev's marks go in the existing ledger. An old state file loads unchanged, proven by a seeded fixture.
- **Fail-safe**:
  - Jev offline or benched: the code's rules act.
  - Probe distrusted: the zone falls back to today's fixed targets for the day.
  - No history (fresh install): the zone's existing rate EWMAs, or else the current defaults.
  - Restart: state is restored and the line rebuilt.

## Delivery and verification

Live, zone by zone, with no shadow runs. One change per pull request into `testing`, classed per `docs/RELEASING.md`; the engine, controller, state and add-on options are C3.

1. **Remember nights and plateaus**: state fields and the morning record. C3.
2. **P1 plateau, live on switched zones**: engine `p1_done`, `plateau.py`, Steer in P1. C3.
3. **The line, Steer, the P2 end and the night net, live**: `line.py`, `tracker.py`, `p2_end.py`, `night.py`, the engine inputs and the judge. C3. The dashboard layer ships as its own C1 PR.
4. **F2 zone order**: zone 1, then zone 3. Zone 2 once the Probe judge trusts its probe.
5. **Room conditions (stage 2)**: the descriptor and the guard change. C3.

Tests:

- the engine golden test (defaults equal today);
- the plateau rule on replayed F2 P1 sequences (it must call zone 1 at 32.9 on 27 Sep, 35.2 on 29 Sep and 36.1 on 30 Sep);
- the line and P2-end arithmetic against the worked example;
- properties for the landing lock and the machine-gun guard;
- clean-night classification;
- the bench;
- council and envelope tests for the Steer judge;
- state migration from a seeded old file;
- `tests_ha/` for the new switch and numbers;
- the vendored engine identical.

Live check on zone 1: the day chart shows the line and Jev's calls, each morning record shows the landing error and whether the night was clean, and Jev's score is on the dashboard.

## Open issues

- **Runoff is not measured.** Athena ends P1 on runoff and moves pore EC by runoff volume: vegetative 8-16 % and generative 1-7 % of the water fed (p.40), measured by the procedure on p.41. Until runoff is measured, the P2 hold cannot be placed against field capacity.
- **Zone 1's plateau is not a full reading.** The generative cells fit only once the morning plateau is full.
- **Zone 2's probe** gives about half the response of the others. It stays on fixed targets until trusted.
- **The 2000 mL minimum** is unchecked against Athena's shot table (p.40).
- **The fallback flush is early for bulk.** The code's fallback flushes at 20 % over the operator's EC target (4.8 on a 4.0 target), which is inside the bulk cell (3.5-6, p.40). With Jev online, Jev holds in that case; with Jev offline the fallback flushes early. Tying the fallback to the top of the stage cell is a candidate change for the plan.
- **F2's AC targets** (27.5 °C day, 27.0 night) sit above Athena's bulk climate card (23.8-26.6 °C, p.33). That is out of scope here.
- **Stage boundaries differ.** Athena's weeks (p.34, p.40) and the owner's stage arc in `doctrine.py` (bulk days 22-42, finish 43-56) disagree. The cell is picked from the zone's steering and stage selects, which the owner sets.
- **Handbook contradictions are kept as they are:**
  - Transplant dryback is 50 % in the p.35 text and on p.40, but 35-40 % in the p.35 drawing.
  - Later veg says "dry to 25 %, start P2 once past 20 %" (p.36), against the p.39 summary card's 30-40 %.
  - The hand-water drawings' 18/6, 12/12 and noon-to-6 pm clock marks (pp.42-43) are ignored.
