/* SPDX-License-Identifier: AGPL-3.0-only */
/*
 * Claude's LLVM Scratch — native runtime.
 *
 * This header is the ABI between compiler-generated LLVM IR and the runtime.
 * The runtime is compiled to LLVM bitcode and linked with the generated module
 * so hot helpers (casts, compares) inline into compiled scripts.
 *
 * Semantics mirror scratch-vm 5.x (src/engine/sequencer.js, src/util/cast.js,
 * src/blocks/scratch3_*). Comments cite the JS behaviour being reproduced.
 */
#ifndef RT_H
#define RT_H

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>

/* ------------------------------------------------------------------------ */
/* Strings                                                                   */

/* Immutable, reference-counted UTF-8 string. rc < 0 means immortal (literal).
 * Layout is mirrored by the compiler when it emits literal strings:
 *   { i32 rc, i32 len, i32 u16len, i8 ascii, i8 numstate, i16 pad, double num, [N x i8] data }
 */
typedef struct Str {
    int32_t rc;
    int32_t len;      /* bytes, excluding terminating NUL */
    int32_t u16len;   /* JS .length (UTF-16 code units), -1 if not yet computed */
    uint8_t ascii;    /* 1 if all bytes < 0x80, 0 if not, 2 if unknown */
    uint8_t numstate; /* 0: num not computed, 1: num holds Number(str) */
    uint16_t pad;
    double num;       /* cached Number(str) (may be NaN) */
    char data[];      /* NUL-terminated */
} Str;

/* ------------------------------------------------------------------------ */
/* Values                                                                    */

enum { T_NUM = 0, T_STR = 1, T_BOOL = 2 };

/* A dynamically typed Scratch value. Passed in registers as two i64. */
typedef struct Value {
    int64_t tag;
    int64_t bits; /* double bits, Str*, or 0/1 */
} Value;

static inline double bits2d(int64_t b) { union { int64_t i; double d; } u; u.i = b; return u.d; }
static inline int64_t d2bits(double d) { union { int64_t i; double d; } u; u.d = d; return u.i; }
static inline Value v_num(double d) { Value v = {T_NUM, d2bits(d)}; return v; }
static inline Value v_bool(bool b) { Value v = {T_BOOL, b ? 1 : 0}; return v; }
static inline Value v_str(Str *s) { Value v = {T_STR, (int64_t)(intptr_t)s}; return v; }
static inline Str *v_as_str(Value v) { return (Str *)(intptr_t)v.bits; }

/* ------------------------------------------------------------------------ */
/* Lists                                                                     */

typedef struct List {
    int32_t len;
    int32_t cap;
    Value *items;
} List;

/* ------------------------------------------------------------------------ */
/* Program description (emitted by the compiler, registered at startup)      */

struct Thread;
struct Frame;
struct Target;

/* Status codes returned by compiled resumable functions. */
enum {
    R_RETURN = 0,      /* frame finished; pop and continue caller this tick */
    R_CALL = 1,        /* pushed a callee frame; run it now */
    R_CALL_YIELD = 2,  /* pushed a callee frame (recursive, non-warp); yield first */
    R_YIELD = 3,       /* util.yield() or loop back-edge: thread stays RUNNING */
    R_YIELD_TICK = 4,  /* util.yieldTick() */
    R_PROMISE = 5,     /* waiting on a timer/answer: STATUS_PROMISE_WAIT */
    R_STOP = 6,        /* thread finished (stop this script at top level, killed, stop all) */
};

typedef int (*ResumableFn)(struct Thread *, struct Frame *);

/* A resumable activation record (script body or procedure body). */
typedef struct Frame {
    ResumableFn fn;
    struct Frame *parent;
    int32_t state;       /* resume point, 0 = start */
    int32_t proc_id;     /* procedure index, -1 for a hat script */
    int32_t site_depth;  /* static C-block depth of the call site currently executing (isRecursiveCall) */
    int32_t warp;        /* frame runs in warp mode */
    int32_t nslots;
    int32_t size_class;
    /* scratch for block-specific state that survives yields */
    double timer_start;  /* stack timer (wait, glide) */
    double timer_dur;
    double glide_x0, glide_y0, glide_x1, glide_y1;
    int32_t timer_on;    /* stack timer initialised (stackTimerNeedsInit) */
    int32_t started_init;/* broadcast-and-wait: threads already started */
    int64_t *started;    /* broadcast-and-wait: uids of started threads */
    int32_t nstarted;
    int32_t executed;    /* ask: question already asked */
    Value slots[];       /* params first, then temporaries */
} Frame;

