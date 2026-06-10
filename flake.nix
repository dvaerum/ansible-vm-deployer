{
  description = "VM Management Tools - Ansible Deployer and VM Manager for libvirt";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    let
      # Overlay providing our packages
      overlay = final: prev: {
        ansible-deployer = self.packages.${final.stdenv.hostPlatform.system}.ansible-deployer;
        vm-manager = self.packages.${final.stdenv.hostPlatform.system}.vm-manager;
      };
    in
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        
        # Python package set
        pythonEnv = pkgs.python3.withPackages (ps: with ps; [
          libvirt
          click
          pydantic
          rich
          jinja2
          pyyaml
          paramiko
          # Development dependencies
          pytest
          pytest-asyncio
          black
          ruff
          mypy
        ]);

        # Runtime dependencies
        runtimeDeps = with pkgs; [
          libvirt
          qemu
          ansible
          python3
        ];

      in {
        # Development shell
        devShells.default = pkgs.mkShell {
          buildInputs = with pkgs; [
            python3
            python3Packages.pip
            python3Packages.setuptools
            python3Packages.wheel
          ];
          
          packages = [
            pythonEnv
          ] ++ runtimeDeps;

          shellHook = ''
            export PYTHONPATH="$PWD/src:$PYTHONPATH"
            export LIBVIRT_DEFAULT_URI="qemu:///system"
            echo "Ansible Deployer Development Shell"
            echo "Python: $(python --version)"
            echo "Libvirt: $(virsh --version 2>/dev/null || echo 'not available')"
          '';
        };

        # Packages
        packages = {
          # Ansible Deployer package
          ansible-deployer = pkgs.python3Packages.buildPythonPackage {
            pname = "ansible-deployer";
            version = "0.1.0";
            
            pyproject = true;
            
            src = ./.;
            
            build-system = with pkgs.python3Packages; [
              setuptools
              wheel
            ];
            
            propagatedBuildInputs = with pkgs.python3Packages; [
              libvirt
              click
              pydantic
              rich
              jinja2
              pyyaml
              paramiko
            ];

            nativeCheckInputs = with pkgs.python3Packages; [
              pytest
              pytest-asyncio
            ];

            doCheck = false;  # Skip tests during build (run separately)
            dontCheckRuntimeDeps = true;  # Ansible provided via system package

            meta = with pkgs.lib; {
              description = "Deploy Ansible playbooks to libvirt-managed VMs with automatic cleanup";
              homepage = "https://github.com/dvaerum/ansible-vm-deployer";
              license = licenses.mit;
              platforms = platforms.linux;
              mainProgram = "ansible-deployer";
            };
          };

          # VM Manager package
          vm-manager = pkgs.python3Packages.buildPythonPackage {
            pname = "vm-manager";
            version = "0.1.0";
            
            pyproject = true;
            
            src = ./.;
            
            build-system = with pkgs.python3Packages; [
              setuptools
              wheel
            ];
            
            propagatedBuildInputs = with pkgs.python3Packages; [
              libvirt
              paramiko
            ];

            nativeCheckInputs = with pkgs.python3Packages; [
              pytest
              pytest-asyncio
            ];

            doCheck = false;  # Skip tests during build (run separately)
            dontCheckRuntimeDeps = true;  # Shares pyproject.toml with ansible-deployer

            meta = with pkgs.lib; {
              description = "Monitor libvirt VMs and manage tags based on SSH connectivity";
              homepage = "https://github.com/dvaerum/ansible-vm-deployer";
              license = licenses.mit;
              platforms = platforms.linux;
              mainProgram = "vm-manager";
            };
          };

          # Default package is ansible-deployer
          default = self.packages.${system}.ansible-deployer;
        };

        # Applications
        apps = {
          # Default app
          default = {
            type = "app";
            program = "${self.packages.${system}.ansible-deployer}/bin/ansible-deployer";
          };

          # Ansible deployer app
          ansible-deployer = {
            type = "app";
            program = "${self.packages.${system}.ansible-deployer}/bin/ansible-deployer";
          };

          # VM manager app
          vm-manager = {
            type = "app";
            program = "${self.packages.${system}.vm-manager}/bin/vm-manager";
          };
        };
      }) // {
        # Overlay for nixpkgs
        overlays.default = overlay;

        # NixOS module
        nixosModules.vm-manager = import ./nixos-modules/vm-manager.nix;
      };
}
