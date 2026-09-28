/* SPDX-License-Identifier: AGPL-3.0-only */
/* Prints the struct layout facts the code generator needs, as JSON. */
#include "rt.h"
#include <stdio.h>
#include <stddef.h>

int main(void) {
    printf("{\n");
    printf("  \"Value.size\": %zu,\n", sizeof(Value));
    printf("  \"List.size\": %zu,\n", sizeof(List));
    printf("  \"List.len\": %zu,\n", offsetof(List, len));
    printf("  \"List.items\": %zu,\n", offsetof(List, items));
    printf("  \"Str.data\": %zu,\n", offsetof(Str, data));
    printf("  \"Frame.state\": %zu,\n", offsetof(Frame, state));
    printf("  \"Frame.site_depth\": %zu,\n", offsetof(Frame, site_depth));
    printf("  \"Frame.warp\": %zu,\n", offsetof(Frame, warp));
    printf("  \"Frame.slots\": %zu,\n", offsetof(Frame, slots));
    printf("  \"Thread.target\": %zu,\n", offsetof(Thread, target));
    printf("  \"Thread.status\": %zu,\n", offsetof(Thread, status));
    printf("  \"Thread.resumed\": %zu,\n", offsetof(Thread, resumed));
    printf("  \"Target.vars\": %zu,\n", offsetof(Target, vars));
    printf("  \"Target.lists\": %zu,\n", offsetof(Target, lists));
    printf("  \"Target.x\": %zu,\n", offsetof(Target, x));
    printf("  \"Target.y\": %zu,\n", offsetof(Target, y));
    printf("  \"TS_DONE\": %d\n", TS_DONE);
    printf("}\n");
    return 0;
}
