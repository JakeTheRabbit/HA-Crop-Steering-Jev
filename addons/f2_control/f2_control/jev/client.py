"""Asking Jev (TypeSafe System One) through Cloudflare's AI REST API without ever making the loop wait.

`Asker.submit()` queues a question set and returns at once; a single background worker calls Jev and
stores the answers under the caller's key. The control loop only ever reads `Asker.result()`: an answer
from an earlier pass, or None. Every failure (no credentials, budget spent, timeout, HTTP error, an
envelope that doesn't parse) is recorded and yields no answer, so the caller carries on as the base
engine would.
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

try:
    import requests
except ImportError:  # the lean test harness stubs it; the add-on image installs it
    requests = None

URL = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run"
MODEL = "typesafe/jev"


def parse(payload):
    """Cloudflare wraps TypeSafe's body as result.result: (answers, usage). Anything else: (None, None)."""
    try:
        body = payload["result"]["result"]
        answers = body["answers"]
    except (KeyError, TypeError):
        return None, None
    if not isinstance(answers, dict) or not answers:
        return None, None
    return answers, body.get("usage") or {}


def call(account, token, state, questions, gateway=None, timeout=20.0):
    """One Jev evaluation -> (answers, usage, error). Never raises."""
    if requests is None:
        return None, None, "requests not installed"
    headers = {"Authorization": f"Bearer {token}"}
    if gateway:
        headers["cf-aig-gateway-id"] = gateway
    body = {"model": MODEL, "input": {"state": state, "questions": questions}}
    try:
        resp = requests.post(URL.format(account=account), json=body, headers=headers, timeout=timeout)
    except Exception as e:  # noqa: BLE001 - a network fault of any kind is a missing answer
        return None, None, f"{type(e).__name__}: {e}"[:200]
    if resp.status_code != 200:
        return None, None, f"HTTP {resp.status_code}: {resp.text[:160]}"
    try:
        answers, usage = parse(resp.json())
    except ValueError:
        return None, None, "response is not JSON"
    if answers is None:
        return None, None, "no answers in the response"
    return answers, usage, None


@dataclass
class Answer:
    answers: dict
    at: float  # epoch seconds the answer arrived
    usage: dict = field(default_factory=dict)
    seq: int = 0  # unique per answer: two answers arriving in the same second are still two


class Asker:
    """Background Jev calls, keyed by the caller. `transport` is `call` in the add-on and a fake in tests;
    `threaded=False` runs each submission at once (tests, and one-shot scripts)."""

    def __init__(self, account, token, gateway=None, daily_budget=2000, transport=call,
                 threaded=True, clock=time.time, timeout=20.0):
        self.account, self.token, self.gateway = account, token, gateway
        self.daily_budget, self.transport, self.clock, self.timeout = daily_budget, transport, clock, timeout
        self._results: dict[str, Answer] = {}
        self._pending: set[str] = set()
        self._seq = 0
        self._lock = threading.Lock()
        self.stats = {"day": None, "calls": 0, "errors": 0, "input_tokens": 0, "last_error": None,
                      "last_call": None}
        self._queue = None
        if threaded and self.enabled:
            self._queue = queue.Queue()
            threading.Thread(target=self._work, name="jev-asker", daemon=True).start()

    @property
    def enabled(self):
        return bool(self.account and self.token)

    def _roll_day(self):
        day = datetime.fromtimestamp(self.clock()).date().isoformat()
        if self.stats["day"] != day:
            self.stats.update(day=day, calls=0, errors=0, input_tokens=0)

    def submit(self, key, state, questions):
        """Queue one evaluation. False when Jev is off, the day's budget is spent, or `key` is in flight."""
        if not self.enabled:
            return False
        with self._lock:
            self._roll_day()
            if key in self._pending or self.stats["calls"] >= self.daily_budget:
                return False
            self._pending.add(key)
            self.stats["calls"] += 1
        job = (key, state, questions)
        if self._queue is None:
            self._run(job)
        else:
            self._queue.put(job)
        return True

    def _work(self):
        while True:
            job = self._queue.get()
            try:
                self._run(job)
            except Exception as e:  # noqa: BLE001 - the worker must outlive any one call
                self._fail(job[0], f"{type(e).__name__}: {e}")

    def _run(self, job):
        key, state, questions = job
        answers, usage, error = self.transport(self.account, self.token, state, questions,
                                               gateway=self.gateway, timeout=self.timeout)
        if error or answers is None:
            self._fail(key, error or "no answers")
            return
        with self._lock:
            self._pending.discard(key)
            self._seq += 1
            self._results[key] = Answer(answers, self.clock(), usage or {}, self._seq)
            self.stats["input_tokens"] += int((usage or {}).get("input_tokens") or 0)
            self.stats["last_call"] = self.clock()

    def _fail(self, key, error):
        with self._lock:
            self._pending.discard(key)
            self.stats["errors"] += 1
            self.stats["last_error"] = str(error)[:200]

    def result(self, key):
        """The latest answer for `key`, or None."""
        with self._lock:
            return self._results.get(key)

    def pending(self, key):
        with self._lock:
            return key in self._pending
