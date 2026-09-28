/*
 * Targets, threads and the scheduler.
 *
 * Mirrors scratch-vm 5.x:
 *   - src/engine/sequencer.js   stepThreads / stepThread / stepToProcedure
 *   - src/engine/runtime.js     startHats / _pushThread / _restartThread / stopAll / _step
 *   - src/engine/thread.js      isRecursiveCall
 *   - src/sprites/rendered-target.js  makeClone / dispose
 *
 * Compiled scripts are resumable functions (state machines over a Frame).
 * The trampoline in step_thread() plays the role of stepThread's loop.
 */
#include "rt.h"
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <math.h>
#include <time.h>

/* ------------------------------------------------------------------------ */
/* Program                                                                   */

static Sprite **sprites;
static int32_t nsprites, capsprites;
static Script **all_scripts;
static int32_t nscripts, capscripts;

#define PUSH(arr, n, cap, v) do { \
    if ((n) == (cap)) { (cap) = (cap) ? (cap) * 2 : 16; (arr) = realloc((arr), sizeof(*(arr)) * (size_t)(cap)); } \
    (arr)[(n)++] = (v); } while (0)

Sprite *rt_add_sprite(Str *name, int32_t is_stage, int32_t nvars, int32_t nlists, int32_t ncostumes) {
    Sprite *s = (Sprite *)calloc(1, sizeof(Sprite));
    s->name = name;
    s->is_stage = is_stage;
    s->nvars = nvars;
    s->nlists = nlists;
    s->var_names = (Str **)calloc((size_t)nvars + 1, sizeof(Str *));
    s->var_init = (Value *)calloc((size_t)nvars + 1, sizeof(Value));
    s->list_names = (Str **)calloc((size_t)nlists + 1, sizeof(Str *));
    s->list_init = (List *)calloc((size_t)nlists + 1, sizeof(List));
    s->ncostumes = ncostumes;
    s->costumes = (Costume *)calloc((size_t)ncostumes + 1, sizeof(Costume));
    s->size = 100;
    s->dir = 90;
    s->visible = 1;
    s->volume = 100;
    PUSH(sprites, nsprites, capsprites, s);
    return s;
}

void rt_sprite_var(Sprite *s, int32_t i, Str *name, Value init) {
    s->var_names[i] = name;
    rt_retain(init);
    s->var_init[i] = init;
}

void rt_sprite_list(Sprite *s, int32_t i, Str *name) { s->list_names[i] = name; }

void rt_sprite_list_item(Sprite *s, int32_t i, Value item) { rt_list_add(&s->list_init[i], item); }

void rt_sprite_costume(Sprite *s, int32_t i, Str *name, double w, double h, double cx, double cy,
                       int32_t img_w, int32_t img_h, double img_scale, const uint8_t *rgba_z, int32_t zlen) {
    Costume *c = &s->costumes[i];
    c->name = name;
    c->w = w; c->h = h; c->cx = cx; c->cy = cy;
    c->img_w = img_w; c->img_h = img_h; c->img_scale = img_scale;
    c->rgba = NULL;
    c->rgba_z = rgba_z;
    c->rgba_zlen = zlen;
}

void rt_sprite_state(Sprite *s, double x, double y, double dir, double size, int32_t visible,
                     int32_t costume, int32_t layer, int32_t rotation_style, double volume) {
    s->x = x; s->y = y; s->dir = dir; s->size = size; s->visible = visible;
    s->costume = costume; s->layer = layer; s->rotation_style = rotation_style; s->volume = volume;
}

Script *rt_add_script(Sprite *s, int32_t hat, ResumableFn fn, int32_t nslots, Str *match,
                      Value (*predicate)(Target *)) {
    Script *sc = (Script *)calloc(1, sizeof(Script));
    sc->hat = hat;
    sc->fn = fn;
    sc->nslots = nslots;
    sc->match = match;
    sc->predicate = predicate;
    sc->index = nscripts;
    sc->sprite = -1;
    for (int32_t i = 0; i < nsprites; i++) if (sprites[i] == s) sc->sprite = i;
    PUSH(all_scripts, nscripts, capscripts, sc);
    int32_t n = s->nscripts, cap = n; /* scripts array grows by exact size */
    s->scripts = (Script **)realloc(s->scripts, sizeof(Script *) * (size_t)(n + 1));
    s->scripts[n] = sc;
    s->nscripts = n + 1;
    (void)cap;
    return sc;
}

