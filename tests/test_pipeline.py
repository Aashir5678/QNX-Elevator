#!/usr/bin/env python3
"""Integration tests for the dispatcher <-> vision/motor FIFO pipeline.

These codify the behaviours that were previously verified by hand-injecting
messages. Nothing here is mocked: real named FIFOs, the real ipc.FifoReader /
FifoWriter, and the real Dispatcher class. Only the clock is ours, in the same
spirit as sim/simulate.py.

    python3 tests/test_pipeline.py      (or: make test-pipeline)

Each test runs against a fresh temporary FIFO directory, so runs are isolated
and leave nothing behind in /tmp/elevator.
"""

import os
import shutil
import stat
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

# ipc reads ELEVATOR_FIFO_DIR at import time, so it must be set before the
# import below -- otherwise the tests would scribble in the real /tmp/elevator.
FIFO_DIR = tempfile.mkdtemp(prefix="elevator-test-")
os.environ["ELEVATOR_FIFO_DIR"] = FIFO_DIR

import core  # noqa: E402
import ipc  # noqa: E402
from dispatcher import IDLE, MOVING, POLL_INTERVAL, Dispatcher  # noqa: E402

# How long to let a FIFO write land before the reader polls for it. Generous
# relative to a same-host pipe write, which is effectively instantaneous.
SETTLE = 0.05

failures = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name:<52} {got!r}")
    else:
        print(f"  FAIL {name:<52} got {got!r} want {want!r}")
        failures.append(name)


def check_true(name, cond):
    check(name, bool(cond), True)


class Harness:
    """Wires up both ends of every FIFO the dispatcher talks to.

    Readers are opened first, in the same order dispatcher.main() opens them.
    A FifoWriter drops its message if no reader is attached yet, so the
    peer-side readers for target/served must exist before the dispatcher can
    successfully publish -- that mirrors motor_control and floor_input being
    up, and is exactly the startup-order constraint TESTING.md documents.
    """

    def __init__(self, aging_factor=core.AGING_FACTOR):
        ipc.ensure_fifos()

        # Dispatcher's own inputs.
        self.heads_in = ipc.FifoReader(ipc.FIFO_HEADS)
        self.calls_in = ipc.FifoReader(ipc.FIFO_CALLS)
        self.arrived_in = ipc.FifoReader(ipc.FIFO_ARRIVED)

        # Peer readers: motor_control's target, floor_input's served.
        self.target_in = ipc.FifoReader(ipc.FIFO_TARGET)
        self.served_in = ipc.FifoReader(ipc.FIFO_SERVED)

        # Producer ends.
        self.heads_out = ipc.FifoWriter(ipc.FIFO_HEADS)
        self.calls_out = ipc.FifoWriter(ipc.FIFO_CALLS)
        self.arrived_out = ipc.FifoWriter(ipc.FIFO_ARRIVED)

        # Dispatcher's own outputs.
        self.target_out = ipc.FifoWriter(ipc.FIFO_TARGET)
        self.served_out = ipc.FifoWriter(ipc.FIFO_SERVED)

        self.d = Dispatcher(aging_factor=aging_factor, verbose=False)

    def send_heads(self, heads):
        return self.heads_out.send({"heads": {str(k): v for k, v in heads.items()}})

    def send_calls(self, calls):
        """calls: {floor: since_timestamp_or_None}. None means inactive."""
        return self.calls_out.send(
            {
                "calls": {
                    str(f): {"active": since is not None, "since": since}
                    for f, since in calls.items()
                }
            }
        )

    def send_arrived(self, floor):
        return self.arrived_out.send({"arrived": floor})

    def tick(self, now=None):
        """One iteration of dispatcher.main()'s loop, over real FIFOs."""
        time.sleep(SETTLE)
        for msg in self.heads_in.poll():
            self.d.on_heads(msg)
        for msg in self.calls_in.poll():
            self.d.on_calls(msg)
        return self.d.tick(
            time.time() if now is None else now,
            self.target_out,
            self.served_out,
            self.arrived_in.poll(),
        )

    def targets(self):
        time.sleep(SETTLE)
        return [m.get("target") for m in self.target_in.poll()]

    def served(self):
        time.sleep(SETTLE)
        return [m.get("served") for m in self.served_in.poll()]

    def close(self):
        for c in (
            self.heads_in, self.calls_in, self.arrived_in,
            self.target_in, self.served_in,
            self.heads_out, self.calls_out, self.arrived_out,
            self.target_out, self.served_out,
        ):
            c.close()


# --------------------------------------------------------------------------


