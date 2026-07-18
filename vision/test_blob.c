/* test_blob.c -- host-side tests for the threshold + labelling code.
 *
 * Builds and runs anywhere with a C99 compiler; no QNX, no camera.
 *   cc -std=c99 -Wall -Wextra -o test_blob vision/test_blob.c vision/blob.c
 *   ./test_blob                 # synthetic self-tests
 *   ./test_blob capture.yuyv W H  # count blobs in a real dump
 */

#include "blob.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int failures = 0;

static void check(const char *name, int got, int want)
{
    if (got == want) {
        printf("  ok   %-40s %d\n", name, got);
    } else {
        printf("  FAIL %-40s got %d want %d\n", name, got, want);
        failures++;
    }
}

/* Paint a filled rectangle of "target colour" into a YUYV frame. */
static void paint(uint8_t *f, int w, int stride, int x, int y, int rw, int rh,
                  uint8_t Y, uint8_t U, uint8_t V)
{
    for (int row = y; row < y + rh; row++) {
        for (int col = x; col < x + rw; col += 2) {
            if (col + 1 >= w) break;
            uint8_t *q = f + (size_t)row * stride + (size_t)(col / 2) * 4;
            q[0] = Y; q[1] = U; q[2] = Y; q[3] = V;
        }
    }
}

int main(int argc, char **argv)
{
    const int W = 320, H = 240, STRIDE = W * 2;
    blob_params p = blob_default_params();
    size_t slen = blob_scratch_size(W, H);
    int32_t *scratch = malloc(slen * sizeof(int32_t));
    uint8_t *frame = malloc((size_t)STRIDE * H);
    if (!scratch || !frame) { fprintf(stderr, "oom\n"); return 1; }

    /* Colour inside the default window, and one outside it. */
    const uint8_t OY = 128, OU = 60, OV = 200;   /* on-target  */
    const uint8_t XY = 128, XU = 200, XV = 60;   /* off-target */

    if (argc >= 4) {
        int w = atoi(argv[2]), h = atoi(argv[3]);
        FILE *fp = fopen(argv[1], "rb");
        if (!fp) { perror(argv[1]); return 1; }
        size_t need = (size_t)w * 2 * h;
        uint8_t *buf = malloc(need);
        size_t got = fread(buf, 1, need, fp);
        fclose(fp);
        if (got != need) {
            fprintf(stderr, "short read: %zu of %zu bytes\n", got, need);
            return 1;
        }
        free(scratch);
        slen = blob_scratch_size(w, h);
        scratch = malloc(slen * sizeof(int32_t));
        floor_counts fc;
        if (blob_count_frame(buf, w, h, w * 2, &p, 0, &fc, scratch, slen) < 0) {
            fprintf(stderr, "blob_count_frame failed\n");
            return 1;
        }
        for (int i = 0; i < NUM_FLOORS; i++)
            printf("floor %d: %d blobs\n", i + 1, fc.counts[i]);
        return 0;
    }

    printf("blob self-tests\n");

    /* 1. Empty frame -> nothing. */
    memset(frame, 0, (size_t)STRIDE * H);
    check("empty frame", blob_count_roi(frame, W, H, STRIDE, 0, H, &p, scratch, slen), 0);

    /* 2. One square of the target colour. */
    memset(frame, 0, (size_t)STRIDE * H);
    paint(frame, W, STRIDE, 40, 40, 20, 20, OY, OU, OV);
    check("one blob", blob_count_roi(frame, W, H, STRIDE, 0, H, &p, scratch, slen), 1);

    /* 3. Three well-separated squares. */
    memset(frame, 0, (size_t)STRIDE * H);
    paint(frame, W, STRIDE, 20, 20, 20, 20, OY, OU, OV);
    paint(frame, W, STRIDE, 120, 60, 20, 20, OY, OU, OV);
    paint(frame, W, STRIDE, 220, 120, 20, 20, OY, OU, OV);
    check("three blobs", blob_count_roi(frame, W, H, STRIDE, 0, H, &p, scratch, slen), 3);

    /* 4. Off-target colour is rejected entirely. */
    memset(frame, 0, (size_t)STRIDE * H);
    paint(frame, W, STRIDE, 40, 40, 40, 40, XY, XU, XV);
    check("wrong colour ignored", blob_count_roi(frame, W, H, STRIDE, 0, H, &p, scratch, slen), 0);

    /* 5. A speck below min_blob_area is filtered out. */
    memset(frame, 0, (size_t)STRIDE * H);
    paint(frame, W, STRIDE, 40, 40, 2, 2, OY, OU, OV);
    check("speck filtered", blob_count_roi(frame, W, H, STRIDE, 0, H, &p, scratch, slen), 0);

    /* 6. A U-shape is ONE blob, not two -- this is the real test of the
     *    union-find merge, since the two arms get different provisional
     *    labels and are only joined when the base is reached. */
    memset(frame, 0, (size_t)STRIDE * H);
    paint(frame, W, STRIDE, 40, 40, 8, 40, OY, OU, OV);   /* left arm  */
    paint(frame, W, STRIDE, 80, 40, 8, 40, OY, OU, OV);   /* right arm */
    paint(frame, W, STRIDE, 40, 72, 48, 8, OY, OU, OV);   /* base      */
    check("U-shape merges to one", blob_count_roi(frame, W, H, STRIDE, 0, H, &p, scratch, slen), 1);

    /* 7. Per-floor split: one head on floor 3 (top band), two on floor 1
     *    (bottom band), none on floor 2. */
    memset(frame, 0, (size_t)STRIDE * H);
    paint(frame, W, STRIDE, 40, 10, 20, 20, OY, OU, OV);        /* top    */
    paint(frame, W, STRIDE, 40, 180, 20, 20, OY, OU, OV);       /* bottom */
    paint(frame, W, STRIDE, 140, 180, 20, 20, OY, OU, OV);      /* bottom */
    floor_counts fc;
    check("count_frame ok",
          blob_count_frame(frame, W, H, STRIDE, &p, 0, &fc, scratch, slen), 0);
    check("floor 1 (bottom band)", fc.counts[0], 2);
    check("floor 2 (middle band)", fc.counts[1], 0);
    check("floor 3 (top band)", fc.counts[2], 1);

    /* 8. Suppression blanks the requested floor and marks it. */
    check("count_frame suppress ok",
          blob_count_frame(frame, W, H, STRIDE, &p, 1, &fc, scratch, slen), 0);
    check("floor 1 suppressed flag", fc.suppressed[0], 1);
    check("floor 3 still counted", fc.counts[2], 1);

    printf(failures ? "\n%d FAILED\n" : "\nall passed\n", failures);
    free(frame);
    free(scratch);
    return failures ? 1 : 0;
}
