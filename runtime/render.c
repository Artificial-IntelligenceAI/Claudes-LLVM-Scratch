/* SPDX-License-Identifier: AGPL-3.0-only */
/*
 * Software renderer: composites the stage the way scratch-render layers it
 * (backdrop, pen layer, sprites in execution order) and writes PNG frames.
 *
 * Supported: costume rasters (compile-time rasterized SVG at 2x, PNG), sprite
 * position/direction/size/rotation styles, ghost, brightness and color
 * effects, pen lines/dots (anti-aliased, round caps), stamps and clear.
 * Not rendered: speech bubbles, fisheye/whirl/pixelate/mosaic effects.
 */
#include "rt.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <zlib.h>

#define W 480
#define H 360

static int enabled;
static const char *out_dir;
static int32_t every = 1;
static float *pen;      /* premultiplied RGBA, W*H*4 */
static uint8_t *fb;     /* RGB output, W*H*3 */

double rt_fd_sin(double), rt_fd_cos(double);

void rt_render_init(const char *dir, int32_t n) {
    enabled = 1;
    out_dir = dir;
    every = n > 0 ? n : 1;
    mkdir(dir, 0755);
    pen = (float *)calloc((size_t)W * H * 4, sizeof(float));
    fb = (uint8_t *)malloc((size_t)W * H * 3);
}

/* ------------------------------------------------------------------------ */
/* Pen layer                                                                 */

static void pen_blend(int x, int y, const double c[4], double coverage) {
    if (x < 0 || y < 0 || x >= W || y >= H) return;
    float *p = &pen[(y * W + x) * 4];
    float a = (float)(c[3] * coverage);
    p[0] = (float)c[0] * a + p[0] * (1 - a);
    p[1] = (float)c[1] * a + p[1] * (1 - a);
    p[2] = (float)c[2] * a + p[2] * (1 - a);
    p[3] = a + p[3] * (1 - a);
}

void rt_render_pen_line(Target *t, const double c[4], double w, double x0, double y0, double x1, double y1) {
    (void)t;
    if (!enabled) return;
    /* stage -> pixel space (y down) */
    double ax = x0 + W / 2.0, ay = H / 2.0 - y0, bx = x1 + W / 2.0, by = H / 2.0 - y1;
    double r = w / 2;
    if (r < 0.5) r = 0.5;
    int minx = (int)floor(fmin(ax, bx) - r - 1), maxx = (int)ceil(fmax(ax, bx) + r + 1);
    int miny = (int)floor(fmin(ay, by) - r - 1), maxy = (int)ceil(fmax(ay, by) + r + 1);
    if (minx < 0) minx = 0;
    if (miny < 0) miny = 0;
    if (maxx > W - 1) maxx = W - 1;
    if (maxy > H - 1) maxy = H - 1;
    double dx = bx - ax, dy = by - ay, len2 = dx * dx + dy * dy;
    for (int y = miny; y <= maxy; y++)
        for (int x = minx; x <= maxx; x++) {
            double px = x + 0.5, py = y + 0.5;
            double t_ = len2 > 0 ? ((px - ax) * dx + (py - ay) * dy) / len2 : 0;
            if (t_ < 0) t_ = 0;
            if (t_ > 1) t_ = 1;
            double qx = ax + t_ * dx - px, qy = ay + t_ * dy - py;
            double d = sqrt(qx * qx + qy * qy);
            double cov = r + 0.5 - d;
            if (cov <= 0) continue;
            pen_blend(x, y, c, cov > 1 ? 1 : cov);
        }
}

void rt_render_pen_clear(void) {
    if (!enabled) return;
    memset(pen, 0, sizeof(float) * (size_t)W * H * 4);
}

/* ------------------------------------------------------------------------ */
/* Sprites                                                                   */

static void rgb_to_hsv(double r, double g, double b, double *h, double *s, double *v) {
    double mx = fmax(r, fmax(g, b)), mn = fmin(r, fmin(g, b));
    *v = mx;
    double d = mx - mn;
    *s = mx > 0 ? d / mx : 0;
    if (d == 0) { *h = 0; return; }
    if (mx == r) *h = fmod((g - b) / d, 6);
    else if (mx == g) *h = (b - r) / d + 2;
    else *h = (r - g) / d + 4;
    *h /= 6;
    if (*h < 0) *h += 1;
}