/* ------------------------------------------------------------------------ */
/* Targets                                                                   */

static Target **targets;       /* runtime.targets */
static int32_t ntargets, captargets;
static Target **exec_t;        /* runtime.executableTargets (index 0 = bottom = stage) */
static int32_t nexec, capexec;
Target *rt_stage;
Value *rt_globals;
List *rt_global_lists;
static int32_t clone_counter;
static int32_t next_target_id;
#define MAX_CLONES 300

Target **rt_exec_targets(int32_t *n) { *n = nexec; return exec_t; }

static Target *new_target(Sprite *s) {
    Target *t = (Target *)calloc(1, sizeof(Target));
    t->sprite = s;
    t->is_stage = s->is_stage;
    t->is_original = 1;
    t->alive = 1;
    t->x = s->x; t->y = s->y; t->dir = s->dir; t->size = s->size; t->volume = s->volume;
    t->visible = s->visible; t->costume = s->costume;
    t->rotation_style = s->rotation_style;
    t->vars = (Value *)calloc((size_t)s->nvars + 1, sizeof(Value));
    for (int32_t i = 0; i < s->nvars; i++) { t->vars[i] = s->var_init[i]; rt_retain(t->vars[i]); }
    t->lists = (List *)calloc((size_t)s->nlists + 1, sizeof(List));
    for (int32_t i = 0; i < s->nlists; i++) rt_list_copy(&t->lists[i], &s->list_init[i]);
    t->edge_has = (uint8_t *)calloc((size_t)s->nscripts + 1, 1);
    t->edge_val = (uint8_t *)calloc((size_t)s->nscripts + 1, 1);
    /* Scratch 3 pen defaults: color 66.66 (blue), saturation 100, brightness 100, transparency 0, size 1 */
    t->pen.color = 66.66;
    t->pen.saturation = 100;
    t->pen.brightness = 100;
    t->pen.transparency = 0;
    t->pen.diameter = 1;
    t->id = next_target_id++;
    t->original = t;
    return t;
}

Target *rt_sprite_original_by_name(Str *name) {
    for (int32_t i = 0; i < ntargets; i++) {
        Target *t = targets[i];
        if (t->is_stage || !t->is_original) continue;
        Str *n = t->sprite->name;
        if (n->len == name->len && memcmp(n->data, name->data, (size_t)n->len) == 0) return t;
    }
    return NULL;
}

static void exec_remove(Target *t) {
    for (int32_t i = 0; i < nexec; i++)
        if (exec_t[i] == t) {
            memmove(&exec_t[i], &exec_t[i + 1], sizeof(Target *) * (size_t)(nexec - i - 1));
            nexec--;
            return;
        }
}

static int32_t exec_index(Target *t) {
    for (int32_t i = 0; i < nexec; i++) if (exec_t[i] == t) return i;
    return -1;
}

/* runtime.moveExecutable */
static int32_t exec_move(Target *t, double delta) {
    int32_t old = exec_index(t);
    if (old < 0) return -1;
    memmove(&exec_t[old], &exec_t[old + 1], sizeof(Target *) * (size_t)(nexec - old - 1));
    nexec--;
    double ni = old + delta;
    if (ni > nexec) ni = nexec;
    if (ni <= 0) ni = (nexec > 0 && exec_t[0]->is_stage) ? 1 : 0;
    int32_t idx = ni != ni ? 0 : (int32_t)ni; /* JS splice: ToIntegerOrInfinity, NaN -> 0 */
    if (idx > nexec) idx = nexec;
    PUSH(exec_t, nexec, capexec, NULL);
    memmove(&exec_t[idx + 1], &exec_t[idx], sizeof(Target *) * (size_t)(nexec - 1 - idx));
    exec_t[idx] = t;
    return idx;
}

void rt_go_front_back(Target *t, int32_t front) {
    if (t->is_stage) return;
    exec_move(t, front ? INFINITY : -INFINITY);
}

void rt_go_layers(Target *t, int32_t forward, double n) {
    if (t->is_stage) return;
    exec_move(t, forward ? n : -n);
}

/* ------------------------------------------------------------------------ */
/* Threads                                                                   */

typedef struct ThreadList { Thread **v; int32_t n, cap; } ThreadList;

