# SPDX-License-Identifier: GPL-3.0-or-later
{ pkgs, sourceRoot, pyproject-nix, uv2nix, pyproject-build-systems }:
let
  # Interpret the existing lock, including its Python/platform markers and wheel
  # hashes. Do not substitute nixpkgs' potentially different FastMCP/MCP versions
  # or run networked pip/uv during a Nix build or at container startup.
  workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = sourceRoot; };
  overlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };
  pythonBase = pkgs.callPackage pyproject-nix.build.packages {
    python = pkgs.python312;
  };
  pythonSet = pythonBase.overrideScope (pkgs.lib.composeManyExtensions [
    pyproject-build-systems.overlays.wheel
    overlay
  ]);
in pythonSet.mkVirtualEnv "weechat-mcp-bridge-env" workspace.deps.default
