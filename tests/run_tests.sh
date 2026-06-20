#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "Ansible version: $(ansible --version | head -1)"
echo "Running tasks_serial integration tests..."
ansible-playbook playbooks/test_tasks_serial.yml

echo "All tests passed."