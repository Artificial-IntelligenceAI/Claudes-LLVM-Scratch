/*
 * Small non-inline helpers for compiler-generated code. Everything here is
 * inlined into compiled scripts by link-time optimisation.
 */
#include "rt.h"
#include <math.h>
#include <string.h>

void cg_retain(Value v) { rt_retain(v); }
void cg_release(Value v) { rt_release(v); }
void cg_retain_str(Str *s) { rt_retain_str(s); }
void cg_release_str(Str *s) { rt_release_str(s); }

double cg_str_tonum(Str *s) {
    double d = rt_str_tonum(s);
    return d != d ? 0.0 : d;
}

/* Cast.compare for two numbers. With a NaN operand scratch-vm compares
 * String(a).toLowerCase() with String(b).toLowerCase(): "nan" against a
 * number's string, which always starts with a digit, '-' or "infinity" --
 * all of which sort before 'n'. So NaN is greater than every number and
 * equal to NaN, and no call is needed (keeping hot loops call-free). */
int32_t cg_cmp_nn(double a, double b) {
    if (a == a && b == b) return a < b ? -1 : (a > b ? 1 : 0);
    if (a != a && b != b) return 0;
    return a != a ? 1 : -1;
}

int32_t cg_cmp(Value a, Value b) {
    if (a.tag == T_NUM && b.tag == T_NUM) return cg_cmp_nn(bits2d(a.bits), bits2d(b.bits));
    int c = rt_compare(a, b);
    return c < 0 ? -1 : (c > 0 ? 1 : 0);
}

int32_t cg_tobool(Value v) { return rt_tobool(v); }
int32_t cg_str_tobool(Str *s) { return rt_str_tobool(s); }
Str *cg_bool_tostr(int32_t b) { return rt_bool_tostr(b != 0); }

Value cg_list_item(List *l, Value index) { return rt_list_item(l, index); }
Value cg_list_item_num(List *l, double index) { return rt_list_item_num(l, index); }
int32_t cg_list_contains(List *l, Value v) { return rt_list_contains(l, v); }
int32_t cg_contains(Str *a, Str *b) { return rt_contains(a, b); }

void cg_broadcast(Value name) {
    Str *s = rt_tostr(name);
    Str *l = rt_str_lower(s);
    rt_broadcast(l);
    rt_release_str(l);
    rt_release_str(s);
}

int32_t cg_broadcast_and_wait(struct Thread *th, struct Frame *fr, Value name, int32_t first) {
    Str *s = rt_tostr(name);
    Str *l = rt_str_lower(s);
    int32_t r = rt_broadcast_and_wait(th, fr, l, first);
    rt_release_str(l);
    rt_release_str(s);
    return r;
}

void cg_create_clone(Target *t, Value which) {
    Str *s = rt_tostr(which);
    rt_create_clone(t, s);
    rt_release_str(s);
}

/* Target x/y adjustments (motion_changexby etc.) */
void cg_change_x(Target *t, double dx) { rt_set_xy(t, t->x + dx, t->y); }
void cg_set_x(Target *t, double x) { rt_set_xy(t, x, t->y); }
void cg_change_y(Target *t, double dy) { rt_set_xy(t, t->x, t->y + dy); }
void cg_set_y(Target *t, double y) { rt_set_xy(t, t->x, y); }

/* sound volume */
static double clamp100(double v) {
    if (v != v) return v;
    return v < 0 ? 0 : (v > 100 ? 100 : v);
}
void cg_set_volume(Target *t, double v) { t->volume = clamp100(v); }
void cg_change_volume(Target *t, double d) { t->volume = clamp100(t->volume + d); }
double cg_get_volume(Target *t) { return t->volume; }

int32_t cg_touching(Target *t, Value what) { return rt_touching(t, what); }
int32_t cg_is_recursive(struct Thread *th, int32_t proc, int32_t depth) {
    return rt_is_recursive_call_at(th, proc, depth);
}
int32_t cg_warp_expired(struct Thread *th) { return rt_warp_timer_expired(th); }

Value cg_box_str(Str *s) { return v_str(s); }

void cg_set_effect(Target *t, Str *e, double v) { rt_set_effect(t, e, v); }
void cg_change_effect(Target *t, Str *e, double v) { rt_change_effect(t, e, v); }

/* JS Number(v) (for 1000 * args.SECS in say-for-secs) */
double cg_js_number(Value v) { return rt_js_number(v); }

int32_t cg_say_for_secs(struct Thread *th, struct Frame *fr, Value msg, Value secs, int32_t think) {
    return rt_say_for_secs(th, fr, msg, rt_js_number(secs), think, 1);
}

double cg_current(Str *what) { return rt_current(what); }
