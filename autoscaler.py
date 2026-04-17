#!/usr/bin/env python3
"""
k8s-autoscaler — Karpenter-lite for a vSphere-backed bare-metal cluster.

- Watches for Pending pods that can't schedule due to Insufficient cpu/memory.
- Scales an "autoscale pool" of workers (A-pool) up/down by editing Terraform
  and running the existing Ansible join playbook.
- Touches only the A-pool (JOY-K8S-A01..A05); static workers (W01..W05) are
  never drained or destroyed.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

LOG = logging.getLogger("autoscaler")

STATE_FILE = Path("/var/lib/k8s-autoscaler/state.json")
DEFAULT_CONFIG = Path("/etc/k8s-autoscaler/config.yaml")


@dataclass
class Config:
    min_nodes: int = 0
    max_nodes: int = 5
    scale_up_consecutive: int = 3          # 3 checks * 2 min = 6 min of pending
    scale_down_consecutive: int = 30       # 30 checks * 2 min = 60 min of low util
    scale_down_threshold_pct: float = 30.0
    cooldown_seconds: int = 900            # 15 min between any action
    pool_name_prefix: str = "JOY-K8S-A"
    pool_ip_base: str = "10.0.1.80"        # A01 = .80, A02 = .81, ...
    pool_netmask: int = 24
    terraform_dir: str = "/home/joyson/k8s-cluster"
    ansible_dir: str = "/home/joyson/k8s-cluster/ansible"
    tfvars_file: str = "autoscale.auto.tfvars"
    join_playbook: str = "join-autoscale-worker.yaml"
    discord_webhook: str = ""
    kubectl: str = "/usr/bin/kubectl"
    terraform: str = "/usr/bin/terraform"
    ansible_playbook: str = "/usr/bin/ansible-playbook"
    dry_run: bool = True


@dataclass
class State:
    pending_streak: int = 0
    underutil_streak: int = 0
    last_action_ts: float = 0.0
    history: list = field(default_factory=list)

    @classmethod
    def load(cls) -> "State":
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text())
            return cls(**data)
        return cls()

    def save(self) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(self.__dict__, indent=2))


def load_config(path: Path) -> Config:
    if not path.exists():
        LOG.warning("config %s not found, using defaults", path)
        return Config()
    data = yaml.safe_load(path.read_text()) or {}
    return Config(**data)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    LOG.debug("exec: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kw)


def kubectl_json(cfg: Config, *args: str) -> dict:
    cp = run([cfg.kubectl, *args, "-o", "json"])
    if cp.returncode != 0:
        raise RuntimeError(f"kubectl failed: {cp.stderr}")
    return json.loads(cp.stdout)


def discord(cfg: Config, msg: str) -> None:
    if not cfg.discord_webhook:
        return
    try:
        import urllib.request
        data = json.dumps({"content": msg}).encode()
        req = urllib.request.Request(
            cfg.discord_webhook, data=data,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        LOG.warning("discord post failed: %s", e)


# ---------- signal collection ----------

def cluster_healthy(cfg: Config) -> bool:
    try:
        nodes = kubectl_json(cfg, "get", "nodes")
    except Exception:
        return False
    for n in nodes["items"]:
        ready = next(
            (c for c in n["status"].get("conditions", []) if c["type"] == "Ready"),
            None,
        )
        if not ready or ready["status"] != "True":
            return False
    return True


def count_unschedulable(cfg: Config) -> int:
    pods = kubectl_json(cfg, "get", "pods", "-A", "--field-selector=status.phase=Pending")
    count = 0
    for p in pods["items"]:
        for c in p.get("status", {}).get("conditions", []):
            if c.get("reason") == "Unschedulable" and "Insufficient" in (c.get("message") or ""):
                count += 1
                break
    return count


def workers_all(cfg: Config) -> list[dict]:
    nodes = kubectl_json(cfg, "get", "nodes")
    out = []
    for n in nodes["items"]:
        labels = n["metadata"].get("labels", {})
        if "node-role.kubernetes.io/control-plane" in labels:
            continue
        out.append(n)
    return out


def pool_nodes(cfg: Config) -> list[dict]:
    return [n for n in workers_all(cfg) if n["metadata"]["name"].startswith(cfg.pool_name_prefix)]


def request_utilization(cfg: Config) -> float:
    """Return fleet-wide pct of (sum of cpu+mem requests) / (sum of allocatable)."""
    nodes = workers_all(cfg)
    if not nodes:
        return 0.0
    pods = kubectl_json(cfg, "get", "pods", "-A", "--field-selector=status.phase=Running")

    alloc_cpu = 0.0
    alloc_mem = 0.0
    for n in nodes:
        alloc_cpu += parse_cpu(n["status"]["allocatable"].get("cpu", "0"))
        alloc_mem += parse_mem(n["status"]["allocatable"].get("memory", "0"))

    req_cpu = 0.0
    req_mem = 0.0
    worker_names = {n["metadata"]["name"] for n in nodes}
    for p in pods["items"]:
        if p["spec"].get("nodeName") not in worker_names:
            continue
        for c in p["spec"].get("containers", []):
            req = c.get("resources", {}).get("requests", {}) or {}
            req_cpu += parse_cpu(req.get("cpu", "0"))
            req_mem += parse_mem(req.get("memory", "0"))

    if alloc_cpu == 0 or alloc_mem == 0:
        return 0.0
    return max(req_cpu / alloc_cpu, req_mem / alloc_mem) * 100.0


def parse_cpu(v: str) -> float:
    if not v:
        return 0.0
    if v.endswith("m"):
        return float(v[:-1]) / 1000.0
    if v.endswith("n"):
        return float(v[:-1]) / 1e9
    return float(v)


def parse_mem(v: str) -> float:
    if not v:
        return 0.0
    mult = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
            "K": 1000, "M": 1000**2, "G": 1000**3}
    for suffix, m in mult.items():
        if v.endswith(suffix):
            return float(v[: -len(suffix)]) * m
    return float(v)


# ---------- scale up ----------

def next_free_index(cfg: Config) -> int | None:
    """Pick lowest unused index in 1..max_nodes."""
    used = {int(n["metadata"]["name"][len(cfg.pool_name_prefix):])
            for n in pool_nodes(cfg)}
    for i in range(1, cfg.max_nodes + 1):
        if i not in used:
            return i
    return None


def ip_for_index(cfg: Config, idx: int) -> str:
    parts = cfg.pool_ip_base.split(".")
    parts[-1] = str(int(parts[-1]) + idx - 1)
    return ".".join(parts)


def write_tfvars(cfg: Config, indices: list[int]) -> None:
    path = Path(cfg.terraform_dir) / cfg.tfvars_file
    entries = []
    for i in sorted(indices):
        name = f"{cfg.pool_name_prefix}{i:02d}"
        ip = ip_for_index(cfg, i)
        entries.append(
            f'  {{ name = "{name}", ipv4_address = "{ip}", ipv4_netmask = {cfg.pool_netmask} }},'
        )
    content = "autoscale_workers = [\n" + "\n".join(entries) + "\n]\n" if entries else "autoscale_workers = []\n"
    if cfg.dry_run:
        LOG.info("[dry-run] would write %s:\n%s", path, content)
        return
    path.write_text(content)


def terraform_apply(cfg: Config) -> bool:
    if cfg.dry_run:
        LOG.info("[dry-run] would terraform apply in %s", cfg.terraform_dir)
        return True
    cp = run([cfg.terraform, "-chdir=" + cfg.terraform_dir, "apply", "-auto-approve"])
    if cp.returncode != 0:
        LOG.error("terraform apply failed: %s", cp.stderr)
        return False
    return True


def ansible_join(cfg: Config, name: str, ip: str) -> bool:
    if cfg.dry_run:
        LOG.info("[dry-run] would ansible join %s (%s)", name, ip)
        return True
    cp = run([
        cfg.ansible_playbook,
        "-i", f"{cfg.ansible_dir}/inventory.ini",
        f"{cfg.ansible_dir}/{cfg.join_playbook}",
        "-e", f"target_host={name}",
        "-e", f"target_ip={ip}",
    ])
    if cp.returncode != 0:
        LOG.error("ansible join failed: %s", cp.stderr)
        return False
    return True


def wait_node_ready(cfg: Config, name: str, timeout_s: int = 600) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        cp = run([cfg.kubectl, "get", "node", name, "-o",
                  "jsonpath={.status.conditions[?(@.type=='Ready')].status}"])
        if cp.stdout.strip() == "True":
            return True
        time.sleep(10)
    return False


def scale_up(cfg: Config, state: State) -> None:
    idx = next_free_index(cfg)
    if idx is None:
        LOG.info("pool at max (%d); cannot scale up", cfg.max_nodes)
        return
    name = f"{cfg.pool_name_prefix}{idx:02d}"
    ip = ip_for_index(cfg, idx)
    LOG.info("scaling UP: adding %s (%s)", name, ip)
    discord(cfg, f"🔼 **autoscaler** — scaling up: adding `{name}` ({ip})")

    current = sorted({int(n["metadata"]["name"][len(cfg.pool_name_prefix):])
                      for n in pool_nodes(cfg)} | {idx})
    write_tfvars(cfg, current)
    if not terraform_apply(cfg):
        discord(cfg, f"🔴 **autoscaler** — terraform apply failed for `{name}`")
        return
    if not ansible_join(cfg, name, ip):
        discord(cfg, f"🔴 **autoscaler** — ansible join failed for `{name}`")
        return
    if cfg.dry_run or wait_node_ready(cfg, name):
        discord(cfg, f"✅ **autoscaler** — `{name}` Ready and joined")
        state.last_action_ts = time.time()
        state.history.append({"ts": time.time(), "action": "up", "node": name})
    else:
        discord(cfg, f"⚠️ **autoscaler** — `{name}` not Ready after 10min")


# ---------- scale down ----------

def pick_drain_candidate(cfg: Config) -> str | None:
    """Least-loaded A-pool node by request utilization."""
    pool = pool_nodes(cfg)
    if not pool:
        return None
    pods = kubectl_json(cfg, "get", "pods", "-A", "--field-selector=status.phase=Running")
    load: dict[str, float] = {}
    for n in pool:
        alloc_cpu = parse_cpu(n["status"]["allocatable"].get("cpu", "0"))
        req_cpu = 0.0
        for p in pods["items"]:
            if p["spec"].get("nodeName") != n["metadata"]["name"]:
                continue
            for c in p["spec"].get("containers", []):
                req_cpu += parse_cpu((c.get("resources", {}).get("requests") or {}).get("cpu", "0"))
        load[n["metadata"]["name"]] = (req_cpu / alloc_cpu) if alloc_cpu else 0
    return min(load, key=load.get)


def drain_node(cfg: Config, name: str) -> bool:
    if cfg.dry_run:
        LOG.info("[dry-run] would cordon+drain %s", name)
        return True
    run([cfg.kubectl, "cordon", name])
    cp = run([cfg.kubectl, "drain", name, "--ignore-daemonsets",
              "--delete-emptydir-data", "--timeout=300s"])
    if cp.returncode != 0:
        LOG.error("drain failed: %s", cp.stderr)
        return False
    return True


def delete_node(cfg: Config, name: str) -> None:
    if cfg.dry_run:
        LOG.info("[dry-run] would kubectl delete node %s", name)
        return
    run([cfg.kubectl, "delete", "node", name])


def scale_down(cfg: Config, state: State) -> None:
    target = pick_drain_candidate(cfg)
    if not target:
        LOG.info("no A-pool nodes to scale down")
        return
    LOG.info("scaling DOWN: draining %s", target)
    discord(cfg, f"🔽 **autoscaler** — scaling down: draining `{target}`")

    if not drain_node(cfg, target):
        discord(cfg, f"🔴 **autoscaler** — drain failed for `{target}`; aborting")
        return
    delete_node(cfg, target)

    idx = int(target[len(cfg.pool_name_prefix):])
    remaining = sorted({int(n["metadata"]["name"][len(cfg.pool_name_prefix):])
                        for n in pool_nodes(cfg)} - {idx})
    write_tfvars(cfg, remaining)
    if not terraform_apply(cfg):
        discord(cfg, f"🔴 **autoscaler** — terraform destroy failed for `{target}`")
        return
    discord(cfg, f"✅ **autoscaler** — `{target}` removed")
    state.last_action_ts = time.time()
    state.history.append({"ts": time.time(), "action": "down", "node": target})


# ---------- main loop (single pass) ----------

def tick(cfg: Config, state: State) -> None:
    if not cluster_healthy(cfg):
        LOG.warning("cluster unhealthy; skipping")
        return

    now = time.time()
    in_cooldown = (now - state.last_action_ts) < cfg.cooldown_seconds

    pending = count_unschedulable(cfg)
    util = request_utilization(cfg)
    pool_size = len(pool_nodes(cfg))

    LOG.info("tick: pending=%d util=%.1f%% pool=%d streak_up=%d streak_down=%d cooldown=%s",
             pending, util, pool_size, state.pending_streak, state.underutil_streak, in_cooldown)

    if pending > 0:
        state.pending_streak += 1
    else:
        state.pending_streak = 0

    if util < cfg.scale_down_threshold_pct and pool_size > cfg.min_nodes:
        state.underutil_streak += 1
    else:
        state.underutil_streak = 0

    if in_cooldown:
        state.save()
        return

    if state.pending_streak >= cfg.scale_up_consecutive and pool_size < cfg.max_nodes:
        scale_up(cfg, state)
        state.pending_streak = 0
    elif state.underutil_streak >= cfg.scale_down_consecutive and pool_size > cfg.min_nodes:
        scale_down(cfg, state)
        state.underutil_streak = 0

    state.save()


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    cfg_path = Path(os.environ.get("AUTOSCALER_CONFIG", DEFAULT_CONFIG))
    cfg = load_config(cfg_path)
    if os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes"):
        cfg.dry_run = True
    LOG.info("starting autoscaler (dry_run=%s, min=%d, max=%d)",
             cfg.dry_run, cfg.min_nodes, cfg.max_nodes)
    state = State.load()
    tick(cfg, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
