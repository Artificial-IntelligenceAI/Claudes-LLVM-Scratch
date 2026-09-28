/* SPDX-License-Identifier: AGPL-3.0-only */
/*
 * Looks, motion, sensing and pen blocks, plus speech bubbles and the
 * sprite-bounds model used for fencing, bouncing and touching.
 *
 * Mirrors scratch-vm 5.x src/blocks/scratch3_{looks,motion,sensing}.js,
 * src/extensions/scratch3_pen/index.js and src/sprites/rendered-target.js.
 *
 * Bounds model: a sprite's bounds are the axis-aligned box of its rotated,
 * scaled costume rectangle. scratch-render uses the convex hull of the
 * rendered silhouette instead; for opaque rectangular costumes they agree.
 */
#include "rt.h"
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <math.h>
#include <time.h>

double rt_fd_atan2(double, double);
double rt_fd_sin(double), rt_fd_cos(double);
void rt_trace_say(Target *t, int32_t think, Str *text);
void rt_trace_ask(Str *q);
void rt_trace_pen(const char *op, Target *t, const double *c, double w, const double *p, int np);

/* JS Math.min / Math.max (NaN-propagating, -0 < +0) */
static double js_min(double a, double b) {
    if (a != a || b != b) return NAN;
    if (a == 0 && b == 0) return signbit(a) ? a : b;
    return a < b ? a : b;
}
static double js_max(double a, double b) {
    if (a != a || b != b) return NAN;
    if (a == 0 && b == 0) return signbit(a) ? b : a;
    return a > b ? a : b;
}
static double clamp(double n, double lo, double hi) { return js_min(js_max(n, lo), hi); }
static double wrap_clamp(double n, double lo, double hi) {
    double range = (hi - lo) + 1;
    return n - (floor((n - lo) / range) * range);
}

#define STAGE_W 480.0
#define STAGE_H 360.0

static bool str_is(Str *s, const char *lit) {
    size_t n = strlen(lit);
    return (size_t)s->len == n && memcmp(s->data, lit, n) == 0;
}
static bool val_is(Value v, const char *lit) { return v.tag == T_STR && str_is(v_as_str(v), lit); }

/* ------------------------------------------------------------------------ */
/* Bounds                                                                    */

typedef struct { double left, right, top, bottom; } Rect;

static Costume *cur_costume(Target *t) {
    Sprite *s = t->sprite;
    if (s->ncostumes == 0) return NULL;
    return &s->costumes[t->costume];
}

/* rendered direction/scale (RenderedTarget._getRenderedDirectionAndScale) */
static void rendered_dir_scale(Target *t, double *dir, double *sx, double *sy) {
    *dir = t->dir;
    *sx = t->size;
    *sy = t->size;
    if (t->rotation_style == 2) *dir = 90;
    else if (t->rotation_style == 1) {
        *dir = 90;
        *sx = (t->dir < 0 ? -1 : 1) * t->size;
    }
}

static Rect bounds_at(Target *t, double px, double py) {
    Rect r = {0, 0, 0, 0};
    Costume *c = cur_costume(t);
    if (!c) { r.left = r.right = px; r.top = r.bottom = py; return r; }
    double dir, sx, sy;
    rendered_dir_scale(t, &dir, &sx, &sy);
    double cx = c->cx, cy = c->cy;
    double corners[4][2] = {{-cx, cy}, {c->w - cx, cy}, {-cx, cy - c->h}, {c->w - cx, cy - c->h}};
    double th = (90 - dir) * M_PI / 180;
    double co = rt_fd_cos(th), sn = rt_fd_sin(th);
    r.left = INFINITY; r.right = -INFINITY; r.top = -INFINITY; r.bottom = INFINITY;
    for (int i = 0; i < 4; i++) {
        double x = corners[i][0] * sx / 100;
        double y = corners[i][1] * sy / 100;
        double rx = (x * co) - (y * sn) + px;
        double ry = (x * sn) + (y * co) + py;
        r.left = js_min(r.left, rx); r.right = js_max(r.right, rx);
        r.top = js_max(r.top, ry); r.bottom = js_min(r.bottom, ry);
    }
    return r;
}

static Rect bounds(Target *t) { return bounds_at(t, t->x, t->y); }

/* scratch-render getFencedPositionOfDrawable */
static void fence(Target *t, double *x, double *y) {
    double dx = *x - t->x, dy = *y - t->y;
    Rect a = bounds(t);
    double inset = floor(js_min(a.right - a.left, a.top - a.bottom) / 2);
    double sx = 240 - js_min(15, inset);
    if (a.right + dx < -sx) *x = ceil(t->x - (sx + a.right));
    else if (a.left + dx > sx) *x = floor(t->x + (sx - a.left));
    double sy = 180 - js_min(15, inset);
    if (a.top + dy < -sy) *y = ceil(t->y - (sy + a.top));
    else if (a.bottom + dy > sy) *y = floor(t->y + (sy - a.bottom));
}

