/* vision_service.c -- camera -> per-floor head counts.
 *
 * Polls the camera roughly once a second, splits each frame into three
 * horizontal ROI bands (top=floor 3, bottom=floor 1), counts target-coloured
 * blobs in each, and publishes the counts to a FIFO.
 *
 * Reads FIFO_CARPOS to learn where the car is, and suppresses that band so
 * the car itself is not miscounted as a head.
 *
 * =====================================================================
 * THREE SELECTABLE CAPTURE BACKENDS -- see PIVOT.md for current status.
 *
 * Everything below the capture layer (blob.c, carpos_poll, publish, main)
 * is tested and correct and is NOT backend-specific. Only frame acquisition
 * differs. Select exactly one at build time:
 *
 *   -DVISION_STUB_CAPTURE       (make vision-stub)   WORKS TODAY.
 *       Synthetic frames, no camera. Exercises the entire pipeline.
 *
 *   -DVISION_SENSOR_FRAMEWORK   (make vision-sensor) UNVERIFIED, CURRENT TARGET.
 *       QNX Sensor Framework, for the Raspberry Pi Camera Module 3 (IMX708)
 *       that is physically on this board. Every call in this backend is
 *       marked UNVERIFIED -- the signatures are a HYPOTHESIS modelled on the
 *       external-camera example's start_preview/get_preview_frame/stop_preview
 *       pattern, NOT a confirmed API. Has never been compiled against real
 *       headers. Expect the function names to be wrong.
 *
 *   -DVISION_CAPTURE_H          (make vision)        DEAD ON THIS BOARD.
 *       The original Video Capture framework path, kept rather than deleted
 *       in case the package is ever installed. `find / -iname "capture.h"`
 *       and `find / -iname "libcapture*"` are both EMPTY on qnxpi12, so this
 *       cannot build here. Not a code bug -- the package is simply absent.
 *
 * If none is defined the build fails with an #error rather than silently
 * picking one.
 *
 * PIXEL FORMAT applies to every backend: blob.c assumes packed YUYV 4:2:2
 * (Y0 U Y1 V). If the sensor delivers anything else, the unpacking in
 * blob.c's sample_matches() must change. blob.c is tested and correct --
 * do not edit it speculatively.
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

/* Exactly one backend must be selected. */
#if defined(VISION_STUB_CAPTURE) + defined(VISION_SENSOR_FRAMEWORK) \
  + defined(VISION_CAPTURE_H) != 1
#error "define exactly one of VISION_STUB_CAPTURE, VISION_SENSOR_FRAMEWORK, VISION_CAPTURE_H"
#endif

#ifdef VISION_CAPTURE_H
#include <vcapture/capture.h>
#endif

#ifdef VISION_SENSOR_FRAMEWORK
/* UNVERIFIED: header name and location are a guess. On the board, locate the
 * real one with:  find / -iname "*sensor*.h"  */
#include <sensor/sensor_api.h>
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

#elif defined(VISION_SENSOR_FRAMEWORK)
/* ===================================================================
 * QNX Sensor Framework backend -- Raspberry Pi Camera Module 3 (IMX708).
 *
 * !! EVERY CALL IN THIS BACKEND IS UNVERIFIED !!
 *
 * This is modelled on the external-camera example's start_preview() /
 * get_preview_frame() / stop_preview() pattern. That pattern is a STARTING
 * HYPOTHESIS, not a confirmed API. This code has never been compiled against
 * real Sensor Framework headers and the function names are quite likely
 * wrong. Check each UNVERIFIED marker against the on-board headers before
 * assuming anything here is right. See PIVOT.md.
 *
 * Before touching this at all, confirm the camera works standalone:
 *     camera_example3_viewfinder
 * If that does not produce a live image, nothing below matters.
 * =================================================================== */
