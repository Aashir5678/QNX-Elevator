/* vision_service.c -- webcam -> per-floor head counts.
 *
 * Polls the UVC camera roughly once a second, splits each frame into three
 * horizontal ROI bands (top=floor 3, bottom=floor 1), counts target-coloured
 * blobs in each, and publishes the counts to a FIFO.
 *
 * Reads FIFO_CARPOS to learn where the car is, and suppresses that band so
 * the car itself is not miscounted as a head.
 *
 * =====================================================================
 * !! THE capture.h CALLS IN THIS FILE ARE UNVERIFIED !!
 *
 * The blob detection (blob.c) is tested and correct -- see test_blob.
 * The camera glue below is written from the general shape of the QNX Video
 * Capture API and MUST be checked against the SDP 8.0.0 documentation on
 * qnxpi12 before it is trusted. In particular these are NOT confirmed:
 *
 *   - exact spelling/existence of capture_create_context, capture_set_property_i32,
 *     capture_create_buffers, capture_get_frame, capture_release_frame,
 *     capture_destroy_context
 *   - the property constants (CAPTURE_PROPERTY_*) and whether SRC_ vs DST_
 *     variants are the right ones for a UVC source
 *   - buffer ownership: whether capture_get_frame returns a borrowed pointer
 *     that must be released before the next call, and whether the returned
 *     index is into the buffer array we supplied
 *   - whether YUYV (CAPTURE_PROPERTY_DST_FORMAT / CAPTURE_FRAMETYPE_YUY2) is
 *     what this particular webcam negotiates -- if it hands back MJPEG or
 *     NV12 instead, blob.c's YUYV assumption is wrong and the sample_matches
 *     unpacking must change
 *
 * Build with -DVISION_STUB_CAPTURE to compile and exercise everything except
 * the camera, using synthetic frames. That path works today.
 * =====================================================================
 */

#include "blob.h"

#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#ifndef VISION_STUB_CAPTURE
#include <vcapture/capture.h>
#endif

#define FIFO_HEADS  "/tmp/elevator/heads"
#define FIFO_CARPOS "/tmp/elevator/carpos"

#define FRAME_WIDTH   640
#define FRAME_HEIGHT  480
#define POLL_SECONDS  1.0
#define NUM_BUFFERS   4

static volatile sig_atomic_t running = 1;
static void on_sigint(int s) { (void)s; running = 0; }

/* ---------------------------------------------------------------- carpos */

/* Reads the latest {"car_floor": N} line. Deliberately a tiny hand-rolled
 * scan rather than a JSON parser -- the message is fixed-shape and this
 * avoids a dependency. Returns the last value seen, or keeps the previous. */
static int carpos_poll(int fd, int current)
{
    static char buf[512];
    static size_t len = 0;
    ssize_t n;

    while ((n = read(fd, buf + len, sizeof(buf) - len - 1)) > 0) {
        len += (size_t)n;
        if (len >= sizeof(buf) - 1) len = 0; /* overlong garbage, resync */
    }
    if (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK) return current;

    buf[len] = '\0';
    char *last = NULL, *p = buf;
    while ((p = strstr(p, "\"car_floor\":")) != NULL) { last = p; p += 12; }
    if (last) {
        int v = atoi(last + 12);
        if (v >= 1 && v <= NUM_FLOORS) current = v;
    }

    /* Keep only the trailing partial line for the next poll. */
    char *nl = strrchr(buf, '\n');
    if (nl) {
        size_t rest = len - (size_t)(nl + 1 - buf);
        memmove(buf, nl + 1, rest);
        len = rest;
    }
    return current;
}

/* ----------------------------------------------------------------- output */

static void publish(int fd, const floor_counts *fc)
{
    char line[256];
    int off = snprintf(line, sizeof(line), "{\"heads\":{");
    int first = 1;
    for (int i = 0; i < NUM_FLOORS; i++) {
        /* A suppressed floor is OMITTED, not sent as zero -- the dispatcher
         * retains its last known count for absent floors. Sending zero would
         * wrongly tell it the floor emptied. */
        if (fc->suppressed[i]) continue;
        off += snprintf(line + off, sizeof(line) - (size_t)off, "%s\"%d\":%d",
                        first ? "" : ",", i + 1, fc->counts[i]);
        first = 0;
    }
    off += snprintf(line + off, sizeof(line) - (size_t)off, "}}\n");

    ssize_t w = write(fd, line, (size_t)off);
    (void)w; /* dropped samples are fine: next poll republishes full state */
}

/* ---------------------------------------------------------------- capture */

#ifdef VISION_STUB_CAPTURE
/* Synthetic source so the whole pipeline runs without a camera. */
typedef struct { uint8_t *buf; } cap_ctx;