static ThreadList *threads;      /* runtime.threads */
static Thread *active_thread;    /* sequencer.activeThread */
static int64_t next_uid = 1;
static int32_t redraw_requested;
int32_t rt_deterministic = 1;
int32_t rt_frame = 0;
int32_t rt_stop_flag = 0;
int32_t rt_trace_enabled = 1;
static double now_ms = 1700000000000.0;
static double real_start_ms;
static int32_t ticks_per_frame = 64;
static int32_t turbo = 0;

void rt_request_redraw(void) { redraw_requested = 1; }
double rt_now_ms(void) { return now_ms; }

static double real_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec * 1000.0 + (double)ts.tv_nsec / 1e6;
}

static ThreadList *tl_new(void) { return (ThreadList *)calloc(1, sizeof(ThreadList)); }
static void tl_push(ThreadList *l, Thread *t) { PUSH(l->v, l->n, l->cap, t); }

static void free_frames(Thread *th) {
    Frame *f = th->top;
    while (f) {
        Frame *p = f->parent;
        for (int32_t i = 0; i < f->nslots; i++) rt_release(f->slots[i]);
        free(f->started);
        free(f);
        f = p;
    }
    th->top = NULL;
}

static void free_thread(Thread *th) {
    free_frames(th);
    free(th);
}

Frame *rt_push_frame(Thread *th, ResumableFn fn, int32_t nslots, int32_t proc_id, int32_t warp) {
    Frame *f = (Frame *)calloc(1, sizeof(Frame) + sizeof(Value) * (size_t)(nslots > 0 ? nslots : 1));
    if (!f) rt_panic("out of memory");
    f->fn = fn;
    f->parent = th->top;
    f->proc_id = proc_id;
    f->warp = warp;
    f->nslots = nslots;
    for (int32_t i = 0; i < nslots; i++) f->slots[i] = v_num(0);
    th->top = f;
    return f;
}

static void pop_frame(Thread *th) {
    Frame *f = th->top;
    th->top = f->parent;
    for (int32_t i = 0; i < f->nslots; i++) rt_release(f->slots[i]);
    free(f->started);
    free(f);
}

/* thread.isRecursiveCall: walk up to 5 enclosing stack entries. Each resumable
 * frame contributes site_depth C-block entries plus the call entry that
 * invoked it. The caller passes the static depth of the current call site. */
bool rt_is_recursive_call_at(Thread *th, int32_t proc, int32_t depth) {
    int32_t count = 5;
    Frame *f = th->top;
    int32_t d = depth;
    for (;;) {
        for (int32_t i = 0; i < d; i++)
            if (--count < 0) return false;
        if (!f || !f->parent) return false;
        if (f->proc_id == proc) return true;
        if (--count < 0) return false;
        f = f->parent;
        d = f->site_depth;
    }
}

bool rt_is_recursive_call(Thread *th, int32_t proc) { return rt_is_recursive_call_at(th, proc, 0); }

static Thread *new_thread(Script *sc, Target *t) {
    Thread *th = (Thread *)calloc(1, sizeof(Thread));
    th->target = t;
    th->script = sc;
    th->uid = next_uid++;
    th->status = TS_RUNNING;
    th->in_list = 1;
    rt_push_frame(th, sc->fn, sc->nslots, -1, 0);
    return th;
}

/* runtime._pushThread */
static Thread *push_thread(Script *sc, Target *t) {
    Thread *th = new_thread(sc, t);
    tl_push(threads, th);
    return th;
}

/* runtime._restartThread: a fresh Thread replaces the old one in place. */
static Thread *restart_thread(Thread *old) {
    Thread *th = new_thread(old->script, old->target);
    for (int32_t i = 0; i < threads->n; i++) {
        if (threads->v[i] == old) {
            threads->v[i] = th;
            old->in_list = 0;
            if (old == active_thread) old->detached = 1;
            else free_thread(old);
            return th;
        }
    }
    /* JS pushes the *old* thread back here (quirk); it was not in the list. */
    free_thread(th);
    tl_push(threads, old);
    return old;
}

/* runtime._stopThread / sequencer.retireThread */
static void stop_thread(Thread *th) {
    th->killed = 1;
    th->status = TS_DONE;
    /* frames are freed when the thread is filtered out (it may be running now) */
}