/* Hat kinds */
enum {
    HAT_FLAG = 1,
    HAT_BROADCAST = 2,
    HAT_CLONE = 3,
    HAT_TIMER_GT = 4,
    HAT_KEY = 5,
    HAT_CLICKED = 6,
    HAT_BACKDROP = 7,
    HAT_STAGE_CLICKED = 8,
    HAT_TOUCHING = 9,
};

typedef struct Script {
    int32_t hat;
    int32_t sprite;          /* sprite index (0 = stage) */
    int32_t index;           /* global script index (identity of "topBlock") */
    ResumableFn fn;
    int32_t nslots;
    Str *match;              /* broadcast name (uppercased) / key / backdrop name */
    /* edge-activated hat predicate, evaluated in the context of a target */
    Value (*predicate)(struct Target *);
} Script;

typedef struct Costume {
    Str *name;
    double w, h;       /* logical size (after bitmapResolution) */
    double cx, cy;     /* rotation centre in logical pixels */
    int bitmap_res;
    /* rasterized RGBA (premultiplied=0), for rendering; may be NULL */
    int img_w, img_h;  /* raster size in pixels */
    double img_scale;  /* raster pixels per logical pixel */
    const uint8_t *rgba;     /* decompressed on first use by the renderer */
    const uint8_t *rgba_z;   /* zlib-compressed RGBA embedded in the binary */
    int32_t rgba_zlen;
} Costume;

typedef struct Sprite {
    Str *name;
    int32_t is_stage;
    int32_t nvars, nlists;
    Str **var_names;
    Str **list_names;
    Value *var_init;
    List *list_init;
    int32_t ncostumes;
    Costume *costumes;
    /* initial state */
    double x, y, dir, size, volume;
    int32_t visible, costume, draggable, layer;
    int32_t rotation_style; /* 0 all around, 1 left-right, 2 don't rotate */
    int32_t nscripts;
    Script **scripts;
} Sprite;

enum { EFF_COLOR, EFF_FISHEYE, EFF_WHIRL, EFF_PIXELATE, EFF_MOSAIC, EFF_BRIGHTNESS, EFF_GHOST, EFF_COUNT };

typedef struct PenState {
    int down;
    double color, saturation, brightness, transparency; /* 0..100 each (Scratch 3 pen color model) */
    double diameter;
} PenState;

typedef struct Target {
    Sprite *sprite;
    int32_t is_stage;
    int32_t is_original;
    int32_t alive;
    double x, y, dir, size, volume;
    int32_t visible, costume, draggable;
    int32_t rotation_style;
    double effects[EFF_COUNT];
    Value *vars;
    List *lists;
    /* bubble */
    int32_t bubble_exists;
    int32_t bubble_type;   /* 0 say, 1 think */
    Str *bubble_text;
    int32_t bubble_usage;
    /* pen */
    PenState pen;
    /* edge-activated hat values: per script index */
    uint8_t *edge_has;
    uint8_t *edge_val;
    int32_t id;
    struct Target *original;
    int32_t nclones_dummy;
} Target;

enum { TS_RUNNING = 0, TS_PROMISE_WAIT = 1, TS_YIELD = 2, TS_YIELD_TICK = 3, TS_DONE = 4 };

typedef struct Thread {
    Target *target;
    Script *script;
    Frame *top;
    int32_t status;
    int32_t killed;
    int32_t resumed;     /* just resumed from a promise: skip the next loop yield */
    int32_t generation;
    double warp_start;   /* real-time warp timer start (realtime mode) */
    int32_t warp_timer_on;
    /* promise wait */
    double wake_at;
    int64_t wake_seq;
    int32_t wait_kind;   /* 1 timer, 2 answer */
    int32_t in_list;     /* still present in runtime.threads (for broadcast-and-wait) */
    int32_t detached;    /* replaced in the thread list by a restart while it was running */
    int64_t uid;         /* unique identity (JS object identity of the Thread) */
    int32_t answer_pending;
} Thread;


/* ------------------------------------------------------------------------ */
/* Runtime API used by generated code                                        */