typedef struct {
    /* UNVERIFIED: handle type and name. The example uses a camera/sensor
     * handle of some description; the real type must be taken from the
     * on-board headers. */
    sensor_handle_t handle;
    const uint8_t  *frame;   /* borrowed pointer to the current frame, or NULL */
    int             started;
} cap_ctx;

static int cap_open(cap_ctx *c)
{
    memset(c, 0, sizeof(*c));

    /* UNVERIFIED: does the Sensor Framework need an explicit connect/open
     * before preview starts, and does it take a device name, an index, or a
     * unit enum? Guessing a name-based open. */
    c->handle = sensor_open("/dev/sensor/camera0");
    if (c->handle == NULL) {
        fprintf(stderr, "vision(sensor): sensor_open failed -- is the `sensor` "
                        "service running? Check PIVOT.md.\n");
        return -1;
    }

    /* UNVERIFIED: whether format/resolution are negotiated here, and whether
     * the sensor can be asked for YUYV at all. blob.c requires packed YUYV
     * 4:2:2; IMX708 may only offer RAW or NV12, in which case a conversion
     * step is needed here (NOT a change to blob.c). */
    if (sensor_set_format(c->handle, SENSOR_FORMAT_YUY2,
                          FRAME_WIDTH, FRAME_HEIGHT) != 0) {
        fprintf(stderr, "vision(sensor): sensor_set_format failed -- the "
                        "sensor may not offer YUYV; see PIVOT.md\n");
        sensor_close(c->handle);
        return -1;
    }

    /* UNVERIFIED: start_preview signature, and whether preview is even the
     * right mode for pulling raw buffers rather than rendering to a display. */
    if (start_preview(c->handle) != 0) {
        fprintf(stderr, "vision(sensor): start_preview failed\n");
        sensor_close(c->handle);
        return -1;
    }
    c->started = 1;
    return 0;
}

static const uint8_t *cap_frame(cap_ctx *c)
{
    /* UNVERIFIED: return convention. Does it return a borrowed pointer, fill
     * a caller-supplied buffer, or return an index into a ring? Does it block
     * until a frame is ready, and is there a timeout argument? Assuming a
     * borrowed pointer plus an out-param for size, which must be released
     * before the next call. */
    size_t len = 0;
    const uint8_t *buf = get_preview_frame(c->handle, &len);
    if (buf == NULL) return NULL;

    /* Guard against a short buffer regardless of what the API turns out to
     * do -- blob.c would read out of bounds otherwise. This check is correct
     * even if everything above is wrong. */
    if (len < (size_t)FRAME_WIDTH * 2 * FRAME_HEIGHT) {
        fprintf(stderr, "vision(sensor): frame too small (%zu bytes, need %zu) "
                        "-- wrong pixel format? see PIVOT.md\n",
                len, (size_t)FRAME_WIDTH * 2 * FRAME_HEIGHT);
        return NULL;
    }
    c->frame = buf;
    return buf;
}

static void cap_release(cap_ctx *c)
{
    /* UNVERIFIED: whether a per-frame release exists at all. If frames are
     * copied rather than borrowed this is a no-op and can be deleted. */
    if (c->frame) {
        release_preview_frame(c->handle, c->frame);
        c->frame = NULL;
    }
}

static void cap_close(cap_ctx *c)
{
    /* UNVERIFIED: teardown order and function names. */
    if (c->started) {
        stop_preview(c->handle);
        c->started = 0;
    }
    if (c->handle) {
        sensor_close(c->handle);
        c->handle = NULL;
    }
}

#else /* VISION_CAPTURE_H */
/* ===================================================================
 * Original Video Capture framework backend.
 *
 * DEAD ON qnxpi12 -- neither capture.h nor libcapture is installed
 * (`find / -iname "capture.h"` and `find / -iname "libcapture*"` both empty).
 * Kept rather than deleted in case the package is installed later.
 *
 * These calls were never confirmed either -- they remain as originally
 * written, unverified against real headers.
 * =================================================================== */
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