/* ------------------------------------------------------------------------ */
/* Pen                                                                       */

static void pen_color4f(Target *t, double c[4]) {
    PenState *p = &t->pen;
    double h = fmod(p->color * 360 / 100, 360);
    if (h < 0) h += 360;
    double s = js_max(0, js_min(p->saturation / 100, 1));
    double v = js_max(0, js_min(p->brightness / 100, 1));
    double i = floor(h / 60);
    double f = (h / 60) - i;
    double pp = v * (1 - s), q = v * (1 - (s * f)), tt = v * (1 - (s * (1 - f)));
    double r, g, b;
    switch ((int)i) {
    default:
    case 0: r = v; g = tt; b = pp; break;
    case 1: r = q; g = v; b = pp; break;
    case 2: r = pp; g = v; b = tt; break;
    case 3: r = pp; g = q; b = v; break;
    case 4: r = tt; g = pp; b = v; break;
    case 5: r = v; g = pp; b = q; break;
    }
    c[0] = floor(r * 255) / 255.0;
    c[1] = floor(g * 255) / 255.0;
    c[2] = floor(b * 255) / 255.0;
    c[3] = 1.0 - (p->transparency / 100.0);
}

void rt_render_pen_line(Target *t, const double c[4], double w, double x0, double y0, double x1, double y1);
void rt_render_pen_clear(void);
void rt_render_stamp(Target *t);

static void pen_moved(Target *t, double ox, double oy) {
    if (!t->pen.down) return;
    double c[4];
    pen_color4f(t, c);
    double p[4] = {ox, oy, t->x, t->y};
    rt_trace_pen("line", t, c, t->pen.diameter, p, 4);
    rt_render_pen_line(t, c, t->pen.diameter, ox, oy, t->x, t->y);
    rt_request_redraw();
}

void rt_pen_clear(void) {
    rt_trace_pen("clear", NULL, NULL, 0, NULL, 0);
    rt_render_pen_clear();
    rt_request_redraw();
}

void rt_pen_down(Target *t) {
    t->pen.down = 1;
    double c[4];
    pen_color4f(t, c);
    double p[2] = {t->x, t->y};
    rt_trace_pen("point", t, c, t->pen.diameter, p, 2);
    rt_render_pen_line(t, c, t->pen.diameter, t->x, t->y, t->x, t->y);
    rt_request_redraw();
}

void rt_pen_up(Target *t) { t->pen.down = 0; }

void rt_pen_stamp(Target *t) {
    double p[2] = {t->x, t->y};
    rt_trace_pen("stamp", t, NULL, 0, p, 2);
    rt_render_stamp(t);
    rt_request_redraw();
}

/* Cast.toRgbColorObject + Color.rgbToHsv */
void rt_pen_set_color(Target *t, Value color) {
    double r = 0, g = 0, b = 0, a = 255;
    int has_a = 0;
    if (color.tag == T_STR && v_as_str(color)->len > 0 && v_as_str(color)->data[0] == '#') {
        Str *s = v_as_str(color);
        const char *h = s->data + 1;
        int n = s->len - 1;
        char buf[7];
        int ok = 1;
        for (int i = 0; i < n; i++)
            if (!((h[i] >= '0' && h[i] <= '9') || ((h[i] | 0x20) >= 'a' && (h[i] | 0x20) <= 'f'))) ok = 0;
        if (ok && n == 3) {
            buf[0] = buf[1] = h[0]; buf[2] = buf[3] = h[1]; buf[4] = buf[5] = h[2]; buf[6] = 0;
        } else if (ok && n == 6) {
            memcpy(buf, h, 6); buf[6] = 0;
        } else ok = 0;
        if (ok) {
            long v = strtol(buf, NULL, 16);
            r = (v >> 16) & 0xFF; g = (v >> 8) & 0xFF; b = v & 0xFF;
        } else {
            r = g = b = 0; a = 255; has_a = 1; /* {r:0,g:0,b:0,a:255} */
        }
    } else {
        /* Color.decimalToRgb(Cast.toNumber(value)): JS bit ops on ToInt32 */
        double d = rt_tonum(color);
        int32_t dec;
        if (d != d || isinf(d)) dec = 0;
        else {
            double m = fmod(trunc(d), 4294967296.0);
            if (m < 0) m += 4294967296.0;
            dec = (int32_t)(uint32_t)m;
        }
        int32_t aa = (dec >> 24) & 0xFF;
        r = (dec >> 16) & 0xFF; g = (dec >> 8) & 0xFF; b = dec & 0xFF;
        a = aa > 0 ? aa : 255;
        has_a = 1;
    }
    double rr = r / 255, gg = g / 255, bb = b / 255;
    double x = js_min(js_min(rr, gg), bb), v = js_max(js_max(rr, gg), bb);
    double hh = 0, ss = 0;
    if (x != v) {
        double f = (rr == x) ? gg - bb : ((gg == x) ? bb - rr : rr - gg);
        double i = (rr == x) ? 3 : ((gg == x) ? 5 : 1);
        hh = fmod((i - (f / (v - x))) * 60, 360);
        ss = (v - x) / v;
    }
    t->pen.color = (hh / 360) * 100;
    t->pen.saturation = ss * 100;
    t->pen.brightness = v * 100;
    t->pen.transparency = has_a ? 100 * (1 - (a / 255.0)) : 0;
}

