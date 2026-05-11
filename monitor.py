#!/usr/bin/env python3
"""
Dashboard local para monitorar servidor doméstico Linux.

Recursos:
- Disco /mnt/Disk2 e destaque para Disk2_crypt
- CPU, load average, RAM e swap
- Interfaces tailscale0 e wlp1s0 com IP e SSID quando disponível
- Temperaturas via lm-sensors
- Status HTTP de serviços locais
- Links de acesso via IP interno e Tailscale

Instalação recomendada:
  sudo apt update
  sudo apt install -y python3-venv python3-pip lm-sensors wireless-tools iproute2
  python3 -m venv ~/.venvs/server-monitor
  ~/.venvs/server-monitor/bin/pip install flask psutil requests
  sudo sensors-detect

Execução manual:
  ~/.venvs/server-monitor/bin/python server_monitor_dashboard.py

Acesse:
  http://127.0.0.1:8090
  http://ip_address:8090
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import psutil
import requests
from flask import Flask, jsonify, render_template_string


# =========================
# Configuração editável
# =========================

HOST = "0.0.0.0"
PORT = 8090
REFRESH_SECONDS = 30

DISK2_MOUNT = "/mnt/Disk2"
DISK2_CRYPT_NAME = "Disk2_crypt"
DISK2_DEVICE_HINTS = ["/dev/mapper/Disk2_crypt", "Disk2_crypt"]

NETWORK_INTERFACES = ["tailscale0", "wlp1s0"]

SERVICES = [
    {
        "name": "Nextcloud",
        "path": "/",
        "ports": [80],
        "local_urls": ["http://127.0.0.1/"],
        "expected_status_max": 499,
    },
    {
        "name": "File Browser",
        "path": "/",
        "ports": [8080],
        "local_urls": ["http://127.0.0.1:8080/"],
        "expected_status_max": 499,
    },
    {
        "name": "Downloader YouTube",
        "path": "/",
        "ports": [8081],
        "local_urls": ["http://127.0.0.1:8081/"],
        "expected_status_max": 499,
    },
    {
        "name": "Syncthing",
        "path": "/",
        "ports": [8384],
        "local_urls": ["http://127.0.0.1:8384/"],
        "expected_status_max": 499,
    },
    {
        "name": "Jellyfin",
        "path": "/",
        "ports": [8096],
        "local_urls": ["http://127.0.0.1:8096/"],
        "expected_status_max": 499,
    },
    {
        "name": "qBittorrent",
        "path": "/",
        "ports": [8086],
        "local_urls": ["http://127.0.0.1:8086/"],
        "expected_status_max": 499,
    },
]

REQUEST_TIMEOUT_SECONDS = 2.5
CACHE_TTL_SECONDS = 5


# =========================
# App e cache simples
# =========================

app = Flask(__name__)
_LAST_PAYLOAD: dict[str, Any] | None = None
_LAST_PAYLOAD_TS = 0.0


@dataclass
class ServiceCheck:
    name: str
    status: str
    http_status: int | None
    response_ms: float | None
    checked_url: str | None
    error: str | None
    links: list[dict[str, str]]


# =========================
# Utilitários
# =========================

def run_cmd(args: list[str], timeout: float = 2.0) -> str:
    try:
        result = subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)
        return result.stdout.strip()
    except Exception:
        return ""


def bytes_human(value: float | int | None) -> str:
    if value is None:
        return "—"
    value = float(value)
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    if idx == 0:
        return f"{value:.0f} {units[idx]}"
    if value >= 10:
        return f"{value:.0f} {units[idx]}"
    return f"{value:.1f} {units[idx]}"


def percent(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 1)


def get_hostname() -> str:
    return socket.gethostname()


def get_ipv4_for_interface(interface: str) -> str | None:
    addrs = psutil.net_if_addrs().get(interface, [])
    for addr in addrs:
        if addr.family == socket.AF_INET:
            return addr.address
    return None


def get_interface_state(interface: str) -> str:
    stats = psutil.net_if_stats().get(interface)
    if not stats:
        return "missing"
    return "up" if stats.isup else "down"


def get_wifi_ssid(interface: str) -> str | None:
    # iwgetid costuma estar no pacote wireless-tools.
    output = run_cmd(["iwgetid", interface, "-r"])
    if output:
        return output

    # Fallback por nmcli, caso NetworkManager esteja instalado.
    output = run_cmd(["nmcli", "-t", "-f", "active,ssid,device", "dev", "wifi"], timeout=2.0)
    for line in output.splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[0] == "yes" and parts[2] == interface:
            return parts[1] or None
    return None


def get_tailscale_status_name(ip: str | None) -> str | None:
    if not ip:
        return None
    output = run_cmd(["tailscale", "status", "--json"], timeout=2.0)
    if not output:
        return None
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return None
    self_info = data.get("Self") or {}
    dns_name = self_info.get("DNSName")
    host_name = self_info.get("HostName")
    return dns_name or host_name


def collect_network() -> list[dict[str, Any]]:
    items = []
    for iface in NETWORK_INTERFACES:
        ip = get_ipv4_for_interface(iface)
        state = get_interface_state(iface)
        name = None
        if iface.startswith("wl"):
            name = get_wifi_ssid(iface)
        elif iface == "tailscale0":
            name = get_tailscale_status_name(ip)

        items.append(
            {
                "interface": iface,
                "state": state,
                "ipv4": ip,
                "network_name": name,
            }
        )
    return items


def find_ip(network: list[dict[str, Any]], interface: str, fallback: str | None = None) -> str | None:
    for item in network:
        if item.get("interface") == interface and item.get("ipv4"):
            return item["ipv4"]
    return fallback


def collect_disk2() -> dict[str, Any]:
    mount_path = Path(DISK2_MOUNT)
    mounted = os.path.ismount(DISK2_MOUNT)
    crypt_present = any(Path(hint).exists() for hint in DISK2_DEVICE_HINTS if hint.startswith("/"))

    lsblk = run_cmd(["lsblk", "-J", "-o", "NAME,TYPE,SIZE,MOUNTPOINT,FSTYPE,LABEL,UUID"], timeout=2.0)
    lsblk_data: dict[str, Any] = {}
    if lsblk:
        try:
            lsblk_data = json.loads(lsblk)
        except json.JSONDecodeError:
            lsblk_data = {}

    def flatten(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for block in blocks:
            out.append(block)
            out.extend(flatten(block.get("children") or []))
        return out

    block_match = None
    for block in flatten(lsblk_data.get("blockdevices", [])):
        if block.get("name") == DISK2_CRYPT_NAME or block.get("mountpoint") == DISK2_MOUNT:
            block_match = block
            break

    usage = None
    if mounted:
        try:
            du = shutil.disk_usage(DISK2_MOUNT)
            usage = {
                "total_bytes": du.total,
                "used_bytes": du.used,
                "free_bytes": du.free,
                "used_percent": round((du.used / du.total) * 100, 1) if du.total else None,
                "total_human": bytes_human(du.total),
                "used_human": bytes_human(du.used),
                "free_human": bytes_human(du.free),
            }
        except Exception as exc:
            usage = {"error": str(exc)}

    return {
        "name": DISK2_CRYPT_NAME,
        "mount": DISK2_MOUNT,
        "path_exists": mount_path.exists(),
        "mounted": mounted,
        "crypt_present": crypt_present or bool(block_match),
        "block": block_match,
        "usage": usage,
        "status": "ok" if mounted else "missing",
    }


def collect_all_mounts() -> list[dict[str, Any]]:
    """
    Coleta pontos de montagem relevantes para o dashboard.

    Filtra montagens temporárias/sistema/snap para evitar poluição visual,
    especialmente entradas como:
      /snap/...
      /run/...
      /dev/...
      /proc/...
      squashfs
      tmpfs
    """

    IGNORED_MOUNT_PREFIXES = (
        "/snap/",
        "/run/",
        "/dev/",
        "/sys/",
        "/proc/",
        "/var/lib/snapd/",
        "/boot",
    )

    IGNORED_FS_TYPES = {
        "squashfs",
        "tmpfs",
        "devtmpfs",
        "proc",
        "sysfs",
        "cgroup",
        "cgroup2",
        "securityfs",
        "debugfs",
        "tracefs",
        "fusectl",
        "mqueue",
        "hugetlbfs",
        "autofs",
        "pstore",
        "bpf",
        "configfs",
        "efivarfs",
    }

    mounts = []

    for part in psutil.disk_partitions(all=False):
        mountpoint = part.mountpoint
        fstype = part.fstype

        if fstype in IGNORED_FS_TYPES:
            continue

        if mountpoint.startswith(IGNORED_MOUNT_PREFIXES):
            continue

        try:
            usage = psutil.disk_usage(mountpoint)
        except Exception:
            continue

        mounts.append(
            {
                "device": part.device,
                "mountpoint": mountpoint,
                "fstype": fstype,
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
                "used_percent": round(usage.percent, 1),
                "total_human": bytes_human(usage.total),
                "used_human": bytes_human(usage.used),
                "free_human": bytes_human(usage.free),
            }
        )

    return mounts


def collect_cpu_memory() -> dict[str, Any]:
    load1, load5, load15 = os.getloadavg()
    cpu_count = psutil.cpu_count(logical=True) or 1
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()

    return {
        "cpu": {
            "count": cpu_count,
            "percent": psutil.cpu_percent(interval=0.25),
            "load1": round(load1, 2),
            "load5": round(load5, 2),
            "load15": round(load15, 2),
            "load1_percent_of_cores": round((load1 / cpu_count) * 100, 1),
            "load5_percent_of_cores": round((load5 / cpu_count) * 100, 1),
            "load15_percent_of_cores": round((load15 / cpu_count) * 100, 1),
        },
        "memory": {
            "total_bytes": mem.total,
            "used_bytes": mem.used,
            "available_bytes": mem.available,
            "percent": mem.percent,
            "total_human": bytes_human(mem.total),
            "used_human": bytes_human(mem.used),
            "available_human": bytes_human(mem.available),
        },
        "swap": {
            "total_bytes": swap.total,
            "used_bytes": swap.used,
            "free_bytes": swap.free,
            "percent": swap.percent,
            "total_human": bytes_human(swap.total),
            "used_human": bytes_human(swap.used),
            "free_human": bytes_human(swap.free),
        },
    }


def parse_sensors_output(output: str) -> list[dict[str, Any]]:
    temps = []
    current_chip = None
    # Exemplos comuns:
    # Package id 0:  +55.0°C  (high = +80.0°C, crit = +100.0°C)
    # temp1:         +42.0°C
    pattern = re.compile(r"^\s*([^:]+):\s*\+?(-?\d+(?:\.\d+)?)°C")

    for line in output.splitlines():
        if not line.strip():
            current_chip = None
            continue
        if not line.startswith(" ") and ":" not in line:
            current_chip = line.strip()
            continue
        match = pattern.match(line)
        if match:
            label = match.group(1).strip()
            value = float(match.group(2))
            temps.append({"label": label, "value_c": value, "source": current_chip})
    return temps


def collect_temperatures() -> dict[str, Any]:
    temps = []

    if hasattr(psutil, "sensors_temperatures"):
        try:
            sensor_map = psutil.sensors_temperatures(fahrenheit=False)
            for chip, entries in sensor_map.items():
                for entry in entries:
                    if entry.current is not None:
                        temps.append(
                            {
                                "label": entry.label or chip,
                                "value_c": round(float(entry.current), 1),
                                "source": chip,
                            }
                        )
        except Exception:
            pass

    if not temps and shutil.which("sensors"):
        output = run_cmd(["sensors"], timeout=2.0)
        temps = parse_sensors_output(output)

    max_temp = max([t["value_c"] for t in temps], default=None)
    return {
        "items": temps,
        "max_c": max_temp,
        "status": "ok" if temps else "missing",
    }


def build_links_for_service(service: dict[str, Any], internal_ip: str | None, tailscale_ip: str | None) -> list[dict[str, str]]:
    links = []
    path = service.get("path", "/")
    ports = service.get("ports") or []

    for port in ports:
        if internal_ip:
            if port == 80:
                url = f"http://{internal_ip}{path}"
            else:
                url = f"http://{internal_ip}:{port}{path}"
            links.append({"label": f"LAN {internal_ip}", "url": url})

        if tailscale_ip:
            if port == 80:
                url = f"http://{tailscale_ip}{path}"
            else:
                url = f"http://{tailscale_ip}:{port}{path}"
            links.append({"label": f"Tailscale {tailscale_ip}", "url": url})

    return links


def build_urls_for_service_check(service: dict[str, Any], internal_ip: str | None) -> list[str]:
    urls = list(service.get("local_urls") or [])
    path = service.get("path", "/")
    ports = service.get("ports") or []

    if internal_ip:
        for port in ports:
            if port == 80:
                url = f"http://{internal_ip}{path}"
            else:
                url = f"http://{internal_ip}:{port}{path}"
            if url not in urls:
                urls.append(url)

    return urls


def check_service(service: dict[str, Any], internal_ip: str | None, tailscale_ip: str | None) -> ServiceCheck:
    urls_to_check = build_urls_for_service_check(service, internal_ip)
    expected_max = int(service.get("expected_status_max", 399))
    links = build_links_for_service(service, internal_ip, tailscale_ip)

    last_error = None
    for url in urls_to_check:
        started = time.perf_counter()
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=False)
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            ok = response.status_code <= expected_max
            return ServiceCheck(
                name=service["name"],
                status="ok" if ok else "warn",
                http_status=response.status_code,
                response_ms=elapsed_ms,
                checked_url=url,
                error=None if ok else f"HTTP {response.status_code}",
                links=links,
            )
        except requests.RequestException as exc:
            last_error = str(exc)

    return ServiceCheck(
        name=service["name"],
        status="down",
        http_status=None,
        response_ms=None,
        checked_url=urls_to_check[0] if urls_to_check else None,
        error=last_error or "Sem URL local para verificar",
        links=links,
    )


def collect_services(network: list[dict[str, Any]]) -> list[dict[str, Any]]:
    internal_ip = find_ip(network, "wlp1s0")
    tailscale_ip = find_ip(network, "tailscale0")
    return [asdict(check_service(service, internal_ip, tailscale_ip)) for service in SERVICES]


def collect_payload(force: bool = False) -> dict[str, Any]:
    global _LAST_PAYLOAD, _LAST_PAYLOAD_TS

    now = time.time()
    if not force and _LAST_PAYLOAD is not None and (now - _LAST_PAYLOAD_TS) < CACHE_TTL_SECONDS:
        return _LAST_PAYLOAD

    network = collect_network()
    payload = {
        "generated_at": now,
        "generated_at_text": time.strftime("%Y-%m-%d %H:%M:%S"),
        "hostname": get_hostname(),
        "disk2": collect_disk2(),
        "mounts": collect_all_mounts(),
        "performance": collect_cpu_memory(),
        "network": network,
        "temperatures": collect_temperatures(),
        "services": collect_services(network),
    }

    _LAST_PAYLOAD = payload
    _LAST_PAYLOAD_TS = now
    return payload


# =========================
# Rotas
# =========================

@app.route("/api/status")
def api_status():
    return jsonify(collect_payload())


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML, refresh_seconds=REFRESH_SECONDS)


DASHBOARD_HTML = r"""
<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Home Lab Monitor</title>
  <style>
    :root {
      --bg: #0f172a;
      --panel: #111827;
      --panel2: #182235;
      --text: #e5e7eb;
      --muted: #94a3b8;
      --border: rgba(148, 163, 184, 0.22);
      --good: #22c55e;
      --warn: #f59e0b;
      --bad: #ef4444;
      --info: #38bdf8;
      --bar: rgba(148, 163, 184, 0.16);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      background: radial-gradient(circle at top left, rgba(56,189,248,.16), transparent 32rem), var(--bg);
    }
    main { width: min(1500px, calc(100% - 28px)); margin: 0 auto; padding: 26px 0 42px; }
    header { display:flex; justify-content:space-between; gap:16px; align-items:flex-start; margin-bottom:18px; }
    h1 { margin:0; font-size: clamp(28px, 4vw, 46px); letter-spacing:-.045em; }
    .subtitle { color:var(--muted); margin:8px 0 0; line-height:1.5; }
    .pill-row { display:flex; gap:10px; flex-wrap:wrap; justify-content:flex-end; }
    .pill { display:inline-flex; gap:8px; align-items:center; border:1px solid var(--border); border-radius:999px; padding:8px 11px; color:var(--muted); background:rgba(17,24,39,.7); font-size:13px; }
    .dot { width:9px; height:9px; border-radius:50%; background: var(--muted); box-shadow:0 0 14px currentColor; }
    .good { color:var(--good); } .warn { color:var(--warn); } .bad { color:var(--bad); } .info { color:var(--info); } .muted { color:var(--muted); }
    .grid { display:grid; gap:16px; }
    .main-grid { grid-template-columns: minmax(420px, .95fr) minmax(440px, 1.05fr); align-items:start; }
    .left-stack, .right-stack { display:grid; gap:16px; }
    .card { border:1px solid var(--border); border-radius:24px; padding:18px; background:linear-gradient(180deg, rgba(24,34,53,.92), rgba(17,24,39,.92)); box-shadow:0 18px 60px rgba(0,0,0,.34); min-width:0; }
    .card h2 { font-size:15px; margin:0 0 4px; }
    .card .desc { color:var(--muted); font-size:12px; margin:0 0 14px; line-height:1.45; }
    .kpi { font-size: clamp(24px, 3vw, 38px); font-weight:850; letter-spacing:-.04em; margin:8px 0 2px; }
    .label { color:var(--muted); font-size:13px; line-height:1.45; }
    .hero { display:flex; justify-content:space-between; align-items:center; gap:16px; padding:20px; border-radius:25px; border:1px solid rgba(245,158,11,.35); background:linear-gradient(135deg, rgba(245,158,11,.16), rgba(56,189,248,.08)); margin-bottom:16px; }
    .hero.ok { border-color:rgba(34,197,94,.38); background:linear-gradient(135deg, rgba(34,197,94,.17), rgba(56,189,248,.08)); }
    .hero.bad { border-color:rgba(239,68,68,.45); background:linear-gradient(135deg, rgba(239,68,68,.20), rgba(245,158,11,.08)); }
    .hero h2 { font-size:22px; margin:0 0 5px; letter-spacing:-.035em; }
    .hero p { margin:0; color:var(--muted); line-height:1.45; }
    .gauge { width:112px; height:112px; border-radius:50%; display:grid; place-items:center; background:conic-gradient(var(--info) calc(var(--p) * 1%), rgba(148,163,184,.16) 0); position:relative; flex:0 0 auto; }
    .gauge::after { content:""; position:absolute; inset:11px; border-radius:inherit; background:var(--panel); }
    .gauge span { position:relative; z-index:1; font-size:23px; font-weight:850; }
    .row { display:grid; grid-template-columns: 155px 1fr auto; gap:10px; align-items:center; padding:11px 0; border-top:1px solid rgba(148,163,184,.13); }
    .row:first-child { border-top:0; }
    .bar { height:10px; border-radius:999px; overflow:hidden; background:var(--bar); }
    .bar > div { height:100%; width:calc(var(--p) * 1%); background:var(--info); border-radius:inherit; }
    .bar.good > div { background:var(--good); } .bar.warn > div { background:var(--warn); } .bar.bad > div { background:var(--bad); }
    .item { display:flex; justify-content:space-between; align-items:center; gap:12px; padding:13px; border:1px solid rgba(148,163,184,.14); background:rgba(15,23,42,.42); border-radius:18px; margin-top:10px; }
    .item strong { display:block; }
    .item span { display:block; color:var(--muted); font-size:13px; margin-top:3px; word-break:break-word; }
    .badge { border-radius:999px; padding:6px 9px; border:1px solid var(--border); color:var(--muted); font-size:12px; font-weight:800; white-space:nowrap; }
    .badge.good { border-color:rgba(34,197,94,.35); color:var(--good); } .badge.warn { border-color:rgba(245,158,11,.35); color:var(--warn); } .badge.bad { border-color:rgba(239,68,68,.35); color:var(--bad); }
    .links { display:flex; flex-wrap:wrap; gap:8px; margin-top:8px; }
    a { color:#bfdbfe; text-decoration:none; }
    a:hover { text-decoration:underline; }
    .linkbtn { display:inline-flex; padding:6px 9px; border:1px solid var(--border); border-radius:999px; font-size:12px; background:rgba(15,23,42,.56); }
    .service-status { text-align:right; }
    .notice { color:var(--muted); font-size:13px; line-height:1.55; }
    code { color:#dbeafe; background:rgba(148,163,184,.14); border:1px solid rgba(148,163,184,.14); border-radius:8px; padding:2px 6px; }
    @media (max-width: 1100px) { header { flex-direction:column; } .pill-row { justify-content:flex-start; } .main-grid { grid-template-columns:1fr; } }
    @media (max-width: 650px) { main { width:min(100% - 18px, 1500px); padding-top:16px; } .row { grid-template-columns:1fr; } .hero { flex-direction:column; align-items:flex-start; } .item { align-items:flex-start; flex-direction:column; } .service-status { text-align:left; } }
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>Home Lab Monitor</h1>
      </div>
      <div class="pill-row">
        <span class="pill"><span id="apiDot" class="dot"></span><span id="apiStatus">Carregando...</span></span>
        <span class="pill">Host: <strong id="hostname">—</strong></span>
        <span class="pill">Atualizado: <strong id="updated">—</strong></span>
        <span class="pill">Refresh: <strong>{{ refresh_seconds }}s</strong></span>
      </div>
    </header>

    <section class="grid main-grid">
      <div class="left-stack">
        <article class="card"><h2>Temperaturas</h2><p class="desc">Sensores detectados pelo sistema</p><div id="tempRows"></div></article>
        <article class="card"><h2>Processamento</h2><p class="desc">CPU e load average</p><div id="cpuRows"></div></article>
        <article class="card"><h2>Memória</h2><p class="desc">RAM e swap</p><div id="memRows"></div></article>
        <article class="card"><h2>Montagens</h2><p class="desc">Sistemas de arquivos montados no Linux</p><div id="mountRows"></div></article>
        <article class="card"><h2>Rede</h2><p class="desc">tailscale0 e wlp1s0 com IP e nome de rede quando disponível</p><div id="networkRows"></div></article>
      </div>

      <div class="right-stack">
        <div id="diskHero" class="hero"><div><h2>Disk2_crypt</h2><p>Carregando...</p></div><div class="gauge" style="--p:0"><span>—</span></div></div>
        <article class="card"><h2>Serviços</h2><p class="desc">Status local e links via LAN/Tailscale</p><div id="serviceRows"></div></article>
      </div>
    </section>
  </main>

<script>
const REFRESH_MS = {{ refresh_seconds }} * 1000;

function clsByPct(p, warn=75, bad=90) {
  if (p === null || p === undefined || Number.isNaN(Number(p))) return 'info';
  p = Number(p);
  if (p >= bad) return 'bad';
  if (p >= warn) return 'warn';
  return 'good';
}
function clsByTemp(t) {
  if (t === null || t === undefined || Number.isNaN(Number(t))) return 'info';
  t = Number(t);
  if (t >= 80) return 'bad';
  if (t >= 65) return 'warn';
  return 'good';
}
function pctLabel(p) { return p === null || p === undefined ? '—' : `${Number(p).toFixed(1)}%`; }
function row(name, pct, value, cls=clsByPct(pct)) {
  const safePct = Math.max(0, Math.min(100, Number(pct || 0)));
  return `<div class="row"><div class="label">${name}</div><div class="bar ${cls}" style="--p:${safePct}"><div></div></div><strong>${value}</strong></div>`;
}
function badge(status) {
  const cls = status === 'ok' || status === 'up' ? 'good' : status === 'warn' ? 'warn' : 'bad';
  return `<span class="badge ${cls}">${status}</span>`;
}
function setApiStatus(ok, msg) {
  document.getElementById('apiDot').className = `dot ${ok ? 'good' : 'bad'}`;
  document.getElementById('apiStatus').textContent = msg;
}
function renderDisk2(d) {
  const hero = document.getElementById('diskHero');
  const mounted = d?.mounted === true;
  const usage = d?.usage || {};
  const p = usage.used_percent ?? 0;
  const cls = mounted ? clsByPct(p, 80, 92) : 'bad';
  hero.className = `hero ${mounted ? 'ok' : 'bad'}`;
  hero.innerHTML = mounted ? `
    <div><h2>Disk2_crypt conectado</h2><p>${d.mount} • ${usage.used_human} usados de ${usage.total_human} • ${usage.free_human} livres</p></div>
    <div class="gauge" style="--p:${p}"><span class="${cls}">${Math.round(p)}%</span></div>` : `
    <div><h2>Disk2_crypt NÃO conectado</h2><p>Mount esperado: <code>${d?.mount || '/mnt/Disk2'}</code>. Verifique HD externo, cryptsetup e montagem.</p></div>
    <div class="gauge" style="--p:100"><span class="bad">OFF</span></div>`;
}
function renderPerformance(p) {
  const cpu = p.performance.cpu;
  const mem = p.performance.memory;
  const swap = p.performance.swap;

  document.getElementById('cpuRows').innerHTML = [
    row('CPU instantâneo', cpu.percent, pctLabel(cpu.percent)),
    row('Load 1 min', cpu.load1_percent_of_cores, `${cpu.load1} / ${cpu.count}`),
    row('Load 5 min', cpu.load5_percent_of_cores, `${cpu.load5} / ${cpu.count}`),
    row('Load 15 min', cpu.load15_percent_of_cores, `${cpu.load15} / ${cpu.count}`),
  ].join('');
  document.getElementById('memRows').innerHTML = [
    row('RAM', mem.percent, `${mem.used_human} / ${mem.total_human}`),
    row('Swap', swap.percent, `${swap.used_human} / ${swap.total_human}`),
  ].join('');
}
function renderNetwork(items) {
  document.getElementById('networkRows').innerHTML = items.map(n => {
    const ok = n.state === 'up';
    const name = n.network_name ? ` • ${n.network_name}` : '';
    return `<div class="item"><div><strong>${n.interface}</strong><span>IPv4: ${n.ipv4 || '—'}${name}</span></div>${badge(ok ? 'up' : n.state)}</div>`;
  }).join('');
}
function renderServices(items) {
  document.getElementById('serviceRows').innerHTML = items.map(s => {
    const statusText = s.status === 'ok' ? `HTTP ${s.http_status} • ${s.response_ms} ms` : (s.error || 'offline');
    const links = (s.links || []).map(l => `<a class="linkbtn" href="${l.url}" target="_blank" rel="noreferrer">${l.label}</a>`).join('');
    return `<div class="item"><div><strong>${s.name}</strong><span>Check: ${s.checked_url || '—'} • ${statusText}</span><div class="links">${links}</div></div><div class="service-status">${badge(s.status)}</div></div>`;
  }).join('');
}
function renderMounts(items) {
  const sorted = [...items].sort((a,b) => {
    const aDisk2 = a.mountpoint === '/mnt/Disk2' ? -1 : 0;
    const bDisk2 = b.mountpoint === '/mnt/Disk2' ? -1 : 0;
    return aDisk2 - bDisk2 || b.used_percent - a.used_percent;
  });
  document.getElementById('mountRows').innerHTML = sorted.map(m => {
    const cls = clsByPct(m.used_percent, 80, 92);
    return `<div class="item"><div style="min-width:220px"><strong>${m.mountpoint}</strong><span>${m.device} • ${m.fstype}</span></div><div style="flex:1; min-width:180px"><div class="bar ${cls}" style="--p:${m.used_percent}"><div></div></div><span>${m.used_human} / ${m.total_human} • ${m.free_human} livres</span></div><span class="badge ${cls}">${pctLabel(m.used_percent)}</span></div>`;
  }).join('');
}
function renderTemperatures(t) {
  const items = t.items || [];
  document.getElementById('tempRows').innerHTML = items.length ? items.map(x => {
    const cls = clsByTemp(x.value_c);
    return `<div class="item"><div><strong>${x.label}</strong><span>${x.source || ''}</span></div><span class="badge ${cls}">${Number(x.value_c).toFixed(1)}°C</span></div>`;
  }).join('') : '<p class="notice">Nenhum sensor encontrado. Instale/configure <code>lm-sensors</code> e execute <code>sudo sensors-detect</code>.</p>';
}
async function refresh() {
  try {
    const res = await fetch('/api/status', {cache: 'no-store'});
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const p = await res.json();
    setApiStatus(true, 'Monitor online');
    document.getElementById('hostname').textContent = p.hostname;
    document.getElementById('updated').textContent = p.generated_at_text;
    renderDisk2(p.disk2);
    renderPerformance(p);
    renderNetwork(p.network);
    renderServices(p.services);
    renderMounts(p.mounts);
    renderTemperatures(p.temperatures);
  } catch (err) {
    console.error(err);
    setApiStatus(false, err.message || 'Erro');
  }
}
refresh();
setInterval(refresh, REFRESH_MS);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    print(f"Servidor de monitoramento iniciado em http://{HOST}:{PORT}")
    app.run(host=HOST, port=PORT, debug=False)
