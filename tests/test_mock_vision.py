#!/usr/bin/env python3
"""Guards the mock vision path -- the guaranteed demo fallback.

The one thing that must never break: whatever mock_vision publishes has to be
something the real Dispatcher accepts and interprets identically to
vision_service's output. These tests feed mock_vision's messages through the
real Dispatcher, so a wire-format drift fails here rather than at the demo.

    python3 tests/test_mock_vision.py      (or: make test-mock)
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

from dispatcher import Dispatcher  # noqa: E402
from mock_vision import build_message, parse_counts  # noqa: E402

failures = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name:<50} {got!r}")
    else:
        print(f"  FAIL {name:<50} got {got!r} want {want!r}")
        failures.append(name)


def test_parse():
    print("\ncount parsing")
    check("single floor", parse_counts("2=4"), {2: 4})
    check("multiple floors", parse_counts("1=2,3=1"), {1: 2, 3: 1})
    check("whitespace tolerated", parse_counts(" 1=2 , 3=1 "), {1: 2, 3: 1})
    for bad in ("4=1", "1=-2", "junk", ""):
        try:
            parse_counts(bad)
            check(f"rejects {bad!r}", "accepted", "ValueError")
        except ValueError:
            check(f"rejects {bad!r}", "ValueError", "ValueError")


def test_wire_format_matches_dispatcher():
    """The critical assertion: Dispatcher reads mock output correctly."""
    print("\nmock output is consumed correctly by the real Dispatcher")

    d = Dispatcher(verbose=False)
    d.on_heads(build_message({1: 5, 2: 0, 3: 1}, suppress=None))
    check("all three floors credited", d.head_counts, {1: 5, 2: 0, 3: 1})

    # Keys must be strings and values ints, exactly as vision_service emits.
    msg = build_message({1: 5, 3: 1}, suppress=None)
    check("keys are strings", sorted(msg["heads"]), ["1", "3"])
    check("values are ints", all(isinstance(v, int) for v in msg["heads"].values()), True)


def test_suppression_omits_rather_than_zeroes():
    """Suppressed floors must be ABSENT, not zero.

    vision_service omits the car's ROI so the dispatcher retains its last
    known count. Publishing zero instead would wrongly signal the floor
    emptied, so the mock has to get this right too.
    """
    print("\nsuppressed floor is omitted, and last known count is retained")

    msg = build_message({1: 5, 2: 2, 3: 1}, suppress=2)
    check("floor 2 absent from message", "2" in msg["heads"], False)
    check("others present", sorted(msg["heads"]), ["1", "3"])

    d = Dispatcher(verbose=False)
    d.on_heads(build_message({1: 5, 2: 7, 3: 1}, suppress=None))
    d.on_heads(build_message({1: 5, 2: 7, 3: 1}, suppress=2))
    check("floor 2 count retained while suppressed", d.head_counts.get(2), 7)


def main():
    print("mock vision path tests (guaranteed demo fallback)")
    test_parse()
    test_wire_format_matches_dispatcher()
    test_suppression_omits_rather_than_zeroes()

    if failures:
        print(f"\n{len(failures)} FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nall passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
