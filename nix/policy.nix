# SPDX-License-Identifier: GPL-3.0-or-later
{ lib, rustPlatform, stdenv, sourceRoot }:
let
  policySource = lib.cleanSourceWith {
    src = sourceRoot + "/policy";
    filter = path: type:
      builtins.baseNameOf path != "target" && lib.cleanSourceFilter path type;
  };
in rustPlatform.buildRustPackage {
  pname = "weechat-managed-policy";
  version = "0.1.0";
  src = policySource;
  cargoLock.lockFile = sourceRoot + "/policy/Cargo.lock";
  doCheck = true;
  # Cargo's global cache must stay in this derivation's scratch directory,
  # including on supported single-user builders without a per-build home.
  preBuild = ''
    export CARGO_HOME="$TMPDIR/cargo-home"
    mkdir -p "$CARGO_HOME"
  '';
  installPhase = ''
    runHook preInstall
    mkdir -p $out/lib $out/include $out/share/weechat-managed-policy
    cp target/${stdenv.hostPlatform.rust.rustcTargetSpec}/release/libweechat_managed_policy.a $out/lib/
    cp include/weechat_managed_policy.h $out/include/
    cp managed.rego example-data.json $out/share/weechat-managed-policy/
    runHook postInstall
  '';
  meta = {
    description = "Independent immutable Rego authorization evaluator for managed WeeChat scripts";
    license = lib.licenses.gpl3Plus;
    platforms = lib.platforms.linux;
  };
}
