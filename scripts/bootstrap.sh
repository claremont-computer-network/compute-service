#!/usr/bin/env bash
set -euo pipefail

# Simple bootstrap script for compute-service: validates presence of local inventory and runs Ansible playbook.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INVENTORY="$ROOT_DIR/inventory.yml"

if ! command -v ansible-playbook >/dev/null 2>&1; then
  echo "ansible-playbook not found in PATH. Install Ansible before running this script." >&2
  exit 1
fi

if [ ! -f "$INVENTORY" ]; then
  echo "Missing local inventory: $INVENTORY"
  echo "Copy ansible/inventory.example.yml to inventory.yml and edit host addresses before running."
  exit 2
fi

echo "Running Ansible playbook against inventory: $INVENTORY"
echo "Extra ansible-playbook args: $*"
echo ""
echo "Tip: common extra flags:"
echo "  --ask-pass                        prompt for SSH password"
echo "  --ask-become-pass                 prompt for sudo password"
echo "  --private-key ~/.ssh/my_key       use a specific SSH key"
echo "  -e ansible_port=2222              override SSH port"
echo ""

ansible-playbook \
  -i "$INVENTORY" \
  "$ROOT_DIR/ansible/playbook.yml" \
  --diff \
  "$@"

echo "Bootstrap complete."
