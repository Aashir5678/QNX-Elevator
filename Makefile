CC      ?= cc
# _POSIX_C_SOURCE is required: strict -std=c99 hides struct timespec and
# nanosleep() behind the POSIX feature-test macro on host glibc, which breaks
# vision-stub.
#
# It must be 200112L or later, NOT 199309L. POSIX.1b (199309L) exposes
# nanosleep but predates snprintf joining POSIX, so on Darwin it hides
# snprintf and vision_service.c fails to compile. 200809L (POSIX.1-2008)
# exposes both and builds clean on Darwin and glibc alike.
CFLAGS  ?= -std=c99 -D_POSIX_C_SOURCE=200809L -Wall -Wextra -O2
BUILD   := build

# Pinned explicitly so adding a target above `all` can never hijack bare `make`.
.DEFAULT_GOAL := all

.PHONY: all test test-pipeline test-all sim clean vision-stub vision deploy

# Default target builds and runs everything that works off-device.
all: test test-pipeline vision-stub

# Every off-device test in one go.
test-all: test test-pipeline

$(BUILD):
	@mkdir -p $(BUILD)

# --- tests ----------------------------------------------------------------

# Host-side blob tests -- no QNX, no camera.
$(BUILD)/test_blob: vision/test_blob.c vision/blob.c vision/blob.h | $(BUILD)
	$(CC) $(CFLAGS) -o $@ vision/test_blob.c vision/blob.c

test: $(BUILD)/test_blob
	./$(BUILD)/test_blob

# Dispatcher <-> vision/motor FIFO pipeline, over real named FIFOs in a
# temporary directory. No hardware, no camera.
test-pipeline:
	python3 tests/test_pipeline.py

sim:
	python3 sim/simulate.py --sweep

# --- builds ---------------------------------------------------------------

# Full pipeline minus the camera: synthetic frames, real FIFOs.
vision-stub: $(BUILD)
	$(CC) $(CFLAGS) -DVISION_STUB_CAPTURE -o $(BUILD)/vision_stub \
		vision/vision_service.c vision/blob.c

# ON-DEVICE ONLY. Needs QNX SDP 8.0.0 headers and libcapture; the capture.h
# calls in vision_service.c are unverified -- see the banner in that file.
vision: $(BUILD)
	$(CC) $(CFLAGS) -o $(BUILD)/vision_service \
		vision/vision_service.c vision/blob.c -lcapture

# --- deployment (see DEPLOY.md) -------------------------------------------
#
# UNVERIFIED UNTIL TRIED AGAINST THE REAL BOARD ONCE. Two specifics are
# expected to need adjusting on first connect, both documented in DEPLOY.md:
#
#   1. Remote root login may be disabled. If so, deploy as the default
#      non-root user (that is what HOST should name) and `su root` in the ssh
#      session afterwards for GPIO access.
#   2. macOS scp now speaks SFTP by default. If the board's implementation
#      does not support it, force the legacy protocol:
#          make deploy HOST=qnxuser@192.168.1.50 SCP_FLAGS=-O
#
# SCP_FLAGS defaults to empty rather than -O because which one is needed is
# exactly what has not been checked yet. Try plain first, add -O if it fails.
HOST      ?=
DEST      ?= elevator
SCP_FLAGS ?=

# Runtime files only -- the host-only test and simulation files (tests/,
# sim/, vision/test_blob.c) are deliberately NOT deployed.
DEPLOY_PY := src/core.py src/ipc.py src/dispatcher.py \
             src/floor_input.py src/motor_control.py

# vision_service must be built for aarch64 QNX, which cannot be done on a
# non-QNX host without the SDP cross-toolchain. So the SOURCES ship and are
# built on the board (`make vision`). If a real cross-compiled or on-device
# binary exists at build/vision_service it is shipped too -- but note that
# `make vision-stub` produces build/vision_stub, a different and host-only
# file that is never deployed.
DEPLOY_VISION := vision/blob.c vision/blob.h vision/vision_service.c

deploy:
ifeq ($(HOST),)
	@echo "usage: make deploy HOST=user@ip [DEST=elevator] [SCP_FLAGS=-O]" >&2
	@echo "see DEPLOY.md -- scp flags are unverified against this board" >&2
	@exit 1
endif
	@echo "deploying to $(HOST):$(DEST)"
	ssh $(HOST) 'mkdir -p $(DEST)/src $(DEST)/vision'
	scp $(SCP_FLAGS) $(DEPLOY_PY) $(HOST):$(DEST)/src/
	scp $(SCP_FLAGS) $(DEPLOY_VISION) $(HOST):$(DEST)/vision/
	scp $(SCP_FLAGS) Makefile $(HOST):$(DEST)/
	@if [ -f $(BUILD)/vision_service ]; then \
		echo "shipping prebuilt $(BUILD)/vision_service"; \
		ssh $(HOST) 'mkdir -p $(DEST)/$(BUILD)'; \
		scp $(SCP_FLAGS) $(BUILD)/vision_service $(HOST):$(DEST)/$(BUILD)/; \
	else \
		echo "no $(BUILD)/vision_service to ship -- build it on the board:"; \
		echo "    ssh $(HOST) 'cd $(DEST) && make vision'"; \
	fi

# --------------------------------------------------------------------------

clean:
	rm -rf $(BUILD)
