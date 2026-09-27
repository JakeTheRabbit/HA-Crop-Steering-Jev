"""The only things Jev can cause, and the code that admits or refuses each one.

A judge turns Jev's answer into a `Directive`. `admit()` then checks it against arithmetic Jev can't do
and limits Jev can't cross: the daily order of the phases, how far into a phase the zone is, the P3
emergency floor, the ramp's minimum shots, the evidence needed to set a probe aside. A refused directive
does nothing; the base engine decides as it always has.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
NEXT = {"P0": "P1", "P1": "P2", "P2": "P3"}


@dataclass(frozen=True)
class Directive:
    judge: str
    kind: str  # "advance" | "distrust" | "ec_mode" | "alert" | "push"
    value: object  # the phase to go to, an EC mode, an alert dict, True/False for a push
    why: str
    expires: datetime


def _p1_ceiling(p):
    return min(p.p1_target, p.field_capacity)


def admit(d: Directive, ctx, confirmed=0, evidence_ok=False):
    """(True, why) when code lets `d` act on this pass, else (False, why). `confirmed` is how many
    agreeing verdicts in a row stand behind it; `evidence_ok` is the judge's own code-checked evidence."""
    if ctx.now > d.expires:
        return False, "expired"
    snap, p = ctx.snap, ctx.params
    if d.kind == "advance":
        target = d.value
        if NEXT.get(ctx.phase) != target:
            return False, f"only {ctx.phase} -> {NEXT.get(ctx.phase)} may be brought forward"
        if snap is None:
            return False, "no usable probe: phases wait for the base engine"
        if not ctx.lights_on:
            return False, "lights off: the base engine owns the night"
        if target == "P1":
            half = p.dryback_target / 2.0
            quarter = p.p0_max_wait_min / 4.0
            if snap.dryback_pct < half and snap.phase_minutes < quarter:
                return False, (f"dryback {snap.dryback_pct:.1f}% under half the target ({half:.1f}%) "
                               f"and only {snap.phase_minutes:.0f} of {p.p0_max_wait_min:.0f} min waited")
            return True, "P0 far enough along"
        if target == "P2":
            if snap.shot_count < max(1, p.p1_min_shots):
                return False, f"only {snap.shot_count} ramp shots in (minimum {max(1, p.p1_min_shots)})"
            ceiling = _p1_ceiling(p)
            if snap.vwc < ceiling - 3.0:
                return False, f"VWC {snap.vwc:.1f} more than 3 points under the ramp ceiling {ceiling:.1f}"
            return True, "ramp near its ceiling with the minimum shots in"
        if target == "P3":
            if ctx.hours_to_off > 3.0:
                return False, f"{ctx.hours_to_off:.1f} h to lights-off: only the last 3 h"
            if snap.vwc < p.p3_emergency_floor + 3.0:
                return False, (f"VWC {snap.vwc:.1f} within 3 points of the P3 emergency floor "
                               f"{p.p3_emergency_floor:.1f}")
            return True, "in the last 3 h with room above the emergency floor"
        return False, "unknown phase"
    if d.kind == "distrust":
        if confirmed < 2:
            return False, f"{confirmed} agreeing verdict(s) in a row; 2 needed"
        if not evidence_ok:
            return False, "code has not confirmed it (no shot without a rise while a sibling rose, no flat line)"
        return True, "council twice in a row, confirmed by code"
    if d.kind == "ec_mode":
        if d.value not in ("steer", "hold", "decay"):
            return False, "unknown EC mode"
        if ctx.phase != "P2":
            return False, "the EC steer runs in P2"
        return True, "the EC steer stays inside its own clamp"
    if d.kind in ("alert", "push"):
        return True, "alerts never move water"
    return False, "unknown directive"
