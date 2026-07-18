# Running the elevator on real hardware

How to start the system on `qnxpi12` and confirm it is working. Getting the code onto the
board is **[DEPLOY.md](DEPLOY.md)**; validating logic off-device is **[TESTING.md](TESTING.md)**.

**Before the first run:** the servo must be calibrated or `motor_control.py` will refuse to
start. `FLOOR_ANGLES` are `None` placeholders. See TESTING.md Layer 3.

---

## Startup

### Order matters: dispatcher first

```
cd elevator
python3 src/dispatcher.py &      # FIRST — creates all six FIFOs
python3 src/floor_input.py &     # needs root/GPIO
python3 src/motor_control.py &   # needs root/GPIO; refuses to run uncalibrated
./build/vision_service &
```

`dispatcher` calls `ipc.ensure_fifos()` before opening anything and owns creation of all six
channels. The other three assume the FIFOs already exist.

This is load-bearing, not stylistic. `ipc.FifoWriter` opens with `O_WRONLY | O_NONBLOCK`,
which fails with `ENOENT` if the FIFO does not exist and `ENXIO` while no reader has it open.
In both cases `send()` returns `False` and **the message is silently dropped**. A producer
started before the dispatcher throws away everything it publishes until the dispatcher
attaches.

That is safe by design: every channel publishes full state rather than deltas, so the next
message resynchronises the reader. Expect the first `carpos` send from `motor_control` to be
dropped if `vision_service` has not opened its read end yet — correct behaviour, not a fault.

GPIO access needs privilege, so `floor_input` and `motor_control` are typically run after
`su root` (see DEPLOY.md). FIFOs live in `/tmp/elevator`; override with `ELEVATOR_FIFO_DIR`,
which must be set identically for **all four** processes or they will not find each other.

### Shutdown

`Ctrl-C` or `kill` each process. All four handle interruption and release their FIFO handles;
`motor_control` also stops PWM and calls `GPIO.cleanup()`. The FIFOs themselves persist in
`/tmp/elevator` and are reused on the next run.

---

## Confirming all four processes are running

QNX uses `pidin` rather than `ps`. Plain `pidin` lists processes:

```
pidin | grep -E 'python|vision_service'
```

All four Python processes report as `python3`, so to tell them apart you need the argument
list. On QNX that is `pidin ar`:

```
pidin ar | grep -E 'dispatcher|floor_input|motor_control|vision_service'
```

Expect four lines. If `pidin ar` is unavailable on this image, check what the local build
supports with `use pidin`, which prints the utility's usage.

If a process is missing, run it in the foreground without `&` to see why it exited — the
common causes are an `import rpi_gpio` failure, the uncalibrated-servo refusal from
`motor_control`, or a missing FIFO because `dispatcher` was not started first.

### Checking the FIFOs

The six channels should exist as FIFOs:

```
ls -l /tmp/elevator
```

> **Do not `cat` a FIFO while the system is running.** These are single-reader channels. A
> `cat` on `/tmp/elevator/heads` competes with the dispatcher for messages and will steal
> roughly half of them, producing symptoms that look like a vision or dispatch bug. Inspect a
> channel only when its real consumer is stopped.

---

## What a working run looks like

There is **no LED or indicator output in the current code** — `floor_input` configures its
four pins as inputs only, and the sole GPIO output is the servo PWM pin. The observable
signals are the four processes' stdout logs and the physical movement of the car. If you add
indicator LEDs later, `floor_input` is where the call state lives and `motor_control` is where
car position lives.

Run each process in its own terminal to watch the sequence.

**1. Press a hall call button.** `floor_input` logs the press and republishes call state:

```
[floor_input] press floor3_down -> floor 3
```

A press on a floor that already has an active call is ignored, and deliberately does **not**
reset that floor's `wait_start` — an impatient rider cannot zero out their own aging bonus.

**2. Vision reports heads for that floor — eventually, and maybe not before dispatch.**
`vision_service` polls the camera roughly once per second, so head counts lag the button
press. The band the car currently occupies is omitted from the message entirely rather than
sent as zero.

**3. The dispatcher picks a target,** printing the winning floor and the score for every
active-call floor that lost:

```
[dispatcher] -> dispatch to floor 1
    floor 1:   5.15 = 5 heads + 0.15 aging (1.5s waited)
    floor 3:   1.24 = 1 heads + 0.24 aging (2.4s waited)
```

**4. The servo moves.** `motor_control` logs the target, drives the servo to the calibrated
angle for that floor, and publishes car position continuously:

```
[motor_control] moving to floor 1
```

**5. Arrival is confirmed** once the settle time elapses, and the dispatcher clears the call:

```
[motor_control] arrived at floor 1
[dispatcher] served floor 1
```

`floor_input` logs `cleared floor 1` when it receives the served message, and the car sits
idle until the next call.

### Two behaviours that look like bugs but are correct

Both are asserted by the integration tests in `tests/test_pipeline.py`.

**The dispatcher does not wait for vision.** It ticks every `POLL_INTERVAL = 0.2s` and decides
with whatever data it has, while `vision_service` polls about once per second. A button press
will frequently be dispatched *before* any head count for that floor exists. A floor with an
active call is always credited at least one person (`MIN_ASSUMED_HEADS` in `src/core.py`) — the
button was physically pressed, so somebody is there regardless of what the camera reports. So
a lone caller scores `1.00`, never `0.00`, and can never be ignored outright.

**A committed trip is never re-targeted.** Once the dispatcher enters `MOVING`, later news does
not change the target — not even a floor with a far larger crowd and an older call. The trip
finishes, `motor_control` confirms arrival, and only then is the better option taken up.
Watching the car continue past a floor where a crowd just appeared is correct.

---

## Troubleshooting

Work from the bottom of the stack up: confirm the logic is sound off-device, then the
pipeline, then the hardware. `make test-all` on the host proves the algorithm and the FIFO
plumbing are fine, which narrows any remaining fault to the board.

| Symptom | Likely cause |
|---|---|
| Nothing happens on button press | `floor_input` not running, wrong BCM pins, or not run as root |
| Presses logged, car never moves | `motor_control` not running, or it refused to start uncalibrated |
| Car moves to the wrong floor | `FLOOR_ANGLES` miscalibrated — TESTING.md Layer 3 |
| Head counts always 0 or absurd | colour window untuned, or wrong pixel format — TESTING.md Layer 3 |
| Head counts stuck for one floor | that band is being suppressed as the car's position; check `motor_control` is publishing `carpos` |
| Processes running but no interaction | started out of order, or mismatched `ELEVATOR_FIFO_DIR` |
| Erratic/missing messages | something is `cat`-ing a FIFO and stealing messages (see above) |
| Servo jitters or hits a hard stop | PWM duty-cycle units — TESTING.md Layer 3. **Stop immediately** |

**Hardware-specific unknowns are documented once, in [TESTING.md](TESTING.md) Layer 3** —
the unverified `capture.h` calls and pixel-format assumption, the `rpi_gpio` PWM duty-cycle
units and MS-mode selection, `add_event_detect` bouncetime support, and the servo calibration
procedure. They are not repeated here; that file is the single source of truth for them.

The most likely first-run failure is the **pixel format**: `blob.c` assumes packed YUYV 4:2:2,
and if the webcam negotiates MJPEG or NV12 instead, head counts will be garbage while every
process appears healthy. Confirm the format before trusting any count.