def test_ensure_fifos_creates_everything():
    """dispatcher.main() calls ensure_fifos() before opening anything.

    Every FIFO must exist as an actual FIFO afterwards -- a writer that
    attaches before creation would get ENOENT and silently drop messages.
    """
    print("\nensure_fifos creates all channels up front")
    for path in ipc.ALL_FIFOS:
        # Remove first, so we prove ensure_fifos creates rather than that a
        # previous test left it behind.
        if os.path.exists(path):
            os.unlink(path)

    ipc.ensure_fifos()

    for path in ipc.ALL_FIFOS:
        name = os.path.basename(path)
        exists = os.path.exists(path)
        is_fifo = exists and stat.S_ISFIFO(os.stat(path).st_mode)
        check(f"{name} exists and is a FIFO", is_fifo, True)

    check("all six channels created", len(ipc.ALL_FIFOS), 6)

    # And ensure_fifos must be idempotent -- every process calls it at startup.
    ipc.ensure_fifos()
    check("ensure_fifos is idempotent", True, True)

    check("a fresh dispatcher starts idle", Dispatcher(verbose=False).state, IDLE)


def test_heads_before_calls_credits_full_count():
    """Heads arriving before the call still count.

    vision_service publishes continuously and floor_input only on change, so
    the heads-first ordering is the common case, not an edge case. The head
    count must be retained and credited in full when the call shows up.
    """
    print("\nheads arriving BEFORE calls are credited in full")
    h = Harness()
    try:
        check("heads message accepted", h.send_heads({3: 4}), True)
        d = h.tick()
        check("no dispatch with heads but no call", d.target, None)
        check("head count retained across ticks", h.d.head_counts.get(3), 4)

        now = time.time()
        check("calls message accepted", h.send_calls({3: now}), True)
        d = h.tick(now=now)

        check("dispatched to floor 3", d.target, 3)
        score = next(s for s in d.scores if s.floor == 3)
        check("credited all 4 heads", score.credited_heads, 4)
        check("no aging yet", round(score.aging_bonus, 6), 0.0)
        check("priority is 4.00", round(score.priority, 2), 4.0)
        check("explain shows '4 heads'", "4 heads" in score.reason(), True)
        check("target published to motor_control", h.targets(), [3])
    finally:
        h.close()


def test_call_without_any_heads_dispatches_immediately():
    """A call with no vision data at all must still dispatch, at once.

    Two distinct guarantees:
      1. min-credited-heads-of-1 -- the button was physically pressed, so
         somebody is there even if vision has never reported on that floor.
      2. the dispatcher never blocks waiting for vision. It ticks on its own
         POLL_INTERVAL and decides with whatever it has.
    """
    print("\ncalls with NO heads data dispatch immediately, credited 1")
    h = Harness()
    try:
        check("no heads data at all", h.d.head_counts, {})

        now = time.time()
        h.send_calls({2: now})
        start = time.time()
        d = h.tick(now=now)
        elapsed = time.time() - start

        check("dispatched to floor 2", d.target, 2)
        score = next(s for s in d.scores if s.floor == 2)
        check("vision saw nothing", score.head_count, 0)
        check("credited the minimum of 1", score.credited_heads, 1)
        check("priority is 1.00", round(score.priority, 2), 1.0)
        check("still no heads data recorded", h.d.head_counts, {})
        check("target published", h.targets(), [2])

        # The decision must not have waited on a vision poll (~1s in
        # vision_service). One dispatcher tick is the upper bound.
        check_true(
            f"decided in {elapsed:.3f}s, under one poll interval",
            elapsed < POLL_INTERVAL + SETTLE + 0.2,
        )
    finally:
        h.close()


def test_no_preemption_once_moving():
    """A committed trip is not re-targeted by later, higher-priority news.

    This is deliberate: reversing a moving car mid-travel would be worse than
    finishing the trip. Someone watching a big head count appear on another
    floor and seeing the car keep going is seeing correct behaviour.
    """
    print("\nno preemption once a target is committed")
    h = Harness()
    try:
        now = time.time()
        h.send_calls({3: now})
        d = h.tick(now=now)
        check("committed to floor 3", d.target, 3)
        check("state is MOVING", h.d.state, MOVING)
        check("first target published", h.targets(), [3])

        # Floor 1 now looks far more attractive: a crowd, and an older call.
        h.send_heads({1: 99})
        h.send_calls({3: now, 1: now - 60.0})
        d = h.tick(now=now + 1.0)

        check("tick returns no new decision", d, None)
        check("still MOVING", h.d.state, MOVING)
        check("target unchanged", h.d.pending_target, 3)
        check("nothing new published to motor_control", h.targets(), [])
        check("the better option was received", h.d.head_counts.get(1), 99)

        # Completing the trip releases the car, and only then does floor 1 win.
        h.send_arrived(3)
        d = h.tick(now=now + 2.0)
        check("served notice sent for floor 3", h.served(), [3])
        check("now dispatches to floor 1", d.target, 1)
        check("state is MOVING again", h.d.state, MOVING)
        check("floor 1 target published", h.targets(), [1])
    finally:
        h.close()


def main():
    print(f"pipeline integration tests (FIFO dir: {FIFO_DIR})")
    try:
        test_ensure_fifos_creates_everything()
        test_heads_before_calls_credits_full_count()
        test_call_without_any_heads_dispatches_immediately()
        test_no_preemption_once_moving()
    finally:
        shutil.rmtree(FIFO_DIR, ignore_errors=True)

    if failures:
        print(f"\n{len(failures)} FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nall passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
