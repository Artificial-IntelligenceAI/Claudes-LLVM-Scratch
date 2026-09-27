/* Rendering hooks. Replaced by a real software renderer later; no-ops for now. */
#include "rt.h"
void rt_render_pen_line(Target *t, const double c[4], double w, double x0, double y0, double x1, double y1) {
    (void)t; (void)c; (void)w; (void)x0; (void)y0; (void)x1; (void)y1;
}
void rt_render_pen_clear(void) {}
void rt_render_stamp(Target *t) { (void)t; }
void rt_render_init(const char *dir, int32_t every) { (void)dir; (void)every; }
void rt_render_frame(int32_t frame) { (void)frame; }
void rt_render_final(void) {}
