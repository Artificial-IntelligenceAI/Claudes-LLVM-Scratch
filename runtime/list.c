/*
 * Lists (scratch-vm src/blocks/scratch3_data.js). Items keep their original
 * type: a list of the strings "1","2","3" reports "123" but a list of the
 * numbers 1,2,3 reports "1 2 3".
 */
#include "rt.h"
#include <stdlib.h>
#include <string.h>

#define LIST_ITEM_LIMIT 200000
#define LIST_INVALID -1
#define LIST_ALL -2

Str *rt_empty_str(void);

static void grow(List *l, int32_t need) {
    if (need <= l->cap) return;
    int32_t cap = l->cap ? l->cap : 8;
    while (cap < need) cap *= 2;
    l->items = (Value *)realloc(l->items, sizeof(Value) * (size_t)cap);
    if (!l->items) rt_panic("out of memory");
    l->cap = cap;
}

static bool str_eq_lit(Value v, const char *lit) {
    if (v.tag != T_STR) return false;
    Str *s = v_as_str(v);
    size_t n = strlen(lit);
    return (size_t)s->len == n && memcmp(s->data, lit, n) == 0;
}

/* Cast.toListIndex */
static int32_t to_list_index(Value index, int32_t length, bool accept_all) {
    if (index.tag != T_NUM) {
        if (str_eq_lit(index, "all")) return accept_all ? LIST_ALL : LIST_INVALID;
        if (str_eq_lit(index, "last")) return length > 0 ? length : LIST_INVALID;
        if (str_eq_lit(index, "random") || str_eq_lit(index, "any")) {
            if (length > 0) return 1 + (int32_t)(rt_js_random() * length);
            return LIST_INVALID;
        }
    }
    double d = rt_tonum(index);
    d = __builtin_floor(d);
    if (d < 1 || d > length) return LIST_INVALID;
    return (int32_t)d;
}

void rt_list_add(List *l, Value v) {
    if (l->len >= LIST_ITEM_LIMIT) return;
    grow(l, l->len + 1);
    rt_retain(v);
    l->items[l->len++] = v;
}

void rt_list_delete(List *l, Value index) {
    int32_t i = to_list_index(index, l->len, true);
    if (i == LIST_INVALID) return;
    if (i == LIST_ALL) { rt_list_clear(l); return; }
    rt_release(l->items[i - 1]);
    memmove(&l->items[i - 1], &l->items[i], sizeof(Value) * (size_t)(l->len - i));
    l->len--;
}

void rt_list_clear(List *l) {
    for (int32_t i = 0; i < l->len; i++) rt_release(l->items[i]);
    l->len = 0;
}

void rt_list_insert(List *l, Value index, Value v) {
    int32_t i = to_list_index(index, l->len + 1, false);
    if (i == LIST_INVALID) return;
    if (i > LIST_ITEM_LIMIT) return;
    grow(l, l->len + 1);
    rt_retain(v);
    memmove(&l->items[i], &l->items[i - 1], sizeof(Value) * (size_t)(l->len - (i - 1)));
    l->items[i - 1] = v;
    l->len++;
    if (l->len > LIST_ITEM_LIMIT) {
        rt_release(l->items[l->len - 1]);
        l->len--;
    }
}

void rt_list_replace(List *l, Value index, Value v) {
    int32_t i = to_list_index(index, l->len, false);
    if (i == LIST_INVALID) return;
    rt_retain(v);
    Value old = l->items[i - 1];
    l->items[i - 1] = v;
    rt_release(old);
}

Value rt_list_item(List *l, Value index) {
    int32_t i = to_list_index(index, l->len, false);
    if (i == LIST_INVALID) return v_str(rt_empty_str());
    return l->items[i - 1];
}

/* Fast path when the index is statically a number. */
Value rt_list_item_num(List *l, double d) {
    d = __builtin_floor(d != d ? 0 : d);
    if (d < 1 || d > l->len) return v_str(rt_empty_str());
    return l->items[(int32_t)d - 1];
}

double rt_list_index_of(List *l, Value v) {
    for (int32_t i = 0; i < l->len; i++)
        if (rt_compare(l->items[i], v) == 0) return i + 1;
    return 0;
}

double rt_list_length(List *l) { return l->len; }

bool rt_list_contains(List *l, Value v) {
    /* indexOf (strict equality) first, then Cast.compare; the result is the same
     * as compare alone except that strict equality also matches NaN? No: NaN !== NaN.
     * compare(NaN, NaN) compares "nan" == "nan" -> equal, so compare covers it. */
    for (int32_t i = 0; i < l->len; i++) {
        Value it = l->items[i];
        if (it.tag == v.tag && it.bits == v.bits && !(v.tag == T_NUM && bits2d(v.bits) != bits2d(v.bits)))
            return true;
    }
    for (int32_t i = 0; i < l->len; i++)
        if (rt_compare(l->items[i], v) == 0) return true;
    return false;
}

/* JS String .length of a string item is 1 */
static bool is_single_letter(Value v) {
    if (v.tag != T_STR) return false;
    Str *s = v_as_str(v);
    if (s->len == 1) return true;
    if (s->len < 2 || s->len > 4) return false;
    /* one BMP code point encoded in 2-3 bytes has UTF-16 length 1 */
    unsigned char c = (unsigned char)s->data[0];
    if ((c & 0xE0) == 0xC0) return s->len == 2;
    if ((c & 0xF0) == 0xE0) return s->len == 3;
    return false; /* 4-byte sequences are surrogate pairs: length 2 */
}

Str *rt_list_contents(List *l) {
    bool single = true;
    for (int32_t i = 0; i < l->len; i++)
        if (!is_single_letter(l->items[i])) { single = false; break; }
    if (l->len == 0) return rt_empty_str();
    Str **parts = (Str **)malloc(sizeof(Str *) * (size_t)l->len);
    size_t total = 0;
    for (int32_t i = 0; i < l->len; i++) {
        parts[i] = rt_tostr(l->items[i]);
        total += (size_t)parts[i]->len;
    }
    if (!single) total += (size_t)(l->len - 1);
    Str *r = rt_str_new(NULL, 0);
    r = (Str *)realloc(r, sizeof(Str) + total + 1);
    size_t off = 0;
    for (int32_t i = 0; i < l->len; i++) {
        if (i && !single) r->data[off++] = ' ';
        memcpy(r->data + off, parts[i]->data, (size_t)parts[i]->len);
        off += (size_t)parts[i]->len;
        rt_release_str(parts[i]);
    }
    r->data[off] = 0;
    r->len = (int32_t)off;
    free(parts);
    return r;
}

void rt_list_copy(List *dst, const List *src) {
    dst->len = 0;
    dst->cap = 0;
    dst->items = NULL;
    grow(dst, src->len > 0 ? src->len : 1);
    for (int32_t i = 0; i < src->len; i++) {
        rt_retain(src->items[i]);
        dst->items[i] = src->items[i];
    }
    dst->len = src->len;
}

void rt_list_free_items(List *l) {
    rt_list_clear(l);
    free(l->items);
    l->items = NULL;
    l->cap = 0;
}
