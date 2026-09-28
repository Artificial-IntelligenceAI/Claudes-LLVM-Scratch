/* SPDX-License-Identifier: AGPL-3.0-only */
/*
 * Values, strings, and the Scratch cast rules (scratch-vm src/util/cast.js),
 * plus the JavaScript number<->string conversions they depend on.
 */
#include "rt.h"
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <math.h>

/* ------------------------------------------------------------------------ */
/* Strings                                                                   */

static int is_ascii_bytes(const char *s, int32_t len) {
    for (int32_t i = 0; i < len; i++)
        if ((unsigned char)s[i] >= 0x80) return 0;
    return 1;
}

Str *rt_str_new(const char *s, int32_t len) {
    Str *r = (Str *)malloc(sizeof(Str) + (size_t)len + 1);
    if (!r) rt_panic("out of memory");
    r->rc = 1;
    r->len = len;
    r->u16len = -1;
    r->ascii = 2;
    r->numstate = 0;
    r->pad = 0;
    r->num = 0;
    if (len) memcpy(r->data, s, (size_t)len);
    r->data[len] = 0;
    return r;
}

Str *rt_str_cstr(const char *s) { return rt_str_new(s, (int32_t)strlen(s)); }

void rt_str_free(Str *s) { free(s); }

static int str_ascii(Str *s) {
    if (s->ascii == 2) s->ascii = (uint8_t)is_ascii_bytes(s->data, s->len);
    return s->ascii;
}

/* Decode one UTF-8 code point. Invalid bytes decode as U+FFFD, consuming 1 byte. */
static uint32_t utf8_next(const unsigned char *s, int32_t len, int32_t *i) {
    unsigned char c = s[*i];
    if (c < 0x80) { (*i)++; return c; }
    int n;
    uint32_t cp;
    if ((c & 0xE0) == 0xC0) { n = 1; cp = c & 0x1F; }
    else if ((c & 0xF0) == 0xE0) { n = 2; cp = c & 0x0F; }
    else if ((c & 0xF8) == 0xF0) { n = 3; cp = c & 0x07; }
    else { (*i)++; return 0xFFFD; }
    if (*i + n >= len) { (*i)++; return 0xFFFD; }
    for (int k = 1; k <= n; k++) {
        unsigned char cc = s[*i + k];
        if ((cc & 0xC0) != 0x80) { (*i)++; return 0xFFFD; }
        cp = (cp << 6) | (cc & 0x3F);
    }
    *i += n + 1;
    return cp;
}

static int utf8_put(char *out, uint32_t cp) {
    if (cp < 0x80) { out[0] = (char)cp; return 1; }
    if (cp < 0x800) { out[0] = (char)(0xC0 | (cp >> 6)); out[1] = (char)(0x80 | (cp & 0x3F)); return 2; }
    if (cp < 0x10000) {
        out[0] = (char)(0xE0 | (cp >> 12)); out[1] = (char)(0x80 | ((cp >> 6) & 0x3F));
        out[2] = (char)(0x80 | (cp & 0x3F)); return 3;
    }
    out[0] = (char)(0xF0 | (cp >> 18)); out[1] = (char)(0x80 | ((cp >> 12) & 0x3F));
    out[2] = (char)(0x80 | ((cp >> 6) & 0x3F)); out[3] = (char)(0x80 | (cp & 0x3F)); return 4;
}

/* JS String .length: UTF-16 code units. */
static int32_t str_u16len(Str *s) {
    if (s->u16len >= 0) return s->u16len;
    if (str_ascii(s)) { s->u16len = s->len; return s->len; }
    int32_t i = 0, n = 0;
    const unsigned char *p = (const unsigned char *)s->data;
    while (i < s->len) {
        uint32_t cp = utf8_next(p, s->len, &i);
        n += cp >= 0x10000 ? 2 : 1;
    }
    s->u16len = n;
    return n;
}

/* ------------------------------------------------------------------------ */
/* JS whitespace (WhiteSpace + LineTerminator, as used by trim/StringToNumber) */

static int js_is_space(uint32_t c) {
    switch (c) {
    case 0x09: case 0x0A: case 0x0B: case 0x0C: case 0x0D: case 0x20: case 0xA0:
    case 0x1680: case 0x2028: case 0x2029: case 0x202F: case 0x205F: case 0x3000: case 0xFEFF:
        return 1;
    }
    return c >= 0x2000 && c <= 0x200A;
}

