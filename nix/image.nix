# SPDX-License-Identifier: GPL-3.0-or-later
{ pkgs, sourceRoot, weechatManaged, bridgeEnv }:
let
  inherit (pkgs) lib dockerTools runCommand buildEnv coreutils bash cacert;
  policyData = pkgs.writeText "weechat-managed-deny-all.json"
    (builtins.toJSON { managed = { buffers = [ ]; http_methods = [ ]; http_hosts = [ ]; log = false; }; });
  application = runCommand "weechat-mcp-container-root" { } ''
    # The image build itself verifies that the exact locked bridge dependencies
    # import successfully; this is not satisfied by the native runtime alone.
    PYTHONPATH=${sourceRoot} ${bridgeEnv}/bin/python - <<'PY'
    from importlib.metadata import version
    import bridge
    import managed_js
    import managed_http
    assert version("fastmcp") == "4.1.0"
    assert version("mcp") == "2.3.0"
    PY
    mkdir -p $out/app $out/opt $out/etc/weechat-managed
    for name in bridge.py native_buffer.py command_manifest.py weechat_mcp.py \
      container_entrypoint.py mcp_events.py jev_filter.py event_protocol.py \
      event_webhooks.py managed_js.py managed_http.py LICENSE; do
      ln -s ${sourceRoot}/$name $out/app/$name
    done
    ln -s ${bridgeEnv} $out/app/.venv
    ln -s ${weechatManaged} $out/opt/weechat-managed
    cp ${sourceRoot}/policy/managed.rego $out/etc/weechat-managed/managed.rego
    printf '%s\n' '{"managed":{"buffers":[],"http_methods":[],"http_hosts":[],"log":false}}' \
      > $out/etc/weechat-managed/data.json
    chmod 0444 $out/etc/weechat-managed/*
    printf '%s\n' 'root:x:0:0:root:/root:/bin/sh' \
      'weechat:x:10001:10001:WeeChat:/home/weechat:/bin/sh' > $out/etc/passwd
    printf '%s\n' 'root:x:0:' 'weechat:x:10001:' > $out/etc/group
    printf '%s\n' 'passwd: files' 'group: files' 'hosts: files dns' > $out/etc/nsswitch.conf
  '';
  root = buildEnv {
    name = "weechat-mcp-managed-image-root";
    paths = [ application coreutils bash cacert ];
    pathsToLink = [ "/app" "/opt" "/etc" "/bin" ];
  };
in dockerTools.buildLayeredImage {
  name = "weechat-mcp";
  tag = "managed-js-nix";
  contents = [ root ];
  maxLayers = 100;
  # No VM, Docker daemon, networked installer or root execution is needed to
  # construct this image. The runtime closure and bridge environment are Nix
  # dependencies; this output is an actual docker-loadable image tarball.
  fakeRootCommands = ''
    mkdir -p ./tmp ./root ./home/weechat/managed-js
    chmod 1777 ./tmp
    chmod 0700 ./root ./home/weechat/managed-js
    chown -R 10001:10001 ./home/weechat
  '';
  config = {
    Entrypoint = [ "/app/.venv/bin/python" "/app/container_entrypoint.py" ];
    WorkingDir = "/app";
    User = "0:0";
    Env = [
      "PATH=/app/.venv/bin:/opt/weechat-managed/bin:/bin"
      "HOME=/home/weechat"
      "PYTHONUNBUFFERED=1"
      "PYTHONUTF8=1"
      "SSL_CERT_FILE=/etc/ssl/certs/ca-bundle.crt"
      "NIX_SSL_CERT_FILE=/etc/ssl/certs/ca-bundle.crt"
      "WEECHAT_MCP_MANAGED_JS=1"
      "WEECHAT_MCP_FULL_CONTROL=0"
      "WEECHAT_MANAGED_ROOT=/home/weechat/managed-js"
      # These exact canonical store paths, not /etc symlink aliases, satisfy
      # both the native policy loader and the controller's path invariants.
      "WEECHAT_MANAGED_POLICY=${sourceRoot}/policy/managed.rego"
      "WEECHAT_MANAGED_DATA=${policyData}"
      "WEECHAT_EXTRA_LIBDIR=/opt/weechat-managed/lib/weechat"
    ];
    Labels = {
      "org.opencontainers.image.title" = "WeeChat MCP managed JavaScript";
      "org.opencontainers.image.description" = "Opt-in Nix-built WeeChat 5.0.0-dev with independent embedded Rego; existing apt Docker image unchanged";
      "org.opencontainers.image.licenses" = "GPL-3.0-or-later";
    };
  };
  meta = {
    description = "Actual Nix-built OCI-compatible Docker image, including locked MCP bridge dependencies";
    platforms = lib.platforms.linux;
  };
}
