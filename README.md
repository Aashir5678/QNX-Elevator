# QNX-Elevator

An elevator that moves as many people as possible by prioritizing floors with the most people.

A 3-floor model on a Raspberry Pi 5 running QNX SDP 8.0.0 (`qnxpi12`, aarch64). Four
processes communicate over named FIFOs; dispatch is priority + aging rather than
first-come-first-served.

## Layout

```
src/core.py             priority formula and target selection (pure, no I/O)
src/ipc.py              named-FIFO line protocol shared by all processes
src/dispatcher.py       the decision loop and its state machine
src/floor_input.py      4 hall-call buttons via rpi_gpio
src/motor_control.py    servo via rpi_gpio PWM, publishes car position
vision/blob.c/.h        YUV threshold + connected-component blob counting
vision/vision_service.c capture.h glue and the publish loop
vision/test_blob.c      host-side tests for the blob code
sim/simulate.py         hardware-free simulation and aging_factor tuning
```

## Start here: tuning without hardware

```
make sim                              # aging_factor sweep on the default scenario
python3 sim/simulate.py               # verbose trace of every dispatch decision
python3 sim/simulate.py --list-scenarios
python3 sim/simulate.py --scenario starvation --sweep
```

The simulator drives the **real** `Dispatcher` class from `dispatcher.py` against a virtual
clock and mock FIFO messages in the true wire format, so anything it validates carries over
to the live system. Only the clock, the buttons, and the car are fake.

`--sweep` also runs an FCFS baseline for comparison. On `crowd_vs_first` the priority
algorithm cuts average wait from 9.00s to 5.80s — a 36% improvement, which is the number the
demo is built to show.

### Choosing `aging_factor`

`aging_factor` (top of `src/core.py`) is the priority gained per second of waiting — the
exchange rate between people waiting and seconds waited. At `0.10`, ten seconds of waiting is
worth one extra person.

The `starvation` scenario is what justifies the parameter existing. Floor 1 refills faster
than the car can serve it, so a lone rider on floor 3 competes with a permanent crowd:

| aging_factor | avg wait | worst wait |
|---|---|---|
| 0.00 | 4.03s | **48.3s** (rider starves) |
| 0.10 | 4.66s | 27.3s |
| 0.25+ | 4.41s | 13.2s |

Pure headcount greedy wins on average and is unacceptable on worst case. Pick from this
curve based on how much tail latency the demo should tolerate; `0.25` is the knee.

## Running on the Pi

Start `dispatcher` first — the other three need its FIFO readers open before they can publish.

```
python3 src/dispatcher.py &
python3 src/floor_input.py &
python3 src/motor_control.py &      # refuses to run until calibrated, see below
make vision && ./build/vision_service &
```

FIFOs live in `/tmp/elevator` (override with `ELEVATOR_FIFO_DIR`).

## Independent development

Each piece runs without the others:

- **Blob detection**, no camera and no QNX: `make test`. To tune the colour window against a
  real capture, dump a raw YUYV frame and run `./build/test_blob capture.yuyv 640 480`.
- **Vision pipeline**, no camera: `make vision-stub` builds `vision_service` against a
  synthetic frame source, exercising the FIFO publishing and occlusion suppression for real.
- **Dispatch logic**, no hardware at all: `sim/simulate.py`.
- **Motor and button logic**: `CarModel` and `CallBoard` are hardware-free classes; only
  `main()` in each file touches `rpi_gpio`.

## Things that must be verified on-device

These are flagged in-file and were deliberately not guessed at:

1. **`capture.h` usage** (`vision/vision_service.c`) — function names, property constants,
   buffer ownership, and whether the webcam actually negotiates YUYV are all unconfirmed.
   `blob.c` assumes packed YUYV 4:2:2; if the camera hands back MJPEG or NV12 the unpacking
   in `sample_matches` must change. See the banner at the top of that file.
2. **`rpi_gpio` PWM duty-cycle units** (`src/motor_control.py`) — RPi.GPIO-style APIs take a
   percent, pigpio-style take microseconds, and they are not interchangeable. Getting this
   wrong drives the servo into a hard stop. Also unconfirmed: the exact call that selects MS
   mode.
3. **`rpi_gpio` event detection** (`src/floor_input.py`) — whether `add_event_detect`
   supports `bouncetime=`, and the `PUD_UP` constant naming.
4. **Servo calibration** (`src/motor_control.py`) — `FLOOR_ANGLES` are `None` placeholders,
   not invented numbers. The process refuses to start until they are measured. Detach the
   drive linkage before jogging the servo.

## Design notes

- **Occlusion suppression.** `motor_control` publishes the car's floor continuously;
  `vision_service` skips that ROI band so the car isn't counted as a head. A suppressed floor
  is *omitted* from the heads message rather than sent as zero, and the dispatcher retains the
  last known count for absent floors — otherwise the car parked at a floor would blind the
  system to a crowd building up there. The count resets to zero on arrival, since the car
  just emptied that floor.
- **Minimum credited heads.** A floor with an active call is always credited at least one
  person even if vision sees none. The button was physically pressed, so someone is there
  regardless of what the camera thinks.
- **Re-pressing a button doesn't reset the wait.** `wait_start` is kept from the first press,
  so an impatient rider can't zero out their own aging bonus.
- **No preemption.** A committed trip runs to completion; the dispatcher only re-decides when
  idle. This is why simulated scenarios need the car to be busy before the strategies diverge.
- **FIFOs over POSIX message queues**, since Python mqueue bindings aren't confirmed on this
  image. Every channel publishes full state rather than deltas, so a dropped message is
  self-healing — the next one resynchronizes the reader.