static void hsv_to_rgb(double h, double s, double v, double *r, double *g, double *b) {
    double i = floor(h * 6), f = h * 6 - i, p = v * (1 - s), q = v * (1 - f * s), t = v * (1 - (1 - f) * s);
    switch (((int)i) % 6) {
    case 0: *r = v; *g = t; *b = p; break;
    case 1: *r = q; *g = v; *b = p; break;
    case 2: *r = p; *g = v; *b = t; break;
    case 3: *r = p; *g = q; *b = v; break;
    case 4: *r = t; *g = p; *b = v; break;
    default: *r = v; *g = p; *b = q; break;
    }
}

/* Costume rasters are embedded zlib-compressed; inflate on first use. */
static const uint8_t *pixels(Costume *c) {
    if (c->rgba || !c->rgba_z || c->img_w <= 0) return c->rgba;
    uLongf n = (uLongf)c->img_w * (uLongf)c->img_h * 4;
    uint8_t *buf = (uint8_t *)malloc(n);
    if (uncompress(buf, &n, c->rgba_z, (uLong)c->rgba_zlen) != Z_OK) { free(buf); c->rgba_z = NULL; return NULL; }
    c->rgba = buf;
    return buf;
}

/* Draw a target into the pen layer (premultiplied RGBA) or the RGB accumulator. */
static void draw_target(Target *t, float *dst_pen, double *dst_rgb) {
    Sprite *s = t->sprite;
    if (s->ncostumes == 0) return;
    Costume *c = &s->costumes[t->costume];
    if (!pixels(c) || c->img_w <= 0) return;
    double dir = t->dir, sx = t->size, sy = t->size;
    if (t->is_stage) { dir = 90; sx = sy = 100; }
    else if (t->rotation_style == 2) dir = 90;
    else if (t->rotation_style == 1) { dir = 90; if (t->dir < 0) sx = -sx; }
    double th = (90 - dir) * M_PI / 180, co = rt_fd_cos(th), sn = rt_fd_sin(th);
    double kx = sx / 100, ky = sy / 100;
    if (kx == 0 || ky == 0) return;
    double tx = t->is_stage ? 0 : t->x, ty = t->is_stage ? 0 : t->y;
    /* bounding box in stage space */
    double corners[4][2] = {{-c->cx, c->cy}, {c->w - c->cx, c->cy}, {-c->cx, c->cy - c->h}, {c->w - c->cx, c->cy - c->h}};
    double l = 1e9, r = -1e9, top = -1e9, bot = 1e9;
    for (int i = 0; i < 4; i++) {
        double x = corners[i][0] * kx, y = corners[i][1] * ky;
        double rx = x * co - y * sn + tx, ry = x * sn + y * co + ty;
        l = fmin(l, rx); r = fmax(r, rx); top = fmax(top, ry); bot = fmin(bot, ry);
    }
    int x0 = (int)floor(l + W / 2.0) - 1, x1 = (int)ceil(r + W / 2.0) + 1;
    int y0 = (int)floor(H / 2.0 - top) - 1, y1 = (int)ceil(H / 2.0 - bot) + 1;
    if (x0 < 0) x0 = 0;
    if (y0 < 0) y0 = 0;
    if (x1 > W - 1) x1 = W - 1;
    if (y1 > H - 1) y1 = H - 1;
    double ghost = 1 - fmin(fmax(t->effects[EFF_GHOST], 0), 100) / 100;
    double bright = fmin(fmax(t->effects[EFF_BRIGHTNESS], -100), 100) / 100;
    double hue = t->effects[EFF_COLOR] / 200;
    for (int py = y0; py <= y1; py++)
        for (int px = x0; px <= x1; px++) {
            double wx = px + 0.5 - W / 2.0 - tx, wy = H / 2.0 - (py + 0.5) - ty;
            /* inverse rotate, inverse scale */
            double lx = (wx * co + wy * sn) / kx, ly = (-wx * sn + wy * co) / ky;
            double cx = (lx + c->cx) * c->img_scale, cy = (c->cy - ly) * c->img_scale;
            int ix = (int)floor(cx), iy = (int)floor(cy);
            if (ix < 0 || iy < 0 || ix >= c->img_w || iy >= c->img_h) continue;
            const uint8_t *src = &c->rgba[(iy * c->img_w + ix) * 4];
            double a = src[3] / 255.0 * ghost;
            if (a <= 0) continue;
            double R = src[0] / 255.0, G = src[1] / 255.0, B = src[2] / 255.0;
            if (hue != 0) {
                double h, s_, v;
                rgb_to_hsv(R, G, B, &h, &s_, &v);
                h = fmod(h + hue, 1);
                if (h < 0) h += 1;
                hsv_to_rgb(h, s_, v, &R, &G, &B);
            }
            if (bright != 0) {
                R = fmin(fmax(R + bright, 0), 1);
                G = fmin(fmax(G + bright, 0), 1);
                B = fmin(fmax(B + bright, 0), 1);
            }
            if (dst_pen) {
                float *p = &dst_pen[(py * W + px) * 4];
                p[0] = (float)(R * a + p[0] * (1 - a));
                p[1] = (float)(G * a + p[1] * (1 - a));
                p[2] = (float)(B * a + p[2] * (1 - a));
                p[3] = (float)(a + p[3] * (1 - a));
            } else {
                double *p = &dst_rgb[(py * W + px) * 3];
                p[0] = R * a + p[0] * (1 - a);
                p[1] = G * a + p[1] * (1 - a);
                p[2] = B * a + p[2] * (1 - a);
            }
        }
}

