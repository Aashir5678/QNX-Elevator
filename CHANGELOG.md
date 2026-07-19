# Changelog

Running record of every change to this project.

**Adding an entry:** one or two lines under the newest dated heading — what changed, which
file(s), and why. Add a new `### YYYY-MM-DD` heading when the date changes. Keep it terse;
this file is a log, not a design document. Rationale that outlives the change belongs in the
relevant doc (TESTING.md, DEPLOY.md, USAGE.md) or in a code comment.

---

## Unreleased

### 2026-07-18 — Camera pivot: capture.h dead, mock is the demo path

- **Added `tools/mock_vision.py`** — publishes head counts to the heads FIFO in
  `vision_service`'s exact wire format. Static, scripted, or interactive. Depends only on
  stdlib and `src/ipc.py`; no camera, no vision build, no capture backend. **This is the
  guaranteed demo path.** Added `make mock`.
- **Added `tests/test_mock_vision.py`** (`Makefile`) — feeds mock output through the real
  `Dispatcher` so wire-format drift fails at test time, not at the demo. Includes the
  suppressed-floor-is-omitted rule. Added `make test-mock`.
- **Three selectable capture backends** (`vision/vision_service.c`, `Makefile`) —
  `VISION_STUB_CAPTURE` (works), `VISION_SENSOR_FRAMEWORK` (new, unverified),
  `VISION_CAPTURE_H` (dead but kept, not deleted). Build fails with `#error` if not exactly
  one is selected. `blob.c` and the FIFO-publishing logic untouched.
- **Added Sensor Framework backend** (`vision/vision_service.c`) — targets Camera Module 3
  (IMX708). Every call marked `UNVERIFIED`; modelled on the external-camera
  `start_preview`/`get_preview_frame` pattern as a hypothesis. Never compiled against real
  headers. `make vision-sensor`.
- **Added `PIVOT.md`** — why `capture.h` is dead (package absent, verified via `find`, not a
  code bug), Sensor Framework status, and the explicit mock fallback.
- **Docs point at PIVOT.md for vision status** (`TESTING.md`, `README.md`, `USAGE.md`) —
  removed stale text describing `capture.h` as the active plan.
- **Noted `vision_service.c` ignores `ELEVATOR_FIFO_DIR`** (`TESTING.md`, `README.md`) — it
  hardcodes `/tmp/elevator` unlike the Python processes. Documented, not changed.

### 2026-07-18 — rpi_gpio API corrections (confirmed against QNX docs)

Source for all of the below: QNX's official rpi_gpio API comparison table —
<https://www.qnx.com/developers/docs/qnxeverywhere/com.qnx.doc.interfacing/topic/rpi/rpi_GPIO-apis.html>

- **Fixed `TypeError` on `add_event_detect`** (`src/floor_input.py`) — removed the
  `bouncetime=` kwarg, which crashed on-device. QNX supports no bouncetime-based debouncing
  anywhere; the supported signature is `(channel, edge, callback=fn)` only.
- **Added manual software debounce** (`src/floor_input.py`) — new `Debouncer` class gates the
  edge callback on a per-channel last-accepted timestamp, reusing `DEBOUNCE_MS` as the window.
  Rejected triggers deliberately do not extend the window, or chatter would leave the button
  permanently dead.
- **Added debounce and API-conformance tests** (`tests/test_floor_input.py`, `Makefile`) —
  covers the debounce logic against the real `CallBoard`, plus an AST-based guard that fails
  if `bouncetime=` or any QNX-unavailable rpi_gpio function is reintroduced. Added
  `make test-floor-input`; wired into `all` and `test-all`.
- **Confirmed `ChangeDutyCycle()` is percentage-based** (`src/motor_control.py`) — comment
  moved from unverified to confirmed. **No code change needed**: `angle_to_duty_percent()`
  already converted to a percentage (5–10% for 1–2ms pulses at 50Hz), so the existing
  assumption was correct.
- **Audited for QNX-unavailable rpi_gpio functions** — `add_event_callback`, `wait_for_edge`,
  `event_detected`, `remove_event_detect`, `getmode`, `gpio_function`, `setwarnings`: no call
  sites anywhere in the repo. No fixes required; now guarded by test.
- **MS-mode selection and `capture.h` remain unverified** — unchanged by this work.

### 2026-07-18 — Deployment, usage, and change documentation

- **Added `make deploy HOST=user@ip`** (`Makefile`) — scp's runtime files only (`src/*.py`
  plus the vision C sources) to the board; host-only `tests/`, `sim/`, and `test_blob.c` are
  deliberately excluded. Errors with usage if `HOST` is unset.
- **Deploy ships vision sources, not a binary** (`Makefile`) — `vision_service` needs an
  aarch64 QNX toolchain that a non-QNX host does not have, so it is built on the board. A
  prebuilt `build/vision_service` is shipped if one exists.
- **Pinned `.DEFAULT_GOAL := all` and grouped targets** (`Makefile`) — adding `deploy` above
  `all` had silently made it the default goal, so bare `make` tried to deploy. Pinned so
  target order cannot hijack `make` again.
- **Added `DEPLOY.md`** — scp/ssh over LAN as the primary path, with remote-root-login and
  macOS `scp -O` marked *confirm on first connect* rather than stated as fact; SD-card and USB
  fallbacks for if sshd is not enabled on this image.
- **Added `USAGE.md`** — startup order and why, confirming processes with `pidin ar`, an
  end-to-end walkthrough of a demo run, and troubleshooting. Records that no LED/indicator
  output exists in the current code; the observable signals are process logs and car movement.
- **Troubleshooting points at TESTING.md Layer 3** (`USAGE.md`) — the on-device unknowns are
  documented once, in TESTING.md, and referenced rather than repeated.
- **Trimmed README's "Running on the Pi"** (`README.md`) — now a short pointer to DEPLOY.md
  and USAGE.md instead of duplicating the run procedure; added a documentation index table.
- **Added this file** (`CHANGELOG.md`).

### Earlier — backfilled, predates this changelog

Dates not recorded at the time; these all landed before the entries above.

- **Fixed `make vision-stub` build failure** (`Makefile`) — added
  `-D_POSIX_C_SOURCE=200809L`, since strict `-std=c99` hides `struct timespec` and
  `nanosleep()` on host glibc. Uses `200809L`, not `199309L`: POSIX.1b predates `snprintf`
  joining POSIX and hides it on Darwin, breaking the build in the other direction.
- **Added FIFO pipeline integration tests** (`tests/test_pipeline.py`, `Makefile`) — real
  `Dispatcher`, real `ipc` classes, real named FIFOs in a temp dir, no mocks. Covers
  `ensure_fifos` creating all six channels, heads-before-calls crediting the full count,
  calls-without-heads dispatching immediately on the minimum-credited-1 rule, and
  no-preemption once committed. Added `make test-pipeline` and `make test-all`.
- **Added `TESTING.md`** — full test procedure in three layers: pure logic, full pipeline with
  synthetic camera, and on-device only. Restates the unverified `capture.h` and `rpi_gpio`
  items in full so it stands alone as a runbook.
- **Corrected README claim about `CarModel`** (`README.md`) — it is not fully hardware-free:
  `start_move()` raises `TypeError` until the `SETTLE_TIME_*` placeholders are calibrated.
  `CallBoard` is genuinely hardware-free.
- **Initial implementation** — four processes (`vision_service`, `floor_input`, `dispatcher`,
  `motor_control`), FIFO IPC layer, priority+aging dispatch logic, from-scratch YUV threshold
  and connected-component blob counting, and a hardware-free simulator for tuning
  `aging_factor`.
