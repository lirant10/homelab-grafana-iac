#!/usr/bin/env python3
"""Keep my Vaultwarden "homelab" folders up to date with everything I need to log in.

  homelab/Machines  one Login per machine, e.g. "route-host": how to reach it (SSH over
                    Tailscale/LAN), what runs there, every container with its web address,
                    and how to check it is healthy
  homelab/Web       one Login per web UI, e.g. "Grafana": URL, username/password, checks
  homelab/Secrets   one Secure note per Podman secret / secret file: the value, where it
                    is used and how to re-create it

Run it again whenever something changes: existing items are updated in place
(machine passwords you typed before are kept), new ones are created.
Secret values are never printed.

Needs the Bitwarden CLI, logged in and unlocked:
    bw config server https://vaultwarden.tail9f548c.ts.net
    bw login
    export BW_SESSION="$(bw unlock --raw)"
"""
import base64
import getpass
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import date

FOLDERS = {"machine": "homelab/Machines", "web": "homelab/Web", "secret": "homelab/Secrets"}
OLD_FOLDER = "homelab"  # flat folder used by the first version of this script
ARCH = "192.168.0.10"

# ---------------------------------------------------------------- what I have
MACHINES = {
    "arch-pc": {
        "ssh": "",  # empty = this machine
        "user": "Lirant",
        "addresses": [(ARCH, "LAN")],
        "role": "Monitoring stack: Grafana, Prometheus, Thanos, MinIO, exporters "
                "(rootless Podman Quadlets, repo ~/homelab-grafana-iac)",
        "checks": ["podman ps", "systemctl --user list-timers grafana-gitsync.timer",
                   f"http://{ARCH}:9090/targets  -> every job UP"],
    },
    "route-host": {
        "ssh": "lirant@route-host",
        "user": "lirant",
        "addresses": [("route-host", "Tailscale"), ("192.168.0.125", "LAN")],
        "role": "LocalStack Pro (AWS emulator) in Docker + cAdvisor on :8088",
        "checks": ["sudo docker ps", "curl -s localhost:4566/_localstack/health"],
    },
    "azure-docker": {
        "ssh": "lirant@azure-docker",
        "user": "lirant",
        "addresses": [("azure-docker", "Tailscale"), ("192.168.0.63", "LAN")],
        "role": "Azurite (Azure Storage emulator) + Azure Storage Explorer UI (Podman)",
        "checks": ["podman ps", "curl -s localhost:9882/metrics | head -3"],
    },
    "vaultwarden": {
        "ssh": "lirant@vaultwarden",
        "user": "lirant",
        "addresses": [("vaultwarden", "Tailscale"), ("192.168.0.89", "LAN")],
        "role": "Vaultwarden password manager (Podman), HTTPS via 'tailscale serve' -> :8080",
        "checks": ["podman ps", "tailscale serve status"],
    },
}

# web UIs; "user"/"password" can be text or (machine, podman secret name)
SERVICES = [
    {"name": "Grafana", "url": f"http://{ARCH}:3000",
     "user": "admin", "password": ("arch-pc", "grafana_admin_password"),
     "how": "Browser",
     "checks": ["Dashboards -> homelab -> VMware & Podman Infrastructure: panels show data",
                f"http://{ARCH}:3000/api/health -> database ok"]},
    {"name": "MinIO console", "url": f"http://{ARCH}:9001",
     "user": ("arch-pc", "minio_root_user"), "password": ("arch-pc", "minio_root_password"),
     "how": "Browser (S3 API itself is on :9000)",
     "checks": ["Buckets -> the Thanos bucket keeps growing (new blocks every 2h)"]},
    {"name": "Prometheus", "url": f"http://{ARCH}:9090",
     "how": "Browser, no login",
     "checks": ["Status -> Targets: vmware_esx and podman jobs all UP"]},
    {"name": "Thanos Query", "url": f"http://{ARCH}:9091",
     "how": "Browser, no login (Grafana's data source)",
     "checks": ["Stores page: thanos-sidecar and thanos-store UP"]},
    {"name": "Vaultwarden web vault", "url": "https://vaultwarden.tail9f548c.ts.net",
     "how": "Browser, Tailscale only. Master password is NOT stored here",
     "checks": ["loads with a valid HTTPS certificate"]},
    {"name": "GitHub - homelab-grafana-iac", "url": "https://github.com/lirant10/homelab-grafana-iac",
     "how": "Browser",
     "checks": ["Actions: last 'validate' run is green",
                "push -> arch-pc pulls it within 5 min (grafana-gitsync.timer)"]},
]

