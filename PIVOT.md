# Vision pipeline: current status

Status of frame acquisition on `qnxpi12`. This file is the source of truth for what is
actually running; TESTING.md and README point here rather than restating it.

**Last updated:** 2026-07-18

---

## Bottom line

| Path | Status |
|---|---|
| **Mock input** (`tools/mock_vision.py`) | ✅ **Working, tested, guaranteed demo path** |
| Video Capture framework (`capture.h`) | ❌ **Dead** — package not installed on this board |
| Sensor Framework (Camera Module 3) | ⚠️ **Attempted, unverified** — C API is a hypothesis, not confirmed |

**The demo runs on mock input.** Everything downstream of frame acquisition — blob detection,
FIFO publishing, dispatch, servo control — is unchanged and tested. Only the source of head
counts differs.

---

## What was tried first: Video Capture framework (`capture.h`)

`vision_service.c` was originally written against QNX's Video Capture framework, targeting a
UVC USB webcam.

**Why it's dead:** the package is not installed on this board. Confirmed by:

```
find / -iname "capture.h"        -> empty
find / -iname "libcapture*"      -> empty
```

Neither the header nor the library exists on the image. **This is not a code bug** — the
original `capture.h` code was never compiled against real headers, so it was never proven
wrong, just unrunnable. It remains in the source behind `VISION_CAPTURE_H` in case the package
is ever installed; it was not deleted.

## What's being tried now: Sensor Framework (Camera Module 3)

QNX's Quick Start Target Image supports the Raspberry Pi Camera Module 3 (IMX708 — the camera
physically on this board) through the **Sensor Framework**, a different subsystem from
`capture.h`, exercised by a `camera_example3_viewfinder` tool and a `sensor` service process.

**Current status:**

- `camera_example3_viewfinder` producing a live image: **NOT YET RUN.** Nobody has confirmed
  the camera produces a picture on this board yet. This is the first thing to check, and it
  gates everything else — if the viewfinder does not show an image, the C integration is
  irrelevant.
- C integration in `vision_service.c`: **ATTEMPTED, UNVERIFIED.** A `sensor` backend exists
  behind `VISION_SENSOR_FRAMEWORK`, modelled on the external-camera example's
  `start_preview()` / `stop_preview()` / `get_preview_frame()` pattern. **That pattern is a
  starting hypothesis, not a confirmed API.** Every call in that backend is marked
  `UNVERIFIED` in-file. It has never been compiled against real Sensor Framework headers.
- Pixel format: **unknown.** `blob.c` assumes packed YUYV 4:2:2. IMX708 may deliver something
  else entirely. If so, the unpacking in `sample_matches()` needs changing — `blob.c` itself
  is correct and tested, and must not be edited speculatively.

**Realistic assessment:** the Sensor Framework backend is a skeleton with the right shape and
almost certainly the wrong function names. Treat it as a starting point for someone with the
docs open, not as something close to working.

---

## Fallback: mock input is the demo path

`tools/mock_vision.py` publishes head counts to the heads FIFO in exactly the format
`vision_service` uses. The dispatcher cannot tell the difference.

```
python3 src/dispatcher.py &          # first, creates the FIFOs
python3 tools/mock_vision.py --interactive
```

Then type `1=5` to put five people on floor 1 and watch the dispatch decision. Also:
`--counts 1=5,3=1` for static values, `--scenario rush` for a scripted sequence, `make mock`
for the interactive default.

**It depends on nothing in `vision/`.** Its only imports are stdlib plus `src/ipc.py`. It
cannot be broken by anything that happens to either camera backend. `make test-mock` asserts
its output is consumed correctly by the real `Dispatcher`, including the
suppressed-floor-is-omitted rule.

This is the intended demo path, not an emergency measure. The scheduling algorithm — which is
what the demo is actually about — is fully exercised by it.

---

## If picking this up with time to spare

In order, stopping at the first failure:

1. Run `camera_example3_viewfinder` on the board. Does a live image appear? If not, stop —
   the camera is not working and no amount of C is going to fix that.
2. Find the real Sensor Framework headers on the board (`find / -iname "*sensor*.h"`) and
   check the actual function signatures against the `UNVERIFIED` markers in
   `vision/vision_service.c`.
3. Determine the pixel format the sensor delivers before trusting any head count. A wrong
   format produces plausible-looking garbage while every process appears healthy.
4. Build with `make vision-sensor`. It will not compile until step 2 is done.

Do not spend demo-critical time on this. The mock path already demonstrates the algorithm.
