# Crop Steering, Jev edition

This fork of [HA-Irrigation-Strategy](https://github.com/JakeTheRabbit/HA-Irrigation-Strategy) puts
**TypeSafe Jev**, a typed-decision model served through Cloudflare, in charge of every judgement the
crop-steering engine makes, while code keeps charge of everything physical.

The base engine is rules: fixed thresholds that are right most of the time and wrong in the cases
nobody wrote down (a probe channeling, a salt front still passing, a ramp that is done but not by the
numbers). Each fix added another "if". This edition replaces the "ifs" with a judge.

## What "truly insane and amazing" means here

Eight properties. Each one is concrete and checked by a test or a published number.

| # | Property | What it means | How it is checked |
|---|---|---|---|
| 1 | **Jev everywhere a judgement lives** | Every decision the engine makes on ambiguous evidence is asked of Jev: when the morning ramp starts, when it is done, whether a probe is telling the truth, whether a shot landed, why pore EC moved, when the day's watering stops, why one zone drinks differently, which alert is worth waking someone for. Ten judges cover P0, P1, P2 and P3 for every zone. | The coverage map below; one test per judge. |
| 2 | **Code owns physics and safety** | Jev never opens a valve, never sizes a shot, never crosses the daily floor or cap, the drown ceiling or max EC, never stops a rescue shot and never removes the last probe. Every Jev decision is a *directive* that the **envelope** admits or refuses on arithmetic Jev can't do. | Envelope tests for every directive type. |
| 3 | **Never waits** | Jev is asked in the background; the 60-second loop reads the last answer and carries on. A slow, failing or absent Jev leaves the engine exactly as the base engine behaves. | A golden test: with Jev off or erroring, every decision and every switch call equals the base engine's. |
| 4 | **A council, not a coin toss** | Jev's confidence moves with how a question is worded. So every judge asks each question in two independent phrasings in the same call and acts only when both land on answers that lead to the same action, and the calibrated probabilities of those answers add up ("the slab is full" and "the EC left is the feed passing" both mean hand the ramp over). | Council tests: disagreement means no action. |
| 5 | **It remembers and checks itself** | Every verdict and what code did with it goes into a ledger. When the outcome can be measured (EC after a flush call, the ramp after a hand-over, water after a probe was set aside), code measures it and shows Jev its own track record next time. A move that did not work is not repeated blindly. | Ledger tests; the track record in the evidence. |
| 6 | **Code does the maths, Jev does the judging** | Jev cannot do arithmetic and knows no crop-steering doctrine. Code turns the raw series into plain facts ("rose 3.1 points after the 10:40 shot and held", "EC rising 0.4 an hour since the last flush"), and every question carries the owner's own doctrine from his GrowLabs wiki (below). What is certain goes in code; what is judgement goes to Jev. | Evidence tests: no raw series ever reaches Jev; every question carries its doctrine. |
| 7 | **Explains everything** | Every verdict is published in Home Assistant with its answer, its confidence, whether the council agreed, what code did and why. `sensor.crop_steering_<prefix>zone_N_jev` per zone and `sensor.crop_steering_<prefix>jev` per room. | Publish tests. |
| 8 | **Cheap and bounded** | Each judge has a cadence and a trigger, so Jev is asked when there is something to decide. A room of three zones costs cents a day (input $0.042 per million tokens, output free). A daily call budget caps it. | The room sensor shows calls and tokens today. |

## The judges (coverage map)

| Judge | Phase | Asked | Jev decides | Code does with it | Envelope |
|---|---|---|---|---|---|
| **Dawn** | P0 | every 10 min in P0 | start the ramp now / keep drying / the probe isn't moving | brings P1 forward | only after half the dryback target or a quarter of the max wait; never later than the base engine |
| **Ramp** | P1 | every 15 min once a ramp shot is 10 min old | keep ramping / slab is full / the EC left is the feed passing / real salt (all three: hand over; the ramp only refills, salt leaves in maintenance runoff) / the probe is lagging, wait | brings P2 forward | minimum ramp shots in; VWC within 3 points of the ceiling; never past `p1_maximum_shots` (the base engine still ends it there) |
| **Salt** | P2 | every 30 min when EC is settled | salts accumulating / salt front still passing / the feed changed / the probe is suspect / the target can't be reached / in band | chooses the EC steer: steer, hold, or decay the offset | only changes the P2 offset the base engine already steers, inside its clamp |
| **Dusk** | P2 | every 15 min in the last 3 h before lights-off, never for a zone steered vegetative | go to P3 now / one more top-up then P3 / carry on | brings P3 forward | only in the last 3 hours; never for a vegetative zone (the owner's doctrine: it stops late); VWC at least 3 points above the P3 emergency floor; the P3 rescue still fires |
| **Probe** | all | every 30 min once the zone has had two shots | is this probe tracking the substrate; if not, how (stuck, channeling, out of the block, wrong zone, drifting) | sets the zone's probe aside: the zone runs on the existing dead-probe path (copies a healthy sibling, or its timer) | two agreeing verdicts in a row, **and** code-confirmed evidence (two shots with no rise while a sibling rose, or a flat line for an hour with water going in); lasts only while Jev's latest answer (90 min) still says so |
| **Shot** | all | once per shot, 20 min after it ends | landed / landed slowly / reached the probe but moisture didn't rise / didn't reach the zone | tags the shot, feeds the probe judge, raises CS-701 on two misses in a row | never adds water |
| **Night** | P3 | every 20 min within 3 points of the emergency floor | real drying / a probe fault | raises CS-703 | never stops a rescue |
| **Zones** | P1, P2 | hourly with lights on, when a zone's water per plant is under 70 % or over 140 % of the room's median zone (itself included; the other zone in a room of two) | why one zone differs (recipe, plants drinking less, wet spot, delivery fault, water not landing) | raises CS-702 with the likely cause | alert only |
| **Stage** | P2 | once a grow-day, with lights on, when the flower day is known | is last night's dryback, the settled pore EC, and the stage's move-on signs where the owner's stage arc wants them for today | CS-705 advice; code itself states a steering mode that is not the stage's | advice only: never changes a setting |
| **Alerts** | all | once per alert, again after 6 h | escalate / remind / quiet | whether its repeats push to a phone (every alert is always a card; its first raise always pushes) | pump and valve faults (CS-3xx), lost probes (CS-101 to 103), CS-207, CS-701, CS-703 and CS-704 always push |
| **Setpoints** | P2 | hourly in P2 while Auto Setpoints is on | the base engine's auto-setpoints supervisor, unchanged in this edition (the next piece of work: two-sided, on the ledger) | bounded setpoint nudges | as the base supervisor |

## The owner's doctrine

Jev knows no crop steering, so every question carries its rules. They are the owner's own GrowLabs wiki pages (root-zone TEROS-12, closed loop, flowering stages, rockwool crop steering, one steering law, slab irrigation strategy, ripening and harvest timing), restated as one-sentence rules with their source in `jev/doctrine.py`: 99 rules across ramp, dryback, maintenance, EC, probe, stage, ripening, closed loop and slab. The stage arc is the slab guide the owner chose on 26 Sep 2026 (flower setting days 1-21 generative, bulk 22-42 vegetative, finish the last 14 days); set `jev_flower_start` and every judge sees today's stage and the operator's steering mode.

The live runs drew the line between doctrine for Jev and doctrine for code. Told in the question that a vegetative stage stops late, Jev still called an early stop for a well-watered zone near lights-off (both phrasings, p 0.7): so "a vegetative zone stops late" is an envelope rule. Whether the operator's steering mode matches the stage is arithmetic on the flower day: so the Stage judge's code states it, and Jev judges only what needs judgement.

## The envelope (what code never hands over)

- **Hardware:** valve, main line and pump sequencing, read-backs, fault latches and every interlock.
- **Water limits:** the daily minimum floor, the daily cap, the drown ceiling, max EC flushes, the P3
  emergency rescue, the watchdog. Jev can bring a phase forward; it can never stop one of these firing.
- **Arithmetic:** shot size, duration, litres, rates, peaks and the clock.
- **Direction:** phase directives only ever move a zone *forward* through its day (P0 → P1 → P2 → P3),
  and only earlier than the base engine would; the base engine's own transitions always still happen.
- **Doctrine that is certain:** a zone steered vegetative is never stopped early.
- **Probes:** the last usable probe is never set aside by Jev alone: it takes the council, the code-confirmed
  evidence, and it expires.

## Failing safe

Jev is optional at every level: no Cloudflare credentials, `jev_enabled` off, a judge left out of `jev_judges`, the daily budget spent, Jev slow or down, or an answer that
fails to parse: each means that judge's decisions are the base engine's, unchanged.

## Configuration

Add-on options (the controller app):

| Option | Default | What it does |
|---|---|---|
| `typesafe_api_key` | empty | A TypeSafe API key (`apikey_...`): Jev straight from TypeSafe (`api.typesafe.ai/v1/systemone`, model `jev-latest`). Used when set. |
| `cf_account_id`, `cf_api_token`, `cf_gateway_id` | empty | Or Jev through Cloudflare Workers AI: the account, a token with Workers AI access, and the AI Gateway (optional). With neither a TypeSafe key nor these, Jev is off. |
| `jev_enabled` | on | Off runs the plain engine even with Cloudflare set. |
| `jev_judges` | all | The judges that may act, e.g. `dawn,ramp,salt,dusk,probe,shot,night,zones,alerts`. |
| `jev_daily_calls` | 2000 | The day's call budget across rooms. |
| `jev_flower_start` | empty | Each room's first day of 12/12: an input_datetime or a date, for every room or as `room=value` pairs, e.g. `default=input_datetime.f2_flip_date, f1=input_datetime.f1_flip_date`. |
| `jev_flower_days` | 56 | The cultivar's flowering length; the finish is its last 14 days. |

## What Jev can raise

Five codes, in their own group of the error-code list (docs/ERROR_CODES.md): **CS-701** water isn't reaching a
zone, **CS-702** a zone's water per plant is out of line, **CS-703** an overnight low looks like a probe fault,
**CS-704** Jev set a zone's probe aside, **CS-705** a zone is off its stage's arc. The controller raises them through one method with each code written
out, so a judge can never raise anything the list does not explain.

## What it shows

The dashboard reads these live. The Overview's grow day marks each decision above its zone's chart
(filled when code acted) and puts today's stage in its top line, with the latest five decisions under
the charts; **Activity → Jev decisions** lists all of them, by zone or actions only, with today's calls,
tokens and cost. A room without Jev shows none of it.

- `sensor.crop_steering_<prefix>zone_N_jev`: the judges acting on the zone (or `watching`), and per judge its
  latest verdicts (answer, probability, whether both phrasings agreed), the directive, and why code admitted or
  refused it.
- `sensor.crop_steering_<prefix>jev`: calls and input tokens today, errors and the last error, the judges on.
- `/data/jev_ledger.jsonl`: every directive that acted, what Jev saw, and later how it turned out.

## Checked against the real Jev

`scripts/jev_live_smoke.py` builds realistic zone situations, runs each judge's own evidence and questions through
Cloudflare and compares the council's verdict (and what the envelope then admits) with what an experienced grower
would say. On 27 September 2026, with the doctrine in every question: 14 of 14, about 3,000 input tokens a
question (cents a day for a room), no errors. The same zone two hours before lights-off was stopped early in
flower setting and left watering in flower bulk; a zone left on generative steering in bulk was flagged.
