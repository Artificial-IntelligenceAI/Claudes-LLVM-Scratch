// C entry points for the fdlibm functions Scratch's math blocks use.
// These reproduce Math.* exactly as V8 12.4 (Node 22, Scratch Desktop era) computes them.
#include "src/base/ieee754.h"
extern "C" {
double rt_fd_sin(double x) { return v8::base::ieee754::sin(x); }
double rt_fd_cos(double x) { return v8::base::ieee754::cos(x); }
double rt_fd_tan(double x) { return v8::base::ieee754::tan(x); }
double rt_fd_asin(double x) { return v8::base::ieee754::asin(x); }
double rt_fd_acos(double x) { return v8::base::ieee754::acos(x); }
double rt_fd_atan(double x) { return v8::base::ieee754::atan(x); }
double rt_fd_atan2(double y, double x) { return v8::base::ieee754::atan2(y, x); }
double rt_fd_exp(double x) { return v8::base::ieee754::exp(x); }
double rt_fd_log(double x) { return v8::base::ieee754::log(x); }
double rt_fd_pow(double x, double y) { return v8::base::ieee754::pow(x, y); }
}