static bool thread_in_list(int64_t uid) {
    for (int32_t i = 0; i < threads->n; i++)
        if (threads->v[i]->uid == uid) return true;
    return false;
}

static Thread *thread_by_uid(int64_t uid) {
    for (int32_t i = 0; i < threads->n; i++)
        if (threads->v[i]->uid == uid) return threads->v[i];
    return NULL;
}

/* runtime.isWaitingThread */
static bool is_waiting_thread(int64_t uid) {
    Thread *t = thread_by_uid(uid);
    if (!t) return true;
    if (t->status == TS_PROMISE_WAIT || t->status == TS_YIELD_TICK) return true;
    return t->top == NULL || t->status == TS_DONE;
}

/* ------------------------------------------------------------------------ */
/* Hats                                                                      */

static bool str_eq(Str *a, Str *b) { return a->len == b->len && memcmp(a->data, b->data, (size_t)a->len) == 0; }

/* runtime.startHats. `match` must already be lower-cased (compiler lower-cases hat fields).
 * Returns the number of started threads; their uids are appended to *started. */
int64_t rt_start_hats(int32_t hat, Str *match, Target *only, int64_t **started, int32_t *nstarted) {
    int32_t restart = (hat == HAT_FLAG || hat == HAT_BROADCAST || hat == HAT_CLICKED ||
                       hat == HAT_BACKDROP || hat == HAT_STAGE_CLICKED);
    int32_t edge = (hat == HAT_TIMER_GT || hat == HAT_TOUCHING);
    int64_t count = 0;
    /* snapshot targets: starting threads never changes executableTargets */
    int32_t n = only ? 1 : nexec;
    for (int32_t ti = n - 1; ti >= 0; ti--) {
        Target *t = only ? only : exec_t[ti];
        Sprite *s = t->sprite;
        for (int32_t si = 0; si < s->nscripts; si++) {
            Script *sc = s->scripts[si];
            if (sc->hat != hat) continue;
            if (match && sc->match && !str_eq(match, sc->match)) continue;
            Thread *th = NULL;
            if (restart) {
                for (int32_t i = 0; i < threads->n; i++) {
                    Thread *o = threads->v[i];
                    if (o->target == t && o->script == sc) { th = restart_thread(o); break; }
                }
            } else {
                bool running = false;
                for (int32_t i = 0; i < threads->n; i++) {
                    Thread *o = threads->v[i];
                    if (o->target == t && o->script == sc && o->status != TS_DONE) { running = true; break; }
                }
                if (running) continue;
            }
            if (edge) {
                /* Evaluate the predicate (execute() on the hat) before pushing. */
                Value v = sc->predicate(t);
                bool val = rt_tobool(v);
                rt_release(v);
                bool had = t->edge_has[si];
                bool old = t->edge_val[si];
                t->edge_has[si] = 1;
                t->edge_val[si] = val;
                bool activated = had ? (!old && val) : val;
                if (!activated) continue;
            }
            if (!th) th = push_thread(sc, t);
            count++;
            if (started) {
                *started = (int64_t *)realloc(*started, sizeof(int64_t) * (size_t)(*nstarted + 1));
                (*started)[(*nstarted)++] = th->uid;
            }
        }
    }
    return count;
}

/* ------------------------------------------------------------------------ */
/* Clones                                                                    */

void rt_create_clone(Target *self, Str *which) {
    Target *src;
    if (which->len == 8 && memcmp(which->data, "_myself_", 8) == 0) src = self;
    else src = rt_sprite_original_by_name(which);
    if (!src || src->is_stage) return;
    if (clone_counter >= MAX_CLONES) return;
    clone_counter++;
    Target *c = (Target *)calloc(1, sizeof(Target));
    Sprite *s = src->sprite;
    *c = *src;
    c->is_original = 0;
    c->alive = 1;
    c->id = next_target_id++;
    c->original = src->original;
    c->vars = (Value *)calloc((size_t)s->nvars + 1, sizeof(Value));
    for (int32_t i = 0; i < s->nvars; i++) { c->vars[i] = src->vars[i]; rt_retain(c->vars[i]); }
    c->lists = (List *)calloc((size_t)s->nlists + 1, sizeof(List));
    for (int32_t i = 0; i < s->nlists; i++) rt_list_copy(&c->lists[i], &src->lists[i]);
    c->edge_has = (uint8_t *)malloc((size_t)s->nscripts + 1);
    c->edge_val = (uint8_t *)malloc((size_t)s->nscripts + 1);
    memcpy(c->edge_has, src->edge_has, (size_t)s->nscripts + 1);
    memcpy(c->edge_val, src->edge_val, (size_t)s->nscripts + 1);
    c->bubble_exists = 0;
    c->bubble_text = NULL;
    /* pen state is per target in scratch-vm (copied via custom state on clone) */
    /* initDrawable -> startHats('control_start_as_clone', null, clone) happens
     * before the clone joins executableTargets. */
    rt_start_hats(HAT_CLONE, NULL, c, NULL, NULL);
    if (c->visible) rt_request_redraw(); /* updateAllDrawableProperties */
    PUSH(targets, ntargets, captargets, c);
    PUSH(exec_t, nexec, capexec, c);
    /* goBehindOther(src) */
    int32_t pos = exec_index(src);
    int32_t old = exec_index(c);
    exec_move(c, (double)(pos - old));
}