static void pen_param(Target *t, Str *param, double value, int change) {
    PenState *p = &t->pen;
    if (str_is(param, "color")) p->color = wrap_clamp(value + (change ? p->color : 0), 0, 100);
    else if (str_is(param, "saturation")) p->saturation = clamp(value + (change ? p->saturation : 0), 0, 100);
    else if (str_is(param, "brightness")) p->brightness = clamp(value + (change ? p->brightness : 0), 0, 100);
    else if (str_is(param, "transparency")) p->transparency = clamp(value + (change ? p->transparency : 0), 0, 100);
}

void rt_pen_set_param(Target *t, Str *param, double v) { pen_param(t, param, v, 0); }
void rt_pen_change_param(Target *t, Str *param, double v) { pen_param(t, param, v, 1); }
void rt_pen_set_size(Target *t, double s) { t->pen.diameter = clamp(s, 1, 1200); }
void rt_pen_change_size(Target *t, double d) { t->pen.diameter = clamp(t->pen.diameter + d, 1, 1200); }

/* ------------------------------------------------------------------------ */
/* Motion                                                                    */

/* RenderedTarget.setXY */
void rt_set_xy(Target *t, double x, double y) {
    if (t->is_stage) return;
    double ox = t->x, oy = t->y;
    fence(t, &x, &y);
    t->x = x;
    t->y = y;
    if (t->visible) rt_request_redraw();
    pen_moved(t, ox, oy);
}

/* RenderedTarget.setDirection */
static void set_direction(Target *t, double d) {
    if (t->is_stage) return;
    if (!isfinite(d)) return;
    t->dir = wrap_clamp(d, -179, 180);
    if (t->visible) rt_request_redraw();
}

void rt_move_steps(Target *t, double steps) {
    double rad = (90 - t->dir) * M_PI / 180;
    double dx = steps * rt_fd_cos(rad);
    double dy = steps * rt_fd_sin(rad);
    rt_set_xy(t, t->x + dx, t->y + dy);
}

void rt_turn(Target *t, double degrees) { set_direction(t, t->dir + degrees); }
void rt_point_dir(Target *t, double d) { set_direction(t, d); }

double rt_mouse_x(void) { return 0; }
double rt_mouse_y(void) { return 0; }
bool rt_mouse_down(void) { return false; }

static int target_xy(Value to, double *x, double *y) {
    if (val_is(to, "_mouse_")) { *x = rt_mouse_x(); *y = rt_mouse_y(); return 1; }
    if (val_is(to, "_random_")) {
        *x = rt_round(STAGE_W * (rt_js_random() - 0.5));
        *y = rt_round(STAGE_H * (rt_js_random() - 0.5));
        return 1;
    }
    Str *name = rt_tostr(to);
    Target *o = rt_sprite_original_by_name(name);
    rt_release_str(name);
    if (!o) return 0;
    *x = o->x;
    *y = o->y;
    return 1;
}

void rt_goto(Target *t, Value to) {
    double x, y;
    if (target_xy(to, &x, &y)) rt_set_xy(t, x, y);
}

void rt_point_towards(Target *t, Value towards) {
    double tx = 0, ty = 0;
    if (val_is(towards, "_mouse_")) { tx = rt_mouse_x(); ty = rt_mouse_y(); }
    else if (val_is(towards, "_random_")) { set_direction(t, rt_round(rt_js_random() * 360) - 180); return; }
    else {
        Str *name = rt_tostr(towards);
        Target *o = rt_sprite_original_by_name(name);
        rt_release_str(name);
        if (!o) return;
        tx = o->x; ty = o->y;
    }
    double dx = tx - t->x, dy = ty - t->y;
    set_direction(t, 90 - (rt_fd_atan2(dy, dx) * 180 / M_PI));
}