static int cap_open(cap_ctx *c)
{
    c->buf = calloc((size_t)FRAME_WIDTH * 2 * FRAME_HEIGHT, 1);
    return c->buf ? 0 : -1;
}
static const uint8_t *cap_frame(cap_ctx *c)
{
    /* One on-target square in the bottom band, so counts are non-trivial. */
    memset(c->buf, 0, (size_t)FRAME_WIDTH * 2 * FRAME_HEIGHT);
    for (int row = 380; row < 410; row++)
        for (int col = 100; col < 140; col += 2) {
            uint8_t *q = c->buf + (size_t)row * FRAME_WIDTH * 2 + (size_t)(col / 2) * 4;
            q[0] = 128; q[1] = 60; q[2] = 128; q[3] = 200;
        }
    return c->buf;
}
static void cap_release(cap_ctx *c) { (void)c; }
static void cap_close(cap_ctx *c) { free(c->buf); }

#else
typedef struct {
    capture_context_t ctx;
    int last_idx;
} cap_ctx;

static int cap_open(cap_ctx *c)
{
    /* UNVERIFIED -- see the banner at the top of this file. */
    c->ctx = capture_create_context(CAPTURE_DEVICE_CLASS_VIDEO);
    if (c->ctx == NULL) {
        perror("capture_create_context");
        return -1;
    }
    capture_set_property_i32(c->ctx, CAPTURE_PROPERTY_DST_FORMAT, CAPTURE_FRAMETYPE_YUY2);
    capture_set_property_i32(c->ctx, CAPTURE_PROPERTY_DST_WIDTH,  FRAME_WIDTH);
    capture_set_property_i32(c->ctx, CAPTURE_PROPERTY_DST_HEIGHT, FRAME_HEIGHT);
    capture_set_property_i32(c->ctx, CAPTURE_PROPERTY_DST_NBUFFERS, NUM_BUFFERS);

    if (capture_create_buffers(c->ctx, CAPTURE_PROPERTY_DST_BUFFERS) != 0) {
        perror("capture_create_buffers");
        return -1;
    }
    c->last_idx = -1;
    return 0;
}

static const uint8_t *cap_frame(cap_ctx *c)
{
    /* UNVERIFIED: timeout units and the meaning of the return value. */
    int idx = capture_get_frame(c->ctx, POLL_SECONDS * 1000000ULL, 0);
    if (idx < 0) return NULL;
    c->last_idx = idx;
    return (const uint8_t *)capture_get_buffer(c->ctx, CAPTURE_PROPERTY_DST_BUFFERS, idx);
}

static void cap_release(cap_ctx *c)
{
    if (c->last_idx >= 0) {
        capture_release_frame(c->ctx, (uint32_t)c->last_idx);
        c->last_idx = -1;
    }
}

static void cap_close(cap_ctx *c) { capture_destroy_context(c->ctx); }
#endif

/* -------------------------------------------------------------------- main */

int main(void)
{
    signal(SIGINT, on_sigint);

    int heads_fd = open(FIFO_HEADS, O_WRONLY | O_NONBLOCK);
    if (heads_fd < 0) {
        fprintf(stderr, "vision: no reader on %s yet (%s); "
                        "start dispatcher first\n", FIFO_HEADS, strerror(errno));
        return 1;
    }
    int carpos_fd = open(FIFO_CARPOS, O_RDONLY | O_NONBLOCK);
    if (carpos_fd < 0) {
        fprintf(stderr, "vision: cannot open %s: %s\n", FIFO_CARPOS, strerror(errno));
        return 1;
    }

    cap_ctx cap;
    if (cap_open(&cap) != 0) return 1;

    blob_params params = blob_default_params();
    size_t slen = blob_scratch_size(FRAME_WIDTH, FRAME_HEIGHT);
    int32_t *scratch = malloc(slen * sizeof(int32_t));
    if (!scratch) { fprintf(stderr, "vision: oom\n"); return 1; }

    int car_floor = 1;
    fprintf(stderr, "vision: up, polling every %.1fs\n", POLL_SECONDS);

    while (running) {
        car_floor = carpos_poll(carpos_fd, car_floor);

        const uint8_t *frame = cap_frame(&cap);
        if (frame) {
            floor_counts fc;
            if (blob_count_frame(frame, FRAME_WIDTH, FRAME_HEIGHT,
                                 FRAME_WIDTH * 2, &params, car_floor,
                                 &fc, scratch, slen) == 0) {
                publish(heads_fd, &fc);
            } else {
                fprintf(stderr, "vision: blob_count_frame failed\n");
            }
            cap_release(&cap);
        }

        struct timespec ts = { (time_t)POLL_SECONDS,
                               (long)((POLL_SECONDS - (long)POLL_SECONDS) * 1e9) };
        nanosleep(&ts, NULL);
    }

    cap_close(&cap);
    free(scratch);
    close(heads_fd);
    close(carpos_fd);
    return 0;
}
