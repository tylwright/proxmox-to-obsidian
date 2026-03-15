#!/usr/bin/env python3
"""
Proxmox to Obsidian - Sync Proxmox cluster data into Obsidian markdown pages.

Supports multiple clusters, guest agent data, HA/firewall/replication info,
Mermaid diagrams, Dataview dashboards, changelog tracking, and stale page cleanup.
"""

import argparse
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, ChoiceLoader
from proxmoxer import ProxmoxAPI

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_config(config_path: str) -> dict:
    """
    Load and validate the YAML configuration file.
    """
    path = Path(config_path)
    if not path.exists():
        logger.error("Config file not found: %s", config_path)
        logger.info("Copy config.yaml.example to config.yaml and fill in your settings.")
        sys.exit(1)

    with open(path) as f:
        config = yaml.safe_load(f)

    if "obsidian" not in config:
        logger.error("Missing required config section: obsidian")
        sys.exit(1)

    # Normalize single-cluster `proxmox:` key into `clusters:` list
    if "clusters" not in config:
        if "proxmox" in config:
            cluster = dict(config["proxmox"])
            cluster.setdefault("name", "default")
            config["clusters"] = [cluster]
        else:
            logger.error("Config must have either 'clusters:' or 'proxmox:' section")
            sys.exit(1)

    return config


def connect_proxmox(cluster_cfg: dict) -> ProxmoxAPI:
    """
    Connect to the Proxmox API using the configured auth method.
    """
    host = cluster_cfg["host"]
    port = cluster_cfg.get("port", 8006)
    verify_ssl = cluster_cfg.get("verify_ssl", False)
    user = cluster_cfg["user"]
    auth_method = cluster_cfg.get("auth_method", "token")

    if auth_method == "token":
        logger.info("Connecting to %s:%s via API token (%s)", host, port, user)
        return ProxmoxAPI(
            host,
            port=port,
            user=user,
            token_name=cluster_cfg["token_name"],
            token_value=cluster_cfg["token_value"],
            verify_ssl=verify_ssl,
        )
    elif auth_method == "password":
        logger.info("Connecting to %s:%s via password (%s)", host, port, user)
        return ProxmoxAPI(
            host,
            port=port,
            user=user,
            password=cluster_cfg["password"],
            verify_ssl=verify_ssl,
        )
    else:
        logger.error("Unknown auth_method: %s (use 'token' or 'password')", auth_method)
        sys.exit(1)


def format_uptime(seconds) -> str:
    """
    Convert seconds to a human-readable uptime string.
    """
    if not seconds:
        return "N/A"
    seconds = int(seconds)
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    return " ".join(parts) if parts else "< 1m"


def bytes_to_gb(b) -> float:
    """
    Convert bytes to gigabytes, rounded to 1 decimal.
    """
    if not b:
        return 0.0
    return round(int(b) / 1073741824, 1)


def pct(used, total) -> float:
    """
    Calculate percentage, handling zero division.
    """
    if not total:
        return 0.0
    return round((int(used) / int(total)) * 100, 1)


def sanitize_filename(name: str) -> str:
    """
    Remove characters that are invalid in filenames.
    """
    return re.sub(r'[<>:"/\\|?*]', "_", name)


def format_timestamp(ts) -> str:
    """
    Convert a UNIX timestamp to a readable string.
    """
    if not ts:
        return "N/A"
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, OSError):
        return str(ts)


# ---------------------------------------------------------------------------
# Config parsers
# ---------------------------------------------------------------------------


def parse_vm_disk(key: str, value: str) -> dict | None:
    """
    Parse a VM disk config entry like 'scsi0: local-lvm:vm-100-disk-0,size=32G'.
    """
    # Exclude non-disk keys like scsihw
    if key in ("scsihw",):
        return None
    disk_prefixes = ("scsi", "sata", "ide", "virtio", "efidisk", "tpmstate")
    if not any(key.startswith(p) for p in disk_prefixes):
        return None
    if not isinstance(value, str):
        return None

    parts = value.split(",")
    storage_volume = parts[0]
    storage = storage_volume.split(":")[0] if ":" in storage_volume else "N/A"
    size = "N/A"
    fmt = "N/A"
    for part in parts[1:]:
        if part.startswith("size="):
            size = part.split("=", 1)[1]
        if part.startswith("format="):
            fmt = part.split("=", 1)[1]

    return {"bus": key, "storage": storage, "size": size, "format": fmt}


