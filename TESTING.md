# Testing QNX-Elevator

A standalone runbook, organised by how much hardware each layer needs. Layers 1 and 2 run on
any host with a C99 compiler and Python 3 — no Pi, no QNX, no camera, no servo. Layer 3 can
only be done on `qnxpi12`.

Run everything that works off-device with:

```
make            # blob tests + pipeline tests + vision-stub build
make test-all   # just the two test suites, no builds
```

---

## Layer 1 — pure logic. No hardware, no camera, no FIFOs.

### `make test`

Builds and runs `vision/test_blob.c` against `vision/blob.c`. 13 assertions covering the
colour threshold and the connected-component labelling: empty frames, single and multiple
blobs, off-target colours rejected, sub-`min_blob_area` specks filtered, per-floor band
splitting, and ROI suppression.

The one that matters most is **"U-shape merges to one"**. A U-shape is the real test of the
union-find merge: the two arms get different provisional labels during the first pass and are
only discovered to be the same object when the base is reached. A naive labeller reports 2
here. If that test regresses, blob counting is over-counting heads.

**Proves:** the vision maths is correct on known input. It does *not* prove the camera works,
or that the colour window is right for your lighting — see Layer 3.

### `python3 sim/simulate.py`

Verbose trace of every dispatch decision on the `crowd_vs_first` scenario, showing the target
chosen and the priority score for every active-call floor that lost.

This drives the **real `Dispatcher` class** from `src/dispatcher.py` against a virtual clock
and mock messages in the true wire format. Only the clock, buttons, and car are simulated, so
what you tune here transfers to the live system.

Current result — the number the demo is built to show:

```
priority+aging: avg wait   5.80s | worst  11.60s | served 7/7
fcfs baseline : avg wait   9.00s | worst  10.70s | served 7/7
```

36% lower average wait than first-come-first-served.

Other scenarios: `python3 sim/simulate.py --list-scenarios`.

### `python3 sim/simulate.py --scenario starvation --sweep`

**This is where `aging_factor` should be tuned.** It is the only parameter in the system with
a real tradeoff, and this scenario is what justifies its existence: floor 1 refills faster
than the car can serve it, so a lone rider on floor 3 competes against a permanent crowd.

| aging_factor | avg wait | worst wait |
|---|---|---|
| 0.00 | 4.03s | **48.30s** — the lone rider starves |
| 0.10 | 4.66s | 27.30s |
| 0.25 and up | 4.41s | 13.20s |
| *(fcfs baseline)* | *4.38s* | *12.30s* |

Pure headcount greedy (`0.00`) wins on average and is unacceptable on worst case. `0.25` is
the knee of the curve. Set the value at the top of `src/core.py`.

Note that `crowd_vs_first` is *flat* across the sweep — one contested decision, dominated by
headcount at every factor. Use `starvation` for tuning; use `crowd_vs_first` for the demo
headline.

---

## Layer 2 — full pipeline. Synthetic camera, real FIFOs. Still no hardware.

### Startup order: dispatcher MUST start first

`dispatcher` calls `ipc.ensure_fifos()` before opening anything, and it owns creation of all
six channels. The other processes assume the FIFOs already exist.

This is not a style preference, it is load-bearing. `ipc.FifoWriter` opens with
`O_WRONLY | O_NONBLOCK`, which fails with `ENXIO` while no reader has the FIFO open, and with
`ENOENT` if the FIFO does not exist at all. In both cases `send()` returns `False` and **the
message is silently dropped**. A producer started before the dispatcher will throw away
everything it publishes until the dispatcher attaches.

This is by design and self-healing: every channel publishes full state rather than deltas, so
the next message resynchronises the reader. `vision_service` republishes each poll and
`motor_control` republishes car position continuously. `floor_input` publishes on change plus
a 2s heartbeat, for exactly this reason.

**Expect a dropped first message.** When wiring this up by hand you will see the first
`carpos` send return `False` if `vision_service` has not opened its read end yet. That is
correct behaviour, not a bug.

### Running vision-stub against the dispatcher

`make vision-stub` builds `vision_service.c` with `-DVISION_STUB_CAPTURE`, replacing the
camera with a synthetic frame source that paints one on-target blob in the bottom band
(floor 1). Everything else is real: real blob detection, real FIFO publishing, real occlusion
suppression.

```
python3 src/dispatcher.py &     # must be first — creates the FIFOs
make vision-stub
./build/vision_stub
```

FIFOs default to `/tmp/elevator`; override with `ELEVATOR_FIFO_DIR`.

