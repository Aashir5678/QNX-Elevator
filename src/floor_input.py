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
DEBOUNCE_MS = 200
POLL_INTERVAL = 0.05
# Republish unchanged state this often so a restarted dispatcher resyncs
# without having to wait for the next button press.
HEARTBEAT_INTERVAL = 2.0

# VERIFY ON DEVICE: QNX's rpi_gpio is API-compatible with RPi.GPIO for the
# calls used here, but confirm against the QNX docs before relying on:
#   - GPIO.setmode(GPIO.BCM) constant naming
#   - the pull_up_down= keyword and GPIO.PUD_UP constant
#   - add_event_detect's bouncetime= support (if absent, do the debounce in
#     the callback using the timestamps already tracked below)


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

    GPIO.setmode(GPIO.BCM)
    for name, cfg in BUTTONS.items():
        GPIO.setup(cfg["pin"], GPIO.IN, pull_up_down=GPIO.PUD_UP)

        def make_cb(floor, name):
            def cb(channel):
                board.press(floor, time.time())
                print(f"[floor_input] press {name} -> floor {floor}", flush=True)

            return cb

        GPIO.add_event_detect(
            cfg["pin"],
            GPIO.FALLING,
            callback=make_cb(cfg["floor"], name),
            bouncetime=DEBOUNCE_MS,
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
