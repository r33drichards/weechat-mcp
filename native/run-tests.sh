#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail
# The authoritative Nix build provides these paths/ABI flags. This helper also
# supports an explicitly selected local V8 build; it never downloads a runtime.
: "${WEECHAT_SOURCE:?set to the pinned unpacked WeeChat source}"
: "${V8_CFLAGS:?set matching V8 include paths and ABI definitions}"
: "${V8_LIBS:?set matching V8 linker flags}"
root=$(cd "$(dirname "$0")/.." && pwd)
archive=${POLICY_LIBRARY:-"$root/policy/target/release/libweechat_managed_policy.a"}
test -r "$archive"
build=$(mktemp -d)
trap 'rm -rf "$build"' EXIT
read -r -a cflags <<< "$V8_CFLAGS"
read -r -a libs <<< "$V8_LIBS"
common=(-std=c++20 -Wall -Wextra -Werror -Wno-unused-parameter -pthread)
"${CXX:-c++}" "${common[@]}" "${cflags[@]}" "$root/native/isolate-prototype.cpp" "${libs[@]}" -o "$build/isolate-prototype"
"${CXX:-c++}" "${common[@]}" "${cflags[@]}" -I"$root/policy/include" -I"$WEECHAT_SOURCE/src/plugins/javascript" \
    "$root/native/managed-test.cpp" "$archive" "${libs[@]}" -ldl -lm -o "$build/managed-test"
timeout 10 "$build/isolate-prototype"
timeout 10 "$build/managed-test" "$root/policy/managed.rego"