/* Trim JS whitespace. Returns [start,end) byte offsets. */
static void js_trim(const char *s, int32_t len, int32_t *start, int32_t *end) {
    const unsigned char *p = (const unsigned char *)s;
    int32_t i = 0;
    while (i < len) {
        int32_t j = i;
        uint32_t c = utf8_next(p, len, &j);
        if (!js_is_space(c)) break;
        i = j;
    }
    int32_t e = len;
    /* scan backwards: find last non-space by forward scan from i */
    int32_t last_end = i;
    int32_t k = i;
    while (k < len) {
        int32_t j = k;
        uint32_t c = utf8_next(p, len, &j);
        if (!js_is_space(c)) last_end = j;
        k = j;
    }
    e = last_end;
    *start = i;
    *end = e < i ? i : e;
}

/* ------------------------------------------------------------------------ */
/* JS Number(string)                                                         */

static double parse_radix(const char *s, int32_t n, int radix) {
    if (n == 0) return NAN;
    double v = 0;
    for (int32_t i = 0; i < n; i++) {
        int d;
        char c = s[i];
        if (c >= '0' && c <= '9') d = c - '0';
        else if (c >= 'a' && c <= 'z') d = c - 'a' + 10;
        else if (c >= 'A' && c <= 'Z') d = c - 'A' + 10;
        else return NAN;
        if (d >= radix) return NAN;
        v = v * radix + d;
    }
    if (radix == 16 && n > 13) {
        /* large hex: let strtod round correctly */
        char buf[512];
        if (n + 3 < (int32_t)sizeof buf) {
            buf[0] = '0'; buf[1] = 'x';
            memcpy(buf + 2, s, (size_t)n);
            buf[n + 2] = 0;
            return strtod(buf, NULL);
        }
    }
    return v;
}

static double js_string_to_number(const char *str, int32_t len) {
    int32_t a, b;
    js_trim(str, len, &a, &b);
    const char *s = str + a;
    int32_t n = b - a;
    if (n == 0) return 0.0;
    /* Infinity */
    if (n == 8 && memcmp(s, "Infinity", 8) == 0) return INFINITY;
    if (n == 9 && memcmp(s, "+Infinity", 9) == 0) return INFINITY;
    if (n == 9 && memcmp(s, "-Infinity", 9) == 0) return -INFINITY;
    if (n > 2 && s[0] == '0') {
        char p = s[1];
        if (p == 'x' || p == 'X') return parse_radix(s + 2, n - 2, 16);
        if (p == 'o' || p == 'O') return parse_radix(s + 2, n - 2, 8);
        if (p == 'b' || p == 'B') return parse_radix(s + 2, n - 2, 2);
    }
    /* StrDecimalLiteral */
    int32_t i = 0;
    if (s[i] == '+' || s[i] == '-') i++;
    int32_t int_digits = 0, frac_digits = 0;
    while (i < n && s[i] >= '0' && s[i] <= '9') { i++; int_digits++; }
    if (i < n && s[i] == '.') {
        i++;
        while (i < n && s[i] >= '0' && s[i] <= '9') { i++; frac_digits++; }
    }
    if (int_digits + frac_digits == 0) return NAN;
    if (i < n && (s[i] == 'e' || s[i] == 'E')) {
        i++;
        if (i < n && (s[i] == '+' || s[i] == '-')) i++;
        int32_t exp_digits = 0;
        while (i < n && s[i] >= '0' && s[i] <= '9') { i++; exp_digits++; }
        if (exp_digits == 0) return NAN;
    }
    if (i != n) return NAN;
    char stackbuf[128];
    char *buf = n < (int32_t)sizeof stackbuf ? stackbuf : (char *)malloc((size_t)n + 1);
    memcpy(buf, s, (size_t)n);
    buf[n] = 0;
    double v = strtod(buf, NULL);
    if (buf != stackbuf) free(buf);
    return v;
}

double rt_str_tonum_slow(Str *s) {
    double v = js_string_to_number(s->data, s->len);
    s->num = v;
    s->numstate = 1;
    return v;
}

/* ------------------------------------------------------------------------ */
/* JS Number::toString(10)                                                   */

/* Shortest digit string that round-trips, per ECMA-262 Number::toString.
 * digits receives k digits (no dot), returns k; *n receives the exponent n
 * such that value = 0.d1d2...dk * 10^n. v must be finite and > 0. */
