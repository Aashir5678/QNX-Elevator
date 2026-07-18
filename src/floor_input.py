#!/usr/bin/env python3
"""floor_input -- hall call buttons.

4 buttons: floor1_up, floor2_up, floor2_down, floor3_down. Any button on a
floor raises that floor's call; direction is tracked for display but the
3-floor dispatcher does not currently use it.

Publishes full call state to FIFO_CALLS on every change.
Listens on FIFO_SERVED for {"served": N} to clear a floor.
"""

import sys
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])

import ipc

# --- Pin map: BCM numbering. PLACEHOLDER -- set to your actual wiring. ---
# Buttons wired to ground, using internal pull-ups, so a press reads LOW and
# the edge of interest is FALLING.
BUTTONS = {
    "floor1_up": {"pin": 17, "floor": 1},
    "floor2_up": {"pin": 27, "floor": 2},
    "floor2_down": {"pin": 22, "floor": 2},
    "floor3_down": {"pin": 23, "floor": 3},
}

FLOORS = (1, 2, 3)
# Window for the manual debounce below. Not a bouncetime= kwarg -- QNX has no
# such parameter (see the CONFIRMED block).
DEBOUNCE_MS = 200
POLL_INTERVAL = 0.05
# Republish unchanged state this often so a restarted dispatcher resyncs
# without having to wait for the next button press.
HEARTBEAT_INTERVAL = 2.0

# CONFIRMED against QNX's official rpi_gpio API comparison table:
# https://www.qnx.com/developers/docs/qnxeverywhere/com.qnx.doc.interfacing/topic/rpi/rpi_GPIO-apis.html
#
#   - add_event_detect(channel, edge, callback=fn) -- channel, edge and
#     callback ONLY. There is no bouncetime= parameter, and QNX supports no
#     bouncetime-based debouncing anywhere in its event handling. Passing it
#     raises TypeError on-device. Debouncing must be done manually; see the
#     Debouncer class below.
#   - These are NOT available in QNX's rpi_gpio at all, do not introduce them:
#     add_event_callback(), wait_for_edge(), event_detected(),
#     remove_event_detect(), getmode(), gpio_function(), setwarnings().
#
# STILL UNVERIFIED -- confirm before relying on:
#   - GPIO.setmode(GPIO.BCM) constant naming (setmode itself is supported;
#     it is getmode that is absent)
#   - the pull_up_down= keyword and the GPIO.PUD_UP constant


class Debouncer:
    """Per-channel software debounce, replacing QNX's absent bouncetime=.

    Mechanical pushbuttons bounce for single-digit milliseconds on contact, so
    one physical press can fire several edge callbacks. This is kept for that
    reason, not speculatively -- the original code asked for a 200ms bouncetime
    for the same purpose before it turned out QNX has no such parameter.

    Note it is defence-in-depth rather than load-bearing: CallBoard.press() is
    already idempotent, so a duplicate press on an already-active floor is a
    no-op that cannot reset wait_start. Without debouncing the visible symptom
    would be repeated log lines, plus a genuine risk of a bounce immediately
    after a floor is served re-raising the call that was just cleared. With it,
    one press produces one accepted trigger.

    Hardware-free, so it is unit tested in tests/test_floor_input.py.
    """

    def __init__(self, window_ms=DEBOUNCE_MS):
        self.window = window_ms / 1000.0
        self._last_accepted = {}

    def accept(self, channel, now):
        """True if this trigger should be acted on, False if it is a bounce.

        The window is measured from the last ACCEPTED trigger, and rejected
        triggers deliberately do not extend it -- otherwise a continuously
        chattering line would suppress itself forever and the button would go
        permanently dead.
        """
        last = self._last_accepted.get(channel)
        if last is not None and (now - last) < self.window:
            return False
        self._last_accepted[channel] = now
        return True


class CallBoard:
    """Call state. Hardware-free so it can be unit tested."""

    def __init__(self, floors=FLOORS):
        self.active = {f: False for f in floors}
        self.wait_start = {f: None for f in floors}
        self.dirty = True

    def press(self, floor, now):
        if self.active[floor]:
            # Already waiting -- keep the ORIGINAL wait_start. Re-pressing the
            # button must not reset the age, or an impatient rider could
            # repeatedly zero out their own aging bonus.
            return
        self.active[floor] = True
        self.wait_start[floor] = now
        self.dirty = True

    def serve(self, floor):
        if not self.active.get(floor):
            return
        self.active[floor] = False
        self.wait_start[floor] = None
        self.dirty = True

    def message(self):
        return {
            "calls": {
                str(f): {"active": self.active[f], "since": self.wait_start[f]}
                for f in self.active
            }
        }


def main():
    import rpi_gpio as GPIO

    ipc.ensure_fifos()
    calls_out = ipc.FifoWriter(ipc.FIFO_CALLS)
    served_in = ipc.FifoReader(ipc.FIFO_SERVED)
    board = CallBoard()
    debouncer = Debouncer(DEBOUNCE_MS)

    GPIO.setmode(GPIO.BCM)
    for name, cfg in BUTTONS.items():
        GPIO.setup(cfg["pin"], GPIO.IN, pull_up_down=GPIO.PUD_UP)

        def make_cb(floor, name):
            def cb(channel):
                now = time.time()
                if not debouncer.accept(channel, now):
                    return
                board.press(floor, now)
                print(f"[floor_input] press {name} -> floor {floor}", flush=True)

            return cb

        # channel, edge, callback ONLY -- no bouncetime=, see the CONFIRMED
        # block at the top of this file. Debouncing is handled in the callback.
        GPIO.add_event_detect(
            cfg["pin"],
            GPIO.FALLING,
            callback=make_cb(cfg["floor"], name),
        )

    print("[floor_input] up, watching 4 buttons", flush=True)
    last_publish = 0.0
    try:
        while True:
            for msg in served_in.poll():
                floor = msg.get("served")
                if floor is not None:
                    board.serve(int(floor))
                    print(f"[floor_input] cleared floor {floor}", flush=True)

            # Republish on change, and periodically regardless -- the state is
            # small and idempotent, so a heartbeat lets a restarted dispatcher
            # resynchronize without waiting for the next button press.
            now = time.time()
            if board.dirty or (now - last_publish) >= HEARTBEAT_INTERVAL:
                if calls_out.send(board.message()):
                    board.dirty = False
                    last_publish = now
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        pass
    finally:
        calls_out.close()
        served_in.close()
        GPIO.cleanup()


if __name__ == "__main__":
    main()