# where each Podman secret is used
SECRET_USAGE = {
    "grafana_admin_password": "Grafana admin password (deploy/quadlets/grafana.container)",
    "minio_root_user": "MinIO root user (deploy/quadlets/minio.container)",
    "minio_root_password": "MinIO root password (deploy/quadlets/minio.container)",
    "thanos_bucket": "Thanos bucket.yml, mounted in thanos-sidecar and thanos-store",
}

# secret files that are not Podman secrets
FILES = {
    "arch-pc": [("~/.config/vmware-exporter/config.yml",
                 "vSphere login for vmware-exporter (mounted as /config/config.yml)")],
}


# ---------------------------------------------------------------- helpers
def run(*cmd, stdin=None):
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, check=True).stdout


def on(host, command):
    """Run a shell command on a machine (locally, or over SSH with my key)."""
    target = MACHINES[host]["ssh"]
    if not target:
        return run("bash", "-c", command)
    return run("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", target, command)


def try_on(host, command):
    try:
        return on(host, command)
    except subprocess.CalledProcessError:
        return ""


def bw(*args, data=None):
    # data goes in through stdin, so secrets never show up in the process list
    stdin = base64.b64encode(json.dumps(data).encode()).decode() if data is not None else None
    out = run("bw", *args, stdin=stdin)
    return json.loads(out) if out.strip() else None


def secret_value(host, name):
    return on(host, "podman secret inspect --showsecret --format '{{.SecretData}}' " + shlex.quote(name))


def resolve(value):
    """Text stays text; (machine, secret) is read from that machine's Podman secrets."""
    return secret_value(*value) if isinstance(value, tuple) else value


def containers_with_urls(host):
    """Every running container on the machine, with a clickable URL for each published port."""
    lan = next((a for a, label in MACHINES[host]["addresses"] if label == "LAN"), host)
    lines = []
    for cmd in ("podman ps --format '{{.Names}}|{{.Ports}}'",
                "docker ps --format '{{.Names}}|{{.Ports}}' 2>/dev/null"):
        for row in try_on(host, cmd).splitlines():
            name, _, ports = row.partition("|")
            urls = []
            # e.g. "0.0.0.0:8080->8080/tcp" or "0.0.0.0:10000-10002->10000-10002/tcp"
            for ip, first, last in re.findall(r"([\d.]+|\[::\]):(\d+)(?:-(\d+))?->", ports):
                where = "localhost" if ip.startswith("127.") else lan
                url = f"http://{where}:{first}" + (f" (ports {first}-{last})" if last else "")
                if ip.startswith("127."):
                    url += " - only from the machine itself"
                if url not in urls:
                    urls.append(url)
            lines.append(f"  - {name}: {'  '.join(urls) or '(no published port)'}")
    return lines or ["  (no running containers visible without sudo)"]


# ---------------------------------------------------------------- item builders
def machine_item(host, password):
    m = MACHINES[host]
    notes = [
        "HOW TO CONNECT: SSH",
        *[f"  ssh {m['user']}@{addr}    ({label})" for addr, label in m["addresses"]],
        "",
        f"WHAT RUNS HERE: {m['role']}",
        "",
        f"CONTAINERS (as of {date.today()}):",
        *containers_with_urls(host),
        "",
        "CHECK IT'S HEALTHY:",
        *[f"  {c}" for c in m["checks"]],
    ]
    return {"type": 1, "notes": "\n".join(notes),
            "login": {"username": m["user"], "password": password,
                      "uris": [{"uri": f"ssh://{addr}"} for addr, _ in m["addresses"]]}}


