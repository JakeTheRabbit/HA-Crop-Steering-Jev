"""The controller's side of Jev: build the brain, feed it each zone's pass, apply what it admits.

Every function here is called inside a try/except in the controller: whatever goes wrong in Jev, the
zone is decided and watered by the base engine exactly as it would be without it (docs/JEV.md).
"""
from __future__ import annotations

import dataclasses
import os
from datetime import datetime

from jev.brain import Brain
from jev.client import Asker, call_typesafe
from jev.context import Shot, ZoneContext, ZoneHistory, response, words_response
from jev.doctrine import stage_intent
from jev.judges.dawn import DawnJudge
from jev.judges.dusk import DuskJudge
from jev.judges.night import NightJudge
from jev.judges.probe import ProbeJudge
from jev.judges.ramp import RampJudge
from jev.judges.salt import SaltJudge
from jev.judges.shot import ShotJudge
from jev.judges.stage import StageJudge
from jev.judges.zones import ZonesJudge
from jev.ledger import Ledger
from jev.triage import Triage

ALL = ("dawn", "ramp", "salt", "dusk", "probe", "shot", "night", "zones", "stage", "alerts")


def judges():
    return [DawnJudge(), RampJudge(), SaltJudge(), DuskJudge(), ProbeJudge(), ShotJudge(), NightJudge(),
            ZonesJudge(), StageJudge()]


def build(options, cf, state_path, log):
    """The brain, or None when Jev is off (no TypeSafe key and no Cloudflare credentials, or
    `jev_enabled` false). A TypeSafe API key is used when set; otherwise Cloudflare's Workers AI."""
    account, token, gateway = cf
    typesafe = str(options.get("typesafe_api_key") or "").strip()
    if options.get("jev_enabled", True) is False or not (typesafe or (account and token)):
        return None
    wanted = str(options.get("jev_judges") or "all").replace(" ", "")
    allowed = set(ALL) if wanted in ("", "all") else set(wanted.split(",")) & set(ALL)
    budget = int(options.get("jev_daily_calls", 2000))
    if typesafe:
        asker = Asker("typesafe", typesafe, None, daily_budget=budget, transport=call_typesafe)
        via = "TypeSafe"
    else:
        asker = Asker(account, token, gateway or None, daily_budget=budget)
        via = "Cloudflare Workers AI"
    ledger = Ledger(os.path.join(os.path.dirname(state_path) or ".", "jev_ledger.jsonl"))
    log(f"jev: on via {via}, judges {', '.join(sorted(allowed))}, {asker.daily_budget} calls a day")
    brain = Brain(asker, ledger, judges(), allowed=allowed, log=log)
    brain.triage = Triage(asker) if "alerts" in allowed else None
    brain.flower_start = flower_option(options.get("jev_flower_start"))
    brain.flower_days = int(options.get("jev_flower_days") or 56)
    return brain


def flower_option(value):
    """`jev_flower_start`: one entity or date for every room, or `room=value` pairs, comma separated.
    e.g. "input_datetime.flip" or "default=input_datetime.f2_flip_date, f1=input_datetime.f1_flip_date"."""
    out = {}
    for part in str(value or "").split(","):
        part = part.strip()
        if not part:
            continue
        room, sep, val = part.partition("=")
        out[room.strip() if sep else "*"] = (val if sep else room).strip()
    return out


def flower_day(c, room, now):
    """Day of flower for the room (day 1 = the first day of 12/12), or None when the room doesn't say."""
    brain = c.jev
    val = getattr(brain, "flower_start", {}).get(room.slug) or getattr(brain, "flower_start", {}).get("*")
    if not val:
        return None
    raw = c._jev_read(val) if "." in val and not val[:4].isdigit() else val
    try:
        start = datetime.fromisoformat(str(raw).strip()[:10]).date()
    except (TypeError, ValueError):
        return None
    day = (now.date() - start).days + 1
    return day if day >= 1 else None


def _hist(room, zone):
    store = room.__dict__.setdefault("_jev_hist", {})
    return store.setdefault(zone, ZoneHistory())


