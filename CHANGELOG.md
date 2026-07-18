# Changelog

Running record of every change to this project.

**Adding an entry:** one or two lines under the newest dated heading — what changed, which
file(s), and why. Add a new `### YYYY-MM-DD` heading when the date changes. Keep it terse;
this file is a log, not a design document. Rationale that outlives the change belongs in the
relevant doc (TESTING.md, DEPLOY.md, USAGE.md) or in a code comment.

---

## Unreleased

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
