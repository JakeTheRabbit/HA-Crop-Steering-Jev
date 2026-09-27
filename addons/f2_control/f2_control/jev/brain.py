"""Runs the judges for every zone on every pass, without waiting for any of them.

On each pass `tick()` asks the judges that are due (in the background), reads the answers that have
arrived, turns them into directives, puts each through the envelope, records what acted in the ledger,
and returns the admitted directives for the controller to apply. Nothing here raises into the caller:
a broken judge is skipped and logged in `errors`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import council
from .envelope import admit


@dataclass
class JudgeState:
    last_asked: datetime | None = None
    seen_at: float | None = None
    verdicts: dict = field(default_factory=dict)
    streak: int = 0
    streak_key: object = None
    acted_at: float | None = None  # the answer the ledger last recorded an action for
    last: dict = field(default_factory=dict)  # what was shown on the zone's sensor


class Brain:
    def __init__(self, asker, ledger, judges, allowed=None, log=print):
        self.asker, self.ledger, self.log = asker, ledger, log
        self.judges = [j for j in judges if allowed is None or j.name in allowed]
        self.state: dict[tuple, JudgeState] = {}
        self.errors: dict[str, str] = {}

    @property
    def enabled(self):
        return self.asker.enabled and bool(self.judges)

    def _st(self, ctx, judge):
        return self.state.setdefault((ctx.room, ctx.zone, judge.name), JudgeState())

    def tick(self, ctx, allowed=None):
        """Admitted directives for this zone on this pass: [(Directive, why)]."""
        out = []
        if not self.enabled:
            return out
        for judge in self.judges:
            if allowed is not None and judge.name not in allowed:
                continue
            try:
                d = self._judge(judge, ctx)
                if d is not None:
                    out.append(d)
            except Exception as e:  # noqa: BLE001 - one broken judge never stops the others or the loop
                self.errors[judge.name] = f"{type(e).__name__}: {e}"[:200]
        try:
            self._outcomes(ctx)
        except Exception as e:  # noqa: BLE001
            self.errors["outcomes"] = f"{type(e).__name__}: {e}"[:200]
        return out

    def _judge(self, judge, ctx):
        st = self._st(ctx, judge)
        key = f"{ctx.room}:{ctx.zone}:{judge.name}"
        if judge.due(ctx, st.last_asked) and not self.asker.pending(key):
            ev = judge.evidence(ctx)
            record = self.ledger.track(judge.name, ctx.room, ctx.zone)
            if record:
                ev["your_track_record"] = record
            if self.asker.submit(key, ev, council.expand(judge.questions)):
                st.last_asked = ctx.now
        ans = self.asker.result(key)
        if ans is None:
            return None
        if ans.at != st.seen_at:  # a new answer: read it once
            st.seen_at = ans.at
            st.verdicts = {q: council.combine(ans.answers, q) for q in judge.questions}
            d = judge.decide(st.verdicts, ctx)
            k = None if d is None else (d.kind, str(d.value))
            st.streak = st.streak + 1 if (k is not None and k == st.streak_key) else (1 if k else 0)
            st.streak_key = k
        age = (datetime.fromtimestamp(self.asker.clock()) - datetime.fromtimestamp(ans.at))
        if age > timedelta(minutes=judge.max_age_min):
            st.last = self._shown(judge, st, None, "answer too old to act on")
            return None
        d = judge.decide(st.verdicts, ctx)
        if d is None:
            st.last = self._shown(judge, st, None, "no action")
            return None
        ok, why = admit(d, ctx, confirmed=st.streak, evidence_ok=judge.evidence_ok(ctx))
        st.last = self._shown(judge, st, d, why if ok else f"refused: {why}")
        if not ok:
            return None
        if st.acted_at != ans.at:
            st.acted_at = ans.at
            check_at = (ctx.now + timedelta(minutes=judge.outcome_after_min)
                        if judge.outcome_after_min else None)
            self.ledger.record(judge.name, ctx.room, ctx.zone, d.value, d.kind,
                               evidence={q: (v.label if v else None) for q, v in st.verdicts.items()},
                               check_at=check_at, check=self._check_basis(ctx))
            self.log(f"[jev] {ctx.room} Z{ctx.zone} {judge.name}: {d.kind} {d.value} ({d.why}; {why})")
        return d, why

    @staticmethod
    def _check_basis(ctx):
        s = ctx.snap
        if s is None:
            return {}
        ec = s.ec_settled if s.ec_settled is not None else s.ec
        return {"vwc": s.vwc, "ec": ec, "daily_vol": s.daily_vol, "phase": ctx.phase}

    def _outcomes(self, ctx):
        by_name = {j.name: j for j in self.judges}
        for entry in self.ledger.due(ctx.now):
            if entry["room"] != ctx.room or entry["zone"] != ctx.zone:
                continue
            judge = by_name.get(entry["judge"])
            result = judge.outcome(entry, ctx) if judge else ("judge no longer runs", False)
            if result is not None:
                self.ledger.resolve(entry["id"], result[0], result[1])

    @staticmethod
    def _shown(judge, st, d, why):
        verdicts = {}
        for q, v in st.verdicts.items():
            if v is not None:
                verdicts[q] = {"answer": v.label, "p": v.prob, "agreed": v.agreed}
        return {"verdicts": verdicts, "directive": None if d is None else f"{d.kind} {d.value}",
                "why": why, "streak": st.streak}

    def zone_status(self, room, zone):
        """What the zone's Jev sensor shows: every judge's latest verdicts and what code did."""
        return {name: st.last for (r, z, name), st in self.state.items() if r == room and z == zone and st.last}