/* motion_glidesecstoxy. Returns -1 to continue with the next block. */
int32_t rt_glide(Thread *th, Frame *fr, double secs, double x, double y, int32_t first) {
    Target *t = th->target;
    if (!first && fr->timer_on) {
        double el = rt_now_ms() - fr->timer_start;
        if (el < fr->timer_dur * 1000) {
            double frac = el / (fr->timer_dur * 1000);
            double dx = frac * (fr->glide_x1 - fr->glide_x0);
            double dy = frac * (fr->glide_y1 - fr->glide_y0);
            rt_set_xy(t, fr->glide_x0 + dx, fr->glide_y0 + dy);
            return R_YIELD;
        }
        rt_set_xy(t, fr->glide_x1, fr->glide_y1);
        fr->timer_on = 0;
        return -1;
    }
    fr->timer_on = 1;
    fr->timer_start = rt_now_ms();
    fr->timer_dur = secs;
    fr->glide_x0 = t->x;
    fr->glide_y0 = t->y;
    fr->glide_x1 = x;
    fr->glide_y1 = y;
    if (fr->timer_dur <= 0) {
        rt_set_xy(t, x, y);
        fr->timer_on = 0;
        return -1;
    }
    return R_YIELD;
}

int32_t rt_glide_to(Thread *th, Frame *fr, double secs, Value to, int32_t first) {
    if (!first && fr->timer_on) return rt_glide(th, fr, secs, 0, 0, 0);
    double x, y;
    if (!target_xy(to, &x, &y)) return -1;
    return rt_glide(th, fr, secs, x, y, 1);
}

void rt_bounce(Target *t) {
    if (t->is_stage) return;
    Rect b = bounds(t);
    double distLeft = js_max(0, (STAGE_W / 2) + b.left);
    double distTop = js_max(0, (STAGE_H / 2) - b.top);
    double distRight = js_max(0, (STAGE_W / 2) - b.right);
    double distBottom = js_max(0, (STAGE_H / 2) + b.bottom);
    int edge = 0;
    double minDist = INFINITY;
    if (distLeft < minDist) { minDist = distLeft; edge = 1; }
    if (distTop < minDist) { minDist = distTop; edge = 2; }
    if (distRight < minDist) { minDist = distRight; edge = 3; }
    if (distBottom < minDist) { minDist = distBottom; edge = 4; }
    if (minDist > 0) return;
    double rad = (90 - t->dir) * M_PI / 180;
    double dx = rt_fd_cos(rad), dy = -rt_fd_sin(rad);
    if (edge == 1) dx = js_max(0.2, fabs(dx));
    else if (edge == 2) dy = js_max(0.2, fabs(dy));
    else if (edge == 3) dx = 0 - js_max(0.2, fabs(dx));
    else if (edge == 4) dy = 0 - js_max(0.2, fabs(dy));
    set_direction(t, (rt_fd_atan2(dy, dx) * 180 / M_PI) + 90);
    /* keepInFence */
    Rect nb = bounds(t);
    double fx = 0, fy = 0;
    if (nb.left < -STAGE_W / 2) fx += -STAGE_W / 2 - nb.left;
    if (nb.right > STAGE_W / 2) fx += STAGE_W / 2 - nb.right;
    if (nb.top > STAGE_H / 2) fy += STAGE_H / 2 - nb.top;
    if (nb.bottom < -STAGE_H / 2) fy += -STAGE_H / 2 - nb.bottom;
    rt_set_xy(t, t->x + fx, t->y + fy);
}

void rt_set_rotation_style(Target *t, Str *style) {
    if (str_is(style, "all around")) t->rotation_style = 0;
    else if (str_is(style, "left-right")) t->rotation_style = 1;
    else if (str_is(style, "don't rotate")) t->rotation_style = 2;
    if (t->visible) rt_request_redraw();
}

static double limit_precision(double c) {
    double r = rt_round(c);
    return fabs(c - r) < 1e-9 ? r : c;
}
double rt_get_x(Target *t) { return limit_precision(t->x); }
double rt_get_y(Target *t) { return limit_precision(t->y); }
double rt_get_dir(Target *t) { return t->dir; }

/* ------------------------------------------------------------------------ */
/* Speech bubbles                                                            */

static int32_t bubble_usage_counter;

