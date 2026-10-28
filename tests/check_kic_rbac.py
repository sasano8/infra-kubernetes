"""Check effective Secret grants from the shipped RBAC, including binding scope.

PyYAML is a CI/test dependency only; the installer remains standard-library-only.
This evaluates repository policy, not additional grants in an existing cluster.
"""
from pathlib import Path
import re
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import infra
import inventory


def main():
    directory = ROOT / "plugins/kong/manifests"
    documents = [doc for filename in ("rbac.yaml", "controller.yaml")
                 for doc in yaml.safe_load_all((directory / filename).read_text()) if doc]
    roles = {(d["kind"], d["metadata"].get("namespace", ""), d["metadata"]["name"]): d
             for d in documents if d["kind"] in ("Role", "ClusterRole")}
    bindings = [d for d in documents if d["kind"] in ("RoleBinding", "ClusterRoleBinding")]

    def allowed(verb, resource, namespace="", group=""):
        for binding in bindings:
            if not any(s.get("kind") == "ServiceAccount" and s.get("name") == "kong-ingress"
                       and s.get("namespace") == "kong-system" for s in binding.get("subjects", [])):
                continue
            if binding["kind"] == "RoleBinding" and binding["metadata"]["namespace"] != namespace:
                continue
            ref = binding["roleRef"]
            scope = binding["metadata"].get("namespace", "") if ref["kind"] == "Role" else ""
            for rule in roles[(ref["kind"], scope, ref["name"])]["rules"]:
                if (verb in rule["verbs"] or "*" in rule["verbs"]) and (
                        resource in rule["resources"] or "*" in rule["resources"]) and (
                        group in rule["apiGroups"] or "*" in rule["apiGroups"]):
                    return True
        return False

    # A watch filter never substitutes for an authorization boundary.
    controller = (directory / "kong.yaml.tmpl").read_text()
    match = re.search(r'CONTROLLER_WATCH_NAMESPACE, value: "([^"]+)"', controller)
    watched = set(match.group(1).split(","))
    assert watched == {"test", "kong-system"}
    for verb in ("list", "watch"):
        for namespace in watched:
            assert allowed(verb, "secrets", namespace), (verb, namespace)
    for namespace in ("", "kube-system", "knative-serving", "unrelated", "new-namespace"):
        for verb in ("get", "list", "watch", "create", "update", "patch", "delete"):
            assert not allowed(verb, "secrets", namespace), (verb, namespace)
    for namespace in watched:
        for verb in ("create", "update", "patch", "delete"):
            assert not allowed(verb, "secrets", namespace), (verb, namespace)
    assert allowed("list", "nodes")
    assert allowed("get", "ingressclasses", group="networking.k8s.io")
    for namespace in watched:
        assert allowed("watch", "ingresses", namespace, "networking.k8s.io")

    secret_role = roles[("ClusterRole", "", "kong-ingress-secrets")]
    assert secret_role["metadata"]["labels"]["infra.sasano8.dev/owner"] == "infra-kubernetes"
    assert ("clusterrole", "kong-ingress-secrets") in infra.KIC_CLUSTER_RESOURCES
    secret_bindings = [b for b in bindings if b["roleRef"]["name"] == "kong-ingress-secrets"]
    assert len(secret_bindings) == 2
    assert all(b["kind"] == "RoleBinding" for b in secret_bindings)
    assert {b["metadata"]["namespace"] for b in secret_bindings} == watched
    identities = inventory.yaml_identities((directory / "controller.yaml").read_text(), "controller")
    assert {d["namespace"] for d in identities if d["kind"] == "RoleBinding"
            and d["name"] == "kong-ingress-secrets"} == watched
    print("KIC RBAC: Secret read allowed only in watched namespaces; cluster-wide read/write denied")


if __name__ == "__main__":
    main()