To see occlusion suppression working, publish a car position while the stub runs. With
`{"car_floor": 2}` the stub emits `{"heads": {"1": 1, "3": 0}}` — floor 2 is **omitted**, not
sent as zero, and floor 1 shows the painted blob.

### `make test-pipeline`

Automated integration test (`tests/test_pipeline.py`) for the dispatcher/vision FIFO pipeline.
Uses the real `Dispatcher`, real `ipc.FifoReader`/`FifoWriter`, and real named FIFOs in a
temporary directory — no mocks. Each run is isolated and cleans up after itself.

Four behaviours are covered, in 39 assertions:

1. **`ensure_fifos` creates all six channels up front**, as real FIFOs, before any writer can
   attach — and is idempotent, since every process calls it at startup.
2. **Heads arriving before calls are credited in full.** A `heads` message for floor 3 with 4
   heads, received *before* any `calls` message, produces `4.00 = 4 heads + 0.00 aging` when
   the call arrives. This ordering is the common case, not an edge case: `vision_service`
   publishes continuously while `floor_input` publishes only on change.
3. **A call with no vision data dispatches immediately, credited 1.**
4. **No preemption once committed.**

Both (3) and (4) describe timing behaviour that looks like a bug on first encounter. They are
not — see below.

### Two behaviours that look wrong but are correct

**The dispatcher does not wait for vision.** It ticks every `POLL_INTERVAL = 0.2s` and decides
with whatever it has. `vision_service` polls the camera roughly once per second, so a button
press will frequently be dispatched *before* any head count for that floor exists. The
dispatcher never blocks on missing vision data. The integration test asserts the decision
lands in well under one poll interval with no heads data present at all.

**Minimum credited heads is 1.** A floor with an active call is always credited at least one
person even when vision reports zero or has never reported at all (`MIN_ASSUMED_HEADS` in
`src/core.py`). The button was physically pressed, so somebody is there regardless of what the
camera thinks — bad angle, occlusion, a colour threshold miss, or simply no frame processed
yet. So a lone caller scores `1.00`, not `0.00`, and cannot be ignored forever.

**No preemption once a target is committed.** After the dispatcher enters `MOVING`, later news
does not re-target it — not even a floor with 99 heads and a 60-second-older call. The trip
completes, `motor_control` confirms arrival, and only then is the better option picked up.
Watching the car continue past a floor where a crowd just appeared is correct.

This is also why simulated scenarios need the car to be *busy* before priority and FCFS can
diverge at all: if the car is idle when the first call arrives, both strategies commit to that
same call and produce identical results.

### Off-device unit-testable pieces

`floor_input.py` and `motor_control.py` both import `rpi_gpio` **inside `main()`**, so the
modules import cleanly anywhere:

- `floor_input.CallBoard` is fully usable off-device — press/serve/message logic, including
  the rule that re-pressing a button does not reset `wait_start`.
- `motor_control.CarModel` constructs and `poll()` works, but `start_move()` raises
  `TypeError` until `SETTLE_TIME_PER_FLOOR` and `SETTLE_TIME_MIN` are set to real numbers.
  They are `None` placeholders. This is intentional (see Layer 3).

---

## Layer 3 — on-device only. Cannot be tested off the Pi.

### What will not build or run anywhere else

| File | Requires | Off-device behaviour |
|---|---|---|
| `src/floor_input.py` | `rpi_gpio` | imports fine; `main()` fails at `import rpi_gpio` |
| `src/motor_control.py` | `rpi_gpio` | imports fine; `main()` fails at `import rpi_gpio`, and refuses to start anyway until calibrated |
| `vision/vision_service.c` via `make vision` | `<vcapture/capture.h>`, `-lcapture` | will not compile; use `make vision-stub` |

### Unverified API details — do not trust without checking the QNX docs

These were deliberately not guessed at. Items marked **RESOLVED** have since been confirmed
against QNX's official rpi_gpio API comparison table and are recorded here so they are not
re-investigated; everything else remains open.

Source for the resolved rpi_gpio items:
<https://www.qnx.com/developers/docs/qnxeverywhere/com.qnx.doc.interfacing/topic/rpi/rpi_GPIO-apis.html>

**`capture.h` usage in `vision/vision_service.c`.** None of the following is confirmed:

- Exact spelling and existence of `capture_create_context`, `capture_set_property_i32`,
  `capture_create_buffers`, `capture_get_frame`, `capture_get_buffer`,
  `capture_release_frame`, `capture_destroy_context`.