/* strings / values */
Str *rt_str_new(const char *s, int32_t len);
Str *rt_str_cstr(const char *s);
void rt_str_free(Str *s);
static inline void rt_retain_str(Str *s) { if (s->rc >= 0) s->rc++; }
static inline void rt_release_str(Str *s) { if (s->rc > 0 && --s->rc == 0) rt_str_free(s); }
static inline void rt_retain(Value v) { if (v.tag == T_STR) rt_retain_str(v_as_str(v)); }
static inline void rt_release(Value v) { if (v.tag == T_STR) rt_release_str(v_as_str(v)); }

double rt_str_tonum_slow(Str *s);
static inline double rt_str_tonum(Str *s) {
    if (s->numstate) return s->num;
    return rt_str_tonum_slow(s);
}

/* Cast.toNumber */
double rt_tonum(Value v);
/* Cast.toBoolean */
bool rt_tobool(Value v);
bool rt_str_tobool(Str *s);
/* Cast.toString, returns an owned reference */
Str *rt_tostr(Value v);
Str *rt_num_tostr(double d);
Str *rt_bool_tostr(bool b);
/* Cast.compare: returns <0, 0, >0 */
int rt_compare(Value a, Value b);
int rt_compare_num_str(double a, Str *b);
int rt_compare_str_str(Str *a, Str *b);
/* Number(v) (not Cast.toNumber): NaN preserved; used by compare and some blocks */
double rt_js_number(Value v);
bool rt_is_whitespace(Value v);

/* operators */
Str *rt_join(Str *a, Str *b);
Str *rt_letter_of(double index1, Str *s);
double rt_length(Str *s);
bool rt_contains(Str *hay, Str *needle);
double rt_mod(double n, double m);
double rt_round(double x);
double rt_mathop(int op, double n);
double rt_random(Value from, Value to);
double rt_js_random(void);
void rt_seed_random(uint32_t seed);
Str *rt_str_lower(Str *s);

/* lists */
void rt_list_add(List *l, Value v);                 /* retains v */
void rt_list_delete(List *l, Value index);
void rt_list_clear(List *l);
void rt_list_insert(List *l, Value index, Value v); /* retains v */
void rt_list_replace(List *l, Value index, Value v);/* retains v */
Value rt_list_item(List *l, Value index);           /* borrowed */
Value rt_list_item_num(List *l, double index);      /* borrowed */
double rt_list_index_of(List *l, Value v);
double rt_list_length(List *l);
bool rt_list_contains(List *l, Value v);
Str *rt_list_contents(List *l);                     /* owned */
void rt_list_copy(List *dst, const List *src);
void rt_list_free_items(List *l);

/* variables */
static inline void rt_set_var(Value *slot, Value v) { /* v is owned by caller: transfer */
    Value old = *slot; *slot = v; rt_release(old);
}

/* global runtime state */
extern Value *rt_globals;       /* stage variables */
extern List *rt_global_lists;   /* stage lists */
extern Target *rt_stage;
extern int32_t rt_stop_flag;    /* stop all happened during direct (warp) code */

/* threads / frames */
Frame *rt_push_frame(struct Thread *th, ResumableFn fn, int32_t nslots, int32_t proc_id, int32_t warp);
bool rt_is_recursive_call(struct Thread *th, int32_t proc_id);
bool rt_is_recursive_call_at(struct Thread *th, int32_t proc_id, int32_t depth);
void rt_yield_skip_check(void);

/* events / control */
void rt_broadcast(Str *name);
int32_t rt_broadcast_and_wait(struct Thread *th, struct Frame *fr, Str *name, int32_t first); /* returns R_* or -1 to continue */
void rt_stop_all(struct Thread *th);            /* the calling thread is always stopped */
void rt_stop_other_scripts(struct Thread *th);
void rt_create_clone(Target *self, Str *which);
int32_t rt_delete_clone(struct Thread *th);
int32_t rt_wait(struct Thread *th, struct Frame *fr, double secs, int32_t first);
int32_t rt_wait_until(struct Thread *th, struct Frame *fr, bool cond);
double rt_get_counter(void);
void rt_incr_counter(void);
void rt_clear_counter(void);
bool rt_warp_timer_expired(struct Thread *th);