static int shortest(double v, char *digits, int *n) {
    char tmp[40];
    for (int p = 1; p <= 17; p++) {
        snprintf(tmp, sizeof tmp, "%.*e", p - 1, v);
        /* parse d.ddde±XX */
        char mant[24];
        int k = 0;
        const char *q = tmp;
        for (; *q && *q != 'e'; q++)
            if (*q >= '0' && *q <= '9') mant[k++] = *q;
        int e = atoi(q + 1);
        if (strtod(tmp, NULL) == v) {
            memcpy(digits, mant, (size_t)k);
            *n = e + 1;
            return k;
        }
        /* Asymmetric rounding interval at powers of two: a neighbour of the
         * correctly rounded candidate may round-trip even though it does not. */
        for (int dir = -1; dir <= 1; dir += 2) {
            char m2[24];
            memcpy(m2, mant, (size_t)k);
            int kk = k, ee = e, carry = dir;
            for (int i = kk - 1; i >= 0 && carry; i--) {
                int d = m2[i] - '0' + carry;
                if (d < 0) { d = 9; carry = -1; }
                else if (d > 9) { d = 0; carry = 1; }
                else carry = 0;
                m2[i] = (char)('0' + d);
            }
            if (carry == 1) { memmove(m2 + 1, m2, (size_t)kk); m2[0] = '1'; ee++; /* keep kk digits */ }
            if (m2[0] == '0') continue;
            char cand[48];
            int L = 0;
            cand[L++] = m2[0];
            if (kk > 1) { cand[L++] = '.'; memcpy(cand + L, m2 + 1, (size_t)kk - 1); L += kk - 1; }
            L += snprintf(cand + L, sizeof cand - (size_t)L, "e%d", ee);
            if (strtod(cand, NULL) == v) {
                memcpy(digits, m2, (size_t)kk);
                *n = ee + 1;
                return kk;
            }
        }
    }
    /* unreachable for finite doubles */
    snprintf(tmp, sizeof tmp, "%.16e", v);
    int k = 0;
    const char *q = tmp;
    for (; *q && *q != 'e'; q++)
        if (*q >= '0' && *q <= '9') digits[k++] = *q;
    *n = atoi(q + 1) + 1;
    return k;
}

/* Writes the JS string form of d into buf (>= 32 bytes). Returns length. */
int rt_js_num_format(double d, char *buf) {
    if (d != d) { memcpy(buf, "NaN", 4); return 3; }
    if (d == 0) { buf[0] = '0'; buf[1] = 0; return 1; }
    if (isinf(d)) {
        if (d > 0) { memcpy(buf, "Infinity", 9); return 8; }
        memcpy(buf, "-Infinity", 10); return 9;
    }
    int L = 0;
    if (d < 0) { buf[L++] = '-'; d = -d; }
    /* fast path: integers below 2^53 */
    if (d < 9007199254740992.0 && d == (double)(int64_t)d) {
        L += snprintf(buf + L, 32, "%lld", (long long)(int64_t)d);
        return L;
    }
    char s[24];
    int n;
    int k = shortest(d, s, &n);
    if (k <= n && n <= 21) {
        memcpy(buf + L, s, (size_t)k); L += k;
        for (int i = 0; i < n - k; i++) buf[L++] = '0';
    } else if (0 < n && n <= 21) {
        memcpy(buf + L, s, (size_t)n); L += n;
        buf[L++] = '.';
        memcpy(buf + L, s + n, (size_t)(k - n)); L += k - n;
    } else if (-6 < n && n <= 0) {
        buf[L++] = '0'; buf[L++] = '.';
        for (int i = 0; i < -n; i++) buf[L++] = '0';
        memcpy(buf + L, s, (size_t)k); L += k;
    } else {
        buf[L++] = s[0];
        if (k > 1) { buf[L++] = '.'; memcpy(buf + L, s + 1, (size_t)(k - 1)); L += k - 1; }
        buf[L++] = 'e';
        int e = n - 1;
        buf[L++] = e >= 0 ? '+' : '-';
        L += snprintf(buf + L, 8, "%d", e >= 0 ? e : -e);
    }
    buf[L] = 0;
    return L;
}

/* ------------------------------------------------------------------------ */
/* Casts                                                                     */

/* Immortal "true"/"false" strings (flexible array member initialised at startup). */
static Str *str_true, *str_false, *str_empty;

static Str *immortal(const char *s) {
    Str *r = rt_str_cstr(s);
    r->rc = -1;
    return r;
}

__attribute__((constructor)) static void init_consts(void) {
    str_true = immortal("true");
    str_false = immortal("false");
    str_empty = immortal("");
}

Str *rt_empty_str(void) { return str_empty; }

