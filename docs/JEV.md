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
| 4 | **A council, not a coin toss** | Jev's confidence moves with how a question is worded. So every judge asks each question in two independent phrasings in the same call and acts only when both agree, on the calibrated probabilities, not just the top answer. | Council tests: disagreement means no action. |
| 5 | **It remembers and checks itself** | Every verdict and what code did with it goes into a ledger. When the outcome can be measured (EC after a flush call, the ramp after a hand-over, water after a probe was set aside), code measures it and shows Jev its own track record next time. A move that did not work is not repeated blindly. | Ledger tests; the track record in the evidence. |
| 6 | **Code does the maths, Jev does the judging** | Jev cannot do arithmetic and knows no crop-steering doctrine. Code turns the raw series into plain facts ("rose 3.1 points after the 10:40 shot and held", "EC rising 0.4 an hour since the last flush") and writes the doctrine into each question ("EC going up after a flush means the salt front is still passing: keep flushing"). | Evidence tests: no raw series ever reaches Jev. |
| 7 | **Explains everything** | Every verdict is published in Home Assistant with its answer, its confidence, whether the council agreed, what code did and why. `sensor.crop_steering_<prefix>zone_N_jev` per zone and `sensor.crop_steering_<prefix>jev` per room. | Publish tests. |
| 8 | **Cheap and bounded** | Each judge has a cadence and a trigger, so Jev is asked when there is something to decide. A room of three zones costs cents a day (input $0.042 per million tokens, output free). A daily call budget caps it. | The room sensor shows calls and tokens today. |

## The judges (coverage map)

| Judge | Phase | Asked | Jev decides | Code does with it | Envelope |
|---|---|---|---|---|---|
| **Dawn** | P0 | every 10 min in P0 | start the ramp now / keep drying / the probe isn't moving | brings P1 forward | only after half the dryback target or a quarter of the max wait; never later than the base engine |
| **Ramp** | P1 | 20 min after each ramp shot | keep ramping / slab is full, hand over / the EC left is the feed passing, hand over / real salt, keep flushing / the probe is lagging, wait | brings P2 forward | minimum ramp shots in; VWC within 3 points of the ceiling; never past `p1_maximum_shots` (the base engine still ends it there) |
| **Salt** | P1, P2 | every 30 min when EC is settled | salts accumulating / salt front still passing / the feed changed / the probe is suspect / the target can't be reached / in band | chooses the EC steer: steer, hold, or decay the offset | only changes the P2 offset the base engine already steers, inside its clamp |
| **Dusk** | P2 | every 15 min in the last 3 h before lights-off | go to P3 now / one more top-up then P3 / carry on | brings P3 forward | only in the last 3 hours; VWC at least 3 points above the P3 emergency floor; the P3 rescue still fires |
| **Probe** | all | every 30 min, and after each shot is audited | is this probe tracking the substrate; if not, how (stuck, channeling, out of the block, wrong zone, drifting) | sets the zone's probe aside: the zone runs on the existing dead-probe path (copies a healthy sibling, or its timer) | two agreeing verdicts in a row, **and** code-confirmed evidence (two shots with no rise while a sibling rose, or a flat line for an hour with water going in); expires after 24 h unless confirmed again |
| **Shot** | all | 20 min after every shot | landed / landed slowly / reached the probe but moisture didn't rise / didn't reach the zone | tags the shot, feeds the probe judge, raises CS-601 on two misses in a row | never adds water |
| **Night** | P3 | when VWC falls toward the emergency floor | real drying / a probe fault | wording of the alert only | never stops a rescue |
| **Zones** | room | hourly with lights on | why one zone differs (recipe, plants drinking less, wet spot, delivery fault, water not landing) | raises CS-602 with the likely cause | alert only |
| **Alerts** | all | on each alert | escalate / remind / quiet | phone push or card only | CS-3xx and anything below the P3 floor always push |
| **Setpoints** | P2 | once a grow-day, mid-P2 | the existing auto-setpoints supervisor, now two-sided and with the ledger | bounded setpoint nudges | as the base supervisor, plus no repeat without an outcome |

## The envelope (what code never hands over)

- **Hardware:** valve, main line and pump sequencing, read-backs, fault latches and every interlock.
- **Water limits:** the daily minimum floor, the daily cap, the drown ceiling, max EC flushes, the P3
  emergency rescue, the watchdog. Jev can bring a phase forward; it can never stop one of these firing.
- **Arithmetic:** shot size, duration, litres, rates, peaks and the clock.
- **Direction:** phase directives only ever move a zone *forward* through its day (P0 → P1 → P2 → P3),
  and only earlier than the base engine would; the base engine's own transitions always still happen.
- **Probes:** the last usable probe is never set aside by Jev alone: it takes the council, the code-confirmed
  evidence, and it expires.

## Failing safe

Jev is optional at every level: no Cloudflare credentials, the room's `switch.crop_steering_<prefix>jev_enabled`
off, a judge switched off in the add-on options, the daily budget spent, Jev slow or down, or an answer that
fails to parse: each means that judge's decisions are the base engine's, unchanged.

## Configuration

Add-on options (the controller app):

| Option | Default | What it does |
|---|---|---|
| `cf_account_id`, `cf_api_token`, `cf_gateway_id` | empty | Cloudflare account, a token with Workers AI access, and the AI Gateway (optional). Without the first two, Jev is off. |
| `jev_judges` | all | The judges that may act, e.g. `dawn,ramp,salt,dusk,probe,shot,night,zones,alerts`. |
| `jev_daily_calls` | 2000 | The day's call budget across rooms. |
