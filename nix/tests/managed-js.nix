# SPDX-License-Identifier: GPL-3.0-or-later
{ pkgs, self }:
let
  python = pkgs.python3;
in pkgs.testers.runNixOSTest {
  name = "weechat-managed-js";
  nodes.machine = { ... }: {
    imports = [ self.nixosModules.default ];
    environment.systemPackages = [ python pkgs.coreutils ];
    virtualisation.memorySize = 3072;
    # This globally classified address lives only on the isolated VM's loopback.
    # Production public-address validation is unchanged; no Internet is used.
    networking.interfaces.lo.ipv4.addresses = [ { address = "93.184.216.34"; prefixLength = 32; } ];
    networking.hosts."93.184.216.34" = [ "fixture.test" "wrong.fixture.test" "outside.test" ];
    services.weechat-managed-js = {
      enable = true;
      server = "127.0.0.1";
      port = 16667;
      tls = false;
      channels = [ "#test" "#outside" ];
      policyData.managed = {
        buffers = [ "irc.managed.#test" ];
        http_methods = [ "GET" "POST" "PUT" "DELETE" ];
        # Explicitly granting localhost here must still NOT defeat the separate
        # always-public transport policy. outside.test deliberately lacks a grant.
        http_hosts = [ "fixture.test" "wrong.fixture.test" "127.0.0.1" ];
        log = true;
      };
      extraStartupCommands = [
        "/plugin load ${self.packages.x86_64-linux.weechat-managed}/lib/weechat/plugins/alias.so"
        "/alias add msg /command irc /msg * forbidden-alias-deputy"
        "/set irc.network.send_unknown_commands on"
        "/javascript load ${./trusted.js}"
        "/set irc.server.managed.anti_flood_prio_high 0"
        "/set irc.server.managed.anti_flood_prio_low 0"
      ];
    };
    systemd.services.weechat-managed-js = {
      after = [ "managed-irc.service" "managed-http.service" ];
      requires = [ "managed-irc.service" "managed-http.service" ];
      environment = {
        # Explicit VM-only trust anchor for libcurl/OpenSSL as well as WeeChat.
        SSL_CERT_FILE = "/etc/ssl/certs/ca-certificates.crt";
        NIX_SSL_CERT_FILE = "/etc/ssl/certs/ca-certificates.crt";
        http_proxy = "http://127.0.0.1:1";
        https_proxy = "http://127.0.0.1:1";
        ALL_PROXY = "http://127.0.0.1:1";
      };
    };
    systemd.services.managed-http = {
      wantedBy = [ "multi-user.target" ];
      before = [ "weechat-managed-js.service" ];
      after = [ "network.target" ];
      path = [ pkgs.openssl pkgs.coreutils ];
      preStart = ''
        cd /run/managed-http
        openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
          -subj /CN=WeeChat-VM-test-CA -addext basicConstraints=critical,CA:TRUE \
          -keyout ca.key -out ca.crt
        openssl req -newkey rsa:2048 -nodes -subj /CN=fixture.test \
          -keyout server.key -out server.csr
        printf '%s\n' 'subjectAltName=DNS:fixture.test' 'basicConstraints=critical,CA:FALSE' 'extendedKeyUsage=serverAuth' > extensions
        openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
          -days 1 -extfile extensions -out server.crt
        chmod 0600 ca.key server.key
        # VM-only generated trust anchor; no production trust store is changed.
        rm -f /etc/ssl/certs/ca-certificates.crt
        install -m 0644 ca.crt /etc/ssl/certs/ca-certificates.crt
      '';
      serviceConfig = {
        RuntimeDirectory = "managed-http";
        RuntimeDirectoryMode = "0755";
        ExecStart = "${python}/bin/python ${./http_fixture.py} --state /run/managed-http/state.json --cert /run/managed-http/server.crt --key /run/managed-http/server.key";
      };
    };
    systemd.services.managed-irc = {
      wantedBy = [ "multi-user.target" ];
      before = [ "weechat-managed-js.service" ];
      serviceConfig = {
        RuntimeDirectory = "managed-irc";
        RuntimeDirectoryMode = "0700";
        ExecStart = "${python}/bin/python ${./irc_fixture.py} --control /run/managed-irc/control.sock";
      };
    };
  };

  testScript = ''
    start_all()
    machine.wait_for_unit("managed-irc.service")
    machine.wait_for_unit("managed-http.service")
    machine.wait_for_unit("weechat-managed-js.service")
    machine.wait_until_succeeds("test -S /run/weechat-managed-js/profile/mcp/bridge.sock")
    with subtest("actual managed and trusted JavaScript, independent policy and named source lifecycle"):
        machine.succeed("${python}/bin/python ${./integration.py} --control /run/managed-irc/control.sock --home /run/weechat-managed-js/profile --source ${./managed.js} --http-state /run/managed-http/state.json")
    with subtest("policy inputs remain immutable and outside source storage"):
        machine.fail("runuser -u weechat-managed-js -- test -w ${self.packages.x86_64-linux.managed-policy}/share/weechat-managed-policy/managed.rego")
        machine.succeed("test $(stat -c %a /var/lib/weechat-managed-js/sources) = 700")
  '';
}
