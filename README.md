# homelab-grafana-iac

Grafana dashboards for my home lab, managed as code.
One dashboard shows **VMware (ESXi hosts, VMs, datastores)** and **Podman containers** side by side.

Git is the source of truth: Grafana reads the dashboards from this repo (file provisioning),
a systemd timer runs `git pull`, and GitHub Actions validates every push.

```
 edit JSON / export from UI ──► git push ──► GitHub Actions (lint + PromQL + Grafana load test)
                                                   │
 Grafana host: grafana-gitsync.timer ── git pull ◄─┘
                     │
                     ▼
   ~/homelab-grafana-iac/{provisioning,dashboards}  ──(read-only mount)──►  Grafana container
```

## What's on the dashboard

| Row | Panels | Metrics from |
|---|---|---|
| Overview | VMs running / off, ESXi CPU & memory, containers running / not running | both |
| VMware – VMs | power state, CPU % and memory % gauges, VM inventory table, CPU, CPU ready, network, disk I/O, guest disk usage | [vmware_exporter](https://github.com/pryorda/vmware_exporter) |
| VMware – hosts | host CPU %, host memory %, datastore usage | vmware_exporter |
| Podman | containers table (state, image, CPU, memory, uptime), CPU, memory, network, block I/O | [prometheus-podman-exporter](https://github.com/containers/prometheus-podman-exporter) |

Dropdowns: **Data source**, **ESXi host**, **VM**, **Podman host**, **Container**.
The data source is a variable, so the JSON has no hard-coded datasource UID and works on any Grafana.

## Repo layout

```
dashboards/homelab/            dashboard JSON (folder name = Grafana folder)
provisioning/datasources/      Prometheus data source (URL comes from $PROMETHEUS_URL)
provisioning/dashboards/       tells Grafana to load dashboards/ from disk
deploy/quadlets/               Podman Quadlets: grafana, vmware-exporter, podman-exporter
deploy/systemd/                git-sync timer (auto deploy)
deploy/prometheus/             scrape_configs to add to prometheus.yml
scripts/validate.py            lint checks (+ PromQL extraction for promtool)
scripts/export-dashboard.sh    pull a dashboard from the Grafana API into the repo
.github/workflows/validate.yml CI
```

## Setup

### 1. Exporters

On the machine that talks to ESXi/vCenter (use a **read-only** vSphere user):

```bash
printf '%s' 'the-password' | podman secret create vsphere_password -
cp deploy/quadlets/vmware-exporter.container ~/.config/containers/systemd/
# edit VSPHERE_HOST / VSPHERE_USER in the file
systemctl --user daemon-reload && systemctl --user start vmware-exporter
curl -s localhost:9272/metrics | grep vmware_vm_power_state
```

On **every** machine that runs Podman containers:

```bash
systemctl --user enable --now podman.socket
cp deploy/quadlets/podman-exporter.container ~/.config/containers/systemd/
systemctl --user daemon-reload && systemctl --user start podman-exporter
curl -s localhost:9882/metrics | grep podman_container_info
```

Rootless quadlets stop when you log out unless lingering is on: `loginctl enable-linger $USER`.

### 2. Prometheus

Add the jobs from `deploy/prometheus/scrape-configs.example.yml` to `prometheus.yml` and reload.
Check **Status → Targets** – both jobs should be `UP`.

### 3. Grafana

```bash
git clone git@github.com:<you>/homelab-grafana-iac.git ~/homelab-grafana-iac
```

**Already running Grafana?** Just add two read-only mounts and the env var to your existing container:

```
-v ~/homelab-grafana-iac/provisioning:/etc/grafana/provisioning:ro
-v ~/homelab-grafana-iac/dashboards:/etc/grafana/dashboards:ro
-e PROMETHEUS_URL=http://<prometheus-or-thanos-query>:9090
```

**Or use the quadlet from this repo:**

```bash
printf '%s' 'S0me-Str0ng-Pass' | podman secret create grafana_admin_password -
cp deploy/quadlets/grafana.container deploy/quadlets/grafana-data.volume ~/.config/containers/systemd/
# set Image= to the version you run today, and PROMETHEUS_URL
systemctl --user daemon-reload && systemctl --user start grafana
```

The dashboard appears under **Dashboards → homelab → VMware & Podman Infrastructure**.

### 4. Auto-deploy on push

```bash
cp deploy/systemd/grafana-gitsync.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now grafana-gitsync.timer
```

Every 5 minutes the host pulls `main`; Grafana re-reads changed files within 30 s. No restarts.

## Day-to-day workflow

Provisioned dashboards are **read-only in the UI** (`allowUiUpdates: false`) – that's on purpose, so the UI and Git can't drift apart.

To change a dashboard:

1. **Small change** – edit the JSON in the repo, `python3 scripts/validate.py`, commit, push.
2. **Bigger change** – in Grafana open the dashboard → **Save as** a copy → edit it in the UI →
   export it back:
   ```bash
   export GRAFANA_URL=http://arch-pc:3000 GRAFANA_TOKEN=glsa_...   # service account, Viewer
   ./scripts/export-dashboard.sh <uid-of-the-copy> dashboards/homelab/vmware-podman-overview.json
   ```
   Put the original `"uid": "homelab-vmware-podman"` and title back, commit, push, delete the copy.

Keep the classic JSON model. If Grafana shows `apiVersion: dashboard.grafana.app/v2` in *Edit as code*,
switch to **Export → JSON** instead – `validate.py` rejects v2 resources.

## CI

`.github/workflows/validate.yml` runs on every push / PR:

- `validate.py` – valid JSON/YAML, unique panel IDs, no hard-coded datasource UIDs, `datasource` variable present
- `promtool check rules` – every PromQL expression in the dashboard parses
- boots a real Grafana container with this repo's provisioning and checks the dashboard + data source load

## Secrets

Nothing secret is in this repo. Passwords are Podman secrets (`podman secret create`),
the Prometheus URL is an env var, and `.gitignore` blocks `*.env` / `secrets/`.
