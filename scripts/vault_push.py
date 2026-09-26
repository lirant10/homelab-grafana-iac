#!/usr/bin/env python3
"""Back up every homelab secret into Vaultwarden (folder "homelab").

For each machine in HOSTS it collects:
  * every Podman secret of the user (podman secret ls) -> secure note "<host>: <secret>"
  * extra secret files listed in FILES                  -> secure note "<host>: <file name>"
  * the SSH login of the machine (you type the password) -> login "SSH <user>@<host>"

Items that already exist are skipped, so it is safe to run again - e.g. after you
create a new Podman secret. Secret values are never printed.

Remote machines are read over SSH with your key (ssh-copy-id), no passwords.

Needs the Bitwarden CLI, logged in and unlocked:
    bw config server https://vaultwarden.tail9f548c.ts.net
    bw login
    export BW_SESSION="$(bw unlock --raw)"
"""
import base64
import getpass
import json
import os
import shlex
import subprocess
import sys

FOLDER = "homelab"

# name in Vaultwarden -> (SSH target, login user). An empty target means "this machine".
HOSTS = {
    "arch-pc": ("", "Lirant"),
    "route-host": ("lirant@route-host", "lirant"),
    "azure-docker": ("lirant@azure-docker", "lirant"),
    "vaultwarden": ("lirant@vaultwarden", "lirant"),
}

# secret files that are not Podman secrets
FILES = {
    "arch-pc": ["~/.config/vmware-exporter/config.yml"],
}


def run(*cmd, stdin=None):
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, check=True).stdout


def on(host, command):
    """Run a shell command on a homelab machine (locally or over SSH)."""
    target = HOSTS[host][0]
    if not target:
        return run("bash", "-c", command)
    return run("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", target, command)


def bw(*args, data=None):
    # data goes in through stdin, so secrets never show up in the process list
    stdin = base64.b64encode(json.dumps(data).encode()).decode() if data is not None else None
    out = run("bw", *args, stdin=stdin)
    return json.loads(out) if out.strip() else None


def note_item(folder_id, name, text, note_from):
    return {"type": 2, "name": name, "folderId": folder_id, "secureNote": {"type": 0},
            "notes": text, "fields": [{"name": "source", "value": note_from, "type": 0}]}


def login_item(folder_id, name, user, password, host):
    return {"type": 1, "name": name, "folderId": folder_id,
            "login": {"username": user, "password": password, "uris": [{"uri": f"ssh://{host}"}]}}


def main():
    if not os.environ.get("BW_SESSION"):
        sys.exit('BW_SESSION is not set. Run:  export BW_SESSION="$(bw unlock --raw)"')

    run("bw", "sync")
    folders = {f["name"]: f["id"] for f in bw("list", "folders")}
    folder_id = folders.get(FOLDER) or bw("create", "folder", data={"name": FOLDER})["id"]
    existing = {i["name"] for i in bw("list", "items", "--folderid", folder_id)}
    created = skipped = 0

    def save(name, build):
        nonlocal created, skipped
        if name in existing:
            print(f"  skip     {name} (already in Vaultwarden)")
            skipped += 1
            return
        try:
            bw("create", "item", data=build())
            print(f"  created  {name}")
            existing.add(name)
            created += 1
        except subprocess.CalledProcessError as e:
            print(f"  FAILED   {name}: {e.stderr.strip()}")

    # 1. Podman secrets + secret files on every machine
    for host in HOSTS:
        print(f"== {host}")
        try:
            names = on(host, "podman secret ls --format '{{.Name}}'").split()
        except subprocess.CalledProcessError as e:
            print(f"  (no Podman secrets here: {e.stderr.strip() or 'podman not available'})")
            names = []
        for secret in names:
            save(f"{host}: {secret}", lambda s=secret: note_item(
                folder_id, f"{host}: {s}",
                on(host, "podman secret inspect --showsecret --format '{{.SecretData}}' " + shlex.quote(s)),
                f"podman secret {s} on {host}"))
        for path in FILES.get(host, []):
            file_name = "/".join(path.split("/")[-2:])  # e.g. vmware-exporter/config.yml
            save(f"{host}: {file_name}", lambda p=path, f=file_name: note_item(
                folder_id, f"{host}: {f}", on(host, "cat " + p), f"{p} on {host}"))

    # 2. SSH logins - the only part you type, because Linux keeps only password hashes
    print("== machine logins  (Enter = same password as the last one, '-' = skip)")
    last = None
    for host, (_, user) in HOSTS.items():
        name = f"SSH {user}@{host}"
        if name in existing:
            print(f"  skip     {name} (already in Vaultwarden)")
            skipped += 1
            continue
        pw = getpass.getpass(f"  password for {user}@{host}: ")
        if pw == "-" or (pw == "" and last is None):
            print(f"  skipped  {name}")
            continue
        pw = pw or last
        last = pw
        save(name, lambda h=host, u=user, p=pw, n=name: login_item(folder_id, n, u, p, h))

    print(f"\ndone: {created} created, {skipped} already there")


if __name__ == "__main__":
    main()