__attribute__((always_inline)) double rt_tonum(Value v) {
    if (v.tag == T_NUM) {
        double d = bits2d(v.bits);
        return d != d ? 0.0 : d;
    }
    if (v.tag == T_BOOL) return (double)v.bits;
    double d = rt_str_tonum(v_as_str(v));
    return d != d ? 0.0 : d;
}

double rt_js_number(Value v) {
    if (v.tag == T_NUM) return bits2d(v.bits);
    if (v.tag == T_BOOL) return (double)v.bits;
    return rt_str_tonum(v_as_str(v));
}

bool rt_str_tobool(Str *s) {
    if (s->len == 0) return false;
    if (s->len == 1 && s->data[0] == '0') return false;
    if (s->len == 5) {
        const char *p = s->data;
        if ((p[0] | 0x20) == 'f' && (p[1] | 0x20) == 'a' && (p[2] | 0x20) == 'l' &&
            (p[3] | 0x20) == 's' && (p[4] | 0x20) == 'e')
            return false;
    }
    /* non-ASCII strings that lowercase to "false" do not exist (5 bytes, ASCII only) */
    return true;
}

bool rt_tobool(Value v) {
    if (v.tag == T_BOOL) return v.bits != 0;
    if (v.tag == T_NUM) {
        double d = bits2d(v.bits);
        return d == d && d != 0;
    }
    return rt_str_tobool(v_as_str(v));
}

Str *rt_num_tostr(double d) {
    char buf[40];
    int L = rt_js_num_format(d, buf);
    return rt_str_new(buf, L);
}

Str *rt_bool_tostr(bool b) { return b ? str_true : str_false; }

Str *rt_tostr(Value v) {
    if (v.tag == T_STR) {
        Str *s = v_as_str(v);
        rt_retain_str(s);
        return s;
    }
    if (v.tag == T_BOOL) return v.bits ? str_true : str_false;
    return rt_num_tostr(bits2d(v.bits));
}

bool rt_is_whitespace(Value v) {
    if (v.tag != T_STR) return false;
    Str *s = v_as_str(v);
    int32_t a, b;
    js_trim(s->data, s->len, &a, &b);
    return b == a;
}

/* ------------------------------------------------------------------------ */
/* toLowerCase (ASCII, Latin-1, Latin Extended-A, Greek, Cyrillic, Armenian,  */
/* fullwidth Latin).                                                           */

static uint32_t js_lower(uint32_t c) {
    if (c < 0x80) return (c >= 'A' && c <= 'Z') ? c + 32 : c;
    if (c >= 0xC0 && c <= 0xDE && c != 0xD7) return c + 32;
    if (c >= 0x100 && c <= 0x17F) {
        if ((c >= 0x100 && c <= 0x12F) || (c >= 0x132 && c <= 0x137) || (c >= 0x14A && c <= 0x177))
            return (c & 1) ? c : c + 1;
        if ((c >= 0x139 && c <= 0x148) || (c >= 0x179 && c <= 0x17E))
            return (c & 1) ? c + 1 : c;
        if (c == 0x178) return 0xFF;
        return c;
    }
    if (c >= 0x391 && c <= 0x3A9 && c != 0x3A2) return c + 32;
    if (c == 0x386) return 0x3AC;
    if (c >= 0x388 && c <= 0x38A) return c + 37;
    if (c == 0x38C) return 0x3CC;
    if (c == 0x38E || c == 0x38F) return c + 63;
    if (c >= 0x410 && c <= 0x42F) return c + 32;
    if (c >= 0x400 && c <= 0x40F) return c + 80;
    if ((c >= 0x460 && c <= 0x481) || (c >= 0x48A && c <= 0x4BF) || (c >= 0x4D0 && c <= 0x52F))
        return (c & 1) ? c : c + 1;
    if (c >= 0x4C1 && c <= 0x4CE) return (c & 1) ? c + 1 : c;
    if (c == 0x4C0) return 0x4CF;
    if (c >= 0x531 && c <= 0x556) return c + 48;
    if (c >= 0xFF21 && c <= 0xFF3A) return c + 32;
    return c;
}

