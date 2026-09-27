"""Runs the judges for every zone on every pass, without waiting for any of them.

On each pass `tick()` asks the judges that are due (in the background), reads the answers that have
arrived, turns each NEW answer into a directive exactly once, puts the standing directive through the
envelope (the zone changes between passes, so admission is checked every pass), records what acted in
the ledger, and returns the admitted directives for the controller to apply. Nothing here raises into the
caller: a broken judge is skipped and logged in `errors`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import council
from . import journal as jn
from .envelope import admit


@dataclass
class JudgeState:
    last_asked: datetime | None = None
    seen_seq: int | None = None  # the answer already read
    verdicts: dict = field(default_factory=dict)
    directive: object = None  # what that answer asks for (decided once, when it arrived)
    streak: int = 0
    streak_key: object = None
    acted_seq: int | None = None  # the answer the ledger last recorded an action for
    last: dict = field(default_factory=dict)  # what the zone's sensor shows


def _label(d):
    """What a directive is filed under in the ledger: its phase or mode, an alert's code, or a setpoint move's
    choice (e.g. "smaller_shots")."""
    if isinstance(d.value, dict):
        return d.value.get("code") or d.value.get("choice")
    return d.value


class Brain:
    def __init__(self, asker, ledger, judges, allowed=None, log=print):
        self.asker, self.ledger, self.log = asker, ledger, log
        self.judges = [j for j in judges if allowed is None or j.name in allowed]
        self.state: dict[tuple, JudgeState] = {}
        self.errors: dict[str, str] = {}
        self.triage = None
        self.journal = None  # jev.journal.Journal: every decision, for the dashboard's live log

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
        new = ans.seq != st.seen_seq
        if new:  # a new answer: decide on it once
            st.seen_seq = ans.seq
            st.verdicts = {q: council.combine(ans.answers, q) for q in judge.questions}
            st.directive = judge.decide(st.verdicts, ctx)
            k = None if st.directive is None else (st.directive.kind, str(_label(st.directive)))
            st.streak = st.streak + 1 if (k is not None and k == st.streak_key) else (1 if k else 0)
            st.streak_key = k
        d = st.directive
        if ctx.phase not in judge.phases:
            st.last = self._shown(st, None, "waiting for its phase")
            if new:
                self._note(ctx, judge, st, d, "waiting", "the zone has left the judge's phase")
            return None
        age = self.asker.clock() - ans.at
        if age > judge.max_age_min * 60.0:
            st.last = self._shown(st, None, "answer too old to act on")
            if new:
                self._note(ctx, judge, st, d, "waiting", "the answer came too late to act on")
            return None
        if d is None:
            st.last = self._shown(st, None, "no action")
            if new:
                self._note(ctx, judge, st, None, "no action", "")
            return None
        ok, why = admit(d, ctx, confirmed=st.streak, evidence_ok=judge.evidence_ok(ctx))
        st.last = self._shown(st, d, why if ok else f"refused: {why}")
        if not ok:
            if new:
                self._note(ctx, judge, st, d, "refused", why)
            return None
        if st.acted_seq != ans.seq:
            st.acted_seq = ans.seq
            check_at = (ctx.now + timedelta(minutes=judge.outcome_after_min)
                        if judge.outcome_after_min else None)
            self.ledger.record(judge.name, ctx.room, ctx.zone, _label(d), d.kind,
                               evidence={q: (v.label if v else None) for q, v in st.verdicts.items()},
                               check_at=check_at, check=self._check_basis(ctx), at=ctx.now)
            self.log(f"[jev] {ctx.room} Z{ctx.zone} {judge.name}: {d.kind} {_label(d)} ({d.why}; {why})")
            self._note(ctx, judge, st, d, "acted", why)
        return d, why

    def _note(self, ctx, judge, st, d, result, reason):
        """One line in the journal (the dashboard's live log). Never raises into the pass."""
        if self.journal is None:
            return
        try:
            self.journal.add(jn.decision(ctx.room, ctx.zone, judge.name, ctx.now, st.verdicts, d, result, reason))
        except Exception as e:  # noqa: BLE001 - a log line is never worth a decision
            self.errors["journal"] = f"{type(e).__name__}: {e}"[:200]

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
                if self.journal is not None:
                    self.journal.add(jn.outcome(ctx.room, ctx.zone, entry["judge"], ctx.now, entry["label"],
                                                result[0], result[1], of=entry.get("at"),
                                                action=entry.get("action")))

    @staticmethod
    def _shown(st, d, why):
        verdicts = {}
        for q, v in st.verdicts.items():
            if v is not None:
                verdicts[q] = {"answer": v.label, "p": v.prob, "agreed": v.agreed}
        return {"verdicts": verdicts, "directive": None if d is None else f"{d.kind} {_label(d)}",
                "why": why, "streak": st.streak}

    def zone_status(self, room, zone):
        """What the zone's Jev sensor shows: every judge's latest verdicts and what code did."""
        return {name: st.last for (r, z, name), st in self.state.items() if r == room and z == zone and st.last}