static void dispose_target(Target *t) {
    clone_counter--;
    rt_stop_for_target(t, NULL);
    exec_remove(t);
    for (int32_t i = 0; i < ntargets; i++)
        if (targets[i] == t) {
            memmove(&targets[i], &targets[i + 1], sizeof(Target *) * (size_t)(ntargets - i - 1));
            ntargets--;
            break;
        }
    if (t->visible) rt_request_redraw();
    rt_bubble_target_exit(t);
    t->alive = 0;
    /* Target memory is kept alive (threads and frames may still point at it). */
}

/* Returns 1 if the calling thread was stopped (it is not, if it is detached). */
int32_t rt_delete_clone(Thread *th) {
    Target *t = th->target;
    if (t->is_original) return 0;
    dispose_target(t);
    rt_stop_for_target(t, NULL);
    return th->status == TS_DONE;
}

void rt_clear_target_questions(Target *t);
void rt_clear_questions(void);

void rt_stop_for_target(Target *t, Thread *except) {
    rt_clear_target_questions(t);
    for (int32_t i = 0; i < threads->n; i++) {
        Thread *th = threads->v[i];
        if (th == except) continue;
        if (th->target == t) stop_thread(th);
    }
}

void rt_stop_other_scripts(Thread *th) { rt_stop_for_target(th->target, th); }

/* runtime.stopAll */
static ThreadList *stale_lists[64];
static int32_t nstale;

void rt_stop_all(Thread *th) {
    (void)th;
    /* PROJECT_STOP_ALL listeners: bubbles reset (looks), questions cleared (sensing) */
    rt_bubbles_reset();
    rt_clear_questions();
    Target **keep = (Target **)malloc(sizeof(Target *) * (size_t)(ntargets + 1));
    int32_t nk = 0;
    int32_t n = ntargets;
    Target **snapshot = (Target **)malloc(sizeof(Target *) * (size_t)(n + 1));
    memcpy(snapshot, targets, sizeof(Target *) * (size_t)n);
    for (int32_t i = 0; i < n; i++) {
        Target *t = snapshot[i];
        rt_clear_effects(t); /* onStopAll */
        if (!t->is_original) {
            /* dispose(): stops its threads, removes from executables */
            clone_counter--;
            rt_stop_for_target(t, NULL);
            exec_remove(t);
            if (t->visible) rt_request_redraw();
            t->alive = 0;
        } else {
            keep[nk++] = t;
        }
    }
    free(snapshot);
    free(targets);
    targets = keep;
    ntargets = nk;
    captargets = nk + 1;
    if (active_thread) stop_thread(active_thread);
    /* `this.threads = []`: the sequencer keeps iterating the old array for the
     * rest of the current pass, so it is retired, not freed, here. */
    if (nstale < 64) stale_lists[nstale++] = threads;
    threads = tl_new();
    rt_stop_flag = 1;
}

/* ------------------------------------------------------------------------ */
/* Timers for promise waits (say for secs, ask)                              */

typedef struct Timer {
    double at;
    int64_t seq;
    int64_t thread_uid;
    Target *target;
    int32_t bubble_usage; /* say/think-for-secs: clear the bubble if unchanged */
    int32_t think;
} Timer;

static Timer *timers;
static int32_t ntimers, captimers;
static int64_t timer_seq = 1;

