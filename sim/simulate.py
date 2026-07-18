#!/usr/bin/env python3
"""Hardware-free simulation of floor_input + dispatcher.

Drives the real Dispatcher state machine from dispatcher.py against a virtual
clock, mock button presses, and mock head counts. No FIFOs, no GPIO, no camera.

Usage:
    python3 sim/simulate.py                    # default scenario, verbose
    python3 sim/simulate.py --aging-factor 0.5
    python3 sim/simulate.py --sweep            # tune aging_factor
    python3 sim/simulate.py --scenario rush    # list with --list-scenarios
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import core
from dispatcher import Dispatcher

# --- Physical model (virtual seconds) -------------------------------------
# Placeholders matched to a small 3D-printed model; refine once the servo is
# calibrated and you can time a real floor-to-floor move.
TRAVEL_TIME_PER_FLOOR = 2.0
DOOR_DWELL = 1.5
DT = 0.1


class FakeWriter:
    """Stands in for ipc.FifoWriter -- records instead of writing."""

    def __init__(self):
        self.messages = []

    def send(self, obj):
        self.messages.append(obj)
        return True

    def drain(self):
        msgs, self.messages = self.messages, []
        return msgs

    def close(self):
        pass


class Person:
    __slots__ = ("floor", "arrived_at", "picked_up_at")

    def __init__(self, floor, arrived_at):
        self.floor = floor
        self.arrived_at = arrived_at
        self.picked_up_at = None

    @property
    def wait(self):
        return self.picked_up_at - self.arrived_at


class World:
    """The physical model: people waiting, and a car that moves between floors."""

    def __init__(self, floors=(1, 2, 3), start_floor=1):
        self.floors = list(floors)
        self.waiting = {f: [] for f in self.floors}
        self.car_floor = start_floor
        self.car_target = None
        self.busy_until = None
        self.people = []

    # -- what floor_input would publish ------------------------------------

    def call_message(self):
        """The exact wire format floor_input publishes."""
        calls = {}
        for f in self.floors:
            queue = self.waiting[f]
            calls[str(f)] = {
                "active": bool(queue),
                # wait_start is when the *button* was pressed, i.e. when the
                # first person showed up -- not the most recent arrival.
                "since": queue[0].arrived_at if queue else None,
            }
        return {"calls": calls}

    # -- what vision_service would publish ---------------------------------

    def heads_message(self, blind=False):
        """The exact wire format vision_service publishes."""
        counts = {}
        for f in self.floors:
            # Occlusion suppression: vision omits the ROI the car occupies.
            if f == self.car_floor and self.busy_until is None:
                continue
            counts[str(f)] = 0 if blind else len(self.waiting[f])
        return {"heads": counts}

    def arrive(self, floor, count, now):
        for _ in range(count):
            p = Person(floor, now)
            self.waiting[floor].append(p)
            self.people.append(p)

    def start_move(self, target, now):
        distance = abs(target - self.car_floor)
        self.car_target = target
        self.busy_until = now + distance * TRAVEL_TIME_PER_FLOOR + DOOR_DWELL

    def step(self, now):
        """Returns the floor just arrived at, or None."""
        if self.busy_until is None or now < self.busy_until:
            return None
        floor = self.car_target
        self.car_floor = floor
        self.car_target = None
        self.busy_until = None
        for p in self.waiting[floor]:
            p.picked_up_at = now
        self.waiting[floor] = []
        return floor


def run(scenario, aging_factor, verbose=True, strategy="priority"):
    world = World()
    disp = Dispatcher(aging_factor=aging_factor, verbose=verbose)
    target_out, served_out = FakeWriter(), FakeWriter()

    # FCFS baseline: aging only, no headcount weighting, so the floor that
    # pressed first always wins. Gives us something to measure against.
    if strategy == "fcfs":
        disp.aging_factor = 1.0

    pending = sorted(scenario, key=lambda e: e[0])
    now = 0.0
    horizon = pending[-1][0] + 120.0
    arrivals = []

    while now < horizon:
        while pending and pending[0][0] <= now:
            t, floor, count = pending.pop(0)
            world.arrive(floor, count, now)
            if verbose:
                print(f"[t={now:6.1f}] {count} arrived at floor {floor}", flush=True)

        arrived = world.step(now)
        if arrived is not None:
            arrivals.append({"arrived": arrived})
            if verbose:
                print(f"[t={now:6.1f}] car reached floor {arrived}", flush=True)

        # Feed the dispatcher through its real message handlers so the
        # simulation exercises the same parsing and merge logic as production.
        disp.on_calls(world.call_message())
        disp.on_heads(world.heads_message(blind=(strategy == "fcfs")))
        if verbose and disp.state == "idle" and any(c.active_call for c in disp.calls.values()):
            print(f"[t={now:6.1f}] ", end="", flush=True)
        disp.tick(now, target_out, served_out, arrivals)
        arrivals = []

        for msg in target_out.drain():
            world.start_move(msg["target"], now)
        served_out.drain()

        if not pending and not any(world.waiting.values()) and world.busy_until is None:
            break
        now += DT

    return world


def report(world, label):
    served = [p for p in world.people if p.picked_up_at is not None]
    stranded = len(world.people) - len(served)
    if not served:
        print(f"{label}: nobody served")
        return None
    waits = [p.wait for p in served]
    avg = sum(waits) / len(waits)
    print(
        f"{label}: avg wait {avg:6.2f}s | worst {max(waits):6.2f}s | "
        f"served {len(served)}/{len(world.people)}"
        + (f" | STRANDED {stranded}" if stranded else "")
    )
    return avg


# --- Scenarios: (time, floor, number_of_people) ---------------------------

SCENARIOS = {
    # The core demo case. A first trip to floor 2 keeps the car busy, so by the
    # time it is free both floor 3 (1 person, pressed first) and floor 1 (5
    # people, pressed second) are waiting. FCFS honours the earlier press and
    # makes 5 people wait; priority collects the crowd first.
    #
    # Note the car must be busy for the strategies to diverge at all -- once a
    # trip is committed neither strategy preempts it. That matches the real
    # dispatcher, which only re-decides when idle.
    "crowd_vs_first": [
        (0.0, 2, 1),
        (1.0, 3, 1),
        (2.0, 1, 5),
    ],
    # Starvation check, and the reason aging_factor exists at all. Floor 1 gets
    # a fresh pair every 4s -- faster than the car can round-trip -- so a pure
    # headcount rule (aging_factor=0) parks at floor 1 forever and the lone
    # rider on floor 3 is never served. Sweep this scenario to find the
    # smallest aging_factor that still rescues them in acceptable time.
    "starvation": (
        [(1.0, 3, 1)]
        + [(float(t), 1, 2) for t in range(0, 40, 2)]
        + [(float(t) + 1.0, 2, 2) for t in range(0, 40, 2)]
    ),
    # Mixed morning rush across all three floors.
    "rush": [
        (0.0, 1, 3),
        (2.0, 2, 1),
        (3.0, 3, 4),
        (12.0, 2, 2),
        (18.0, 1, 2),
        (25.0, 3, 1),
    ],
    # Degenerate case: everyone on one floor, nothing to prioritize.
    "single_floor": [
        (0.0, 2, 4),
    ],
}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scenario", default="crowd_vs_first")
    ap.add_argument("--aging-factor", type=float, default=core.AGING_FACTOR)
    ap.add_argument("--sweep", action="store_true", help="compare aging factors")
    ap.add_argument("--list-scenarios", action="store_true")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()

    if args.list_scenarios:
        for name, events in SCENARIOS.items():
            print(f"{name:16s} {len(events)} events, {sum(e[2] for e in events)} people")
        return

    if args.scenario not in SCENARIOS:
        ap.error(f"unknown scenario {args.scenario!r}; try --list-scenarios")
    scenario = SCENARIOS[args.scenario]

    if args.sweep:
        print(f"scenario: {args.scenario}\n")
        base = report(run(scenario, 1.0, verbose=False, strategy="fcfs"), "fcfs baseline    ")
        print()
        for af in (0.0, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0):
            avg = report(run(scenario, af, verbose=False), f"aging_factor={af:<5.2f}")
            if base and avg:
                delta = (base - avg) / base * 100
                print(f"{'':18s}  -> {delta:+.1f}% vs FCFS")
        return

    print(f"scenario: {args.scenario}, aging_factor={args.aging_factor}\n")
    world = run(scenario, args.aging_factor, verbose=not args.quiet)
    print()
    report(world, "priority+aging")
    report(run(scenario, 1.0, verbose=False, strategy="fcfs"), "fcfs baseline ")


if __name__ == "__main__":
    main()
