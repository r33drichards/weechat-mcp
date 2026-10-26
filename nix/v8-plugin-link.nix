# SPDX-License-Identifier: GPL-3.0-or-later
{ runCommand, stdenv, v8, icu78 }:
runCommand "weechat-v8-shared-plugin-link" {
  nativeBuildInputs = [ stdenv.cc ];
  buildInputs = [ v8 icu78 ];
} ''
  # An executable probe cannot detect non-PIC/local-exec TLS objects that fail
  # when linked into WeeChat's dlopened plugin. Require that link separately.
  # Runtime isolate behavior is covered by the actual WeeChat VM, not this test.
  cat > probe.cpp <<'CPP'
  #include <v8.h>
  #include <libplatform/libplatform.h>
  extern "C" void weechat_v8_link_probe() {
    auto platform = v8::platform::NewSingleThreadedDefaultPlatform();
    v8::V8::InitializePlatform(platform.get());
    v8::V8::Initialize();
    v8::Isolate::CreateParams params;
    params.array_buffer_allocator = v8::ArrayBuffer::Allocator::NewDefaultAllocator();
    auto isolate = v8::Isolate::New(params);
    isolate->Dispose();
    delete params.array_buffer_allocator;
  }
CPP
  $CXX -std=c++20 -fPIC -shared -Wl,-z,defs -I${v8}/include probe.cpp \
    ${v8}/lib/libv8.a -licui18n -licuuc -pthread -ldl -o probe.so
  test -s probe.so
  touch $out
''
