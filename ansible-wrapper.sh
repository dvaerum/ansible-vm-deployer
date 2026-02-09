#!/usr/bin/env bash
#
# Ansible Wrapper Script
#
# This script wraps ansible-playbook execution to allow easy customization
# without modifying the Python code.
#
# Working Directory:
#   When --project-root is set, this script runs with cwd = project-root
#   This allows you to use relative paths for files in your project
#
# Arguments passed from ansible-deployer:
#   $1: /path/to/playbook.yml (always)
#   $2+: -i /path/to/inventory (if --inventory specified)
#   $N: --extra-vars '{"key":"value"}' (if --extra-vars specified)
#   $N+1: [additional flags] (if --ansible-flags specified, e.g., --check --diff -vvv)
#
# Environment variables available:
#   VM_IP_1, VM_IP_2, VM_IP_3, ... - Individual VM IPs
#   VM_IP_ALL - Comma-separated list of all VM IPs
#   (Plus any other environment variables from your shell)
#
# Usage: This script is called automatically by ansible-deployer
#        You can customize it by adding flags or logic below.
#
# Note: Use --ansible-flags to pass custom flags like verbosity (-vvv), --check, etc.
#

set -e  # Exit on error

# Optional: Add custom logic before Ansible execution
# Example: Log environment variables
# echo "VM_IP_ALL: ${VM_IP_ALL}"

# Optional: Add custom Ansible flags here
# CUSTOM_FLAGS=("--forks" "10")

# Execute ansible-playbook with all arguments
# "$@" passes all script arguments to ansible-playbook
exec ansible-playbook "$@" ${CUSTOM_FLAGS[@]}

# Optional: Add post-execution logic here (unreachable due to exec)
# Note: Use a different approach if you need post-execution hooks