- The `CAPTURE_PROPERTY_*` constants, and whether the `SRC_` or `DST_` variants are correct
  for a UVC source.
- Buffer ownership: whether `capture_get_frame` returns a borrowed pointer that must be
  released before the next call, and whether the returned index is into the buffer array we
  supplied.
- The timeout units on `capture_get_frame`, and the meaning of its return value.
- **Pixel format.** `blob.c` assumes packed YUYV 4:2:2 (`Y0 U Y1 V`). If the webcam negotiates
  MJPEG or NV12 instead, the unpacking in `sample_matches()` is wrong and must change. This is
  the single most likely thing to be wrong on first run.

The blob detection itself is tested and correct; only the camera glue is unverified. It is
isolated behind `cap_open` / `cap_frame` / `cap_release` / `cap_close` so it can be corrected
without touching the algorithm.

**`rpi_gpio` PWM in `src/motor_control.py`:**

- ~~Duty-cycle units.~~ **RESOLVED** — `ChangeDutyCycle()` is percentage-based (0–100) in
  QNX's rpi_gpio, same as Linux RPi.GPIO. `angle_to_duty_percent()` was already correct.
- The exact call that selects **MS mode**. QNX documents MS mode for servos, but the setter
  name and signature are unconfirmed — the line is present but commented out.
- `SERVO_MIN_PULSE_US` / `SERVO_MAX_PULSE_US` (1000/2000) are typical hobby-servo values, not
  measured for your servo.
- `SERVO_PIN = 18` is a placeholder and must be a hardware-PWM capable pin.

**`rpi_gpio` event detection in `src/floor_input.py`:**

- ~~Whether `add_event_detect` supports `bouncetime=`.~~ **RESOLVED — it does not.** The
  supported signature is `(channel, edge, callback=fn)` only, and QNX has no bouncetime-based
  debouncing anywhere. Passing it raises `TypeError` on-device. Debouncing is now done
  manually by the `Debouncer` class, covered by `make test-floor-input`.
- **These rpi_gpio functions do not exist on QNX** and must not be introduced:
  `add_event_callback()`, `wait_for_edge()`, `event_detected()`, `remove_event_detect()`,
  `getmode()`, `gpio_function()`, `setwarnings()`. Note `setmode()` *is* supported; it is
  `getmode()` that is absent. `tests/test_floor_input.py` fails the build if any appear.
- `GPIO.setmode(GPIO.BCM)` constant naming.
- The `pull_up_down=` keyword and the `GPIO.PUD_UP` constant.
- The BCM pin numbers in `BUTTONS` (17, 27, 22, 23) are placeholders for your wiring. Buttons
  are assumed wired to ground with internal pull-ups, so a press reads LOW and the edge of
  interest is FALLING.

### Servo calibration procedure

`FLOOR_ANGLES` in `src/motor_control.py` is `{1: None, 2: None, 3: None}`. These are **not**
real angles and were deliberately left unset rather than filled with plausible-looking
numbers. `check_calibrated()` runs at startup and refuses to launch while any placeholder
remains:

```
motor_control: refusing to start -- calibration values are still placeholders
(floors missing angles: [1, 2, 3]). Measure them on the rig and edit the
constants at the top of this file.
```

To calibrate:

1. **Detach the drive linkage first.** With uncalibrated angles the servo can slam the car
   into an end stop.
2. Jog the servo one degree at a time and record the angle at which the car sits level with
   each floor. Write those into `FLOOR_ANGLES`.
3. Time a one-floor move and set `SETTLE_TIME_PER_FLOOR` to that plus margin.
4. Set `SETTLE_TIME_MIN` to the minimum settle time for a zero-distance move.
5. Reattach the linkage and verify each floor individually before running the full system.

Steps 3 and 4 also unblock `CarModel.start_move()` for off-device testing.

### First on-device bring-up order

1. `make test` and `make test-pipeline` — confirm nothing regressed in the port.
2. Calibrate the servo with the linkage detached, as above.
3. `python3 src/dispatcher.py` alone, then `python3 src/floor_input.py`, and confirm button
   presses appear in the dispatcher log.
4. `make vision` and check the pixel format assumption before trusting any head count. Dump a
   raw frame and run `./build/test_blob capture.yuyv 640 480` to tune the colour window under
   the actual demo lighting — the values in `blob_default_params()` are a starting point, not
   a calibration.
5. Reattach the linkage and run all four processes, dispatcher first.
