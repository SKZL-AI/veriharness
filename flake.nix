# VeriHarness (package `hoh`) for Nix.
#
# STATUS: written without a Nix binary and without network access. It has been
# checked statically (tests/test_nix_packaging_static.py) and has NEVER been
# evaluated or built. `nix flake check`, `nix build` and `nix develop` are
# NOT_RUN until someone runs them on a Nix host. Pin `nixpkgs` with
# `nix flake lock` there; no lock file is shipped because none can be produced
# offline.
{
  description = "VeriHarness: an evidence-bound harness-of-harness (package hoh)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      lib = nixpkgs.lib;
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forEachSystem = f: lib.genAttrs systems (system: f system nixpkgs.legacyPackages.${system});
      pyproject = builtins.fromTOML (builtins.readFile ./pyproject.toml);
    in
    {
      packages = forEachSystem (system: pkgs: {
        hoh = pkgs.python3Packages.buildPythonApplication {
          pname = pyproject.project.name;
          version = pyproject.project.version;
          pyproject = true;
          src = lib.fileset.toSource {
            root = ./.;
            fileset = lib.fileset.unions [
              ./src
              ./tests
              ./policy
              ./pyproject.toml
              ./README.md
              ./LICENSE
            ];
          };
          build-system = [ pkgs.python3Packages.setuptools ];
          dependencies = [ pkgs.python3Packages.pydantic ];
          # The product shells out to git (worktrees, archives), bash (every
          # acceptance check) and, for --isolation strict, bwrap. herdr and the
          # agent CLIs are the operator's own and are not wrapped in.
          makeWrapperArgs = [
            "--prefix" "PATH" ":"
            (lib.makeBinPath [ pkgs.git pkgs.bash pkgs.coreutils pkgs.bubblewrap ])
          ];
          pythonImportsCheck = [ "hoh" ];
          # NOT_RUN: the suite has not been run inside the Nix build sandbox
          # (no user namespaces there, HOME unset). Enable after a first
          # successful run on a Nix host.
          doCheck = false;
        };
        default = self.packages.${system}.hoh;
      });

      apps = forEachSystem (system: pkgs: {
        default = {
          type = "app";
          program = "${self.packages.${system}.hoh}/bin/hoh";
        };
      });

      devShells = forEachSystem (system: pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: [
              ps.pydantic
              ps.pytest
              ps.zstandard
              ps.setuptools
            ]))
            pkgs.git
            pkgs.bash
            pkgs.bubblewrap
          ];
          shellHook = ''
            export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
          '';
        };
      });
    };
}
