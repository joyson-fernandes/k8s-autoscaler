# k8s-autoscaler

Karpenter-lite for a vSphere-backed bare-metal K8s cluster. Adds and
removes worker VMs based on pending pods and fleet request utilization.

## What it does

- **Scale up** — when ≥1 pod is `Pending` with `Insufficient cpu/memory`
  for 6 consecutive minutes, clone a new worker VM via Terraform and
  join it with the existing Ansible playbook.
- **Scale down** — when fleet request utilization (CPU or memory, whichever
  is higher) stays below 30% for 60 minutes, cordon + drain the least-loaded
  A-pool node, `kubectl delete node`, and `terraform destroy` the VM.
- **Never touches** static workers (W01–W05); only the dynamic A-pool
  (JOY-K8S-A01–A05, IPs 10.0.1.80–84).

## Guardrails

- 15-minute cooldown between any action.
- Aborts the whole tick if any node is not Ready.
- Runs in **dry-run** by default — flip `dry_run: false` in config only
  after watching logs.
- Discord notifications for every action, success, and failure.

## Runtime

- Single Python script (`autoscaler.py`), systemd `oneshot` service
  triggered every 2 minutes by a systemd timer.
- Installed to `/opt/k8s-autoscaler`, config at `/etc/k8s-autoscaler/`,
  state (streak counters, cooldown timestamp) at `/var/lib/k8s-autoscaler/`.
- Runs as user `joyson` on `10.0.1.40` (same host as the alert-receiver
  and k8s-upgrade GitHub Actions runner).

## Install

```bash
git clone https://github.com/joyson-fernandes/k8s-autoscaler ~/k8s-autoscaler
cd ~/k8s-autoscaler
./install.sh
```

Then follow the steps printed by `install.sh`:
1. Set `discord_webhook` in `/etc/k8s-autoscaler/config.yaml`.
2. Apply the Terraform patch described in `terraform/README.md` to
   your `k8s-cluster` repo.
3. Tail logs: `journalctl -u k8s-autoscaler.service -f`.
4. When the dry-run output looks sane, flip `dry_run: false`.

## Config

See `config.yaml` — everything is tunable:
- `min_nodes` / `max_nodes` — pool bounds (default 0–5).
- `scale_up_consecutive` / `scale_down_consecutive` — streak length
  required before acting (ticks, not minutes).
- `scale_down_threshold_pct` — utilization below which scale-down is
  considered.
- `cooldown_seconds` — lockout window after any action.

## Architecture

```
┌──────────────┐   tick every 2 min   ┌───────────────────────────┐
│ systemd      ├─────────────────────►│ autoscaler.py             │
│ timer        │                      │                           │
└──────────────┘                      │ 1. cluster_healthy?       │
                                      │ 2. count_unschedulable    │
                                      │ 3. request_utilization    │
                                      │ 4. update streaks         │
                                      │ 5. scale_up / scale_down  │
                                      └────────────┬──────────────┘
                                                   │
                         ┌─────────────────────────┼─────────────────────┐
                         ▼                         ▼                     ▼
                  edit tfvars            terraform apply      ansible-playbook
                                         (clone VM)           (kubeadm join)
```

## Why not Karpenter?

Karpenter needs a cloud provider API (AWS EC2, Azure VMSS). The CAPI
provider exists but is alpha. This script is ~500 lines, uses the
infrastructure I already have (Terraform + Ansible + vSphere), and
solves the only real problem for a homelab — "sometimes I need +1 node."
