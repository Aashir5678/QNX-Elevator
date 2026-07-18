CC      ?= cc
CFLAGS  ?= -std=c99 -Wall -Wextra -O2
BUILD   := build

.PHONY: all test sim clean vision-stub

# Default target builds only what works off-device.
all: test vision-stub

$(BUILD):
	@mkdir -p $(BUILD)

# Host-side blob tests -- no QNX, no camera.
$(BUILD)/test_blob: vision/test_blob.c vision/blob.c vision/blob.h | $(BUILD)
	$(CC) $(CFLAGS) -o $@ vision/test_blob.c vision/blob.c

test: $(BUILD)/test_blob
	./$(BUILD)/test_blob

# Full pipeline minus the camera: synthetic frames, real FIFOs.
vision-stub: $(BUILD)
	$(CC) $(CFLAGS) -DVISION_STUB_CAPTURE -o $(BUILD)/vision_stub \
		vision/vision_service.c vision/blob.c

# ON-DEVICE ONLY. Needs QNX SDP 8.0.0 headers and libcapture; the capture.h
# calls in vision_service.c are unverified -- see the banner in that file.
vision: $(BUILD)
	$(CC) $(CFLAGS) -o $(BUILD)/vision_service \
		vision/vision_service.c vision/blob.c -lcapture

sim:
	python3 sim/simulate.py --sweep

clean:
	rm -rf $(BUILD)
