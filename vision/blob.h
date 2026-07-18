/* blob.h -- pure YUV threshold + connected-component head counting.
 *
 * No QNX dependencies, no capture.h. Everything here is plain C99 operating
 * on a caller-supplied frame buffer, so it can be compiled and tested on any
 * host against captured .yuyv dumps before the camera is involved.
 */
#ifndef BLOB_H
#define BLOB_H

#include <stddef.h>
#include <stdint.h>

#define NUM_FLOORS 3

/* Chrominance window for the target lego-head colour, plus a minimum blob
 * area in mask cells. Tune with vision/test_blob against real captures. */
typedef struct {
    uint8_t u_min, u_max;
    uint8_t v_min, v_max;
    uint8_t y_min, y_max; /* reject near-black/blown-out pixels */
    int     min_blob_area;
    int     max_blob_area; /* rejects large regions: hands, the car, lighting */
} blob_params;

typedef struct {
    int counts[NUM_FLOORS];    /* index 0 = floor 1 ... index 2 = floor 3 */
    int suppressed[NUM_FLOORS];/* 1 if the ROI was skipped (car occupying) */
} floor_counts;

/* Default starting point. These are NOT calibrated -- they are a plausible
 * window for a saturated colour and must be tuned against real captures. */
blob_params blob_default_params(void);

/* Count blobs in one horizontal ROI band of a YUYV 4:2:2 frame.
 *
 * frame      packed YUYV (Y0 U Y1 V), stride bytes per row
 * row_start  first row of the band, row_end one past the last
 * scratch    caller-supplied label buffer, at least (width/2)*(rows) ints
 *
 * Returns the number of accepted blobs, or -1 on bad arguments.
 */
int blob_count_roi(const uint8_t *frame, int width, int height, int stride,
                   int row_start, int row_end, const blob_params *p,
                   int32_t *scratch, size_t scratch_len);

/* Split a frame into NUM_FLOORS equal horizontal bands and count each.
 *
 * Band order is top-to-bottom in the image, which is floor 3 -> floor 1, so
 * the result is written back in floor order (index 0 = floor 1).
 *
 * suppress_floor is a floor number (1..3) whose band should be skipped
 * because the car is occupying it, or 0 for none.
 */
int blob_count_frame(const uint8_t *frame, int width, int height, int stride,
                     const blob_params *p, int suppress_floor,
                     floor_counts *out, int32_t *scratch, size_t scratch_len);

/* Scratch ints needed by blob_count_frame for a given frame geometry. */
size_t blob_scratch_size(int width, int height);

#endif /* BLOB_H */
