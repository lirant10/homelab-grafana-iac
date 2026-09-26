#!/usr/bin/env python3
"""Static checks for the Grafana-as-code repo.

    python scripts/validate.py                      # lint dashboards + provisioning
    python scripts/validate.py --emit-rules r.yml   # also write every PromQL expr
                                                    # into a rules file for
                                                    # `promtool check rules`
"""
import argparse
import glob
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ALLOWED_DS_UIDS = {"${datasource}", "-- Grafana --"}

# Values used to turn dashboard variables into plain PromQL for promtool.
VAR_SUBST = {
    "$__rate_interval": "5m",
    "$__interval": "1m",
    "$__range": "1h",
}

errors: list[str] = []


def err(msg: str) -> None:
    errors.append(msg)


def walk_panels(panels):
    for p in panels:
        yield p
        yield from walk_panels(p.get("panels", []))


def check_dashboard(path: Path, exprs: list[tuple[str, str]]) -> None:
    rel = path.relative_to(ROOT)
    try:
        dash = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        err(f"{rel}: invalid JSON: {e}")
        return

    if "apiVersion" in dash or "spec" in dash:
        err(f"{rel}: looks like a v2 resource (apiVersion/spec). File provisioning here "
            "expects the classic dashboard JSON model (Export > JSON, 'Export for sharing' off).")
        return

    for key in ("uid", "title", "panels"):
        if key not in dash:
            err(f"{rel}: missing '{key}'")
    if dash.get("id") not in (None,):
        err(f"{rel}: remove the numeric 'id' (Grafana assigns it); keep only 'uid'")

    seen_ids = set()
    for p in walk_panels(dash.get("panels", [])):
        pid = p.get("id")
        if pid in seen_ids:
            err(f"{rel}: duplicate panel id {pid} ('{p.get('title')}')")
        seen_ids.add(pid)
        if p.get("type") == "row":
            continue

        ds = p.get("datasource") or {}
        if isinstance(ds, str) or (ds.get("uid") not in ALLOWED_DS_UIDS):
            err(f"{rel}: panel '{p.get('title')}' has hard-coded datasource {ds!r}; "
                "use {\"type\": \"prometheus\", \"uid\": \"${datasource}\"}")

        refs = set()
        for t in p.get("targets", []):
            ref = t.get("refId")
            if ref in refs:
                err(f"{rel}: panel '{p.get('title')}' has duplicate refId {ref}")
            refs.add(ref)
            if t.get("expr"):
                exprs.append((f"{p.get('title')} [{ref}]", t["expr"]))

    names = [v.get("name") for v in dash.get("templating", {}).get("list", [])]
    if "datasource" not in names:
        err(f"{rel}: add a 'datasource' variable so the dashboard is portable")


def check_provisioning(exprs: list[tuple[str, str]]) -> None:
    for f in sorted(glob.glob(str(ROOT / "provisioning/**/*.y*ml"), recursive=True)):
        rel = Path(f).relative_to(ROOT)
        try:
            doc = yaml.safe_load(Path(f).read_text())
        except yaml.YAMLError as e:
            err(f"{rel}: invalid YAML: {e}")
            continue
        if not isinstance(doc, dict) or doc.get("apiVersion") != 1:
            err(f"{rel}: expected 'apiVersion: 1' at the top")
            continue
        for group in doc.get("groups", []):
            for rule in group.get("rules", []):
                for q in rule.get("data", []):
                    expr = q.get("model", {}).get("expr")
                    if expr:
                        exprs.append((f"alert {rule.get('uid')}", expr.replace("$$", "$")))


def to_plain_promql(expr: str) -> str:
    for k, v in VAR_SUBST.items():
        expr = expr.replace(k, v)
    # any remaining $var / ${var} is a label matcher value -> match everything
    return re.sub(r"\$\{?\w+\}?", ".*", expr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit-rules", help="write PromQL exprs to this promtool rules file")
    args = ap.parse_args()

    dashboards = sorted((ROOT / "dashboards").rglob("*.json"))
    if not dashboards:
        err("no dashboards found under dashboards/")
    exprs: list[tuple[str, str]] = []
    for d in dashboards:
        check_dashboard(d, exprs)
    check_provisioning(exprs)

    if args.emit_rules:
        rules = [{"record": f"check:expr_{i}", "expr": to_plain_promql(e)}
                 for i, (_, e) in enumerate(exprs)]
        Path(args.emit_rules).write_text(
            yaml.safe_dump({"groups": [{"name": "dashboard-exprs", "rules": rules}]}, sort_keys=False))
        print(f"wrote {len(rules)} expressions to {args.emit_rules}")

    if errors:
        print("FAILED:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"OK: {len(dashboards)} dashboard(s), {len(exprs)} PromQL expressions checked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
