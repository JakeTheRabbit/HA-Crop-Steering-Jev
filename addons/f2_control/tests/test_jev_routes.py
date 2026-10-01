"""Jev's routes and its asker: TypeSafe direct, then Cloudflare's /ai/run, each retried while a failure may pass
and inside a window, any failure moving on to the next route; the day's budget warning, a question that waited too
long, and the day's counts (docs/JEV.md, Availability)."""
import jev_kit as K
from jev import client
from jev.client import NO_ANSWERS, NO_REQUESTS, NOT_JSON, Route, Routes, retryable

ANSWERS = {"q__v0": K.noul_answer(0.9)}


class FakeClock:
    def __init__(self):
        self.t, self.slept = 0.0, []

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


class Flaky:
    """A route that fails with `errors` in turn, then answers; each call takes `takes` seconds on `clock`."""

    def __init__(self, *errors, clock=None, takes=0.0):
        self.errors, self.calls, self.clock, self.takes = list(errors), [], clock, takes

    def __call__(self, account, token, state, questions, gateway=None, timeout=20.0):
        self.calls.append((account, token, gateway))
        if self.clock is not None:
            self.clock.t += self.takes
        if self.errors:
            return None, None, self.errors.pop(0)
        return dict(ANSWERS), {"input_tokens": 900}, None


def _routes(*fns, tries=3, clock=None):
    clock = clock or FakeClock()
    named = [Route(name, fn, f"{name}-acct", f"{name}-key") for name, fn in zip(("TypeSafe", "Cloudflare"), fns)]
    return Routes(named, tries=tries, sleep=clock.sleep, clock=clock), clock


def test_what_is_worth_another_try():
    for error in ("HTTP 429: slow down", "HTTP 500: boom", "HTTP 503: busy", "HTTP 529: overloaded",
                  "ReadTimeout: read timed out", "ConnectionError: refused"):
        assert retryable(error), error
    for error in ("HTTP 400: bad", "HTTP 401: bad key", "HTTP 403: no", "HTTP 404: none",
                  NOT_JSON, NO_ANSWERS, NO_REQUESTS):
        assert not retryable(error), error


def test_a_server_error_is_tried_again_with_backoff_then_answers():
    ts = Flaky("HTTP 503: busy", "HTTP 529: overloaded")
    send, clock = _routes(ts)
    answers, usage, error = send("ignored", "ignored", {"s": 1}, {"q__v0": {}})
    assert error is None and answers == ANSWERS and len(ts.calls) == 3
    assert (usage["route"], usage["failover"], usage["retries"], usage["input_tokens"]) == ("TypeSafe", False, 2, 900)
    assert clock.slept == [2.0, 4.0]
    assert ts.calls[0] == ("TypeSafe-acct", "TypeSafe-key", None)  # each route uses its own credentials


def test_a_refused_key_moves_straight_to_the_next_route():
    ts, cf = Flaky("HTTP 401: bad key"), Flaky()
    send, clock = _routes(ts, cf)
    answers, usage, error = send("", "", {}, {})
    assert error is None and len(ts.calls) == 1 and len(cf.calls) == 1
    assert (usage["route"], usage["failover"], usage["retries"]) == ("Cloudflare", True, 0)
    assert clock.slept == []


def test_three_server_errors_then_the_next_route():
    ts, cf = Flaky("HTTP 500: a", "HTTP 502: b", "HTTP 503: c"), Flaky()
    send, _clock = _routes(ts, cf)
    answers, usage, error = send("", "", {}, {})
    assert len(ts.calls) == 3 and len(cf.calls) == 1
    assert (usage["route"], usage["failover"], usage["retries"]) == ("Cloudflare", True, 2)


def test_timeouts_stop_once_another_try_would_pass_the_window():
    clock = FakeClock()
    ts = Flaky("ReadTimeout: 20 s", "ReadTimeout: 20 s", "ReadTimeout: 20 s", clock=clock, takes=20.0)
    send, _ = _routes(ts, clock=clock)
    answers, usage, error = send("", "", {}, {}, timeout=20.0)
    assert answers is None and len(ts.calls) == 2  # 20 s, a 2 s wait, 20 s: a third try could end past 60 s
    assert error == "TypeSafe: ReadTimeout: 20 s"


def test_every_route_failing_names_each_one():
    send, _clock = _routes(Flaky("HTTP 401: bad key"), Flaky("HTTP 403: no access"))
    answers, usage, error = send("", "", {}, {})
    assert (answers, usage) == (None, None)
    assert error == "TypeSafe: HTTP 401: bad key; Cloudflare: HTTP 403: no access"


def test_typesafe_direct_makes_one_try(monkeypatch):
    """The TypeSafe call no longer retries a 429 itself: Routes retries every route alike."""
    posts = []

    class Resp:
        status_code, text = 429, "slow down"

    class Requests:
        @staticmethod
        def post(*args, **kwargs):
            posts.append(1)
            return Resp()

    monkeypatch.setattr(client, "requests", Requests)
    monkeypatch.setattr(client.time, "sleep", lambda s: posts.append(f"slept {s}"))
    assert client.call_typesafe("typesafe", "key", {}, {})[2] == "HTTP 429: slow down"
    assert posts == [1]
