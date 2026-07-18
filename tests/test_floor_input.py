#!/usr/bin/env python3
"""Tests for floor_input's software debounce and GPIO API conformance.

QNX's rpi_gpio has no bouncetime= parameter and no bouncetime-based debouncing
anywhere, so debouncing is done manually in the edge callback. These tests
exercise that real logic, plus a static guard against reintroducing any of the
rpi_gpio functions QNX does not provide.

    python3 tests/test_floor_input.py      (or: make test-floor-input)

Source for the API constraints:
https://www.qnx.com/developers/docs/qnxeverywhere/com.qnx.doc.interfacing/topic/rpi/rpi_GPIO-apis.html
"""

import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from floor_input import DEBOUNCE_MS, CallBoard, Debouncer  # noqa: E402

# Not available in QNX's rpi_gpio at all. Calling any of these fails the same
# way the bouncetime= kwarg did.
UNAVAILABLE = {
    "add_event_callback",
    "wait_for_edge",
    "event_detected",
    "remove_event_detect",
    "getmode",
    "gpio_function",
    "setwarnings",
}

WINDOW = DEBOUNCE_MS / 1000.0

failures = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name:<54} {got!r}")
    else:
        print(f"  FAIL {name:<54} got {got!r} want {want!r}")
        failures.append(name)


# --------------------------------------------------------------------------


def test_debouncer_basics():
    print("\ndebouncer accepts one trigger per physical press")
    d = Debouncer(DEBOUNCE_MS)

    check("first trigger accepted", d.accept(17, 0.0), True)
    check("bounce at +1ms rejected", d.accept(17, 0.001), False)
    check("bounce at +5ms rejected", d.accept(17, 0.005), False)
    check("still rejected just inside window", d.accept(17, WINDOW - 0.001), False)
    check("accepted once past window", d.accept(17, WINDOW + 0.001), True)


def test_realistic_bounce_burst():
    """A real contact bounce: a burst of edges over the first few ms."""
    print("\na contact-bounce burst yields exactly one accepted trigger")
    d = Debouncer(DEBOUNCE_MS)

    burst = [0.0, 0.0008, 0.0021, 0.0034, 0.0047, 0.0062, 0.0090]
    accepted = [t for t in burst if d.accept(17, t)]
    check("one accept from a 7-edge burst", len(accepted), 1)
    check("it is the first edge", accepted[0], 0.0)

    # A genuine second press, well after the window, must still register.
    check("deliberate second press accepted", d.accept(17, 1.0), True)


def test_rejections_do_not_extend_window():
    """A chattering line must not suppress the button forever.

    The window is measured from the last ACCEPTED trigger. If rejected
    triggers reset it, continuous chatter would keep pushing the deadline out
    and the button would go permanently dead.
    """
    print("\nchatter does not permanently suppress a channel")
    d = Debouncer(DEBOUNCE_MS)
    check("initial press accepted", d.accept(17, 0.0), True)

    # Chatter every 10ms right through the window and beyond.
    t = 0.01
    while t < WINDOW:
        d.accept(17, t)
        t += 0.01

    check("accepted immediately past window despite chatter",
          d.accept(17, WINDOW + 0.0001), True)


def test_channels_are_independent():
    print("\ndebounce is per-channel")
    d = Debouncer(DEBOUNCE_MS)

    check("floor1 pin accepted", d.accept(17, 0.0), True)
    check("floor2 pin accepted at same instant", d.accept(27, 0.0), True)
    check("floor1 pin bounce rejected", d.accept(17, 0.002), False)
    check("floor2 pin rejected by its OWN window", d.accept(27, 0.002), False)

    # The real independence check: a channel that has never fired must be
    # accepted even while another channel is mid-window. A single shared
    # timestamp instead of a per-channel one would reject this.
    check("untouched floor3 pin accepted mid-bounce", d.accept(23, 0.002), True)
    check("floor2 pin accepts past its own window", d.accept(27, WINDOW + 0.001), True)


