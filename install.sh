#!/usr/bin/env bash
# Install k8s-autoscaler on 10.0.1.40.
# Run as the joyson user; prompts for sudo for systemd units.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="/opt/k8s-autoscaler"
CONFIG_DIR="/etc/k8s-autoscaler"
STATE_DIR="/var/lib/k8s-autoscaler"

echo "[1/6] Creating directories"
sudo mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$STATE_DIR"
sudo chown -R joyson:joyson "$INSTALL_DIR" "$STATE_DIR"

echo "[2/6] Copying files"
cp "$REPO_DIR/autoscaler.py" "$INSTALL_DIR/"
sudo cp "$REPO_DIR/config.yaml" "$CONFIG_DIR/config.yaml"
sudo chown joyson:joyson "$CONFIG_DIR/config.yaml"

echo "[3/6] Setting up venv"
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet pyyaml

echo "[4/6] Installing ansible playbook"
K8S_CLUSTER_DIR="${K8S_CLUSTER_DIR:-/home/joyson/k8s-cluster}"
cp "$REPO_DIR/ansible/join-autoscale-worker.yaml" "$K8S_CLUSTER_DIR/ansible/"

echo "[5/6] Installing systemd units"
sudo cp "$REPO_DIR/systemd/k8s-autoscaler.service" /etc/systemd/system/
sudo cp "$REPO_DIR/systemd/k8s-autoscaler.timer"   /etc/systemd/system/
sudo systemctl daemon-reload

echo "[6/6] Enabling timer (service starts in dry-run)"
sudo systemctl enable --now k8s-autoscaler.timer

cat <<EOF

Done. Next steps:

  1. Edit $CONFIG_DIR/config.yaml and set discord_webhook.
  2. Apply the Terraform patch in your k8s-cluster repo (see terraform/README.md).
  3. Tail logs:
       journalctl -u k8s-autoscaler.service -f
  4. When you trust it, flip dry_run: false in $CONFIG_DIR/config.yaml.

EOF