Str *rt_str_lower(Str *s) {
    if (str_ascii(s)) {
        int32_t i = 0;
        while (i < s->len && !(s->data[i] >= 'A' && s->data[i] <= 'Z')) i++;
        if (i == s->len) { rt_retain_str(s); return s; }
        Str *r = rt_str_new(s->data, s->len);
        for (; i < s->len; i++) {
            char c = r->data[i];
            if (c >= 'A' && c <= 'Z') r->data[i] = (char)(c + 32);
        }
        r->ascii = 1;
        return r;
    }
    char *out = (char *)malloc((size_t)s->len * 2 + 8);
    int32_t L = 0, i = 0;
    const unsigned char *p = (const unsigned char *)s->data;
    while (i < s->len) {
        uint32_t c = utf8_next(p, s->len, &i);
        if (c == 0x130) { out[L++] = 'i'; L += utf8_put(out + L, 0x307); continue; }
        L += utf8_put(out + L, js_lower(c));
    }
    Str *r = rt_str_new(out, L);
    free(out);
    return r;
}

/* ------------------------------------------------------------------------ */
/* Cast.compare                                                              */

static int str_cmp_lower(Value a, Value b) {
    Str *sa = rt_tostr(a), *sb = rt_tostr(b);
    Str *la = rt_str_lower(sa), *lb = rt_str_lower(sb);
    rt_release_str(sa);
    rt_release_str(sb);
    int32_t n = la->len < lb->len ? la->len : lb->len;
    int c = memcmp(la->data, lb->data, (size_t)n);
    int r;
    if (c != 0) r = c < 0 ? -1 : 1;
    else r = la->len < lb->len ? -1 : (la->len > lb->len ? 1 : 0);
    rt_release_str(la);
    rt_release_str(lb);
    return r;
}

int rt_compare(Value a, Value b) {
    double n1 = rt_js_number(a);
    double n2 = rt_js_number(b);
    /* bug-compatible: only one side is adjusted (else-if) */
    if (n1 == 0 && rt_is_whitespace(a)) n1 = NAN;
    else if (n2 == 0 && rt_is_whitespace(b)) n2 = NAN;
    if (n1 != n1 || n2 != n2) return str_cmp_lower(a, b);
    if (n1 == n2) return 0; /* also covers +-Infinity pairs */
    return n1 < n2 ? -1 : 1;
}

int rt_compare_num_str(double a, Str *b) { return rt_compare(v_num(a), v_str(b)); }
int rt_compare_str_str(Str *a, Str *b) { return rt_compare(v_str(a), v_str(b)); }

/* ------------------------------------------------------------------------ */
/* Operators                                                                 */

Str *rt_join(Str *a, Str *b) {
    if (a->len == 0) { rt_retain_str(b); return b; }
    if (b->len == 0) { rt_retain_str(a); return a; }
    Str *r = (Str *)malloc(sizeof(Str) + (size_t)a->len + (size_t)b->len + 1);
    if (!r) rt_panic("out of memory");
    r->rc = 1;
    r->len = a->len + b->len;
    r->u16len = (a->u16len >= 0 && b->u16len >= 0) ? a->u16len + b->u16len : -1;
    r->ascii = (a->ascii == 1 && b->ascii == 1) ? 1 : ((a->ascii == 0 || b->ascii == 0) ? 0 : 2);
    r->numstate = 0;
    r->pad = 0;
    r->num = 0;
    memcpy(r->data, a->data, (size_t)a->len);
    memcpy(r->data + a->len, b->data, (size_t)b->len);
    r->data[r->len] = 0;
    return r;
}

static Str *single_char_cache[128];

static Str *ascii_char(char c) {
    unsigned char u = (unsigned char)c;
    if (!single_char_cache[u]) {
        Str *s = rt_str_new(&c, 1);
        s->rc = -1;
        s->ascii = 1;
        s->u16len = 1;
        single_char_cache[u] = s;
    }
    return single_char_cache[u];
}

Str *rt_letter_of(double letter, Str *s) {
    double index = letter - 1;
    int32_t len = str_u16len(s);
    if (index < 0 || index >= len || index != index) return str_empty;
    int32_t idx = (int32_t)index; /* charAt truncates */
    if (str_ascii(s)) return ascii_char(s->data[idx]);
    const unsigned char *p = (const unsigned char *)s->data;
    int32_t i = 0, u = 0;
    while (i < s->len) {
        int32_t start = i;
        uint32_t cp = utf8_next(p, s->len, &i);
        int w = cp >= 0x10000 ? 2 : 1;
        if (idx < u + w) {
            if (w == 2) { char buf[4]; int L = utf8_put(buf, 0xFFFD); return rt_str_new(buf, L); }
            return rt_str_new(s->data + start, i - start);
        }
        u += w;
    }
    return str_empty;
}

double rt_length(Str *s) { return (double)str_u16len(s); }

