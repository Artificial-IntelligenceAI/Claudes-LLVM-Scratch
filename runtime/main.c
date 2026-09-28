/* SPDX-License-Identifier: AGPL-3.0-only */
/*
 * Entry point: frame loop, trace output (the same JSON-lines format as the
 * scratch-vm oracle in ref/run.js), and the end-of-run state dump.
 *
 * Usage: ./project [--frames N] [--ticks N] [--seed N] [--answers a,b,c]
 *                  [--realtime] [--turbo] [--no-trace] [--render DIR] [--render-every N]
 */
#include "rt.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <time.h>
#include <unistd.h>

void rt_setup(void);
void rt_green_flag(void);
void rt_step(void);
int32_t rt_idle(void);
void rt_frame_begin(void);
void rt_frame_end(void);
void rt_set_ticks(int32_t t);
void rt_set_turbo(int32_t t);
void rt_set_deterministic(int32_t d);
void rt_sync_clock(void);
int32_t rt_has_edge_hats(void);
Target **rt_all_targets(int32_t *n);
void rt_set_answers(const char **a, int32_t n);
void rt_deliver_answer(void);
int32_t rt_questions_pending(void);
int64_t rt_work_ticks(void);
void rt_render_init(const char *dir, int32_t every);
void rt_render_frame(int32_t frame);
void rt_render_final(void);

static FILE *out;

void rt_panic(const char *msg) {
    fprintf(stderr, "scratch runtime panic: %s\n", msg);
    exit(3);
}

/* JSON.stringify for a string */
static void json_str(FILE *f, const char *s, int32_t len) {
    fputc('"', f);
    for (int32_t i = 0; i < len; i++) {
        unsigned char c = (unsigned char)s[i];
        switch (c) {
        case '"': fputs("\\\"", f); break;
        case '\\': fputs("\\\\", f); break;
        case '\b': fputs("\\b", f); break;
        case '\f': fputs("\\f", f); break;
        case '\n': fputs("\\n", f); break;
        case '\r': fputs("\\r", f); break;
        case '\t': fputs("\\t", f); break;
        default:
            if (c < 0x20) fprintf(f, "\\u%04x", c);
            else fputc(c, f);
        }
    }
    fputc('"', f);
}

static void json_s(FILE *f, Str *s) { json_str(f, s->data, s->len); }

/* R(v) = Math.round(v * 1e6) / 1e6, printed as JSON number */
static void json_r(FILE *f, double v) {
    char buf[40];
    double r = rt_round(v * 1e6) / 1e6;
    if (r != r || isinf(r)) { fputs("null", f); return; }
    rt_js_num_format(r, buf);
    fputs(buf, f);
}

static void json_value_str(FILE *f, Value v) {
    Str *s = rt_tostr(v);
    json_s(f, s);
    rt_release_str(s);
}

void rt_trace_say(Target *t, int32_t think, Str *text) {
    if (!rt_trace_enabled) return;
    fprintf(out, "{\"t\":\"%s\",\"f\":%d,\"s\":", think ? "think" : "say", rt_frame);
    json_s(out, t->sprite->name);
    fputs(",\"v\":", out);
    json_s(out, text);
    fputs("}\n", out);
}

void rt_trace_ask(Str *q) {
    if (!rt_trace_enabled) return;
    fprintf(out, "{\"t\":\"ask\",\"f\":%d,\"v\":", rt_frame);
    json_s(out, q);
    fputs("}\n", out);
}

void rt_trace_pen(const char *op, Target *t, const double *c, double w, const double *p, int np) {
    (void)t;
    if (!rt_trace_enabled) return;
    fprintf(out, "{\"t\":\"pen\",\"f\":%d,\"op\":\"%s\"", rt_frame, op);
    if (c) {
        fputs(",\"c\":[", out);
        for (int i = 0; i < 4; i++) { if (i) fputc(',', out); json_r(out, c[i]); }
        fputs("],\"w\":", out);
        json_r(out, w);
    }
    if (p) {
        fputs(",\"p\":[", out);
        for (int i = 0; i < np; i++) { if (i) fputc(',', out); json_r(out, p[i]); }
        fputc(']', out);
    }
    fputs("}\n", out);
}