/* _formatBubbleText */
static Str *format_bubble(Value v) {
    if (v.tag == T_NUM) {
        double d = bits2d(v.bits);
        if (fabs(d) >= 0.01 && fmod(d, 1) != 0) {
            char buf[400];
            snprintf(buf, sizeof buf, "%.2f", d);
            return rt_str_cstr(buf);
        }
    }
    Str *s = rt_tostr(v);
    if (s->len > 330) {
        Str *r = rt_str_new(s->data, 330); /* approximation: byte-truncation for non-ASCII */
        rt_release_str(s);
        return r;
    }
    return s;
}

static void render_bubble(Target *t) {
    if (!t->visible || !t->bubble_text || t->bubble_text->len == 0) {
        rt_bubble_target_exit(t);
        return;
    }
    t->bubble_exists = 1;
    rt_request_redraw();
}

void rt_bubble_target_exit(Target *t) {
    if (t->bubble_exists) {
        t->bubble_exists = 0;
        rt_request_redraw();
    }
}

static void update_bubble(Target *t, int32_t think, Value v) {
    t->bubble_type = think;
    if (t->bubble_text) rt_release_str(t->bubble_text);
    t->bubble_text = format_bubble(v);
    t->bubble_usage = ++bubble_usage_counter;
    render_bubble(t);
}

void rt_bubbles_reset(void) {
    int32_t n;
    Target **ts = rt_exec_targets(&n);
    for (int32_t i = 0; i < n; i++) {
        Target *t = ts[i];
        if (t->bubble_text) { rt_release_str(t->bubble_text); t->bubble_text = NULL; }
        rt_bubble_target_exit(t);
    }
}

/* runtime.emit('SAY', target, type, text): trace + bubble listener */
void rt_emit_say(Target *t, int32_t think, Value v) {
    Str *s = rt_tostr(v);
    rt_trace_say(t, think, s);
    rt_release_str(s);
    update_bubble(t, think, v);
}

void rt_say(Target *t, Value msg, int32_t think) { rt_emit_say(t, think, msg); }

void rt_bubble_clear_if(Target *t, int32_t usage, int32_t think) {
    if (t->bubble_usage == usage) update_bubble(t, think, v_str(rt_empty_str()));
}

void rt_add_timer(Thread *th, double delay_ms, int32_t bubble_usage, int32_t think);

/* looks_sayforsecs: setTimeout(..., 1000 * args.SECS) — note JS ToNumber, not Cast */
int32_t rt_say_for_secs(Thread *th, Frame *fr, Value msg, double secs_js, int32_t think, int32_t first) {
    (void)fr; (void)first;
    Target *t = th->target;
    rt_emit_say(t, think, msg);
    double ms = 1000 * secs_js;
    rt_add_timer(th, ms != ms ? 0 : ms, t->bubble_usage, think);
    return R_PROMISE;
}

void rt_show(Target *t) {
    if (!t->is_stage) {
        t->visible = 1;
        rt_request_redraw();
    }
    render_bubble(t);
}

void rt_hide(Target *t) {
    if (!t->is_stage) t->visible = 0; /* setVisible(false) requests no redraw */
    render_bubble(t);
}

/* ------------------------------------------------------------------------ */
/* Costumes and backdrops                                                    */

static void set_costume(Target *t, double index) {
    index = rt_round(index);
    if (isinf(index) || index != index) index = 0;
    int32_t n = t->sprite->ncostumes;
    if (n <= 0) return;
    t->costume = (int32_t)wrap_clamp(index, 0, n - 1);
    if (t->visible) rt_request_redraw();
}

static int32_t costume_by_name(Target *t, Str *name) {
    for (int32_t i = 0; i < t->sprite->ncostumes; i++) {
        Str *n = t->sprite->costumes[i].name;
        if (n->len == name->len && memcmp(n->data, name->data, (size_t)n->len) == 0) return i;
    }
    return -1;
}

/* looks._setCostume */
static void set_costume_value(Target *t, Value req, int zero_index) {
    if (req.tag == T_NUM) {
        double d = bits2d(req.bits);
        set_costume(t, zero_index ? d : d - 1);
        return;
    }
    Str *name = rt_tostr(req);
    int32_t idx = costume_by_name(t, name);
    if (idx != -1) set_costume(t, idx);
    else if (str_is(name, "next costume")) set_costume(t, t->costume + 1);
    else if (str_is(name, "previous costume")) set_costume(t, t->costume - 1);
    else if (!(rt_js_number(req) != rt_js_number(req) || rt_is_whitespace(req))) {
        double d = rt_js_number(req);
        set_costume(t, zero_index ? d : d - 1);
    }
    rt_release_str(name);
}

void rt_switch_costume(Target *t, Value costume) { set_costume_value(t, costume, 0); }
void rt_next_costume(Target *t) { set_costume(t, t->costume + 1); }