void rt_add_timer(Thread *th, double delay_ms, int32_t bubble_usage, int32_t think) {
    Timer tm;
    if (!(delay_ms > 0)) delay_ms = 0;
    tm.at = now_ms + delay_ms;
    tm.seq = timer_seq++;
    tm.thread_uid = th->uid;
    tm.target = th->target;
    tm.bubble_usage = bubble_usage;
    tm.think = think;
    PUSH(timers, ntimers, captimers, tm);
}

void rt_bubble_clear_if(Target *t, int32_t usage, int32_t think);

static int timer_cmp(const void *a, const void *b) {
    const Timer *x = (const Timer *)a, *y = (const Timer *)b;
    if (x->at != y->at) return x->at < y->at ? -1 : 1;
    return x->seq < y->seq ? -1 : (x->seq > y->seq ? 1 : 0);
}

static void resume_thread_uid(int64_t uid) {
    Thread *t = thread_by_uid(uid);
    if (t && t->status == TS_PROMISE_WAIT) {
        t->status = TS_RUNNING;
        t->resumed = 1;
    }
}

static void fire_due_timers(void) {
    if (!ntimers) return;
    qsort(timers, (size_t)ntimers, sizeof(Timer), timer_cmp);
    int32_t k = 0;
    while (k < ntimers && timers[k].at <= now_ms) {
        Timer tm = timers[k++];
        if (tm.bubble_usage >= 0) rt_bubble_clear_if(tm.target, tm.bubble_usage, tm.think);
        resume_thread_uid(tm.thread_uid);
    }
    memmove(timers, timers + k, sizeof(Timer) * (size_t)(ntimers - k));
    ntimers -= k;
}

/* Promises that resolve immediately (Promise.resolve()): the thread resumes
 * after the current step's microtasks, i.e. before the next frame's timers. */
static int64_t *immediates;
static int32_t nimm, capimm;
int32_t rt_promise_immediate(Thread *th) {
    PUSH(immediates, nimm, capimm, th->uid);
    return R_PROMISE;
}
void rt_resolve_immediates(void) {
    for (int32_t i = 0; i < nimm; i++) resume_thread_uid(immediates[i]);
    nimm = 0;
}

/* Pending answers for ask-and-wait are resolved at the end of the frame
 * (the oracle answers in a microtask after the step). */
static int64_t *answer_waiters;
static int32_t nanswer, capanswer;
void rt_add_answer_waiter(Thread *th) { PUSH(answer_waiters, nanswer, capanswer, th->uid); }
void rt_ask_resolve_pending(void (*deliver)(void));

/* ------------------------------------------------------------------------ */
/* Control primitives                                                        */

/* control_wait. `first` is true on the first execution (stackTimerNeedsInit). */
int32_t rt_wait(Thread *th, Frame *fr, double secs, int32_t first) {
    (void)th;
    if (first || !fr->timer_on) {
        double dur = 1000 * secs;
        if (!(dur > 0)) dur = 0;
        fr->timer_on = 1;
        fr->timer_start = now_ms;
        fr->timer_dur = dur;
        rt_request_redraw();
        return R_YIELD;
    }
    if (now_ms - fr->timer_start < fr->timer_dur) return R_YIELD;
    fr->timer_on = 0;
    return -1;
}

int32_t rt_wait_until(Thread *th, Frame *fr, bool cond) {
    (void)th; (void)fr;
    return cond ? -1 : R_YIELD;
}

bool rt_warp_timer_expired(Thread *th) {
    if (rt_deterministic) return false;
    if (!th->warp_timer_on) { th->warp_timer_on = 1; th->warp_start = real_ms(); return false; }
    return real_ms() - th->warp_start > 500;
}

void rt_broadcast(Str *name) { rt_start_hats(HAT_BROADCAST, name, NULL, NULL, NULL); }

/* The waiting half of broadcast-and-wait / switch-backdrop-and-wait:
 * returns -1 once no started thread is still in runtime.threads. */
int32_t rt_wait_started(Frame *fr) {
    bool waiting = false;
    for (int32_t i = 0; i < fr->nstarted; i++)
        if (thread_in_list(fr->started[i])) { waiting = true; break; }
    if (!waiting) {
        fr->started_init = 0;
        free(fr->started);
        fr->started = NULL;
        fr->nstarted = 0;
        return -1;
    }
    for (int32_t i = 0; i < fr->nstarted; i++)
        if (!is_waiting_thread(fr->started[i])) return R_YIELD;
    return R_YIELD_TICK;
}