static void dump_end(int32_t frame) {
    if (!rt_trace_enabled) return;
    int32_t n;
    Target **ts = rt_exec_targets(&n);
    fprintf(out, "{\"t\":\"end\",\"f\":%d,\"targets\":[", frame);
    for (int32_t i = n - 1; i >= 0; i--) {
        Target *t = ts[i];
        Sprite *s = t->sprite;
        if (i != n - 1) fputc(',', out);
        fputs("{\"name\":", out);
        json_s(out, s->name);
        fprintf(out, ",\"stage\":%s,\"clone\":%s,\"vars\":{", t->is_stage ? "true" : "false",
                t->is_original ? "false" : "true");
        for (int32_t v = 0; v < s->nvars; v++) {
            if (v) fputc(',', out);
            json_s(out, s->var_names[v]);
            fputc(':', out);
            json_value_str(out, t->vars[v]);
        }
        fputs("},\"lists\":{", out);
        for (int32_t l = 0; l < s->nlists; l++) {
            if (l) fputc(',', out);
            json_s(out, s->list_names[l]);
            fputs(":[", out);
            List *L = &t->lists[l];
            for (int32_t k = 0; k < L->len; k++) {
                if (k) fputc(',', out);
                json_value_str(out, L->items[k]);
            }
            fputc(']', out);
        }
        fprintf(out, "},\"costume\":%d", t->costume);
        if (!t->is_stage) {
            fputs(",\"x\":", out); json_r(out, t->x);
            fputs(",\"y\":", out); json_r(out, t->y);
            fputs(",\"dir\":", out); json_r(out, t->dir);
            fputs(",\"size\":", out); json_r(out, t->size);
            fprintf(out, ",\"visible\":%s", t->visible ? "true" : "false");
        }
        fputc('}', out);
    }
    fputs("]}\n", out);
}

static double now_real_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec * 1000.0 + (double)ts.tv_nsec / 1e6;
}

int main(int argc, char **argv) {
    int32_t max_frames = 30 * 60 * 5;
    int32_t ticks = 64;
    uint32_t seed = 1;
    int32_t realtime = 0, turbo = 0, bench = 0;
    const char *answers_arg = NULL;
    const char *render_dir = NULL;
    int32_t render_every = 1;
    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        if (!strcmp(a, "--frames") && i + 1 < argc) max_frames = atoi(argv[++i]);
        else if (!strcmp(a, "--ticks") && i + 1 < argc) ticks = atoi(argv[++i]);
        else if (!strcmp(a, "--seed") && i + 1 < argc) seed = (uint32_t)strtoul(argv[++i], NULL, 10);
        else if (!strcmp(a, "--answers") && i + 1 < argc) answers_arg = argv[++i];
        else if (!strcmp(a, "--realtime")) realtime = 1;
        else if (!strcmp(a, "--turbo")) turbo = 1;
        else if (!strcmp(a, "--bench")) bench = 1;
        else if (!strcmp(a, "--no-trace")) rt_trace_enabled = 0;
        else if (!strcmp(a, "--render") && i + 1 < argc) render_dir = argv[++i];
        else if (!strcmp(a, "--render-every") && i + 1 < argc) render_every = atoi(argv[++i]);
        else { fprintf(stderr, "unknown argument: %s\n", a); return 2; }
    }
    out = stdout;
    static char outbuf[1 << 16];
    setvbuf(out, outbuf, _IOFBF, sizeof outbuf);

    const char **answers = NULL;
    int32_t nanswers = 0;
    if (answers_arg) {
        char *copy = strdup(answers_arg);
        for (char *tok = strtok(copy, ","); tok; tok = strtok(NULL, ",")) {
            answers = realloc(answers, sizeof(char *) * (size_t)(nanswers + 1));
            answers[nanswers++] = tok;
        }
    }
    rt_set_answers(answers, nanswers);
    rt_seed_random(seed);
    rt_set_ticks(ticks);
    rt_set_turbo(turbo);
    rt_set_deterministic(!realtime);
    rt_setup();
    if (render_dir) rt_render_init(render_dir, render_every);

    double t0 = now_real_ms();
    int32_t has_edge = rt_has_edge_hats();
    rt_green_flag();
    int32_t frame;
    for (frame = 0; frame < max_frames; frame++) {
        double fstart = now_real_ms();
        rt_frame = frame;
        rt_sync_clock();
        rt_deliver_answer();
        rt_frame_begin();
        rt_step();
        if (render_dir) rt_render_frame(frame);
        int32_t idle = rt_idle() && !rt_questions_pending();
        rt_frame_end();
        if (idle && !has_edge) { frame++; break; }
        if (realtime && !bench) {
            double el = now_real_ms() - fstart;
            double wait = 1000.0 / 30.0 - el;
            if (wait > 0) usleep((useconds_t)(wait * 1000));
        }
    }
    double t1 = now_real_ms();
    if (render_dir) rt_render_final();
    dump_end(frame);
    fflush(out);
    if (bench) fprintf(stderr, "frames=%d ticks=%lld wall_ms=%.3f\n", frame, (long long)rt_work_ticks(), t1 - t0);
    return 0;
}