def parse_vm_net(key: str, value: str) -> dict | None:
    """
    Parse a VM network config entry like 'net0: virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0'.
    """
    if not key.startswith("net"):
        return None
    if not isinstance(value, str):
        return None

    result = {"device": key, "model": "N/A", "mac": "N/A", "bridge": "N/A", "firewall": "N/A"}
    parts = value.split(",")

    first = parts[0]
    if "=" in first:
        model, mac = first.split("=", 1)
        result["model"] = model
        result["mac"] = mac

    for part in parts[1:]:
        if part.startswith("bridge="):
            result["bridge"] = part.split("=", 1)[1]
        elif part.startswith("firewall="):
            result["firewall"] = "Yes" if part.split("=", 1)[1] == "1" else "No"

    return result


def parse_ct_net(key: str, value: str) -> dict | None:
    """
    Parse a container network config entry.
    """
    if not key.startswith("net"):
        return None
    if not isinstance(value, str):
        return None

    result = {"name": key, "bridge": "N/A", "ip": "N/A", "gw": "N/A", "hwaddr": "N/A"}
    for part in value.split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k == "bridge":
            result["bridge"] = v
        elif k == "ip":
            result["ip"] = v
        elif k == "gw":
            result["gw"] = v
        elif k == "hwaddr":
            result["hwaddr"] = v
        elif k == "name":
            result["name"] = f"{key} ({v})"

    return result


def parse_ct_mountpoint(key: str, value: str) -> dict | None:
    """
    Parse a container mountpoint config entry.
    """
    if key != "rootfs" and not key.startswith("mp"):
        return None
    if not isinstance(value, str):
        return None

    result = {"id": key, "volume": "N/A", "size": "N/A", "mp": "N/A"}
    parts = value.split(",")
    result["volume"] = parts[0]
    for part in parts[1:]:
        if part.startswith("size="):
            result["size"] = part.split("=", 1)[1]
        elif part.startswith("mp="):
            result["mp"] = part.split("=", 1)[1]

    return result


# ---------------------------------------------------------------------------
# Mermaid diagram generators
# ---------------------------------------------------------------------------


def build_cluster_mermaid(nodes_data: list, vms_by_node: dict, cts_by_node: dict) -> str:
    """
    Build a Mermaid diagram showing the full cluster topology.
    """
    lines = ["graph TD"]
    lines.append('    CLUSTER["Proxmox Cluster"]')

    for node in nodes_data:
        name = node["node"]
        status_icon = "✅" if node.get("status") == "online" else "⚠️"
        node_id = re.sub(r"[^a-zA-Z0-9]", "_", name)
        lines.append(f'    {node_id}["{status_icon} {name}"]')
        lines.append(f"    CLUSTER --> {node_id}")

        for vm in vms_by_node.get(name, []):
            vm_name = vm.get("name", f"vm-{vm['vmid']}")
            vm_id = f"vm_{vm['vmid']}"
            icon = "🟢" if vm.get("status") == "running" else "🔴"
            lines.append(f'    {vm_id}["{icon} {vm_name}"]')
            lines.append(f"    {node_id} --> {vm_id}")

        for ct in cts_by_node.get(name, []):
            ct_name = ct.get("name", f"ct-{ct['vmid']}")
            ct_id = f"ct_{ct['vmid']}"
            icon = "🟢" if ct.get("status") == "running" else "🔴"
            lines.append(f'    {ct_id}["{icon} {ct_name}"]')
            lines.append(f"    {node_id} --> {ct_id}")

    return "\n".join(lines)


