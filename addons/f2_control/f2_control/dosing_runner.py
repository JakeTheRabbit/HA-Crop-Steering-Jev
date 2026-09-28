"""Batch-tank dosing for every room, on one thread of its own (docs/DOSING.md).

The integration keeps each room's dosing setup and its one request, and publishes them as
sensor.crop_steering_<prefix>dosing_config. This thread reads that every 2 s and takes each request
once. A dose sets a pump's volume number and presses its start: the pump's own firmware runs the
motor for that volume and switches it off. A batch holds the watering it could disturb, fills the
tank, mixes it, doses the recipe in order and stamps the fill. Nothing here ever switches a dosing
motor on; switching one off (a stop, a dose past its time) is the only thing done to its power.
What each room is doing is published as sensor.crop_steering_<prefix>dosing.

Whatever could be left running is written to dosing_state.json before anything moves, so the next
start switches it off (recover). The irrigation loop asks holds() before every shot. A batch sets
its hold under the lock a shot starts under (Controller._shot_lock): a shot either started before
it, and the batch waits for that shot to end, or never starts.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from copy import deepcopy
from datetime import datetime, timezone

POLL_S = 2.0  # each room's dosing_config is read this often
READ_S = 1.0  # a dosing pump is read this often during a dose
MAX_AGE_S = 120.0  # a request older than this is never acted on
HOLD_WAIT_S = 600.0  # a batch waits this long at most for a shot in flight in a room it holds
MIX_POWER_S = 20.0  # the mix pump must draw its power within this
# Read-back after a command, as the controller reads back a valve (Controller._confirm_switches).
CONFIRM_FIRST_S, CONFIRM_POLL_S, CONFIRM_TIMEOUT_S = 1.0, 0.5, 6.0
VOLUME_TOLERANCE_ML = 0.5
REFRESH_S = 30.0  # idle pumps' flow and state are read this often
REPUBLISH_S = 60.0  # an unchanged status is published again this often (with a new updated_at)
HISTORY = 20
DEAD = ("", "unknown", "unavailable", "none")
_RUNNING = {"dose": "dosing", "batch": "making a batch"}  # handled_result until it ends


class _Ended(Exception):
    """A batch ends here, short of finishing; the message says why."""


class _Exit(BaseException):
    """The app is stopping: leave whatever is recorded for the next start. Not an Exception, so no
    handler meant for a failure catches it."""


def _domain(entity):
    return entity.split(".", 1)[0]


def _fmt(value):
    return f"{value:g}" if isinstance(value, (int, float)) else str(value)


class _Job:
    """The one dose or batch this thread is running."""

    def __init__(self, room, config, request, kind):
        self.room, self.config, self.request, self.kind = room, config, request, kind
        self.stopped = False  # a stop request for this room arrived
        self.stopped_by = None
        self.deadline = None  # a batch's watchdog, on the runner's clock
        self.step = None  # the batch step running
        self.dose = None  # the dose running: pump, ml, started_at, expected_s
        self.pressed = None  # when the running dose's start was pressed, on the runner's clock
        self.doses = {}  # pump -> mL of every dose started, for the history
        self.started_at = None


class DosingRunner:
    def __init__(self, controller, *, get, call, publish, alert, state_path, log=print,
                 sleep=time.sleep, monotonic=time.monotonic, now=None):
        self.c = controller
        self._get, self._call, self._publish = get, call, publish
        self._raise, self._log = alert, log
        self._sleep, self._mono = sleep, monotonic
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.path = state_path
        self._lock = threading.RLock()  # guards everything below; never held across a Home Assistant call
        self._held = {}  # batch room slug -> the slugs of the rooms its batch holds
        self._stuck = {}  # batch room slug -> what did not read off when its batch ended (still held)
        self._job = None
        self._exiting = False
        self._recovered = False
        self._polled = None
        self._live = {}  # slug -> what its status shows that is not persisted
        self._doc = self._read()

    # ------------------------------------------------------------------ what the controller calls
    def holds(self, room):
        """"making a batch" while a batch holds this room's watering (docs/DOSING.md), else None."""
        with self._lock:
            held = any(room.slug in slugs for slugs in self._held.values())
        return "making a batch" if held else None

    def start(self):
        thread = threading.Thread(target=self._run, name="dosing", daemon=True)
        thread.start()
        return thread

    def recover(self):
        """At start-up: switch off whatever a dose or batch left recorded, clear the record and raise
        CS-803 (docs/DOSING.md, After a restart). Nothing is ever resumed. True once nothing is left;
        while Home Assistant can't be reached it is tried again on the thread's next pass."""
        if self._recovered:
            return True
        with self._lock:
            records = {
                slug: dict(block["inflight"])
                for slug, block in self._doc["rooms"].items()
                if isinstance(block, dict) and isinstance(block.get("inflight"), dict)
            }
        for slug, record in records.items():
            off = [e for e in record.get("off") or [] if isinstance(e, str) and "." in e]
            hold = record.get("hold") if isinstance(record.get("hold"), str) else None
            sent = [self._call(_domain(e), "turn_off", entity_id=e) for e in off]
            if hold:
                sent.append(self._call("input_boolean", "turn_off", entity_id=hold))
            if sent and not any(sent):
                return False  # Home Assistant is not answering yet
            stuck = self._confirm(off, "off")
            for e in stuck:
                self._call(_domain(e), "turn_off", entity_id=e)
            stuck = self._confirm(stuck, "off")
            kind = "batch" if record.get("kind") == "batch" else "dose"
            with self._lock:
                block = self._block(slug)
                block["inflight"] = None
                self._add_history(block, {
                    "kind": kind, "at": record.get("at"), "ended_at": self._now().isoformat(),
                    "result": "interrupted by a restart", "doses": record.get("doses") or {},
                    "by": record.get("by"),
                })
                self._write()
            room = next((r for r in list(self.c.rooms) if r.slug == slug), None)
            self._raise(
                room, "CS-803", "a dose or batch was interrupted by a restart",
                f"A {kind} that started {record.get('at')} was still running when the controller app "
                "stopped, so it did not finish, and the controller never resumes one. Every dosing "
                "pump's power, the fill valve, the mix pump and the mix valves were switched off and the "
                "hold was released"
                + (f", but {', '.join(stuck)} still do not read off: switch them off by hand now."
                   if stuck else ".")
                + " Check the tank: what was dosed before the stop is in it; dose the rest by hand or make "
                "a fresh batch.",
            )
            self._log(f"[{slug}] dosing: a {kind} interrupted by a restart was switched off")
        self._recovered = True
        return True

    def exit(self):
        """The app is stopping: switch off what a dose or batch has running without waiting to read it
        back, and keep its record, so the next start confirms it and raises CS-803."""
        self._exiting = True
        with self._lock:
            records = [
                dict(block["inflight"]) for block in self._doc["rooms"].values()
                if isinstance(block, dict) and isinstance(block.get("inflight"), dict)
            ]
        for record in records:
            for e in record.get("off") or []:
                self._call(_domain(e), "turn_off", entity_id=e)
            if record.get("hold"):
                self._call("input_boolean", "turn_off", entity_id=record["hold"])

    # ------------------------------------------------------------------ the thread
    def _run(self):
        while not self._exiting:
            try:
                if self.recover():
                    self.poll()
            except _Exit:
                return
            except Exception as e:  # the dosing thread must never die
                self._log("dosing: error", repr(e))
            self._sleep(POLL_S)

    def poll(self, busy=None):
        """Read every room's dosing_config once: take each new request, publish each room's status.
        `busy` is the dose or batch running (this is its pass between reads): then only a stop is
        acted on, and a dose or batch is refused."""
        self._polled = self._mono()
        self._release_stuck()
        self._send_draws()
        for room in list(self.c.rooms):
            config = self._config(room)
            request = config.get("request") if config else None
            if (isinstance(request, dict) and isinstance(request.get("id"), str)
                    and request["id"] != self._block(room)["handled"]):
                self._take(room, config, request, busy)
            self._show(room, config)

    def _take(self, room, config, request, busy):
        age = self._age(request)
        action = request.get("action")
        if age is None or age > MAX_AGE_S:
            return self._settle(room, config, request, "too old to act on")
        if action == "stop":
            return self._settle(room, config, request, self._stop(room, config, busy, request))
        if busy is not None:
            where = "" if busy.room.slug == room.slug else f" in room {busy.room.slug}"
            return self._settle(room, config, request, f"refused: a {busy.kind} is running{where}")
        with self._lock:
            stuck = self._stuck.get(room.slug)
        if stuck:
            return self._settle(room, config, request,
                                f"refused: {', '.join(stuck)} still reads on since the last batch")
        if action not in ("dose", "batch"):
            return self._settle(room, config, request, f"refused: unknown action {action!r}")
        with self._lock:  # taken: from here on it is handled, whatever comes of it
            block = self._block(room)
            block["handled"], block["handled_result"] = request["id"], _RUNNING[action]
            self._write()
        if action == "dose":
            return self._run_dose(room, config, request)
        return self._run_batch(room, config, request)

    def _stop(self, room, config, busy, request):
        """Every pump's power off; a dose or batch running in the room ends at its next check."""
        if busy is not None and busy.room.slug == room.slug:
            busy.stopped, busy.stopped_by = True, request.get("by")
        stuck = self._switch_off([p["power_entity"] for p in config["pumps"]])
        return "stopped" + (f"; {', '.join(stuck)} does not read off" if stuck else "")

    # ------------------------------------------------------------------ one dose
    def _run_dose(self, room, config, request):
        pump = next((p for p in config["pumps"] if p["id"] == request.get("pump")), None)
        if pump is None:
            return self._settle(room, config, request,
                                f"refused: there is no pump {request.get('pump')} in this room's setup")
        job = _Job(room, config, request, "dose")
        with self._lock:
            self._job = job
        try:
            result = self._dose(job, pump, request.get("ml"))
        finally:
            with self._lock:
                self._job = None
        entry = None
        if not result.startswith("refused"):
            entry = {"kind": "dose", "at": job.started_at, "ended_at": self._now().isoformat(),
                     "result": result, "doses": {pump["id"]: request.get("ml")}, "by": request.get("by")}
        self._settle(room, config, request, result, entry=entry, clear=True, final=True)

    def _dose(self, job, pump, ml):
        """One dose of `ml` mL through `pump` (docs/DOSING.md, One dose) -> "finished", "ran past its
        time", "not confirmed" or "stopped", or "refused: why" when nothing was pressed."""
        why, flow = self._dose_check(pump, ml)
        if why:
            return f"refused: {why}"
        volume = pump["volume_entity"]
        old = None
        if pump.get("restore_volume", True):
            old = self._number(volume)
            if old is None:
                return f"refused: {volume} can't be read, so it could not be put back"
        expected = ml / flow
        started = self._now().isoformat()
        job.started_at = job.started_at or started
        job.dose = {"pump": pump["id"], "ml": ml, "started_at": started,
                    "expected_s": round(expected, 1)}
        fields = {"dose": {"pump": pump["id"], "ml": ml, "at": started},
                  "doses": {**job.doses, pump["id"]: ml}}
        if job.kind == "dose":
            fields.update(kind="dose", at=started, request=job.request["id"],
                          by=job.request.get("by"), off=self._dosing_hardware(job.config),
                          hold=job.config["batch"].get("hold_entity"))
        if not self._record(job, fields, merge=job.kind == "batch"):
            job.dose = None
            return "refused: the dose could not be written to dosing_state.json"
        self._show(job.room, job.config)
        job.pressed = None
        try:
            result = self._run_one(job, pump, ml, flow, expected)
        finally:
            job.dose = None
            if old is not None:
                self._call(_domain(volume), "set_value", entity_id=volume, value=old)
        elapsed = self._mono() - job.pressed if job.pressed is not None else 0.0
        result = self._stock_draw(job, pump, ml, flow, result, elapsed, started)
        if not result.startswith("refused"):
            job.doses[pump["id"]] = ml
            with self._lock:
                self._block(job.room)["last"][pump["id"]] = {"ml": ml, "at": started, "result": result}
        return result

    def _stock_draw(self, job, pump, ml, flow, result, elapsed, started):
        """What the dose took from the pump's stock tank (docs/DOSING.md, Stock tanks): finished, the
        mL asked for; past its time or stopped, that or the seconds since the start press x the flow,
        whichever is less; not confirmed, nothing, and the result says so. Queued for stock_draw."""
        tank = pump.get("stock_tank")
        if not tank or result.startswith("refused"):
            return result
        if result == "not confirmed":
            return "not confirmed; nothing drawn from its stock tank"
        amount = ml if result == "finished" else min(ml, max(0.0, elapsed) * flow)
        if amount > 0:
            self._queue_draw(job.room, {
                "key": f"{job.room.prefix}:{pump['id']}:{started}",
                "room_id": f"room:{job.room.prefix}",
                "draws": {tank: round(amount, 1)},
                "source": job.kind,
                "note": f"{pump['name']}: {result}",
            })
        return result

    def _queue_draw(self, room, draw):
        """Saved before it is sent, so it survives Home Assistant being down or a restart."""
        with self._lock:
            block = self._block(room)
            if all(pending.get("key") != draw["key"] for pending in block["draws"]):
                block["draws"].append(draw)
            self._write()
        self._send_draws()

    def _send_draws(self):
        """Send every undelivered stock draw; each is dropped once the service answers. Sent again
        every tick until then: its key means the integration never counts one twice."""
        with self._lock:
            pending = [
                (slug, dict(draw)) for slug, block in self._doc["rooms"].items() if isinstance(block, dict)
                for draw in block.get("draws") or [] if isinstance(draw, dict) and draw.get("key")
            ]
        for slug, draw in pending:
            data = {k: draw[k] for k in ("room_id", "key", "draws", "source", "note") if draw.get(k) is not None}
            if not self._call("crop_steering", "stock_draw", **data):
                continue
            with self._lock:
                block = self._block(slug)
                block["draws"] = [d for d in block["draws"] if d.get("key") != draw["key"]]
                self._write()

    def _uncalibrated(self, pump):
        """Why the pump's flow can't be used, or None: it must read a number above 0 (a firmware
        that divides by a zero flow runs its motor for ever)."""
        flow = self._number(pump["flow_entity"])
        if flow is not None and flow > 0:
            return None
        reading = self._state(pump["flow_entity"])
        return f"{pump['name']} is not calibrated (flow {'missing' if reading is None else reading})"

    def _dose_check(self, pump, ml):
        """Why this dose must not start (docs/DOSING.md, One dose, step 1) and the pump's flow."""
        why = self._uncalibrated(pump)
        if why:
            return why, None
        flow = self._number(pump["flow_entity"])
        if (isinstance(ml, bool) or not isinstance(ml, (int, float)) or not math.isfinite(ml)
                or not 0 < ml <= float(pump.get("max_ml") or 0)):
            return f"{_fmt(ml)} mL is not more than 0 and at most {_fmt(pump.get('max_ml'))} mL", None
        if self._dosing(pump)[0] is True:
            return f"{pump['name']} already reads dosing", None
        return None, flow

    def _run_one(self, job, pump, ml, flow, expected):
        """Set the volume, read it back, press start, and watch the pump dose and stop."""
        volume, start = pump["volume_entity"], pump["start_entity"]
        self._call(_domain(volume), "set_value", entity_id=volume, value=ml)
        if not self._confirm_value(volume, ml):
            return f"refused: {volume} did not read back {_fmt(ml)} mL, so start was not pressed"
        domain = _domain(start)
        if not self._call(domain, "turn_on" if domain == "script" else "press", entity_id=start):
            self._log(f"[{job.room.slug}] dosing: pressing {start} failed; watching the pump anyway")
        pressed, pressed_at = self._mono(), self._now()
        job.pressed = pressed
        deadline, confirm_by = pressed + expected * 1.25 + 20, pressed + expected + 15
        batch = " The batch stops here." if job.kind == "batch" else ""
        seen = False
        while True:
            if self._exiting:
                raise _Exit()
            if job.stopped or (job.deadline is not None and self._mono() > job.deadline):
                if not job.stopped:  # a stop request has switched the power off already
                    self._switch_off([pump["power_entity"]])
                return "stopped"
            dosing, changed = self._dosing(pump)
            if dosing is True:
                seen = True
            elif dosing is False and (seen or (changed is not None and changed >= pressed_at)):
                # Seen dosing, or it went dosing and back between two reads: its state changed since.
                return "finished"
            now = self._mono()
            if not seen and now >= confirm_by:
                self._raise(
                    job.room, "CS-802", "a dose could not be confirmed",
                    f"{pump['name']} was asked for {_fmt(ml)} mL and its start was pressed, but "
                    f"{pump['dosing_entity']} never read dosing within {expected + 15:.0f} seconds, so "
                    "whether it dosed is not known. Check the pump is online and what its dosing sensor "
                    f"reports, and the tank, before dosing again.{batch}",
                )
                return "not confirmed"
            if now >= deadline:
                stuck = self._switch_off([pump["power_entity"]])
                self._raise(
                    job.room, "CS-801", "a dosing pump ran past its time and was switched off",
                    f"{pump['name']} was asked for {_fmt(ml)} mL, about {expected:.0f} seconds at its "
                    f"calibrated {_fmt(flow)} mL/s, and was still dosing after {expected * 1.25 + 20:.0f} "
                    f"seconds. Its power ({pump['power_entity']}) was switched off"
                    + (", but it still does not read off: switch it off by hand now." if stuck else ".")
                    + " The tank may have more than was asked for. Check the pump, its calibration and "
                    f"its firmware before dosing again.{batch}",
                )
                return "ran past its time"
            self._wait(job, READ_S)

    # ------------------------------------------------------------------ a batch
    def _run_batch(self, room, config, request):
        job = _Job(room, config, request, "batch")
        batch = config["batch"]
        why, plan = self._batch_check(job)
        if why:
            return self._settle(room, config, request, f"refused: {why}")
        held = self._held_rooms(room, batch)
        job.started_at = self._now().isoformat()
        record = {"kind": "batch", "at": job.started_at, "request": request["id"],
                  "by": request.get("by"), "off": self._dosing_hardware(config),
                  "hold": batch.get("hold_entity"), "held": sorted(held), "doses": {}}
        if not self._record(job, record):
            return self._settle(room, config, request,
                                "refused: the batch could not be written to dosing_state.json")
        job.deadline = self._mono() + self._watchdog(batch, plan)
        self._live_of(room)["batch"] = {"step": "idle", "pump": None, "started_at": job.started_at,
                                        "step_started_at": None, "steps": [], "result": None,
                                        "ended_at": None}
        with self._lock:
            self._job = job
        stuck = []
        try:
            self._hold(job, held)
            self._close(job, held)
            self._fill(job)
            self._mix(job)
            self._pause(job, "premix", batch.get("premix_min"))
            self._doses(job, plan)
            self._pause(job, "postmix", batch.get("postmix_min"))
            self._finish(job)
            result = "finished"
        except Exception as end:  # _Ended, or anything unforeseen: every way out cleans up the same
            if not isinstance(end, _Ended):
                self._log(f"[{room.slug}] dosing: batch error", repr(end))
            reason = str(end) if isinstance(end, _Ended) else f"an error in the controller ({end!r})"
            result = f"stopped: {job.step}: {reason}"
            try:
                stuck = self._ended(job, reason)
            except Exception as e:  # never hold the rooms for good: whatever may be on is tried again
                self._log(f"[{room.slug}] dosing: batch cleanup error", repr(e))
                stuck = [x for x in (*self._dosing_hardware(config), batch.get("hold_entity")) if x]
        finally:
            with self._lock:
                self._job = None
        view = self._live_of(room)["batch"]
        view.update(step="idle", pump=None, result=result, ended_at=self._now().isoformat())
        with self._lock:
            if stuck:
                self._stuck[room.slug] = stuck  # still held, and still recorded, until it reads off
            else:
                self._held.pop(room.slug, None)
        entry = {"kind": "batch", "at": job.started_at, "ended_at": view["ended_at"], "result": result,
                 "doses": dict(job.doses), "by": request.get("by")}
        self._settle(room, config, request, result, entry=entry, clear=not stuck, final=True)

    def _batch_check(self, job):
        """Why this batch must not start, else its doses: [(pump, ml, flow)] in recipe order, each
        recipe mL entity read now (it wins over the recipe's mL; 0 skips the pump)."""
        room, config = job.room, job.config
        batch = config["batch"]
        if self.c._hardware_fault_block(room):
            return "the room has a hardware fault latched", None
        hold = batch.get("hold_entity")
        if hold and str(self._state(hold)).lower() == "on":
            return f"{hold} is on already: something else is dosing", None
        pumps = {p["id"]: p for p in config["pumps"]}
        plan = []
        for item in batch.get("recipe") or []:
            pump = pumps.get(item.get("pump"))
            if pump is None:
                return f"the recipe doses {item.get('pump')}, which is not set up", None
            ml = item.get("ml") or 0
            if item.get("ml_entity"):  # it wins over the fixed mL, and is never replaced by it
                ml = self._number(item["ml_entity"])
                if ml is None:
                    return f"the recipe amount for {pump['name']} can't be read", None
            if ml <= 0:
                continue
            if ml > float(pump.get("max_ml") or 0):
                return f"{_fmt(ml)} mL of {pump['name']} is more than its {_fmt(pump['max_ml'])} mL", None
            why = self._uncalibrated(pump)
            if why:
                return why, None
            plan.append((pump, ml, self._number(pump["flow_entity"])))
        return None, plan

    def _held_rooms(self, room, batch):
        """The rooms a batch holds: its own, every room whose pump, main line or valves are among the
        batch's fill valve, mix pump, mix valves or switches to close, and every room that shares its
        pump or main line (closing them would cut that room's shot)."""
        hardware = {batch.get("fill_valve"), batch.get("mix_pump"), *(batch.get("mix_valves") or []),
                    *(batch.get("close_entities") or [])} - {None, ""}
        line = {room.hw.get("pump"), room.hw.get("mainline")} - {None, ""}
        held = {room.slug}
        for other in list(self.c.rooms):
            own = {other.hw.get("pump"), other.hw.get("mainline")} - {None, ""}
            valves = set((other.hw.get("valves") or {}).values()) - {None, ""}
            if (own | valves) & hardware or own & line:
                held.add(other.slug)
        return held

    @staticmethod
    def _watchdog(batch, plan):
        """Fill timeout + premix + postmix + every dose's deadline + 10 min, in seconds."""
        minutes = (float(batch.get("fill_timeout_min") or 20) + float(batch.get("premix_min") or 0)
                   + float(batch.get("postmix_min") or 0) + 10)
        return minutes * 60 + sum(ml / flow * 1.25 + 20 for _pump, ml, flow in plan)

    def _hold(self, job, held):
        self._begin(job, "hold")
        with self.c._shot_lock:  # the lock a shot starts under: see the module note
            with self._lock:
                self._held[job.room.slug] = set(held)
        hold = job.config["batch"].get("hold_entity")
        if hold:
            self._call("input_boolean", "turn_on", entity_id=hold)
        waited = self._mono()
        while self.c._shot_running(held):
            self._check(job)
            if self._mono() - waited >= HOLD_WAIT_S:
                raise _Ended("a shot was still running after 10 minutes")
            self._wait(job, POLL_S)
        self._done(job)

    def _close(self, job, held):
        self._begin(job, "close")
        targets = []
        for other in list(self.c.rooms):
            if other.slug in held:
                hw = other.hw
                targets += [v for _zone, v in sorted((hw.get("valves") or {}).items())]
                targets += [hw.get("mainline"), hw.get("pump")]  # valves first, then back up the line
        stuck = self._switch_off(targets + list(job.config["batch"].get("close_entities") or []))
        if stuck:
            raise _Ended(f"{', '.join(stuck)} did not read off")
        self._done(job)

    def _fill(self, job):
        batch = job.config["batch"]
        valve = batch.get("fill_valve")
        if not valve:
            return self._skip(job, "fill", "no fill valve: the tank is filled already")
        if self._full(batch):
            return self._skip(job, "fill", "the tank reads full already")
        self._begin(job, "fill")
        minutes = float(batch.get("fill_timeout_min") or 20)
        self._call(_domain(valve), "turn_on", entity_id=valve)
        began, filled = self._mono(), False
        while True:
            filled = self._full(batch)
            if filled or self._mono() - began >= minutes * 60:
                break
            self._check(job)
            self._wait(job, POLL_S)
        stuck = self._switch_off([valve])  # always closed, and read back
        if not filled:
            self._raise(
                job.room, "CS-804", "the batch tank did not fill in time",
                f"The fill valve ({valve}) was open for {minutes:g} minutes and {batch.get('full_entity')} "
                f"never read {batch.get('full_state') or 'on'}. The fill valve was switched off"
                + (", but it still does not read off: close it by hand now." if stuck else ".")
                + " The batch stops here. Check the water supply, the fill valve and the full sensor.",
            )
            raise _Ended(f"the tank did not read full within {minutes:g} min")
        if stuck:
            raise _Ended(f"{valve} did not read off after filling")
        self._done(job)

    def _mix(self, job):
        batch = job.config["batch"]
        pump = batch.get("mix_pump")
        if not pump:
            return self._skip(job, "mix", "no mix pump")
        self._begin(job, "mix")
        valves = list(batch.get("mix_valves") or [])
        for valve in valves:
            self._call(_domain(valve), "turn_on", entity_id=valve)
        closed = self._confirm(valves, "on")
        if closed:  # never run the pump against a closed valve
            raise _Ended(f"{', '.join(closed)} did not open")
        self._call(_domain(pump), "turn_on", entity_id=pump)
        watts, sensor = float(batch.get("mix_min_w") or 0), batch.get("mix_power_sensor")
        if watts > 0 and sensor:
            began = self._mono()
            while True:
                drawn = self._number(sensor)
                if drawn is not None and drawn >= watts:
                    break
                if self._mono() - began >= MIX_POWER_S:
                    self._raise(
                        job.room, "CS-805", "the mixing pump did not start",
                        f"The mix pump ({pump}) was switched on, but {sensor} did not read {watts:g} W "
                        f"within {MIX_POWER_S:.0f} seconds (it reads {self._state(sensor)}). The batch "
                        "stops here and the mix pump and valves are switched off. Check the pump, its "
                        "power sensor, and that the mix valves open.",
                    )
                    raise _Ended(f"the mix pump did not draw {watts:g} W within {MIX_POWER_S:.0f} s")
                self._check(job)
                self._wait(job, READ_S)
        self._done(job)

    def _pause(self, job, step, minutes):
        minutes = float(minutes or 0)
        if minutes <= 0:
            return self._skip(job, step, "0 min")
        self._begin(job, step)
        end = self._mono() + minutes * 60
        while self._mono() < end:
            self._check(job)
            self._wait(job, min(POLL_S, end - self._mono()))
        self._done(job)

    def _doses(self, job, plan):
        if not plan:
            return self._skip(job, "dose", "nothing to dose")
        self._begin(job, "dose")
        view = self._live_of(job.room)["batch"]
        for pump, ml, _flow in plan:
            view["pump"] = pump["id"]
            result = self._dose(job, pump, ml)
            view["pump"] = None
            if result != "finished":
                self._check(job)  # a stop or the watchdog says so itself
                raise _Ended(f"{pump['name']}: {result}")
        self._done(job)

    def _finish(self, job):
        batch = job.config["batch"]
        self._begin(job, "finish")
        stuck = self._switch_off([batch.get("mix_pump"), *(batch.get("mix_valves") or [])])
        if stuck:
            raise _Ended(f"{', '.join(stuck)} did not read off")
        if batch.get("filled_at_entity"):
            self._call("input_datetime", "set_datetime", entity_id=batch["filled_at_entity"],
                       timestamp=self._now().timestamp())
        if batch.get("hold_entity"):
            self._call("input_boolean", "turn_off", entity_id=batch["hold_entity"])
        self._done(job)

    def _ended(self, job, reason):
        """Any end but finishing: every dosing pump's power, the fill valve, the mix pump and the mix
        valves off, the hold released, then CS-806 -> what does not read off (the rooms stay held)."""
        batch = job.config["batch"]
        view = self._live_of(job.room)["batch"]
        if view["steps"] and view["steps"][-1]["state"] == "running":
            view["steps"][-1].update(state="failed", note=reason)
        stuck = self._switch_off(self._dosing_hardware(job.config))
        if batch.get("hold_entity"):
            self._call("input_boolean", "turn_off", entity_id=batch["hold_entity"])
        names = {pump["id"]: pump["name"] for pump in job.config["pumps"]}
        dosed = ", ".join(f"{_fmt(ml)} mL of {names.get(p, p)}" for p, ml in job.doses.items()) or "nothing"
        self._raise(
            job.room, "CS-806", "a batch was stopped",
            f"The batch stopped in its {job.step} step: {reason}. Every dosing pump's power, the fill "
            "valve, the mix pump and the mix valves were switched off and the hold released"
            + (f", but {', '.join(stuck)} still do not read off, so the rooms the batch held stay held "
               "until they do." if stuck else ", and the rooms it held water again.")
            + f" Dosed before it stopped: {dosed}. Check the tank before making another batch.",
        )
        self._log(f"[{job.room.slug}] dosing: batch stopped in {job.step}: {reason}")
        return stuck

    def _check(self, job):
        """End the batch now if it must: a stop request, its watchdog, or the app stopping."""
        if self._exiting:
            raise _Exit()
        if job.stopped:
            raise _Ended("stop requested" + (f" by {job.stopped_by}" if job.stopped_by else ""))
        if job.deadline is not None and self._mono() > job.deadline:
            raise _Ended("it ran past its watchdog")

    def _release_stuck(self):
        """A batch that ended with hardware not reading off holds its rooms until it does."""
        with self._lock:
            stuck = dict(self._stuck)
        for slug, entities in stuck.items():
            left = self._switch_off(entities)
            with self._lock:
                if left:
                    self._stuck[slug] = left
                    continue
                self._stuck.pop(slug, None)
                self._held.pop(slug, None)
                self._block(slug)["inflight"] = None
                self._write()
            self._log(f"[{slug}] dosing: {', '.join(entities)} read off now; the batch's hold is released")

    # ------------------------------------------------------------------ the batch's steps, shown
    def _begin(self, job, step):
        job.step = step
        now = self._now().isoformat()
        view = self._live_of(job.room)["batch"]
        view.update(step=step, step_started_at=now)
        view["steps"].append({"step": step, "state": "running", "at": now, "note": None})
        self._show(job.room, job.config)

    def _done(self, job):
        self._live_of(job.room)["batch"]["steps"][-1]["state"] = "done"
        self._show(job.room, job.config)

    def _skip(self, job, step, note):
        job.step = step
        self._live_of(job.room)["batch"]["steps"].append(
            {"step": step, "state": "skipped", "at": self._now().isoformat(), "note": note})
        self._show(job.room, job.config)

    # ------------------------------------------------------------------ status
    def _live_of(self, room):
        return self._live.setdefault(room.slug, {
            "readings": {}, "refreshed": None, "revision": None, "published": None, "published_at": None,
            "batch": {"step": "idle", "pump": None, "started_at": None, "step_started_at": None,
                      "steps": [], "result": None, "ended_at": None},
        })

    def _show(self, room, config):
        """Publish sensor.crop_steering_<prefix>dosing when it changed, and at least once a minute,
        with `updated_at` the time of this publish (the page tells a stale controller by it). Nothing
        is published for a room whose integration has no dosing_config (from before dosing)."""
        live = self._live_of(room)
        if config is None and live["published"] is None:
            return
        if config:
            self._refresh(config, live)
        state, attributes = self._status(room, config, live)
        now = self._mono()
        if live["published"] == (state, attributes) and now - live["published_at"] < REPUBLISH_S:
            return
        self._publish(f"sensor.crop_steering_{room.prefix}dosing", state,
                      {**attributes, "updated_at": self._now().isoformat()})
        live["published"], live["published_at"] = (state, deepcopy(attributes)), now

    def _refresh(self, config, live):
        """Each pump's flow and state, read again when the setup changes and every REFRESH_S."""
        now = self._mono()
        if live["revision"] == config["revision"] and live["refreshed"] is not None \
                and now - live["refreshed"] < REFRESH_S:
            return
        readings = {}
        for pump in config["pumps"]:
            dosing = self._dosing(pump)[0]
            readings[pump["id"]] = {
                "flow": self._number(pump["flow_entity"]),
                "state": "unavailable" if dosing is None else "dosing" if dosing else "idle",
            }
        live.update(readings=readings, refreshed=now, revision=config["revision"])

    def _status(self, room, config, live):
        with self._lock:
            block = deepcopy(self._block(room))
            job = self._job if self._job is not None and self._job.room.slug == room.slug else None
        pumps = {}
        for pump in config["pumps"] if config else []:
            reading = live["readings"].get(pump["id"], {})
            dose = job.dose if job is not None and job.dose and job.dose["pump"] == pump["id"] else None
            pumps[pump["id"]] = {
                "state": "dosing" if dose else reading.get("state", "idle"),
                "flow_ml_s": reading.get("flow"),
                "target_ml": dose["ml"] if dose else None,
                "started_at": dose["started_at"] if dose else None,
                "expected_s": dose["expected_s"] if dose else None,
                "last": block["last"].get(pump["id"]),
            }
        state = ("unavailable" if not config else "idle" if job is None
                 else "batch" if job.kind == "batch" else "dosing")
        return state, {
            "pumps": pumps,
            "batch": deepcopy(live["batch"]),
            "handled": block["handled"],
            "handled_result": block["handled_result"],
            "history": block["history"],
            "friendly_name": "Dosing (controller)",
        }

    # ------------------------------------------------------------------ reading Home Assistant
    def _config(self, room):
        """The room's dosing setup as the integration publishes it: None with no dosing_config at all
        (an integration from before dosing), False when it can't be read (never acted on), else
        {"revision", "pumps", "batch", "request"}."""
        state, attributes, _updated = self._get(f"sensor.crop_steering_{room.prefix}dosing_config")
        if state is None:
            return None
        if str(state).strip().lower() in DEAD or not isinstance(attributes, dict):
            return False
        pumps, batch = attributes.get("pumps"), attributes.get("batch")
        if not (isinstance(pumps, list) and all(self._pump_readable(p) for p in pumps)
                and isinstance(batch, dict) and self._batch_readable(batch)):
            return False
        return {"revision": state, "pumps": pumps, "batch": batch, "request": attributes.get("request")}

    @staticmethod
    def _pump_readable(pump):
        return isinstance(pump, dict) and all(
            isinstance(pump.get(key), str) and pump[key]
            for key in ("id", "name", "volume_entity", "start_entity", "dosing_entity", "power_entity",
                        "flow_entity")
        ) and isinstance(pump.get("max_ml"), (int, float))

    @staticmethod
    def _batch_readable(batch):
        lists = all(isinstance(batch.get(key) or [], list)
                    for key in ("mix_valves", "close_entities", "recipe"))
        return lists and all(isinstance(item, dict) for item in batch.get("recipe") or [])

    def _state(self, entity):
        if not entity:
            return None
        state = self._get(entity)[0]
        return None if state is None else str(state)

    def _number(self, entity):
        try:
            value = float(self._state(entity))
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    def _dosing(self, pump):
        """(True / False / None when it reads nothing usable, when its state last changed)."""
        read = self._get(pump["dosing_entity"])
        state, changed = read[0], getattr(read, "last_changed", None)
        try:
            changed = datetime.fromisoformat(str(changed).replace("Z", "+00:00"))
            changed = changed if changed.tzinfo else None
        except (TypeError, ValueError):
            changed = None
        if state is None or str(state).strip().lower() in DEAD:
            return None, changed
        if pump["dosing_entity"].startswith("binary_sensor."):
            return str(state).lower() == "on", changed
        return str(state).startswith(pump.get("dosing_prefix") or "Dosing"), changed

    def _full(self, batch):
        state = self._state(batch.get("full_entity"))
        return state is not None and state.strip().lower() == str(batch.get("full_state") or "on").strip().lower()

    def _age(self, request):
        try:
            at = datetime.fromisoformat(str(request.get("at")).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        return (self._now() - at).total_seconds() if at.tzinfo else None

    @staticmethod
    def _dosing_hardware(config):
        """What a stop, an interrupted batch or a restart switches off: every pump's power, then the
        fill valve, the mix pump and the mix valves."""
        batch = config["batch"]
        return [e for e in (*(p["power_entity"] for p in config["pumps"]), batch.get("fill_valve"),
                            batch.get("mix_pump"), *(batch.get("mix_valves") or [])) if e]

    # ------------------------------------------------------------------ switching, with read-back
    def _switch_off(self, entities):
        """Switch these off, read them back, re-send once to what does not read off -> whatever still
        does not (empty when every one reads off). The off is sent whatever a switch reads: a stale
        OFF must never be the reason a pump or valve was not told."""
        entities = [e for e in dict.fromkeys(entities) if e]
        for e in entities:
            self._call(_domain(e), "turn_off", entity_id=e)
        stuck = self._confirm(entities, "off")
        for e in stuck:
            self._call(_domain(e), "turn_off", entity_id=e)
        return self._confirm(stuck, "off")

    def _confirm(self, entities, want):
        """Read these back until each reads `want`: first at 1 s, then every 0.5 s up to 6 s -> the
        ones that don't."""
        if not entities:
            return []
        deadline = self._mono() + CONFIRM_TIMEOUT_S
        self._sleep(CONFIRM_FIRST_S)
        while True:
            left = [e for e in entities if self._state(e) != want]
            if not left or self._mono() >= deadline:
                return left
            self._sleep(CONFIRM_POLL_S)

    def _confirm_value(self, entity, value):
        deadline = self._mono() + CONFIRM_TIMEOUT_S
        self._sleep(CONFIRM_FIRST_S)
        while True:
            read = self._number(entity)
            if read is not None and abs(read - value) <= VOLUME_TOLERANCE_ML:
                return True
            if self._mono() >= deadline:
                return False
            self._sleep(CONFIRM_POLL_S)

    def _wait(self, job, seconds):
        """Sleep a moment, answering every room's requests on the way (a stop among them)."""
        if self._polled is None or self._mono() - self._polled >= POLL_S:
            self.poll(busy=job)
        self._sleep(seconds)

    # ------------------------------------------------------------------ dosing_state.json
    def _read(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except FileNotFoundError:
            doc = {}
        except (OSError, ValueError) as e:
            self._log("dosing: dosing_state.json can't be read, starting without it:", e)
            doc = {}
        rooms = doc.get("rooms") if isinstance(doc, dict) else None
        return {"rooms": {str(k): v for k, v in rooms.items() if isinstance(v, dict)}
                if isinstance(rooms, dict) else {}}

    def _write(self):
        """Atomically (tmp + os.replace); False when it could not be saved."""
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._doc, fh)
            os.replace(tmp, self.path)
            return True
        except (OSError, TypeError, ValueError) as e:
            self._log("dosing: dosing_state.json could not be saved:", e)
            return False

    def _block(self, room):
        """One room's saved part: the last request handled and its result, the dose or batch in
        flight, each pump's last dose, the history, and the stock draws not yet delivered."""
        slug = getattr(room, "slug", room)
        block = self._doc["rooms"].get(slug)
        if not isinstance(block, dict):
            block = self._doc["rooms"][slug] = {}
        for key, default in (("handled", None), ("handled_result", None), ("inflight", None)):
            block.setdefault(key, default)
        for key in ("history", "draws"):
            if not isinstance(block.get(key), list):
                block[key] = []
        if not isinstance(block.get("last"), dict):
            block["last"] = {}
        return block

    @staticmethod
    def _add_history(block, entry):
        block["history"] = [entry, *block["history"]][:HISTORY]

    def _record(self, job, fields, merge=False):
        """What is about to move, saved before it moves. A new record also takes the job's request, in
        the same write; one added to (a batch's next dose) leaves alone any request answered since."""
        with self._lock:
            block = self._block(job.room)
            record = dict(block["inflight"] or {}) if merge else {}
            record.update(fields)
            block["inflight"] = record
            if not merge:
                block["handled"], block["handled_result"] = job.request["id"], _RUNNING[job.kind]
            return self._write()

    def _settle(self, room, config, request, result, entry=None, clear=False, final=False):
        """A request handled: its id and result saved (and the record of what ran cleared) and shown.
        `final`: the end of the dose or batch it started, which leaves a request handled since (a stop,
        a refusal) as the one shown, so none is ever taken twice."""
        with self._lock:
            block = self._block(room)
            if not final or block["handled"] == request["id"]:
                block["handled"], block["handled_result"] = request["id"], result
            if clear:
                block["inflight"] = None
            if entry is not None:
                self._add_history(block, entry)
            self._write()
        self._log(f"[{room.slug}] dosing: {request.get('action')} request: {result}")
        self._show(room, config)
        return result