def test_bounce_does_not_disturb_call_state():
    """Debouncer composed with the real CallBoard, as main() composes them.

    The edge callback in main() is a thin adapter over exactly this pair:
    accept() gates, then press() records.
    """
    print("\nbounced press produces one clean call-state change")
    d = Debouncer(DEBOUNCE_MS)
    board = CallBoard()

    for t in (0.0, 0.001, 0.003, 0.007):
        if d.accept(17, t):
            board.press(3, t)

    check("floor 3 active", board.active[3], True)
    check("wait_start is the first edge", board.wait_start[3], 0.0)
    check("other floors untouched", board.active[1] or board.active[2], False)


def test_bounce_after_serve_does_not_reraise():
    """The case where debouncing is actually load-bearing.

    CallBoard.press() is idempotent, so a bounce while a call is already
    active is harmless. But a bounce arriving just after the floor is served
    would re-raise the call that was only just cleared, and the car would be
    dispatched to an empty floor.
    """
    print("\nbounce arriving after a serve does not re-raise the call")
    d = Debouncer(DEBOUNCE_MS)
    board = CallBoard()

    d.accept(17, 0.0)
    board.press(3, 0.0)
    check("call raised", board.active[3], True)

    board.serve(3)
    check("call cleared by dispatcher", board.active[3], False)

    # Late bounce from the same physical press, still inside the window.
    late = 0.05
    if d.accept(17, late):
        board.press(3, late)
    check("late bounce suppressed, call stays clear", board.active[3], False)

    # A real new press after the window must still work.
    if d.accept(17, WINDOW + 0.1):
        board.press(3, WINDOW + 0.1)
    check("genuine later press re-raises", board.active[3], True)


def test_no_unavailable_gpio_calls_in_src():
    """Static guard against reintroducing QNX-unsupported rpi_gpio APIs.

    Parses every module under src/ rather than grepping, so a call is only
    flagged when it is genuinely a call and not a mention in a comment or
    docstring -- both files legitimately name these functions in their
    CONFIRMED blocks.
    """
    print("\nsrc/ calls no QNX-unavailable rpi_gpio functions")

    found_unavailable = []
    bouncetime_sites = []

    for fname in sorted(os.listdir(SRC)):
        if not fname.endswith(".py"):
            continue
        tree = ast.parse(open(os.path.join(SRC, fname)).read(), filename=fname)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            attr = node.func.attr if isinstance(node.func, ast.Attribute) else None
            if attr in UNAVAILABLE:
                found_unavailable.append(f"{fname}:{node.lineno} {attr}()")
            for kw in node.keywords:
                if kw.arg == "bouncetime":
                    bouncetime_sites.append(f"{fname}:{node.lineno}")

    check("no unavailable rpi_gpio calls", found_unavailable, [])
    check("no bouncetime= kwarg anywhere", bouncetime_sites, [])


def test_add_event_detect_signature():
    """add_event_detect must be called as (channel, edge, callback=fn) only."""
    print("\nadd_event_detect uses the QNX-supported signature")

    tree = ast.parse(open(os.path.join(SRC, "floor_input.py")).read())
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "add_event_detect"
    ]
    check("exactly one add_event_detect call", len(calls), 1)
    if not calls:
        return

    call = calls[0]
    check("two positional args (channel, edge)", len(call.args), 2)
    check("only 'callback' passed by keyword",
          sorted(kw.arg for kw in call.keywords), ["callback"])


def main():
    print("floor_input debounce + GPIO API conformance tests")
    test_debouncer_basics()
    test_realistic_bounce_burst()
    test_rejections_do_not_extend_window()
    test_channels_are_independent()
    test_bounce_does_not_disturb_call_state()
    test_bounce_after_serve_does_not_reraise()
    test_no_unavailable_gpio_calls_in_src()
    test_add_event_detect_signature()

    if failures:
        print(f"\n{len(failures)} FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nall passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
