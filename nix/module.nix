# SPDX-License-Identifier: GPL-3.0-or-later
{ self }:
{ config, lib, pkgs, ... }:
let
  inherit (lib) mkEnableOption mkIf mkOption types;
  cfg = config.services.weechat-managed-js;
  native = cfg.package;
  profile = "/run/weechat-managed-js/profile";
  sourceRoot = "/var/lib/weechat-managed-js/sources";
  policy = pkgs.writeText "weechat-managed-policy.rego" (builtins.readFile cfg.policyFile);
  policyData = pkgs.writeText "weechat-managed-policy-data.json" (builtins.toJSON cfg.policyData);
  commands = [
    "/plugin load ${native}/lib/weechat/plugins/irc.so"
    "/plugin load ${native.python}/lib/weechat/plugins/python.so"
    "/plugin load ${native}/lib/weechat/plugins/javascript.so"
    "/set plugins.var.python.mcp.buffers ${lib.concatMapStringsSep "," (c: "irc.managed.${c}") cfg.channels}"
    "/python load ${self}/weechat_mcp.py"
    "/server add managed ${cfg.server}/${toString cfg.port} -${if cfg.tls then "tls" else "notls"}"
    "/set irc.server.managed.tls_verify on"
    "/set irc.server.managed.nicks ${cfg.nick}"
    "/set irc.server.managed.autojoin ${lib.concatStringsSep "," cfg.channels}"
    "/set irc.server.managed.autoreconnect on"
    "/set irc.server.managed.autoreconnect_delay 1"
  ] ++ cfg.extraStartupCommands ++ [ "/connect managed" ];
  commandArgs = lib.concatMapStrings (c: " --run-command " + lib.escapeShellArg c) commands;
in {
  options.services.weechat-managed-js = {
    enable = mkEnableOption "opt-in mixed-trust WeeChat managed JavaScript";
    package = mkOption {
      type = types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.weechat-managed;
      description = "Pinned opt-in modern WeeChat fork with the additive managed lane; not stock WeeChat 4.x.";
    };
    policyFile = mkOption {
      type = types.path;
      default = ../policy/managed.rego;
      description = "Operator-owned Rego, snapshotted immutably into the Nix store; never a managed-script path.";
    };
    policyData = mkOption {
      type = types.attrs;
      default = { managed = { buffers = [ ]; http_methods = [ ]; http_hosts = [ ]; log = false; }; };
      description = "Operator-owned grants, snapshotted into the Nix store. Empty grants deny every managed effect.";
    };
    server = mkOption { type = types.str; description = "IRC hostname or IPv4 address; no credentials or URL."; };
    port = mkOption { type = types.port; default = 6697; };
    tls = mkOption { type = types.bool; default = true; };
    nick = mkOption { type = types.str; default = "managed-js"; };
    channels = mkOption { type = types.listOf types.str; default = [ "#wchat" ]; };
    extraStartupCommands = mkOption {
      type = types.listOf types.str;
      default = [ ];
      description = "Trusted operator-only WeeChat startup commands, e.g. loading legacy trusted scripts. Never exposed to managed JavaScript.";
    };
    memoryMax = mkOption {
      type = types.str;
      default = "512M";
      description = "Whole-service memory ceiling, not an isolate memory-security boundary.";
    };
  };

  config = mkIf cfg.enable {
    assertions = [
      { assertion = builtins.match "[A-Za-z0-9][A-Za-z0-9.-]*" cfg.server != null; message = "Managed WeeChat IRC server must be a plain hostname or IPv4 address."; }
      { assertion = builtins.match "[A-Za-z][A-Za-z0-9_-]{0,29}" cfg.nick != null; message = "Managed WeeChat nick must be a simple nickname."; }
      { assertion = cfg.channels != [ ] && builtins.length cfg.channels <= 20 && builtins.length cfg.channels == builtins.length (lib.unique cfg.channels) && lib.all (c: builtins.match "#[A-Za-z0-9_.-]{1,100}" c != null) cfg.channels; message = "Managed WeeChat requires 1-20 unique simple #channel names."; }
    ];
    users.users.weechat-managed-js = { isSystemUser = true; group = "weechat-managed-js"; };
    users.groups.weechat-managed-js = { };
    systemd.services.weechat-managed-js = {
      description = "Opt-in WeeChat managed V8 scripts with independent immutable Rego policy";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      path = [ pkgs.coreutils ];
      environment = {
        HOME = profile;
        PYTHONHOME = "${pkgs.python3}";
        WEECHAT_MCP_FULL_CONTROL = "0";
        WEECHAT_MCP_MANAGED_JS = "1";
        WEECHAT_MANAGED_ROOT = sourceRoot;
        WEECHAT_MANAGED_POLICY = "${policy}";
        WEECHAT_MANAGED_DATA = "${policyData}";
      };
      script = ''
        mkdir -p -m 0700 ${sourceRoot}
        rm -rf ${profile}
        mkdir -m 0700 ${profile}
        exec ${native}/bin/weechat-headless --dir ${profile} --no-plugin --no-connect --no-script --stdout${commandArgs}
      '';
      serviceConfig = {
        User = "weechat-managed-js";
        Group = "weechat-managed-js";
        UMask = "0077";
        StateDirectory = "weechat-managed-js";
        StateDirectoryMode = "0700";
        RuntimeDirectory = "weechat-managed-js";
        RuntimeDirectoryMode = "0700";
        MemoryMax = cfg.memoryMax;
        TasksMax = 128;
        Restart = "on-failure";
        RestartSec = "2s";
        # Deliberately no global no-exec, seccomp/Landlock profile, or pathname
        # restrictions that would silently change existing trusted plugin APIs.
        # Managed scripts are constrained by their separate isolate and typed,
        # native policy boundary; trusted scripts retain their existing trust.
      };
    };
  };
}
