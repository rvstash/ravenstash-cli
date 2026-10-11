{
  description = "Ravenstash developer CLI";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
      ...
    }:
    let
      inherit (nixpkgs) lib;
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = lib.genAttrs systems;

      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };
      workspaceOverlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };

      pythonSets = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          pythonBase = pkgs.callPackage pyproject-nix.build.packages { python = pkgs.python314; };
          buildHacks = pkgs.callPackage pyproject-nix.build.hacks { };
          cryptographySourceBuild = final: prev:
            lib.optionalAttrs (system == "x86_64-darwin") {
              # Cryptography 50 no longer publishes Intel macOS wheels. Build
              # the exact locked sdist with its Cargo and native dependencies.
              cryptography = (buildHacks.importCargoLock {
                prev = prev.cryptography;
                cargoRoot = "src/rust";
              }).overrideAttrs (old: {
                nativeBuildInputs = (old.nativeBuildInputs or [ ]) ++ [ pkgs.pkg-config ] ++ final.resolveBuildSystem {
                  cffi = [ ];
                  maturin = [ ];
                  pycparser = [ ];
                  setuptools = [ ];
                };
                buildInputs = (old.buildInputs or [ ]) ++ [
                  pkgs.libiconv
                  pkgs.openssl
                ];
              });
            };
        in
        pythonBase.overrideScope (
          lib.composeManyExtensions [
            pyproject-build-systems.overlays.wheel
            workspaceOverlay
            cryptographySourceBuild
          ]
        )
      );
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          pythonSet = pythonSets.${system};
          inherit (pkgs.callPackages pyproject-nix.build.util { }) mkApplication;
          ravenstash-cli = (mkApplication {
            venv = pythonSet.mkVirtualEnv "ravenstash-cli-env" workspace.deps.default;
            package = pythonSet.ravenstash-cli;
          }).overrideAttrs (old: {
            nativeBuildInputs = (old.nativeBuildInputs or [ ]) ++ [ pkgs.makeWrapper ];
            postFixup = (old.postFixup or "") + ''
              for command in rvs ravenstash docker-credential-rvs; do
                wrapProgram "$out/bin/$command" \
                  --set-default SSL_CERT_FILE "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
              done
            '';
          });
        in
        {
          default = ravenstash-cli;
          inherit ravenstash-cli;
        }
      );

      checks = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          pythonSet = pythonSets.${system};
          testEnvironment = pythonSet.mkVirtualEnv "ravenstash-cli-test-env" {
            ravenstash-cli = [ "dev" ];
          };
          tests = pkgs.runCommand "ravenstash-cli-tests" { nativeBuildInputs = [ testEnvironment ]; } ''
            cp -r ${self} source
            chmod -R u+w source
            cd source
            export HOME="$TMPDIR/home"
            export SSL_CERT_FILE="${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
            mkdir -p "$HOME"
            pytest \
              --deselect tests/test_auth_stores.py::test_encrypted_vault_round_trip_and_lock \
              --deselect tests/test_packaging.py::test_apt_publisher_uses_constant_number_of_storage_calls \
              --deselect tests/test_packaging.py::test_release_slot_check_fails_closed \
              --deselect tests/test_packaging.py::test_release_slot_check_accepts_candidate_tag
            touch "$out"
          '';
        in
        {
          inherit tests;
          inherit (self.packages.${system}) ravenstash-cli;
        }
      );
    };
}