static void start_backdrop_hats(int64_t **started, int32_t *nstarted) {
    Target *st = rt_stage;
    Str *name = st->sprite->costumes[st->costume].name;
    Str *lower = rt_str_lower(name);
    rt_start_hats(HAT_BACKDROP, lower, NULL, started, nstarted);
    rt_release_str(lower);
}

/* looks._setBackdrop */
static void set_backdrop_value(Value req, int zero_index, int64_t **started, int32_t *nstarted) {
    Target *st = rt_stage;
    if (req.tag == T_NUM) {
        double d = bits2d(req.bits);
        set_costume(st, zero_index ? d : d - 1);
    } else {
        Str *name = rt_tostr(req);
        int32_t idx = costume_by_name(st, name);
        if (idx != -1) set_costume(st, idx);
        else if (str_is(name, "next backdrop")) set_costume(st, st->costume + 1);
        else if (str_is(name, "previous backdrop")) set_costume(st, st->costume - 1);
        else if (str_is(name, "random backdrop")) {
            int32_t n = st->sprite->ncostumes;
            if (n > 1) {
                /* MathUtil.inclusiveRandIntWithout(0, n-1, current) */
                int32_t r = (int32_t)floor(rt_js_random() * (n - 1));
                if (r >= st->costume) r++;
                set_costume(st, r);
            }
        } else if (!(rt_js_number(req) != rt_js_number(req) || rt_is_whitespace(req))) {
            double d = rt_js_number(req);
            set_costume(st, zero_index ? d : d - 1);
        }
        rt_release_str(name);
    }
    start_backdrop_hats(started, nstarted);
}

void rt_switch_backdrop(Value backdrop) { set_backdrop_value(backdrop, 0, NULL, NULL); }
void rt_next_backdrop(void) { set_backdrop_value(v_num(rt_stage->costume + 1), 1, NULL, NULL); }

int32_t rt_wait_started(Frame *fr);

/* looks_switchbackdroptoandwait. Returns -1 to continue with the next block. */
int32_t rt_switch_backdrop_and_wait(Thread *th, Frame *fr, Value backdrop, int32_t first) {
    (void)th;
    if (first || !fr->started_init) {
        free(fr->started);
        fr->started = NULL;
        fr->nstarted = 0;
        set_backdrop_value(backdrop, 0, &fr->started, &fr->nstarted);
        fr->started_init = 1;
        if (fr->nstarted == 0) { fr->started_init = 0; return -1; }
    }
    return rt_wait_started(fr);
}

double rt_costume_number(Target *t) { return t->costume + 1; }
Str *rt_costume_name(Target *t) {
    Str *n = t->sprite->costumes[t->costume].name;
    rt_retain_str(n);
    return n;
}
double rt_backdrop_number(void) { return rt_stage->costume + 1; }
Str *rt_backdrop_name(void) { return rt_costume_name(rt_stage); }

/* RenderedTarget.setSize */
void rt_set_size(Target *t, double size) {
    if (t->is_stage) return;
    Costume *c = cur_costume(t);
    double w = c ? c->w : 0, h = c ? c->h : 0;
    double minScale = js_min(1, js_max(5 / w, 5 / h));
    double maxScale = js_min((1.5 * STAGE_W) / w, (1.5 * STAGE_H) / h);
    t->size = clamp(size / 100, minScale, maxScale) * 100;
    if (t->visible) rt_request_redraw();
}
void rt_change_size(Target *t, double d) { rt_set_size(t, t->size + d); }
double rt_get_size(Target *t) { return rt_round(t->size); }

static int effect_index(Str *e) {
    static const char *names[] = {"color", "fisheye", "whirl", "pixelate", "mosaic", "brightness", "ghost"};
    Str *l = rt_str_lower(e);
    int r = -1;
    for (int i = 0; i < EFF_COUNT; i++) if (str_is(l, names[i])) r = i;
    rt_release_str(l);
    return r;
}

static double clamp_effect(int i, double v) {
    if (i == EFF_GHOST) return clamp(v, 0, 100);
    if (i == EFF_BRIGHTNESS) return clamp(v, -100, 100);
    return v;
}

void rt_set_effect(Target *t, Str *effect, double v) {
    int i = effect_index(effect);
    if (i < 0) return;
    t->effects[i] = clamp_effect(i, v);
    if (t->visible) rt_request_redraw();
}

void rt_change_effect(Target *t, Str *effect, double d) {
    int i = effect_index(effect);
    if (i < 0) return;
    t->effects[i] = clamp_effect(i, d + t->effects[i]);
    if (t->visible) rt_request_redraw();
}