/* looks */
void rt_say(Target *t, Value msg, int32_t think);
int32_t rt_say_for_secs(struct Thread *th, struct Frame *fr, Value msg, double secs, int32_t think, int32_t first);
void rt_show(Target *t);
void rt_hide(Target *t);
void rt_switch_costume(Target *t, Value costume);
void rt_next_costume(Target *t);
void rt_switch_backdrop(Value backdrop);
void rt_next_backdrop(void);
double rt_costume_number(Target *t);
Str *rt_costume_name(Target *t);
double rt_backdrop_number(void);
Str *rt_backdrop_name(void);
void rt_set_size(Target *t, double size);
void rt_change_size(Target *t, double d);
double rt_get_size(Target *t);
void rt_set_effect(Target *t, Str *effect, double v);
void rt_change_effect(Target *t, Str *effect, double d);
void rt_clear_effects(Target *t);
void rt_go_front_back(Target *t, int32_t front);
void rt_go_layers(Target *t, int32_t forward, double n);

/* motion */
void rt_set_xy(Target *t, double x, double y);
void rt_move_steps(Target *t, double steps);
void rt_turn(Target *t, double degrees);
void rt_point_dir(Target *t, double d);
void rt_point_towards(Target *t, Value towards);
void rt_goto(Target *t, Value to);
int32_t rt_glide(struct Thread *th, struct Frame *fr, double secs, double x, double y, int32_t first);
int32_t rt_glide_to(struct Thread *th, struct Frame *fr, double secs, Value to, int32_t first);
void rt_bounce(Target *t);
void rt_set_rotation_style(Target *t, Str *style);
double rt_get_x(Target *t);
double rt_get_y(Target *t);
double rt_get_dir(Target *t);

/* sensing */
double rt_timer(void);
void rt_reset_timer(void);
double rt_mouse_x(void);
double rt_mouse_y(void);
bool rt_mouse_down(void);
bool rt_key_pressed(Value key);
double rt_distance_to(Target *t, Value what);
Value rt_sensing_of(Target *t, Str *prop, Value obj); /* owned */
bool rt_touching(Target *t, Value what);
double rt_current(Str *what);
double rt_days_since_2000(void);
Str *rt_username(void);
int32_t rt_ask(struct Thread *th, struct Frame *fr, Value question, int32_t first);
Str *rt_answer(void);
double rt_loudness(void);

/* pen */
void rt_pen_clear(void);
void rt_pen_down(Target *t);
void rt_pen_up(Target *t);
void rt_pen_stamp(Target *t);
void rt_pen_set_color(Target *t, Value color);
void rt_pen_set_param(Target *t, Str *param, double v);
void rt_pen_change_param(Target *t, Str *param, double v);
void rt_pen_set_size(Target *t, double s);
void rt_pen_change_size(Target *t, double d);

/* program registration (called from generated rt_program_init) */
Sprite *rt_add_sprite(Str *name, int32_t is_stage, int32_t nvars, int32_t nlists, int32_t ncostumes);
void rt_sprite_var(Sprite *s, int32_t i, Str *name, Value init);
void rt_sprite_list(Sprite *s, int32_t i, Str *name);
void rt_sprite_list_item(Sprite *s, int32_t i, Value item);
void rt_sprite_costume(Sprite *s, int32_t i, Str *name, double w, double h, double cx, double cy,
                       int32_t img_w, int32_t img_h, double img_scale, const uint8_t *rgba_z, int32_t zlen);
void rt_sprite_state(Sprite *s, double x, double y, double dir, double size, int32_t visible,
                     int32_t costume, int32_t layer, int32_t rotation_style, double volume);
Script *rt_add_script(Sprite *s, int32_t hat, ResumableFn fn, int32_t nslots, Str *match,
                      Value (*predicate)(Target *));

/* the compiler provides this */
void rt_program_init(void);

/* scheduler internals shared by the block implementations */
void rt_request_redraw(void);
double rt_now_ms(void);
Target *rt_sprite_original_by_name(Str *name);
Target **rt_exec_targets(int32_t *n);
void rt_emit_say(Target *t, int32_t think, Value v);
void rt_pen_trace_line(Target *t, double x0, double y0, double x1, double y1);
void rt_pen_trace_point(Target *t, double x, double y);
void rt_pen_trace_clear(void);
void rt_pen_trace_stamp(Target *t);
void rt_stop_for_target(Target *t, Thread *except);
void rt_bubbles_reset(void);
void rt_bubble_target_exit(Target *t);
int64_t rt_start_hats(int32_t hat, Str *match, Target *only, int64_t **started, int32_t *nstarted);
Str *rt_empty_str(void);
int rt_js_num_format(double d, char *buf);
extern int32_t rt_deterministic;
extern int32_t rt_frame;

/* trace / diagnostics */
extern int32_t rt_trace_enabled;
void rt_panic(const char *msg);

#endif