def build_node_mermaid(node_name: str, vms: list, containers: list) -> str:
    """
    Build a Mermaid diagram for a single node's guests.
    """
    node_id = re.sub(r"[^a-zA-Z0-9]", "_", node_name)
    lines = ["graph TD"]
    lines.append(f'    {node_id}["{node_name}"]')

    if vms:
        lines.append(f'    {node_id}_vms["Virtual Machines"]')
        lines.append(f"    {node_id} --> {node_id}_vms")
        for vm in vms:
            vm_name = vm.get("name", f"vm-{vm['vmid']}")
            vm_id = f"vm_{vm['vmid']}"
            icon = "🟢" if vm.get("status") == "running" else "🔴"
            lines.append(f'    {vm_id}["{icon} {vm_name}"]')
            lines.append(f"    {node_id}_vms --> {vm_id}")

    if containers:
        lines.append(f'    {node_id}_cts["Containers"]')
        lines.append(f"    {node_id} --> {node_id}_cts")
        for ct in containers:
            ct_name = ct.get("name", f"ct-{ct['vmid']}")
            ct_id = f"ct_{ct['vmid']}"
            icon = "🟢" if ct.get("status") == "running" else "🔴"
            lines.append(f'    {ct_id}["{icon} {ct_name}"]')
            lines.append(f"    {node_id}_cts --> {ct_id}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main syncer
# ---------------------------------------------------------------------------


class ProxmoxToObsidian:
    """
    Fetches Proxmox data and writes Obsidian markdown pages.
    """

    def __init__(self, config: dict, cluster_cfg: dict):
        self.config = config
        self.cluster_cfg = cluster_cfg
        self.cluster_name = cluster_cfg.get("name", "default")
        self.proxmox = connect_proxmox(cluster_cfg)
        self.sync_config = config.get("sync", {})
        self.options = config.get("options", {})
        self.synced_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.dry_run = False

        obsidian = config["obsidian"]
        self.vault_path = Path(obsidian["vault_path"])
        self.base_folder = obsidian.get("base_folder", "Proxmox")

        # Multi-cluster: each cluster gets its own subfolder
        num_clusters = len(config.get("clusters", []))
        if num_clusters > 1:
            self.output_path = self.vault_path / self.base_folder / sanitize_filename(self.cluster_name)
        else:
            self.output_path = self.vault_path / self.base_folder

        # Jinja2 template loading: custom templates override built-in
        loaders = []
        custom_dir = self.options.get("custom_templates_dir", "")
        if custom_dir and Path(custom_dir).is_dir():
            loaders.append(FileSystemLoader(custom_dir))
        loaders.append(FileSystemLoader(str(Path(__file__).parent / "templates")))
        self.jinja_env = Environment(
            loader=ChoiceLoader(loaders),
            trim_blocks=True,
            lstrip_blocks=True,
        )

        # Track written files for stale cleanup & changelog
        self.written_files: set[Path] = set()
        self.changes: list[dict] = []

        # Cache node data to avoid redundant API calls
        self._nodes_cache = None
        self._vms_by_node: dict[str, list] = {}
        self._cts_by_node: dict[str, list] = {}

    def _get_nodes(self) -> list:
        """
        Get and cache the node list.
        """
        if self._nodes_cache is None:
            self._nodes_cache = self.proxmox.nodes.get()
        return self._nodes_cache

    def _get_vms_for_node(self, node_name: str) -> list:
        """
        Get and cache VMs for a node.
        """
        if node_name not in self._vms_by_node:
            try:
                self._vms_by_node[node_name] = self.proxmox.nodes(node_name).qemu.get()
            except Exception as e:
                logger.warning("  Could not fetch VMs for %s: %s", node_name, e)
                self._vms_by_node[node_name] = []
        return self._vms_by_node[node_name]

    def _get_cts_for_node(self, node_name: str) -> list:
        """
        Get and cache containers for a node.
        """
        if node_name not in self._cts_by_node:
            try:
                self._cts_by_node[node_name] = self.proxmox.nodes(node_name).lxc.get()
            except Exception as e:
                logger.warning("  Could not fetch CTs for %s: %s", node_name, e)
                self._cts_by_node[node_name] = []
        return self._cts_by_node[node_name]

    def _write_page(self, subfolder: str, filename: str, template_name: str, context: dict):
        """
        Render a Jinja2 template and write it to the appropriate folder.
        Tracks the file for stale cleanup and changelog diff.
        """
        folder = self.output_path / subfolder
        safe_name = sanitize_filename(filename)
        filepath = folder / f"{safe_name}.md"

        template = self.jinja_env.get_template(template_name)
        new_content = template.render(synced_at=self.synced_at, **context)

        # Changelog: detect changes
        action = "added"
        if filepath.exists():
            old_content = filepath.read_text()
            if old_content.strip() == new_content.strip():
                self.written_files.add(filepath)
                return  # No change, skip write
            action = "changed"

        if self.dry_run:
            logger.info("[DRY RUN] Would %s: %s", action, filepath)
            self.written_files.add(filepath)
            self.changes.append({
                "action": action,
                "resource_type": subfolder,
                "name": filename,
                "details": f"Would be {action}",
            })
            return

        folder.mkdir(parents=True, exist_ok=True)
        filepath.write_text(new_content)
        self.written_files.add(filepath)
        self.changes.append({
            "action": action,
            "resource_type": subfolder,
            "name": filename,
            "details": f"File {action}",
        })
        logger.debug("Wrote (%s): %s", action, filepath)

    # ------------------------------------------------------------------
    # Fetch helpers for new data types
    # ------------------------------------------------------------------

    def _fetch_guest_agent(self, node_name: str, vmid: int) -> dict:
        """
        Fetch guest agent info (network interfaces + OS info) for a running VM.
        Returns dict with 'interfaces' (flattened for templates) and 'os_info' keys.
        """
        result = {"interfaces": [], "os_info": {}}

        try:
            ifaces = self.proxmox.nodes(node_name).qemu(vmid).agent("network-get-interfaces").get()
            raw = ifaces.get("result", [])
            # Flatten interface data for Jinja2 templates
            for iface in raw:
                ips = []
                for addr in iface.get("ip-addresses", []):
                    ip = addr.get("ip-address", "")
                    if ip:
                        ips.append(ip)
                result["interfaces"].append({
                    "name": iface.get("name", "N/A"),
                    "ips": ", ".join(ips) if ips else "N/A",
                    "mac": iface.get("hardware-address", "N/A"),
                })
        except Exception:
            pass

        try:
            osinfo = self.proxmox.nodes(node_name).qemu(vmid).agent("get-osinfo").get()
            result["os_info"] = osinfo.get("result", {})
        except Exception:
            pass

        return result

    def _fetch_firewall_rules(self, resource_path) -> list:
        """
        Fetch firewall rules for a given API resource path.
        """
        try:
            rules = resource_path.firewall.rules.get()
            return rules if isinstance(rules, list) else []
        except Exception:
            return []

    def _extract_ip_from_config(self, config: dict) -> str:
        """
        Try to extract an IP address from container network config.
        """
        for key, value in config.items():
            if key.startswith("net") and isinstance(value, str):
                for part in value.split(","):
                    if part.startswith("ip="):
                        ip = part.split("=", 1)[1]
                        return ip.split("/")[0] if "/" in ip else ip
        return ""

    def _extract_ip_from_agent(self, guest_agent: dict) -> str:
        """
        Extract the first non-loopback IPv4 address from guest agent data.
        """
        for iface in guest_agent.get("interfaces", []):
            if iface.get("name") == "lo":
                continue
            for addr in iface.get("ip-addresses", []):
                if addr.get("ip-address-type") == "ipv4":
                    ip = addr.get("ip-address", "")
                    if ip and not ip.startswith("127."):
                        return ip
        return ""

    # ------------------------------------------------------------------
    # Sync methods
    # ------------------------------------------------------------------

    def sync_cluster(self):
        """
        Sync cluster-level information including HA and replication.
        """
        if not self.sync_config.get("cluster", True):
            return

        logger.info("Syncing cluster info...")
        try:
            status = self.proxmox.cluster.status.get()
        except Exception as e:
            logger.warning("Could not fetch cluster status: %s", e)
            return

        cluster_info = {}
        members = []
        for item in status:
            if item.get("type") == "cluster":
                cluster_info = item
            elif item.get("type") == "node":
                members.append(item)

        resources_raw = self.proxmox.cluster.resources.get()
        resource_counts = {}
        for r in resources_raw:
            rtype = r.get("type", "unknown")
            resource_counts[rtype] = resource_counts.get(rtype, 0) + 1

        # HA groups and resources
        ha_groups = []
        ha_resources = []
        if self.sync_config.get("ha", True):
            try:
                ha_groups = self.proxmox.cluster.ha.groups.get()
            except Exception:
                pass
            try:
                ha_resources = self.proxmox.cluster.ha.resources.get()
            except Exception:
                pass

        # Replication
        replication_jobs = []
        if self.sync_config.get("replication", True):
            try:
                replication_jobs = self.proxmox.cluster.replication.get()
            except Exception:
                pass

        # Mermaid diagram
        nodes = self._get_nodes()
        for node in nodes:
            self._get_vms_for_node(node["node"])
            self._get_cts_for_node(node["node"])
        mermaid_diagram = build_cluster_mermaid(nodes, self._vms_by_node, self._cts_by_node)

        self._write_page(
            "Cluster",
            "Cluster Overview",
            "cluster.md.j2",
            {
                "cluster": cluster_info,
                "members": members,
                "resources": resource_counts,
                "ha_groups": ha_groups,
                "ha_resources": ha_resources,
                "replication_jobs": replication_jobs,
                "mermaid_diagram": mermaid_diagram,
            },
        )

    def sync_nodes(self):
        """
        Sync node information with firewall rules and Mermaid diagrams.
        """
        if not self.sync_config.get("nodes", True):
            return

        logger.info("Syncing nodes...")
        nodes = self._get_nodes()

        for node in nodes:
            node_name = node["node"]
            logger.info("  Node: %s", node_name)

            try:
                node_status = self.proxmox.nodes(node_name).status.get()
            except Exception:
                node_status = {}

            vms = self._get_vms_for_node(node_name)
            containers = self._get_cts_for_node(node_name)

            storage = []
            networks = []
            try:
                storage = self.proxmox.nodes(node_name).storage.get()
            except Exception as e:
                logger.warning("  Could not fetch storage for %s: %s", node_name, e)
            try:
                networks = self.proxmox.nodes(node_name).network.get()
            except Exception as e:
                logger.warning("  Could not fetch networks for %s: %s", node_name, e)

            # Firewall rules
            firewall_rules = []
            if self.sync_config.get("firewall", True):
                firewall_rules = self._fetch_firewall_rules(self.proxmox.nodes(node_name))

            # Mermaid
            mermaid_diagram = build_node_mermaid(node_name, vms, containers)

            mem_used = node.get("mem", 0)
            mem_total = node.get("maxmem", 0)
            disk_used = node.get("disk", 0)
            disk_total = node.get("maxdisk", 0)

            self._write_page(
                "Nodes",
                node_name,
                "node.md.j2",
                {
                    "node": node,
                    "node_status": node_status,
                    "uptime_str": format_uptime(node.get("uptime", 0)),
                    "mem_used_gb": bytes_to_gb(mem_used),
                    "mem_total_gb": bytes_to_gb(mem_total),
                    "mem_pct": pct(mem_used, mem_total),
                    "disk_used_gb": bytes_to_gb(disk_used),
                    "disk_total_gb": bytes_to_gb(disk_total),
                    "disk_pct": pct(disk_used, disk_total),
                    "vms": vms,
                    "containers": containers,
                    "storage": storage,
                    "networks": networks,
                    "firewall_rules": firewall_rules,
                    "mermaid_diagram": mermaid_diagram,
                },
            )

    def sync_vms(self):
        """
        Sync virtual machine information with guest agent and firewall data.
        """
        if not self.sync_config.get("vms", True):
            return

        logger.info("Syncing VMs...")
        nodes = self._get_nodes()

        for node_info in nodes:
            node_name = node_info["node"]
            vms = self._get_vms_for_node(node_name)

            for vm in vms:
                vmid = vm["vmid"]
                vm_name = vm.get("name", f"vm-{vmid}")
                logger.info("  VM %s: %s (%s)", vmid, vm_name, node_name)

                try:
                    config = self.proxmox.nodes(node_name).qemu(vmid).config.get()
                except Exception:
                    config = {}

                try:
                    snapshots = self.proxmox.nodes(node_name).qemu(vmid).snapshot.get()
                    snapshots = [s for s in snapshots if s.get("name") != "current"]
                except Exception:
                    snapshots = []

                disks = []
                networks = []
                for key, value in config.items():
                    disk = parse_vm_disk(key, value)
                    if disk:
                        disks.append(disk)
                    nic = parse_vm_net(key, value)
                    if nic:
                        networks.append(nic)

                # Guest agent data (only for running VMs)
                guest_agent = {"interfaces": [], "os_info": {}}
                if vm.get("status") == "running":
                    guest_agent = self._fetch_guest_agent(node_name, vmid)

                # Firewall
                firewall_rules = []
                if self.sync_config.get("firewall", True):
                    firewall_rules = self._fetch_firewall_rules(
                        self.proxmox.nodes(node_name).qemu(vmid)
                    )

                # Extract IP for frontmatter
                ip_address = self._extract_ip_from_agent(guest_agent)

                vm["node"] = node_name
                page_name = f"{vmid} - {vm_name}"

                self._write_page(
                    "VMs",
                    page_name,
                    "vm.md.j2",
                    {
                        "vm": vm,
                        "node": node_name,
                        "config": config,
                        "uptime_str": format_uptime(vm.get("uptime", 0)),
                        "disks": disks,
                        "networks": networks,
                        "snapshots": snapshots,
                        "guest_agent": guest_agent,
                        "firewall_rules": firewall_rules,
                        "ip_address": ip_address,
                    },
                )

    def sync_containers(self):
        """
        Sync LXC container information with firewall data.
        """
        if not self.sync_config.get("containers", True):
            return

        logger.info("Syncing containers...")
        nodes = self._get_nodes()

        for node_info in nodes:
            node_name = node_info["node"]
            containers = self._get_cts_for_node(node_name)

            for ct in containers:
                vmid = ct["vmid"]
                ct_name = ct.get("name", f"ct-{vmid}")
                logger.info("  CT %s: %s (%s)", vmid, ct_name, node_name)

                try:
                    config = self.proxmox.nodes(node_name).lxc(vmid).config.get()
                except Exception:
                    config = {}

                try:
                    snapshots = self.proxmox.nodes(node_name).lxc(vmid).snapshot.get()
                    snapshots = [s for s in snapshots if s.get("name") != "current"]
                except Exception:
                    snapshots = []

                networks = []
                mountpoints = []
                for key, value in config.items():
                    nic = parse_ct_net(key, value)
                    if nic:
                        networks.append(nic)
                    mp = parse_ct_mountpoint(key, value)
                    if mp:
                        mountpoints.append(mp)

                # Firewall
                firewall_rules = []
                if self.sync_config.get("firewall", True):
                    firewall_rules = self._fetch_firewall_rules(
                        self.proxmox.nodes(node_name).lxc(vmid)
                    )

                # Extract IP for frontmatter
                ip_address = self._extract_ip_from_config(config)

                ct["node"] = node_name
                page_name = f"{vmid} - {ct_name}"

                self._write_page(
                    "Containers",
                    page_name,
                    "container.md.j2",
                    {
                        "ct": ct,
                        "node": node_name,
                        "config": config,
                        "uptime_str": format_uptime(ct.get("uptime", 0)),
                        "networks": networks,
                        "mountpoints": mountpoints,
                        "snapshots": snapshots,
                        "firewall_rules": firewall_rules,
                        "ip_address": ip_address,
                    },
                )

    def sync_storage(self):
        """
        Sync storage information.
        """
        if not self.sync_config.get("storage", True):
            return

        logger.info("Syncing storage...")
        try:
            storages = self.proxmox.storage.get()
        except Exception as e:
            logger.warning("Could not fetch storage: %s", e)
            return

        nodes = self._get_nodes()

        for storage in storages:
            storage_name = storage["storage"]
            logger.info("  Storage: %s", storage_name)

            nodes_usage = []
            for node_info in nodes:
                node_name = node_info["node"]
                try:
                    node_storages = self.proxmox.nodes(node_name).storage.get()
                    for ns in node_storages:
                        if ns.get("storage") == storage_name:
                            used = ns.get("used", 0)
                            total = ns.get("total", 0)
                            nodes_usage.append({
                                "node": node_name,
                                "used": used,
                                "total": total,
                                "pct": pct(used, total),
                            })
                            break
                except Exception:
                    pass

            self._write_page(
                "Storage",
                storage_name,
                "storage.md.j2",
                {"storage": storage, "nodes_usage": nodes_usage},
            )

    def sync_networks(self):
        """
        Sync network interface information.
        """
        if not self.sync_config.get("networks", True):
            return

        logger.info("Syncing networks...")
        nodes = self._get_nodes()

        for node_info in nodes:
            node_name = node_info["node"]
            try:
                networks = self.proxmox.nodes(node_name).network.get()
            except Exception as e:
                logger.warning("  Could not fetch networks for %s: %s", node_name, e)
                continue

            for net in networks:
                iface = net.get("iface", "unknown")
                net_type = net.get("type", "")
                if net_type not in ("bridge", "bond", "vlan", "OVSBridge", "OVSBond"):
                    continue

                logger.info("  Network: %s (%s)", iface, node_name)
                self._write_page(
                    "Networks",
                    f"{node_name} - {iface}",
                    "network.md.j2",
                    {"net": net, "node": node_name},
                )

    def sync_pools(self):
        """
        Sync resource pool information.
        """
        if not self.sync_config.get("pools", True):
            return

        logger.info("Syncing pools...")
        try:
            pools = self.proxmox.pools.get()
        except Exception as e:
            logger.warning("Could not fetch pools: %s", e)
            return

        for pool in pools:
            pool_id = pool["poolid"]
            logger.info("  Pool: %s", pool_id)

            try:
                pool_detail = self.proxmox.pools(pool_id).get()
            except Exception:
                pool_detail = pool

            members = []
            storage_members = []
            for member in pool_detail.get("members", []):
                if member.get("type") == "storage":
                    storage_members.append(member)
                else:
                    members.append(member)

            self._write_page(
                "Pools",
                pool_id,
                "pool.md.j2",
                {"pool": pool_detail, "members": members, "storage_members": storage_members},
            )

    def sync_backups(self):
        """
        Sync backup job information.
        """
        if not self.sync_config.get("backups", True):
            return

        logger.info("Syncing backup jobs...")
        try:
            jobs = self.proxmox.cluster.backup.get()
        except Exception as e:
            logger.warning("Could not fetch backup jobs: %s", e)
            return

        for job in jobs:
            job_id = job.get("id", "unknown")
            logger.info("  Backup job: %s", job_id)
            self._write_page(
                "Backups",
                f"Backup - {job_id}",
                "backup.md.j2",
                {"job": job},
            )

    def sync_tasks(self):
        """
        Sync recent cluster tasks.
        """
        if not self.sync_config.get("tasks", True):
            return

        logger.info("Syncing recent tasks...")
        try:
            raw_tasks = self.proxmox.cluster.tasks.get()
        except Exception as e:
            logger.warning("Could not fetch tasks: %s", e)
            return

        tasks = []
        for t in raw_tasks[:100]:  # Limit to last 100 tasks
            tasks.append({
                "node": t.get("node", "N/A"),
                "type": t.get("type", "N/A"),
                "user": t.get("user", "N/A"),
                "status": t.get("status", "N/A"),
                "starttime_str": format_timestamp(t.get("starttime")),
                "endtime_str": format_timestamp(t.get("endtime")),
                "upid": t.get("upid", ""),
                "description": t.get("type", ""),
            })

        self._write_page(
            "Cluster",
            "Recent Tasks",
            "tasks.md.j2",
            {"tasks": tasks},
        )

    def sync_dashboard(self):
        """
        Generate the Dataview dashboard / MOC page.
        """
        if not self.sync_config.get("dashboard", True):
            return

        logger.info("Generating dashboard...")
        nodes = self._get_nodes()

        node_summaries = []
        total_cpu = 0
        total_mem = 0
        used_mem = 0

        for node in nodes:
            cpu_count = node.get("maxcpu", 0)
            total_cpu += cpu_count
            mem_total = node.get("maxmem", 0)
            mem_used = node.get("mem", 0)
            total_mem += mem_total
            used_mem += mem_used

            node_summaries.append({
                "name": node["node"],
                "status": node.get("status", "unknown"),
                "cpu": f"{node.get('cpu', 0) * 100:.1f}%" if node.get("cpu") else "N/A",
                "mem_pct": pct(mem_used, mem_total),
                "disk_pct": pct(node.get("disk", 0), node.get("maxdisk", 0)),
            })

        # Aggregate VM/CT status counts
        vms_by_status = {}
        cts_by_status = {}
        total_vms = 0
        total_cts = 0

        for node in nodes:
            node_name = node["node"]
            for vm in self._get_vms_for_node(node_name):
                total_vms += 1
                s = vm.get("status", "unknown")
                vms_by_status[s] = vms_by_status.get(s, 0) + 1
            for ct in self._get_cts_for_node(node_name):
                total_cts += 1
                s = ct.get("status", "unknown")
                cts_by_status[s] = cts_by_status.get(s, 0) + 1

        # Mermaid overview
        mermaid_overview = build_cluster_mermaid(nodes, self._vms_by_node, self._cts_by_node)

        self._write_page(
            "",
            "Dashboard",
            "dashboard.md.j2",
            {
                "nodes": node_summaries,
                "vms_by_status": vms_by_status,
                "cts_by_status": cts_by_status,
                "total_vms": total_vms,
                "total_cts": total_cts,
                "total_cpu": total_cpu,
                "total_mem_gb": bytes_to_gb(total_mem),
                "used_mem_gb": bytes_to_gb(used_mem),
                "cluster_name": self.cluster_name,
                "mermaid_overview": mermaid_overview,
            },
        )

    def sync_changelog(self):
        """
        Write the changelog page summarizing what changed in this sync.
        """
        if not self.changes:
            logger.info("No changes detected.")
            return

        self._write_page(
            "Cluster",
            "Changelog",
            "changelog.md.j2",
            {"changes": self.changes, "sync_time": self.synced_at},
        )

    def cleanup_stale(self):
        """
        Remove markdown files in the output path that were not written during this sync.
        """
        if not self.options.get("cleanup_stale", False):
            return
        if self.dry_run:
            logger.info("[DRY RUN] Would check for stale pages...")

        logger.info("Cleaning up stale pages...")
        removed = 0
        for md_file in self.output_path.rglob("*.md"):
            if md_file not in self.written_files:
                if self.dry_run:
                    logger.info("[DRY RUN] Would remove: %s", md_file)
                else:
                    md_file.unlink()
                    logger.info("  Removed stale: %s", md_file)
                self.changes.append({
                    "action": "removed",
                    "resource_type": md_file.parent.name,
                    "name": md_file.stem,
                    "details": "Resource no longer exists in Proxmox",
                })
                removed += 1

        if removed:
            logger.info("Removed %d stale page(s).", removed)

    def sync_all(self):
        """
        Run all sync operations.
        """
        logger.info("Starting sync for cluster '%s' to: %s", self.cluster_name, self.output_path)
        if not self.dry_run:
            self.output_path.mkdir(parents=True, exist_ok=True)

        self.sync_cluster()
        self.sync_nodes()
        self.sync_vms()
        self.sync_containers()
        self.sync_storage()
        self.sync_networks()
        self.sync_pools()
        self.sync_backups()
        self.sync_tasks()
        self.sync_dashboard()

        # Cleanup stale pages before changelog so removals are included
        self.cleanup_stale()

        # Changelog is always written last
        self.sync_changelog()

        logger.info("Sync complete for cluster '%s'!", self.cluster_name)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


ALL_SYNC_TYPES = [
    "cluster", "nodes", "vms", "containers", "storage",
    "networks", "pools", "backups", "tasks", "dashboard",
    "ha", "firewall", "replication",
]


def main():
    parser = argparse.ArgumentParser(
        description="Sync Proxmox cluster data to Obsidian markdown pages"
    )
    parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        help="Path to config file (default: config.yaml)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Suppress non-error output",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without writing files",
    )
    parser.add_argument(
        "--only",
        choices=["cluster", "nodes", "vms", "containers", "storage", "networks", "pools", "backups", "tasks", "dashboard"],
        help="Only sync a specific resource type",
    )
    parser.add_argument(
        "--cluster",
        help="Only sync a specific cluster by name (for multi-cluster configs)",
    )
    args = parser.parse_args()

    # Determine log level: quiet > verbose > default
    if args.quiet:
        log_level = logging.ERROR
    elif args.verbose:
        log_level = logging.DEBUG
    else:
        log_level = logging.INFO

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    config = load_config(args.config)

    # Apply config-level quiet option
    if config.get("options", {}).get("quiet", False) and not args.verbose:
        logging.getLogger().setLevel(logging.ERROR)

    clusters = config["clusters"]
    if args.cluster:
        clusters = [c for c in clusters if c.get("name") == args.cluster]
        if not clusters:
            logger.error("No cluster named '%s' found in config", args.cluster)
            sys.exit(1)

    for cluster_cfg in clusters:
        syncer = ProxmoxToObsidian(config, cluster_cfg)
        syncer.dry_run = args.dry_run

        if args.only:
            sync_method = getattr(syncer, f"sync_{args.only}", None)
            if sync_method:
                if not syncer.dry_run:
                    syncer.output_path.mkdir(parents=True, exist_ok=True)
                sync_method()
                syncer.sync_changelog()
            else:
                logger.error("Unknown sync type: %s", args.only)
                sys.exit(1)
        else:
            syncer.sync_all()


if __name__ == "__main__":
    main()