void rt_clear_effects(Target *t) {
    for (int i = 0; i < EFF_COUNT; i++) t->effects[i] = 0;
    if (t->visible) rt_request_redraw();
}

/* ------------------------------------------------------------------------ */
/* Sensing                                                                   */

bool rt_key_pressed(Value key) { (void)key; return false; }

double rt_distance_to(Target *t, Value what) {
    if (t->is_stage) return 10000;
    double tx, ty;
    if (val_is(what, "_mouse_")) { tx = rt_mouse_x(); ty = rt_mouse_y(); }
    else {
        Str *name = rt_tostr(what);
        Target *o = rt_sprite_original_by_name(name);
        rt_release_str(name);
        if (!o) return 10000;
        tx = o->x; ty = o->y;
    }
    double dx = t->x - tx, dy = t->y - ty;
    return sqrt((dx * dx) + (dy * dy));
}

Value rt_sensing_of(Target *self, Str *prop, Value obj) {
    (void)self;
    Target *t;
    if (val_is(obj, "_stage_")) t = rt_stage;
    else {
        Str *name = rt_tostr(obj);
        t = rt_sprite_original_by_name(name);
        rt_release_str(name);
    }
    if (!t) return v_num(0);
    if (t->is_stage) {
        if (str_is(prop, "background #") || str_is(prop, "backdrop #")) return v_num(t->costume + 1);
        if (str_is(prop, "backdrop name")) return v_str(rt_costume_name(t));
        if (str_is(prop, "volume")) return v_num(t->volume);
    } else {
        if (str_is(prop, "x position")) return v_num(t->x);
        if (str_is(prop, "y position")) return v_num(t->y);
        if (str_is(prop, "direction")) return v_num(t->dir);
        if (str_is(prop, "costume #")) return v_num(t->costume + 1);
        if (str_is(prop, "costume name")) return v_str(rt_costume_name(t));
        if (str_is(prop, "size")) return v_num(t->size);
        if (str_is(prop, "volume")) return v_num(t->volume);
    }
    /* lookupVariableByNameAndType(name, '', skipStage=true): the target's own variables */
    Sprite *s = t->sprite;
    for (int32_t i = 0; i < s->nvars; i++) {
        Str *n = s->var_names[i];
        if (n->len == prop->len && memcmp(n->data, prop->data, (size_t)n->len) == 0) {
            rt_retain(t->vars[i]);
            return t->vars[i];
        }
    }
    return v_num(0);
}

static bool overlap(Rect a, Rect b) {
    return a.left < b.right && b.left < a.right && a.bottom < b.top && b.bottom < a.top;
}

bool rt_touching(Target *t, Value what) {
    if (val_is(what, "_mouse_")) {
        if (!t->visible) return false;
        Rect a = bounds(t);
        double x = rt_mouse_x(), y = rt_mouse_y();
        return x >= a.left && x <= a.right && y >= a.bottom && y <= a.top;
    }
    if (val_is(what, "_edge_")) {
        Rect b = bounds(t);
        return b.left < -STAGE_W / 2 || b.right > STAGE_W / 2 || b.top > STAGE_H / 2 || b.bottom < -STAGE_H / 2;
    }
    Str *name = rt_tostr(what);
    Target *o = rt_sprite_original_by_name(name);
    rt_release_str(name);
    if (!o || !t->visible) return false;
    Rect a = bounds(t);
    int32_t n;
    Target **ts = rt_exec_targets(&n);
    for (int32_t i = 0; i < n; i++) {
        Target *c = ts[i];
        if (c == t || c->sprite != o->sprite || !c->visible) continue;
        if (overlap(a, bounds(c))) return true;
    }
    return false;
}

/* sensing_current: local time; the runtime runs with TZ=UTC like the oracle */
double rt_current(Str *what) {
    Str *l = rt_str_lower(what);
    time_t secs = (time_t)floor(rt_now_ms() / 1000);
    struct tm tmv;
    gmtime_r(&secs, &tmv);
    double r = 0;
    if (str_is(l, "year")) r = tmv.tm_year + 1900;
    else if (str_is(l, "month")) r = tmv.tm_mon + 1;
    else if (str_is(l, "date")) r = tmv.tm_mday;
    else if (str_is(l, "dayofweek")) r = tmv.tm_wday + 1;
    else if (str_is(l, "hour")) r = tmv.tm_hour;
    else if (str_is(l, "minute")) r = tmv.tm_min;
    else if (str_is(l, "second")) r = tmv.tm_sec;
    rt_release_str(l);
    return r;
}

