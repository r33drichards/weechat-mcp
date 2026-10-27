# SPDX-License-Identifier: GPL-3.0-or-later
{
  lib,
  callPackage,
  weechat-unwrapped,
  nodejs-slim_22,
  icu78,
  zstd,
  policy,
  sourceRoot,
  weechatSource,
}:
let
  # Node 22 is maintained and its cached monolith supports shared-plugin links.
  # Node 24's current archive has local-exec TLS relocations and cannot be used
  # in javascript.so even though an executable ABI probe succeeds.
  v8 = nodejs-slim_22.libv8;
  v8PluginLink = callPackage ./v8-plugin-link.nix { inherit v8; };
  nativeSource = builtins.path {
    path = sourceRoot + "/native";
    name = "weechat-managed-native";
    filter = path: type: type == "directory" || builtins.elem (builtins.baseNameOf path) [
      "weechat-js-managed.cpp" "weechat-js-managed.h"
    ];
  };
  patchesSource = builtins.path {
    path = sourceRoot + "/vendor/weechat/patches";
    name = "weechat-managed-patches";
  };
  # Nixpkgs' cached V8 monolith includes libplatform. No Node.js environment,
  # module loader, process object or application runtime is exposed to scripts.
  v8Libraries = lib.concatStringsSep ";" [
    "${v8}/lib/libv8.a"
    "${lib.getLib icu78}/lib/libicui18n.so"
    "${lib.getLib icu78}/lib/libicuuc.so"
    "pthread"
  ];
in weechat-unwrapped.overrideAttrs (old: {
  pname = "weechat-managed";
  # This pinned fork is newer than both the stock Docker runtime and nixpkgs'
  # stable WeeChat. Do not disguise the opt-in upgrade as a patch to stock 4.x.
  version = "5.0.0-dev";
  src = weechatSource;
  buildInputs = old.buildInputs ++ [ v8 v8PluginLink icu78 zstd policy ];
  cmakeFlags = builtins.filter
    (flag: !(lib.hasPrefix "-DENABLE_PYTHON3" flag))
    old.cmakeFlags ++ [
      "-DENABLE_PYTHON=ON"
      "-DENABLE_JAVASCRIPT=ON"
      "-DENABLE_ZSTD=ON"
      "-DV8_INCLUDE_DIR=${v8}/include"
      "-DV8_LIBRARIES=${v8Libraries}"
      "-DWEECHAT_MANAGED_POLICY_LIBRARY=${policy}/lib/libweechat_managed_policy.a"
      "-DWEECHAT_MANAGED_POLICY_INCLUDE_DIR=${policy}/include"
    ];
  # Keep upstream FindV8's real compile/link/run ABI-definition probe. In
  # particular, do not force V8_DEFINITIONS or skip checks to hide an ABI mismatch.
  postPatch = (old.postPatch or "") + ''
    for patch in ${patchesSource}/*.patch; do
      patch -p1 < "$patch"
    done
    cp ${nativeSource}/weechat-js-managed.cpp src/plugins/javascript/
    cp ${nativeSource}/weechat-js-managed.h src/plugins/javascript/
  '';
  preBuild = (old.preBuild or "") + ''
    # Documentation generation and the installed version check can create HOME.
    # Keep their files in this derivation, never the builder's shared fake home.
    export HOME="$TMPDIR/weechat-home"
    mkdir -p "$HOME"
  '';
  passthru = (old.passthru or { }) // {
    managedPolicy = policy;
    inherit v8PluginLink;
    sourceRevision = "b457688b41e429372a3dac64efd2534fdf4b2f73";
  };
  meta = old.meta // {
    description = "Opt-in WeeChat 5.0.0-dev fork with managed V8 scripts and independent Rego policy";
    platforms = lib.platforms.linux;
  };
})