def zone_context(c, room, zone, snap, p, now, lights_on):
    st = room.state[zone]
    plants = c._zone_num(room, zone, "plant_count", 0)
    fday = flower_day(c, room, now)
    fdays = getattr(c.jev, "flower_days", 56)
    return ZoneContext(
        flower_day=fday, flower_days=fdays, stage=stage_intent(fday, fdays),
        steering="vegetative" if c._veg(room, zone) else "generative",
        room=room.slug, prefix=room.prefix, zone=zone, title=c._zone_title(room, zone), now=now,
        phase=st["phase"], snap=snap, params=p, history=_hist(room, zone), lights_on=lights_on,
        hours_to_on=c._hours_to(now, room.lights_on_hour), hours_to_off=c._hours_to(now, room.lights_off_hour),
        siblings={z: v for z, v in room.__dict__.get("_jev_sib", {}).items() if z != zone},
        plants=int(plants) if plants and float(plants).is_integer() else None,
        feed_ec=getattr(snap, "feed_ec", None) if snap is not None else None,
        probe_entity=c._fused_id(room.prefix, "vwc", zone, room.zones[zone].get("vwc")),
    )


def tick(c, room, zone, snap, p, now, lights_on):
    """Record this pass's reading, run the judges, raise their alerts. -> {kind: Directive}."""
    _hist(room, zone).add_reading(now, snap.vwc, snap.ec)
    ctx = zone_context(c, room, zone, snap, p, now, lights_on)
    out = {}
    for d, _why in c.jev.tick(ctx):
        if d.kind == "alert":
            c._jev_alert(room, zone, d.judge, d.value)
        else:
            out[d.kind] = d
    return out


def advance(c, room, zone, snap, d, now):
    """Move the zone forward one phase as Jev judged, with the bookkeeping the engine does itself."""
    st = room.state[zone]
    st["phase"] = d.value
    st["last_phase_change"] = now
    if d.value == "P1":
        st["shots"] = 0
    c._save_state()
    return dataclasses.replace(snap, phase=d.value, phase_minutes=0.0,
                               **({"shot_count": 0} if d.value == "P1" else {}))


def shot_started(room, zone, snap, kind):
    room.__dict__.setdefault("_jev_shot", {})[zone] = (
        datetime.now(), kind, None if snap is None else snap.vwc, None if snap is None else snap.ec)


def shot_counted(room, zone, litres):
    start = room.__dict__.get("_jev_shot", {}).pop(zone, None)
    if start is None:
        return
    t0, kind, pre_v, pre_e = start
    _hist(room, zone).add_shot(Shot(t0, (datetime.now() - t0).total_seconds(), float(litres or 0.0),
                                    str(kind or "shot"), pre_v, pre_e))


def siblings(c, room, snaps, params, now):
    """Each zone's water per plant, reading and latest settled shot, for its siblings' judges next pass."""
    out = {}
    for zone in room.zones:
        v = {}
        plants = c._zone_num(room, zone, "plant_count", 0)
        if plants and plants >= 1:
            lpp = room.state[zone]["daily_vol"] / plants
            v["litres_per_plant"] = lpp
            v["litres_per_plant_words"] = f"{lpp * 1000:.0f} mL a plant today"
        s, p = snaps.get(zone), params.get(zone)
        if s is not None:
            v["vwc_words"] = f"VWC {s.vwc:.1f}%, drying {s.dryback_rate:.2f} points an hour"
        if p is not None:
            v["threshold_words"] = f"re-waters under {p.p2_threshold:.1f}%, shot {p.p2_shot_size:.1f}%"
        h = room.__dict__.get("_jev_hist", {}).get(zone)
        if h and h.shots:
            settled = [r for r in (response(h, sh, now) for sh in h.shots) if r.settled and r.retained is not None]
            if settled:
                v["rise"] = settled[-1].retained
                v["rise_words"] = words_response(settled[-1])
        if v:
            out[zone] = v
    room._jev_sib = out


def publish(c, room, now, ha_set):
    brain = c.jev
    s = brain.asker.stats
    for zone in room.zones:
        status = brain.zone_status(room.slug, zone)
        acting = sorted(n for n, v in status.items() if v.get("directive") and not str(v.get("why", "")).startswith("refused"))
        ha_set(f"sensor.crop_steering_{room.prefix}zone_{zone}_jev", ", ".join(acting) or "watching",
               {"judges": status, "friendly_name": f"Zone {zone} Jev", "engine": "f2-control"})
    ha_set(f"sensor.crop_steering_{room.prefix}jev", "error" if s.get("last_error") and not s.get("last_call") else "on",
           {"calls_today": s.get("calls"), "input_tokens_today": s.get("input_tokens"),
            "errors_today": s.get("errors"), "last_error": s.get("last_error"),
            "judges": sorted(j.name for j in brain.judges), "judge_errors": dict(brain.errors),
            "friendly_name": "Jev", "engine": "f2-control"})
