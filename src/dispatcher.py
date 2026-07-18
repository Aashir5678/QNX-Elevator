#!/usr/bin/env python3
"""dispatcher -- picks which floor the car goes to next.

Reads:  head counts (vision_service), call state (floor_input),
        arrival confirmations (motor_control)
Writes: target floor (motor_control), served notifications (floor_input)

All the actual decision-making lives in core.py so the simulator exercises
exactly the same code path. This file is only plumbing and the state machine.
"""

import sys
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])

import core
import ipc

POLL_INTERVAL = 0.2  # seconds

IDLE = "idle"
MOVING = "moving"


class Dispatcher:
    def __init__(self, aging_factor=core.AGING_FACTOR, verbose=True):
        self.aging_factor = aging_factor
        self.verbose = verbose
        self.calls = {}
        self.head_counts = {}
        self.state = IDLE
        self.pending_target = None

    # -- state updates from upstream producers ----------------------------

    def on_heads(self, msg):
        """{"heads": {"1": 0, "2": 3}} -- floors omitted are suppressed ROIs.

        Merged, not replaced. vision_service omits the ROI the car occupies,
        and the car parks at the floor it just served -- so a plain replace
        would drop that floor's count to zero for exactly as long as the car
        sits there, blinding us to a crowd building up underneath it. Holding
        the last known value keeps the floor competitive until vision can see
        it again.
        """
        heads = msg.get("heads")
        if not isinstance(heads, dict):
            return
        for k, v in heads.items():
            self.head_counts[int(k)] = int(v)

    def on_calls(self, msg):
        """{"calls": {"2": {"active": true, "since": 1234.5}}} -- full state."""
        calls = msg.get("calls")
        if not isinstance(calls, dict):
            return
        self.calls = {
            int(floor): core.FloorCall(
                floor=int(floor),
                active_call=bool(c.get("active")),
                wait_start=c.get("since"),
            )
            for floor, c in calls.items()
        }

    # -- the state machine -------------------------------------------------

    def tick(self, now, out_target, out_served, arrivals):
        for msg in arrivals:
            floor = msg.get("arrived")
            if floor is None:
                continue
            if self.state == MOVING and int(floor) == self.pending_target:
                # Clear the call locally too. floor_input republishes its own
                # full state, but doing it here as well means we won't
                # re-dispatch to the same floor in the gap before that arrives.
                served = self.pending_target
                # We just emptied this floor. Clear the retained count so the
                # stale pre-pickup number can't keep winning while the car is
                # parked there suppressing that ROI.
                self.head_counts[served] = 0
                if served in self.calls:
                    self.calls[served].active_call = False
                    self.calls[served].wait_start = None
                out_served.send({"served": served})
                self.log(f"served floor {served}")
                self.state = IDLE
                self.pending_target = None

        if self.state != IDLE:
            return None

        decision = core.decide(self.calls, self.head_counts, now, self.aging_factor)
        if decision.target is None:
            return decision
        self.log(decision.explain())
        out_target.send({"target": decision.target})
        self.state = MOVING
        self.pending_target = decision.target
        return decision

    def log(self, text):
        if self.verbose:
            print(f"[dispatcher] {text}", flush=True)


def main():
    ipc.ensure_fifos()
    heads_in = ipc.FifoReader(ipc.FIFO_HEADS)
    calls_in = ipc.FifoReader(ipc.FIFO_CALLS)
    arrived_in = ipc.FifoReader(ipc.FIFO_ARRIVED)
    target_out = ipc.FifoWriter(ipc.FIFO_TARGET)
    served_out = ipc.FifoWriter(ipc.FIFO_SERVED)

    d = Dispatcher()
    print(f"[dispatcher] up, aging_factor={d.aging_factor}", flush=True)
    try:
        while True:
            for msg in heads_in.poll():
                d.on_heads(msg)
            for msg in calls_in.poll():
                d.on_calls(msg)
            d.tick(time.time(), target_out, served_out, arrived_in.poll())
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        pass
    finally:
        for c in (heads_in, calls_in, arrived_in, target_out, served_out):
            c.close()


if __name__ == "__main__":
    main()