/* event_broadcastandwait. Returns -1 to continue with the next block. */
int32_t rt_broadcast_and_wait(Thread *th, Frame *fr, Str *name, int32_t first) {
    (void)th;
    if (first || !fr->started_init) {
        free(fr->started);
        fr->started = NULL;
        fr->nstarted = 0;
        rt_start_hats(HAT_BROADCAST, name, NULL, &fr->started, &fr->nstarted);
        fr->started_init = 1;
        if (fr->nstarted == 0) { fr->started_init = 0; return -1; }
    }
    return rt_wait_started(fr);
}

static double counter;
double rt_get_counter(void) { return counter; }
void rt_incr_counter(void) { counter++; }
void rt_clear_counter(void) { counter = 0; }

/* ------------------------------------------------------------------------ */
/* Sequencer                                                                 */

static void step_thread(Thread *th) {
    for (;;) {
        Frame *f = th->top;
        if (!f) { th->status = TS_DONE; return; }
        int r = f->fn(th, f);
        switch (r) {
        case R_CALL:
            continue;
        case R_CALL_YIELD:
            return;
        case R_RETURN:
            if (th->status == TS_DONE) return; /* killed while running */
            pop_frame(th);
            if (!th->top) { th->status = TS_DONE; return; }
            continue;
        case R_YIELD:
            if (th->status != TS_DONE) th->status = TS_RUNNING;
            return;
        case R_YIELD_TICK:
            if (th->status != TS_DONE) th->status = TS_YIELD_TICK;
            return;
        case R_PROMISE:
            if (th->status != TS_DONE) th->status = TS_PROMISE_WAIT;
            return;
        case R_STOP:
        default:
            th->status = TS_DONE;
            return;
        }
    }
}

static int64_t work_ticks;

/* sequencer.stepThreads */
static double current_msecs;

static void step_threads(void) {
    current_msecs = now_ms; /* runtime.updateCurrentMSecs() */
    double work_time = 0.75 * (1000.0 / 30.0);
    double t0 = real_ms();
    int32_t tick = 0;
    int32_t num_active = 1 << 30;
    int32_t ran_first_tick = 0;
    for (;;) {
        if (!(threads->n > 0 && num_active > 0)) break;
        if (rt_deterministic) { if (!(tick < ticks_per_frame)) break; }
        else if (!(real_ms() - t0 < work_time)) break;
        if (!(turbo || !redraw_requested)) break;
        tick++;
        work_ticks++;
        num_active = 0;
        int32_t stopped = 0;
        ThreadList *list = threads; /* stopAll may swap runtime.threads mid-pass */
        for (int32_t i = 0; i < list->n; i++) {
            Thread *th = list->v[i];
            active_thread = th;
            if (th->top == NULL || th->status == TS_DONE) { stopped = 1; continue; }
            if (th->status == TS_YIELD_TICK && !ran_first_tick) th->status = TS_RUNNING;
            if (th->status == TS_RUNNING || th->status == TS_YIELD) {
                th->warp_timer_on = 0;
                step_thread(th);
                th->warp_timer_on = 0;
            }
            /* JS inspects the thread object it just stepped, even if a restart
             * replaced it in the list while it ran (it is then "detached"). */
            if (th->status == TS_RUNNING) num_active++;
            if (th->top == NULL || th->status == TS_DONE) stopped = 1;
            if (th->detached) { active_thread = NULL; free_thread(th); }
        }
        active_thread = NULL;
        ran_first_tick = 1;
        /* free thread lists retired by stop all during this pass */
        while (nstale > 0) {
            ThreadList *old = stale_lists[--nstale];
            for (int32_t i = 0; i < old->n; i++) {
                Thread *th = old->v[i];
                bool alive = false;
                for (int32_t j = 0; j < threads->n; j++) if (threads->v[j] == th) { alive = true; break; }
                if (!alive) free_thread(th);
            }
            free(old->v);
            free(old);
            stopped = 1;
        }
        if (stopped) {
            int32_t k = 0;
            for (int32_t i = 0; i < threads->n; i++) {
                Thread *th = threads->v[i];
                if (th->top != NULL && th->status != TS_DONE) threads->v[k++] = th;
                else { th->in_list = 0; free_thread(th); }
            }
            threads->n = k;
        }
    }
}