double rt_days_since_2000(void) {
    /* UTC: timezone offsets are 0, so this is (now - 2000-01-01) / msPerDay.
     * JS Date values are integral milliseconds. */
    double now = floor(rt_now_ms());
    return (now - 946684800000.0) / 86400000.0;
}

Str *rt_username(void) { return rt_empty_str(); }
double rt_loudness(void) { return -1; }

/* ------------------------------------------------------------------------ */
/* Ask and answer (sensing _questionList)                                    */

typedef struct Question { Str *q; int64_t uid; Target *t; int32_t was_visible, was_stage; } Question;
static Question *questions;
static int32_t nq, capq;
static Str *answer_str;
static int32_t question_pending; /* a QUESTION was emitted and awaits its answer */

static void ask_next(void) {
    if (nq == 0) return;
    Question *q = &questions[0];
    if (q->was_visible && !q->was_stage) {
        rt_emit_say(q->t, 0, v_str(q->q));
        rt_trace_ask(rt_empty_str());
    } else {
        rt_trace_ask(q->q);
    }
    question_pending = 1;
}

int32_t rt_ask(Thread *th, Frame *fr, Value question, int32_t first) {
    (void)fr; (void)first;
    Target *t = th->target;
    int was_asked = nq > 0;
    if (nq == capq) { capq = capq ? capq * 2 : 8; questions = realloc(questions, sizeof(Question) * (size_t)capq); }
    Question q;
    q.q = rt_tostr(question);
    q.uid = th->uid;
    q.t = t;
    q.was_visible = t->visible;
    q.was_stage = t->is_stage;
    questions[nq++] = q;
    if (!was_asked) ask_next();
    return R_PROMISE;
}

void rt_resume_uid(int64_t uid);
static const char **answers;
static int32_t nanswers, answer_idx;
void rt_set_answers(const char **a, int32_t n) { answers = a; nanswers = n; }

/* Called at the start of each frame: answer the pending question. */
void rt_deliver_answer(void) {
    if (!question_pending || nq == 0) return;
    question_pending = 0;
    const char *a = answer_idx < nanswers ? answers[answer_idx++] : "";
    if (answer_str) rt_release_str(answer_str);
    answer_str = rt_str_cstr(a);
    Question q = questions[0];
    memmove(questions, questions + 1, sizeof(Question) * (size_t)(nq - 1));
    nq--;
    if (q.was_visible && !q.was_stage) rt_emit_say(q.t, 0, v_str(rt_empty_str()));
    rt_resume_uid(q.uid);
    rt_release_str(q.q);
    ask_next();
}

int32_t rt_questions_pending(void) { return nq > 0; }

/* sensing._clearTargetQuestions (STOP_FOR_TARGET) */
void rt_clear_target_questions(Target *t) {
    int currently = nq > 0 && questions[0].t == t;
    int32_t k = 0;
    for (int32_t i = 0; i < nq; i++) {
        if (questions[i].t != t) questions[k++] = questions[i];
        else rt_release_str(questions[i].q);
    }
    nq = k;
    if (currently) {
        rt_emit_say(t, 0, v_str(rt_empty_str()));
        question_pending = 0;
        if (nq > 0) ask_next();
    }
}

void rt_clear_questions(void) {
    for (int32_t i = 0; i < nq; i++) rt_release_str(questions[i].q);
    nq = 0;
    question_pending = 0;
}

Str *rt_answer(void) {
    Str *s = answer_str ? answer_str : rt_empty_str();
    rt_retain_str(s);
    return s;
}

/* ------------------------------------------------------------------------ */
/* Music extension timing (sounds are silent headless, but beats take time) */

static double tempo = 60;
double rt_get_tempo(void) { return tempo; }
void rt_set_tempo(double t, int32_t change) {
    if (change) t = t + tempo;
    tempo = clamp(t, 20, 500); /* TEMPO_RANGE */
}

/* playDrumForBeats / restForBeats / playNoteForBeats: a stack timer of
 * (60 / tempo) * clamp(beats, 0, 100) seconds that yields (no redraw).
 * kind 1 (note) with 0 beats does not wait at all. Returns -1 when done. */
int32_t rt_music_wait(Thread *th, Frame *fr, double beats, int32_t kind, int32_t first) {
    (void)th;
    if (first || !fr->timer_on) {
        beats = clamp(beats, 0, 100);
        if (kind == 1 && beats == 0) return -1;
        fr->timer_on = 1;
        fr->timer_start = rt_now_ms();
        fr->timer_dur = (60 / tempo) * beats;
        return R_YIELD;
    }
    if (rt_now_ms() - fr->timer_start < fr->timer_dur * 1000) return R_YIELD;
    fr->timer_on = 0;
    return -1;
}
