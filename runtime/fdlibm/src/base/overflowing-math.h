// Shim for the wraparound helpers used by ieee754.cc.
#ifndef FDLIBM_SHIM_OVERFLOWING_MATH_H
#define FDLIBM_SHIM_OVERFLOWING_MATH_H
#include <cstdint>
#include <type_traits>
#include <cmath>
#include <limits>
namespace v8 {
namespace base {
template <typename T>
inline T AddWithWraparound(T a, T b) {
  using U = typename std::make_unsigned<T>::type;
  return static_cast<T>(static_cast<U>(a) + static_cast<U>(b));
}
template <typename T>
inline T SubWithWraparound(T a, T b) {
  using U = typename std::make_unsigned<T>::type;
  return static_cast<T>(static_cast<U>(a) - static_cast<U>(b));
}
template <typename T>
inline T NegateWithWraparound(T a) {
  using U = typename std::make_unsigned<T>::type;
  return static_cast<T>(U(0) - static_cast<U>(a));
}
// Floating-point division with IEEE results for a zero divisor (V8 base::Divide).
template <typename T>
inline T Divide(T x, T y) {
  if (y != 0) return x / y;
  if (x == 0 || x != x) return std::numeric_limits<T>::quiet_NaN();
  if ((x >= 0) == (std::signbit(y) == 0)) {
    return std::numeric_limits<T>::infinity();
  }
  return -std::numeric_limits<T>::infinity();
}
}  // namespace base
}  // namespace v8
#endif
