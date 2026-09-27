// Shim header for building V8's fdlibm port (ieee754.cc, V8 12.4) standalone.
#ifndef FDLIBM_SHIM_IEEE754_H
#define FDLIBM_SHIM_IEEE754_H
namespace v8 {
namespace base {
namespace ieee754 {
double acos(double x);
double acosh(double x);
double asin(double x);
double asinh(double x);
double atan(double x);
double atan2(double y, double x);
double cos(double x);
double exp(double x);
double atanh(double x);
double log(double x);
double log1p(double x);
double log2(double x);
double log10(double x);
double expm1(double x);
double cbrt(double x);
double sin(double x);
double tan(double x);
double cosh(double x);
double pow(double x, double y);
double sinh(double x);
double tanh(double x);
}  // namespace ieee754
}  // namespace base
}  // namespace v8
#endif