bool rt_contains(Str *hay, Str *needle) {
    Str *h = rt_str_lower(hay), *n = rt_str_lower(needle);
    bool r;
    if (n->len == 0) r = true;
    else if (n->len > h->len) r = false;
    else r = memmem(h->data, (size_t)h->len, n->data, (size_t)n->len) != NULL;
    rt_release_str(h);
    rt_release_str(n);
    return r;
}

double rt_mod(double n, double m) {
    double r = fmod(n, m);
    if (r / m < 0) r += m;
    return r;
}

/* JS Math.round */
double rt_round(double x) {
    if (x != x || isinf(x) || x == 0) return x;
    if (x > 0 && x < 0.5) return 0.0;
    if (x < 0 && x >= -0.5) return -0.0;
    if (fabs(x) >= 4503599627370496.0) return x;
    double r = floor(x);
    if (x - r >= 0.5) r += 1;
    return r;
}

/* V8 12.4's fdlibm port (runtime/fdlibm), so Math.* results match bit-for-bit. */
double rt_fd_sin(double), rt_fd_cos(double), rt_fd_tan(double), rt_fd_asin(double),
    rt_fd_acos(double), rt_fd_atan(double), rt_fd_exp(double), rt_fd_log(double),
    rt_fd_pow(double, double), rt_fd_atan2(double, double);

/* parseFloat(x.toFixed(10)) */
static double to_fixed10_signed(double x) {
    char buf[64];
    snprintf(buf, sizeof buf, "%.10f", x);
    return strtod(buf, NULL);
}

double rt_mathop(int op, double n) {
    switch (op) {
    case 0: return fabs(n);
    case 1: return floor(n);
    case 2: return ceil(n);
    case 3: return sqrt(n);
    case 4: return to_fixed10_signed(rt_fd_sin((M_PI * n) / 180));
    case 5: return to_fixed10_signed(rt_fd_cos((M_PI * n) / 180));
    case 6: {
        double a = fmod(n, 360);
        if (a == -270 || a == 90) return INFINITY;
        if (a == -90 || a == 270) return -INFINITY;
        return to_fixed10_signed(rt_fd_tan((M_PI * a) / 180));
    }
    case 7: return (rt_fd_asin(n) * 180) / M_PI;
    case 8: return (rt_fd_acos(n) * 180) / M_PI;
    case 9: return (rt_fd_atan(n) * 180) / M_PI;
    case 10: return rt_fd_log(n);
    case 11: return rt_fd_log(n) / M_LN10;
    case 12: return rt_fd_exp(n);
    case 13: return rt_fd_pow(10, n);
    }
    return 0;
}

/* ------------------------------------------------------------------------ */
/* Random: seeded mulberry32 (the oracle patches Math.random to the same).   */

static uint32_t rng_state = 1;

void rt_seed_random(uint32_t seed) { rng_state = seed; }

double rt_js_random(void) {
    rng_state += 0x6D2B79F5u;
    uint32_t a = rng_state;
    uint32_t t = (a ^ (a >> 15)) * (1u | a);
    t = (t + ((t ^ (t >> 7)) * (61u | t))) ^ t;
    return (double)(t ^ (t >> 14)) / 4294967296.0;
}

/* Cast.isInt on the original argument value */
static bool is_int_value(Value v) {
    if (v.tag == T_NUM) {
        double d = bits2d(v.bits);
        if (d != d) return true;
        /* val === parseInt(val, 10): parseInt of String(val). */
        if (isinf(d)) return false;
        char buf[40];
        rt_js_num_format(d, buf);
        /* parseInt parses leading integer digits (with sign) */
        const char *p = buf;
        int neg = 0;
        if (*p == '-') { neg = 1; p++; }
        double r = 0;
        int any = 0;
        while (*p >= '0' && *p <= '9') { r = r * 10 + (*p - '0'); p++; any = 1; }
        if (!any) return false;
        if (neg) r = -r;
        return r == d;
    }
    if (v.tag == T_BOOL) return true;
    Str *s = v_as_str(v);
    return memchr(s->data, '.', (size_t)s->len) == NULL;
}

double rt_random(Value from, Value to) {
    double nFrom = rt_tonum(from), nTo = rt_tonum(to);
    double low = nFrom <= nTo ? nFrom : nTo;
    double high = nFrom <= nTo ? nTo : nFrom;
    if (low == high) return low;
    if (is_int_value(from) && is_int_value(to))
        return low + floor(rt_js_random() * ((high + 1) - low));
    return (rt_js_random() * (high - low)) + low;
}
