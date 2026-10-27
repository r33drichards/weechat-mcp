# SPDX-License-Identifier: GPL-3.0-or-later
{
  description = "Opt-in modern WeeChat managed JavaScript with independent Rego policy";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/1f607aa79b6356ed45d3cd3c8079c196e79efa2c";
  inputs.weechat-source = {
    url = "github:r33drichards/weechat/b457688b41e429372a3dac64efd2534fdf4b2f73";
    flake = false;
  };

  inputs.pyproject-nix = {
    url = "github:pyproject-nix/pyproject.nix/ddc400a6883b042646d0940e65971bb7f7e96107";
    inputs.nixpkgs.follows = "nixpkgs";
  };
  inputs.uv2nix = {
    url = "github:pyproject-nix/uv2nix/0fdaa03ea3c3c16dbb1fe4d69108f07227a4b42d";
    inputs.nixpkgs.follows = "nixpkgs";
    inputs.pyproject-nix.follows = "pyproject-nix";
  };
  inputs.pyproject-build-systems = {
    url = "github:pyproject-nix/build-system-pkgs/a17819c85163988e063806ffc4f6d2716bc8ead8";
    inputs.nixpkgs.follows = "nixpkgs";
    inputs.pyproject-nix.follows = "pyproject-nix";
    inputs.uv2nix.follows = "uv2nix";
  };

  outputs = { self, nixpkgs, weechat-source, pyproject-nix, uv2nix, pyproject-build-systems }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      eachSystem = nixpkgs.lib.genAttrs systems;
    in {
      packages = eachSystem (system:
        let
          pkgs = import nixpkgs { inherit system; };
          policy = pkgs.callPackage ./nix/policy.nix { sourceRoot = self; };
          managed = pkgs.callPackage ./nix/weechat.nix {
            inherit policy;
            sourceRoot = self;
            weechatSource = weechat-source;
          };
          container = pkgs.callPackage ./nix/container.nix {
            weechatManaged = managed;
          };
          bridgeEnv = import ./nix/python-env.nix {
            inherit pkgs pyproject-nix uv2nix pyproject-build-systems;
            sourceRoot = self;
          };
        in {
          managed-policy = policy;
          # Explicit opt-in name: the existing Docker/runtime is not replaced.
          weechat-managed = managed;
          weechat-managed-container = container;
          managed-js-python = bridgeEnv;
          managed-js-image = import ./nix/image.nix {
            inherit pkgs bridgeEnv;
            sourceRoot = self;
            weechatManaged = container;
          };
        });

      nixosModules.default = import ./nix/module.nix { inherit self; };

      checks = eachSystem (system:
        let pkgs = import nixpkgs { inherit system; };
        in {
          managed-policy = self.packages.${system}.managed-policy;
          v8-plugin-link = self.packages.${system}.weechat-managed.v8PluginLink;
          weechat-managed = self.packages.${system}.weechat-managed;
        } // pkgs.lib.optionalAttrs (system == "x86_64-linux") {
          managed-js-vm = import ./nix/tests/managed-js.nix { inherit pkgs self; };
        });
    };
}
