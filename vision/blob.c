/* blob.c -- from-scratch colour threshold + connected-component labelling.
 *
 * Works on the chroma grid of a YUYV 4:2:2 frame: U and V are shared by each
 * horizontal pair of pixels, so the natural mask resolution is width/2 by
 * height. Labelling at that resolution halves the work and costs nothing in
 * accuracy, since the colour information is not there at full width anyway.
 *
 * Labelling is the classic two-pass union-find: pass 1 assigns provisional
 * labels using already-visited 8-neighbours and records equivalences, pass 2
 * resolves each provisional label to its root and accumulates areas.
 */

#include "blob.h"

#include <string.h>

blob_params blob_default_params(void)
{
    blob_params p;
    /* PLACEHOLDER -- must be tuned against real captures under the actual
     * demo lighting. Use vision/test_blob to sweep these. */
    p.u_min = 0;   p.u_max = 110;
    p.v_min = 150; p.v_max = 255;
    p.y_min = 40;  p.y_max = 235;
    p.min_blob_area = 12;
    p.max_blob_area = 4000;
    return p;
}

size_t blob_scratch_size(int width, int height)
{
    if (width <= 1 || height <= 0) return 0;
    /* The scratch buffer is carved into three regions, and blob_count_roi
     * rejects the call if any of them would not fit:
     *   [0]                 label plane, mw * mh
     *   [mw*mh]             union-find parents, max_labels
     *   [mw*mh+max_labels]  per-root areas,     max_labels
     * max_labels is bounded by mw*mh/2 + 2 because a new label can only be
     * created at a sample whose visited neighbours are all background, which
     * cannot happen on two horizontally adjacent samples.
     *
     * Sized here for the whole frame; each ROI needs strictly less. */
    size_t plane = (size_t)(width / 2) * (size_t)height;
    size_t max_labels = plane / 2 + 2;
    return plane + 2 * max_labels;
}

/* --- union-find over provisional labels --------------------------------- */

static int32_t uf_find(int32_t *parent, int32_t x)
{
    while (parent[x] != x) {
        parent[x] = parent[parent[x]]; /* path halving */
        x = parent[x];
    }
    return x;
}

static void uf_union(int32_t *parent, int32_t a, int32_t b)
{
    int32_t ra = uf_find(parent, a), rb = uf_find(parent, b);
    if (ra == rb) return;
    /* Keep the smaller root so labels stay in scan order. */
    if (ra < rb) parent[rb] = ra;
    else         parent[ra] = rb;
}

/* Is the chroma sample at (mask_x, y) within the target colour window?
 * Each chroma sample covers pixels 2*mask_x and 2*mask_x+1. */
static int sample_matches(const uint8_t *row, int mask_x, const blob_params *p)
{
    const uint8_t *quad = row + (size_t)mask_x * 4; /* Y0 U Y1 V */
    uint8_t y0 = quad[0], u = quad[1], y1 = quad[2], v = quad[3];

    if (u < p->u_min || u > p->u_max) return 0;
    if (v < p->v_min || v > p->v_max) return 0;

    /* Accept if either of the two covered pixels has plausible luma; a head
     * straddling the pair should not be rejected because one half is in
     * shadow. */
    int y0_ok = (y0 >= p->y_min && y0 <= p->y_max);
    int y1_ok = (y1 >= p->y_min && y1 <= p->y_max);
    return y0_ok || y1_ok;
}