void rt_render_stamp(Target *t) {
    if (!enabled) return;
    draw_target(t, pen, NULL);
}

/* ------------------------------------------------------------------------ */
/* Frames                                                                    */

static void compose(void) {
    static double *acc;
    if (!acc) acc = (double *)malloc(sizeof(double) * W * H * 3);
    for (int i = 0; i < W * H * 3; i++) acc[i] = 1.0; /* white stage */
    int32_t n;
    Target **ts = rt_exec_targets(&n);
    for (int32_t i = 0; i < n; i++) {
        Target *t = ts[i];
        if (t->is_stage) {
            draw_target(t, NULL, acc);
            /* pen layer sits above the backdrop, below sprites */
            for (int k = 0; k < W * H; k++) {
                float *p = &pen[k * 4];
                double *d = &acc[k * 3];
                d[0] = p[0] + d[0] * (1 - p[3]);
                d[1] = p[1] + d[1] * (1 - p[3]);
                d[2] = p[2] + d[2] * (1 - p[3]);
            }
            continue;
        }
        if (t->visible) draw_target(t, NULL, acc);
    }
    for (int i = 0; i < W * H * 3; i++) {
        double v = acc[i] * 255 + 0.5;
        fb[i] = (uint8_t)(v < 0 ? 0 : v > 255 ? 255 : v);
    }
}

static void put32(uint8_t *p, uint32_t v) { p[0] = v >> 24; p[1] = v >> 16; p[2] = v >> 8; p[3] = (uint8_t)v; }

static void chunk(FILE *f, const char *type, const uint8_t *data, uint32_t n) {
    uint8_t hdr[8];
    put32(hdr, n);
    memcpy(hdr + 4, type, 4);
    fwrite(hdr, 1, 8, f);
    if (n) fwrite(data, 1, n, f);
    uLong crc = crc32(0, (const Bytef *)type, 4);
    if (n) crc = crc32(crc, data, n);
    uint8_t c[4];
    put32(c, (uint32_t)crc);
    fwrite(c, 1, 4, f);
}

static void write_png(const char *path) {
    FILE *f = fopen(path, "wb");
    if (!f) return;
    static const uint8_t sig[8] = {0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n'};
    fwrite(sig, 1, 8, f);
    uint8_t ihdr[13];
    put32(ihdr, W);
    put32(ihdr + 4, H);
    ihdr[8] = 8; ihdr[9] = 2; ihdr[10] = 0; ihdr[11] = 0; ihdr[12] = 0;
    chunk(f, "IHDR", ihdr, 13);
    size_t rawn = (size_t)H * (W * 3 + 1);
    uint8_t *raw = (uint8_t *)malloc(rawn);
    for (int y = 0; y < H; y++) {
        raw[y * (W * 3 + 1)] = 0;
        memcpy(raw + y * (W * 3 + 1) + 1, fb + y * W * 3, W * 3);
    }
    uLongf zn = compressBound(rawn);
    uint8_t *z = (uint8_t *)malloc(zn);
    compress2(z, &zn, raw, rawn, 6);
    chunk(f, "IDAT", z, (uint32_t)zn);
    chunk(f, "IEND", NULL, 0);
    fclose(f);
    free(raw);
    free(z);
}

void rt_render_frame(int32_t frame) {
    if (!enabled || frame % every != 0) return;
    compose();
    char path[4096];
    snprintf(path, sizeof path, "%s/frame_%05d.png", out_dir, frame);
    write_png(path);
}

void rt_render_final(void) {
    if (!enabled) return;
    compose();
    char path[4096];
    snprintf(path, sizeof path, "%s/final.png", out_dir);
    write_png(path);
}
