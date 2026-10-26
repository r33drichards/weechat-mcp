# SPDX-License-Identifier: GPL-3.0-or-later
{ lib, symlinkJoin, makeWrapper, python3, weechatManaged }:
symlinkJoin {
  pname = "weechat-managed-container";
  inherit (weechatManaged) version;
  paths = [ weechatManaged weechatManaged.python ];
  nativeBuildInputs = [ makeWrapper ];
  postBuild = ''
    wrapProgram $out/bin/weechat-headless --set PYTHONHOME ${python3}
  '';
  passthru = { native = weechatManaged; };
  meta = (builtins.removeAttrs weechatManaged.meta [ "outputsToInstall" ]) // {
    description = "Runtime closure of the same Nix-built managed WeeChat, including its matching Python plugin";
    mainProgram = "weechat-headless";
    platforms = lib.platforms.linux;
  };
}
