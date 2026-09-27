// Shim for the subset of V8 base macros used by ieee754.cc.
#ifndef FDLIBM_SHIM_MACROS_H
#define FDLIBM_SHIM_MACROS_H
#include <cstring>
#include <cstdint>
#define V8_WARN_UNUSED_RESULT
#define V8_INLINE inline
#define V8_CC_MSVC 0
namespace v8 {
namespace base {
template <class Dest, class Source>
inline Dest bit_cast(const Source& source) {
  static_assert(sizeof(Dest) == sizeof(Source), "size mismatch");
  Dest dest;
  std::memcpy(&dest, &source, sizeof(dest));
  return dest;
}
}  // namespace base
}  // namespace v8
#endif