int blob_count_roi(const uint8_t *frame, int width, int height, int stride,
                   int row_start, int row_end, const blob_params *p,
                   int32_t *scratch, size_t scratch_len)
{
    if (!frame || !p || !scratch) return -1;
    if (width <= 1 || height <= 0 || stride < width * 2) return -1;
    if (row_start < 0 || row_end > height || row_start >= row_end) return -1;

    const int mw = width / 2;            /* mask width  */
    const int mh = row_end - row_start;  /* mask height */
    if ((size_t)mw * (size_t)mh > scratch_len) return -1;

    int32_t *labels = scratch;
    memset(labels, 0, (size_t)mw * (size_t)mh * sizeof(int32_t));

    /* Provisional labels are 1-based; parent[] is sized to the worst case of
     * one label per sample, which for a 4:2:2 mask is bounded by mw*mh/2 + 1.
     * We reuse the tail of the scratch buffer to avoid a heap allocation --
     * but only if it fits, otherwise bail rather than overrun. */
    const size_t max_labels = (size_t)mw * (size_t)mh / 2 + 2;
    if ((size_t)mw * (size_t)mh + max_labels > scratch_len) return -1;
    int32_t *parent = scratch + (size_t)mw * (size_t)mh;

    int32_t next_label = 1;
    parent[0] = 0;

    /* --- pass 1: provisional labels + equivalences --- */
    for (int my = 0; my < mh; my++) {
        const uint8_t *row = frame + (size_t)(row_start + my) * (size_t)stride;
        for (int mx = 0; mx < mw; mx++) {
            if (!sample_matches(row, mx, p)) continue;

            /* Already-visited 8-neighbours: W, NW, N, NE. */
            int32_t best = 0;
            int32_t neigh[4] = {0, 0, 0, 0};
            int n = 0;

            if (mx > 0)                      neigh[n++] = labels[(size_t)my * mw + (mx - 1)];
            if (my > 0 && mx > 0)            neigh[n++] = labels[(size_t)(my - 1) * mw + (mx - 1)];
            if (my > 0)                      neigh[n++] = labels[(size_t)(my - 1) * mw + mx];
            if (my > 0 && mx < mw - 1)       neigh[n++] = labels[(size_t)(my - 1) * mw + (mx + 1)];

            for (int i = 0; i < n; i++) {
                if (neigh[i] && (!best || neigh[i] < best)) best = neigh[i];
            }

            if (!best) {
                if ((size_t)next_label >= max_labels) {
                    /* Pathological frame (noise everywhere). Refuse rather
                     * than corrupt memory; caller should retune thresholds. */
                    return -1;
                }
                best = next_label;
                parent[next_label] = next_label;
                next_label++;
            } else {
                for (int i = 0; i < n; i++) {
                    if (neigh[i]) uf_union(parent, best, neigh[i]);
                }
            }
            labels[(size_t)my * mw + mx] = best;
        }
    }

    if (next_label <= 1) return 0;

    /* --- pass 2: resolve each label to its root, accumulate areas --- */
    const size_t area_off = (size_t)mw * (size_t)mh + max_labels;
    if (area_off + max_labels > scratch_len) return -1;
    int32_t *area = scratch + area_off;
    memset(area, 0, max_labels * sizeof(int32_t));

    for (size_t i = 0, e = (size_t)mw * (size_t)mh; i < e; i++) {
        int32_t l = labels[i];
        if (!l) continue;
        area[uf_find(parent, l)]++;
    }

    int count = 0;
    for (int32_t l = 1; l < next_label; l++) {
        if (uf_find(parent, l) != l) continue; /* not a root */
        if (area[l] >= p->min_blob_area && area[l] <= p->max_blob_area) count++;
    }
    return count;
}

int blob_count_frame(const uint8_t *frame, int width, int height, int stride,
                     const blob_params *p, int suppress_floor,
                     floor_counts *out, int32_t *scratch, size_t scratch_len)
{
    if (!out) return -1;
    memset(out, 0, sizeof(*out));

    const int band = height / NUM_FLOORS;
    if (band <= 0) return -1;

    for (int i = 0; i < NUM_FLOORS; i++) {
        /* Band 0 is the top of the image, which is floor 3. */
        int floor_no = NUM_FLOORS - i;
        int idx = floor_no - 1;

        if (suppress_floor == floor_no) {
            out->suppressed[idx] = 1;
            out->counts[idx] = -1; /* caller must omit this floor when publishing */
            continue;
        }

        int row_start = i * band;
        /* Last band absorbs the remainder rows from integer division. */
        int row_end = (i == NUM_FLOORS - 1) ? height : row_start + band;

        int n = blob_count_roi(frame, width, height, stride, row_start, row_end,
                               p, scratch, scratch_len);
        if (n < 0) return -1;
        out->counts[idx] = n;
    }
    return 0;
}