/* ------------------------------------------------------------------------ */
/* Project lifecycle                                                         */

static int layer_cmp(const void *a, const void *b) {
    const Target *x = *(Target *const *)a, *y = *(Target *const *)b;
    return x->sprite->layer - y->sprite->layer;
}

void rt_setup(void) {
    threads = tl_new();
    rt_program_init();
    for (int32_t i = 0; i < nsprites; i++) {
        Target *t = new_target(sprites[i]);
        PUSH(targets, ntargets, captargets, t);
        if (t->is_stage) {
            rt_stage = t;
            rt_globals = t->vars;
            rt_global_lists = t->lists;
        }
    }
    /* executableTargets in layer order (stage first) */
    for (int32_t i = 0; i < ntargets; i++) PUSH(exec_t, nexec, capexec, targets[i]);
    qsort(exec_t, (size_t)nexec, sizeof(Target *), layer_cmp);
    /* runtime.targets follows the same (sorted) load order */
    memcpy(targets, exec_t, sizeof(Target *) * (size_t)ntargets);
}

/* The project timer reads runtime.currentMSecs, which scratch-vm refreshes at
 * the start of stepThreads -- after edge-activated hats were evaluated, so
 * `when timer >` predicates see the previous frame's clock. */
static double timer_start_ms;
double rt_timer(void) { return (current_msecs - timer_start_ms) / 1000.0; }
void rt_reset_timer(void) { timer_start_ms = current_msecs; }

void rt_green_flag(void) {
    rt_stop_all(NULL);
    /* nothing was running: drop the stale list immediately */
    while (nstale > 0) { ThreadList *old = stale_lists[--nstale]; free(old->v); free(old); }
    rt_stop_flag = 0;
    current_msecs = now_ms;
    timer_start_ms = current_msecs;
    for (int32_t i = 0; i < ntargets; i++) {
        Target *t = targets[i];
        memset(t->edge_has, 0, (size_t)t->sprite->nscripts + 1);
        rt_clear_effects(t);
    }
    rt_start_hats(HAT_FLAG, NULL, NULL, NULL, NULL);
}

int32_t rt_has_edge_hats(void) {
    for (int32_t i = 0; i < nscripts; i++)
        if (all_scripts[i]->hat == HAT_TIMER_GT || all_scripts[i]->hat == HAT_TOUCHING) return 1;
    return 0;
}

/* runtime._step */
void rt_step(void) {
    int32_t k = 0;
    for (int32_t i = 0; i < threads->n; i++) {
        Thread *th = threads->v[i];
        if (!th->killed) threads->v[k++] = th;
        else { th->in_list = 0; free_thread(th); }
    }
    threads->n = k;
    rt_start_hats(HAT_TOUCHING, NULL, NULL, NULL, NULL);
    rt_start_hats(HAT_TIMER_GT, NULL, NULL, NULL, NULL);
    redraw_requested = 0;
    rt_stop_flag = 0;
    step_threads();
}

int32_t rt_idle(void) { return threads->n == 0 && ntimers == 0 && nanswer == 0 && nimm == 0; }

void rt_frame_begin(void) { fire_due_timers(); }

void rt_frame_end(void) {
    rt_resolve_immediates();
    now_ms += 1000.0 / 30.0;
}

void rt_set_ticks(int32_t t) { ticks_per_frame = t; }
void rt_set_turbo(int32_t t) { turbo = t; }
void rt_set_deterministic(int32_t d) {
    rt_deterministic = d;
    if (!d) now_ms = real_ms();
    real_start_ms = real_ms();
}
void rt_sync_clock(void) { if (!rt_deterministic) now_ms = real_ms(); }
int32_t rt_redraw_requested(void) { return redraw_requested; }
int64_t rt_work_ticks(void) { return work_ticks; }

Target **rt_all_targets(int32_t *n) { *n = ntargets; return targets; }

/* ask-and-wait support: resolve all pending waiters with the next answers */
int64_t *rt_answer_waiters(int32_t *n) { *n = nanswer; return answer_waiters; }
void rt_answer_waiters_clear(void) { nanswer = 0; }
void rt_resume_uid(int64_t uid) { resume_thread_uid(uid); }