def service_item(s):
    notes = [f"HOW TO OPEN: {s['how']}", f"  {s['url']}", "", "CHECK IT'S HEALTHY:",
             *[f"  {c}" for c in s["checks"]]]
    return {"type": 1, "notes": "\n".join(notes),
            "login": {"username": resolve(s.get("user")) if s.get("user") else None,
                      "password": resolve(s.get("password")) if s.get("password") else None,
                      "uris": [{"uri": s["url"]}]}}


def secret_item(value, used_for, recreate):
    return {"type": 2, "secureNote": {"type": 0}, "notes": value,
            "fields": [{"name": "used for", "value": used_for, "type": 0},
                       {"name": "re-create with", "value": recreate, "type": 0}]}


# ---------------------------------------------------------------- main
def main():
    if not os.environ.get("BW_SESSION"):
        sys.exit('BW_SESSION is not set. Run:  export BW_SESSION="$(bw unlock --raw)"')

    run("bw", "sync")
    folders = {f["name"]: f["id"] for f in bw("list", "folders")}
    for name in [OLD_FOLDER, *FOLDERS.values()]:
        if name not in folders:
            folders[name] = bw("create", "folder", data={"name": name})["id"]
    # every item already in one of my homelab folders, by name
    existing = {}
    for name in [OLD_FOLDER, *FOLDERS.values()]:
        for i in bw("list", "items", "--folderid", folders[name]):
            existing[i["name"]] = i["id"]
    stats = {"created": 0, "updated": 0}

    def upsert(kind, name, item, old_name=None):
        """Create the item, or update the one with this name - or with its old name from the
        first version of the script, which then gets renamed and moved. An empty password
        keeps the one already stored."""
        item.update(name=name, folderId=folders[FOLDERS[kind]])
        item_id = existing.get(name) or existing.get(old_name)
        try:
            if item_id:
                current = bw("get", "item", item_id)
                if item["type"] == 1 and not item["login"].get("password"):
                    item["login"]["password"] = current.get("login", {}).get("password")
                bw("edit", "item", item_id, data={**current, **item})
                was = f"  (was '{current['name']}')" if current["name"] != name else ""
                print(f"  updated  {FOLDERS[kind]}/{name}{was}")
                stats["updated"] += 1
            else:
                item_id = bw("create", "item", data=item)["id"]
                print(f"  created  {FOLDERS[kind]}/{name}")
                stats["created"] += 1
            existing[name] = item_id
        except subprocess.CalledProcessError as e:
            print(f"  FAILED   {name}: {e.stderr.strip()}")

    print("== web UIs")
    for s in SERVICES:
        upsert("web", s["name"], service_item(s))

    print("== Podman secrets and secret files")
    for host in MACHINES:
        for secret in try_on(host, "podman secret ls --format '{{.Name}}'").split():
            upsert("secret", f"{secret} @ {host}", secret_item(
                secret_value(host, secret),
                SECRET_USAGE.get(secret, f"Podman secret on {host}"),
                f"ssh to {host}, then: podman secret create {secret} -  (paste value, Ctrl+D)"),
                old_name=f"{host}: {secret}")
        for path, used_for in FILES.get(host, []):
            short = "/".join(path.split("/")[-2:])
            upsert("secret", f"{short} @ {host}", secret_item(
                on(host, "cat " + path), used_for, f"save the note as {path} on {host}"),
                old_name=f"{host}: {short}")

    print("== machines  (new ones ask for the login password: Enter = same as the last one, '-' = skip)")
    last = None
    for host, m in MACHINES.items():
        old_name = f"SSH {m['user']}@{host}"
        password = None
        if host not in existing and old_name not in existing:
            pw = getpass.getpass(f"  password for {m['user']}@{host}: ")
            if pw == "-" or (pw == "" and last is None):
                print(f"  skipped  {host}")
                continue
            password = last = pw or last
        upsert("machine", host, machine_item(host, password), old_name=old_name)

    print(f"\ndone: {stats['created']} created, {stats['updated']} updated")


if __name__ == "__main__":
    main()
