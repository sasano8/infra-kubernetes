"""Render the real pinned charts in CI; no cluster or credentials required.

Requires Helm 3.13+ and PyYAML (CI tools only; installer uses stdlib).
"""
import json
from pathlib import Path
import subprocess
import sys
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import platform_plugins as plugins

plugins.require_helm()
for name, chart in plugins.CHARTS.items():
    output = subprocess.run([
        "helm", "template", chart["release"], chart["chart"], "--repo", chart["repo"],
        "--version", chart["version"], "--namespace", chart["namespace"], "--include-crds",
        "--kube-version", "1.34.3", "--values", str(plugins.chart_values(name)),
    ], text=True, capture_output=True, check=True).stdout
    objects = [item for item in yaml.safe_load_all(output) if item]
    assert objects, name + ": empty chart"
    assert any(item["kind"] == "CustomResourceDefinition" for item in objects), name + ": missing CRDs"
    assert any(item["kind"] in ("Deployment", "StatefulSet") for item in objects), name + ": missing controller"
    for item in objects:
        namespace = item.get("metadata", {}).get("namespace")
        assert namespace in (None, chart["namespace"]), (name, item["kind"], namespace)
        if item["kind"] in ("RoleBinding", "ClusterRoleBinding"):
            for subject in item.get("subjects", []):
                if subject["kind"] == "ServiceAccount":
                    assert subject.get("namespace") == chart["namespace"], (name, subject)
        if item["kind"] == "Service":
            is_argo_server = name == "argocd" and item["metadata"]["name"] == "argo-cd-argocd-server"
            expected_type = "NodePort" if is_argo_server else "ClusterIP"
            assert item["spec"].get("type", "ClusterIP") == expected_type, (name, item["metadata"]["name"])
    if name == "argocd":
        server = next(item for item in objects if item["kind"] == "Service"
                      and item["metadata"]["name"] == "argo-cd-argocd-server")
        assert {port["port"]: port["nodePort"] for port in server["spec"]["ports"]} == {80: 30090, 443: 30091}
    print(name + ": pinned chart renders; namespaces, RBAC, and Service exposure verified")
