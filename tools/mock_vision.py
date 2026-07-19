#!/usr/bin/env python3
"""mock_vision -- stands in for vision_service, with no camera involved.

THIS IS THE GUARANTEED DEMO PATH. It publishes head counts to the heads FIFO
in exactly the wire format vision_service uses, so dispatcher, floor_input and
motor_control cannot tell the difference.

Dependencies: python3 and src/ipc.py. Nothing else. It does NOT import, link
against, or require vision_service.c, blob.c, capture.h, the Sensor Framework,
or any camera. It cannot be broken by anything happening in vision/.

    # simplest: 2 people on floor 1, 1 on floor 3, republished every second
    python3 tools/mock_vision.py --counts 1=2,3=1

    # live demo: type new counts while it runs
    python3 tools/mock_vision.py --interactive

    # scripted, hands-off
    python3 tools/mock_vision.py --scenario rush

Start the dispatcher FIRST -- it creates the FIFOs.
"""

import argparse
import os
import select
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import ipc  # noqa: E402

FLOORS = (1, 2, 3)
DEFAULT_INTERVAL = 1.0  # matches vision_service's ~1s poll

# Scripted sequences: (seconds_from_start, {floor: count})
SCENARIOS = {
    # One person waiting upstairs, then a crowd downstairs. Pairs with the
    # crowd_vs_first story in sim/simulate.py.
    "crowd_vs_first": [
        (0.0, {1: 0, 2: 0, 3: 1}),
        (3.0, {1: 5, 2: 0, 3: 1}),
        (15.0, {1: 0, 2: 0, 3: 0}),
    ],
    "rush": [
        (0.0, {1: 3, 2: 1, 3: 0}),
        (5.0, {1: 3, 2: 1, 3: 4}),
        (12.0, {1: 1, 2: 2, 3: 4}),
        (20.0, {1: 0, 2: 0, 3: 1}),
        (28.0, {1: 0, 2: 0, 3: 0}),
    ],
    "empty": [
        (0.0, {1: 0, 2: 0, 3: 0}),
    ],
}


def parse_counts(text):
    """'1=2,3=1' -> {1: 2, 3: 1}"""
    counts = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"expected FLOOR=COUNT, got {part!r}")
        floor, n = part.split("=", 1)
        floor, n = int(floor), int(n)
        if floor not in FLOORS:
            raise ValueError(f"floor {floor} out of range {FLOORS}")
        if n < 0:
            raise ValueError(f"negative count {n}")
        counts[floor] = n
    if not counts:
        raise ValueError("no counts given")
    return counts


class CarTracker:
    """Optionally mirrors vision_service's occlusion suppression.

    vision_service omits the ROI band the car occupies rather than reporting
    zero for it. Reading carpos and doing the same keeps the mock faithful to
    the real thing, including the dispatcher's retain-last-known-count path.
    Entirely optional -- if motor_control is not running, this stays at None
    and nothing is suppressed.
    """

    def __init__(self, enabled):
        self.enabled = enabled
        self.car_floor = None
        self._reader = None
        if enabled:
            try:
                self._reader = ipc.FifoReader(ipc.FIFO_CARPOS)
            except OSError as exc:
                print(f"[mock_vision] carpos unavailable ({exc}); "
                      "occlusion suppression off", flush=True)
                self.enabled = False

    def poll(self):
        if not self._reader:
            return
        for msg in self._reader.poll():
            floor = msg.get("car_floor")
            if floor is not None:
                self.car_floor = int(floor)

    def close(self):
        if self._reader:
            self._reader.close()


def build_message(counts, suppress):
    """Same shape vision_service publishes: suppressed floors are OMITTED."""
    return {
        "heads": {
            str(f): int(n) for f, n in sorted(counts.items()) if f != suppress
        }
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--counts", help="static counts, e.g. 1=2,3=1")
    src.add_argument("--scenario", choices=sorted(SCENARIOS),
                     help="scripted sequence")
    src.add_argument("--interactive", action="store_true",
                     help="type FLOOR=COUNT at the prompt to update live")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                    help=f"republish period, default {DEFAULT_INTERVAL}s")
    ap.add_argument("--suppress-car-floor", action="store_true",
                    help="omit the ROI the car occupies, like vision_service")
    ap.add_argument("--once", action="store_true", help="publish once and exit")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()

    if args.counts:
        counts = parse_counts(args.counts)
    elif args.scenario:
        counts = dict(SCENARIOS[args.scenario][0][1])
    else:
        counts = {f: 0 for f in FLOORS}
        if not args.interactive:
            args.interactive = True  # bare invocation is most useful live

    ipc.ensure_fifos()
    out = ipc.FifoWriter(ipc.FIFO_HEADS)
    car = CarTracker(args.suppress_car_floor)

    script = list(SCENARIOS[args.scenario]) if args.scenario else []
    start = time.time()
    last_publish = 0.0
    dropped = 0

    if not args.quiet:
        print(f"[mock_vision] publishing to {ipc.FIFO_HEADS} "
              f"every {args.interval}s", flush=True)
        if args.interactive:
            print("[mock_vision] type e.g. '2=3' then Enter; 'q' to quit",
                  flush=True)

    try:
        while True:
            now = time.time()

            # Advance the script.
            while script and (now - start) >= script[0][0]:
                _, counts = script.pop(0)
                counts = dict(counts)
                if not args.quiet:
                    print(f"[mock_vision] scenario -> {counts}", flush=True)

            # Interactive edits, without blocking the publish loop.
            if args.interactive and select.select([sys.stdin], [], [], 0)[0]:
                line = sys.stdin.readline()
                if not line or line.strip().lower() in ("q", "quit", "exit"):
                    break
                if line.strip():
                    try:
                        counts.update(parse_counts(line))
                        print(f"[mock_vision] now {counts}", flush=True)
                    except ValueError as exc:
                        print(f"[mock_vision] {exc}", flush=True)

            car.poll()
            suppress = car.car_floor if car.enabled else None

            if now - last_publish >= args.interval or args.once:
                msg = build_message(counts, suppress)
                if out.send(msg):
                    if not args.quiet:
                        note = f"  (floor {suppress} suppressed)" if suppress else ""
                        print(f"[mock_vision] {msg['heads']}{note}", flush=True)
                else:
                    dropped += 1
                    if dropped in (1, 10) and not args.quiet:
                        print("[mock_vision] no reader on heads FIFO -- "
                              "is dispatcher running? (retrying)", flush=True)
                last_publish = now

            if args.once:
                break
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        out.close()
        car.close()
        if not args.quiet:
            print("\n[mock_vision] stopped", flush=True)


if __name__ == "__main__":
    main()
