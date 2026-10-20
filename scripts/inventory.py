"""Resource identities from Git definitions; never execute code from Git history."""
import ast
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".local/resource-inventory"
OWNER_KEY = "infra.sasano8.dev/owner"
OWNERS = {"infra-kubernetes", "sas47"}


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True).stdout


def resolve(ref):
    return git("rev-parse", "--verify", "--end-of-options", ref + "^{commit}").strip()


def identity(api, kind, name, namespace=""):
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", name):
        raise ValueError(f"リソース名を静的に解決できません: {kind}/{name}")
    return {"group": api.split("/")[0] if "/" in api else "", "kind": kind,
            "namespace": namespace or "", "name": name}


def key(resource):
    return "|".join(resource.get(field, "") for field in ("group", "kind", "namespace", "name"))


def label(resource):
    kind = resource["kind"] + ("." + resource["group"] if resource["group"] else "")
    return f"{resource['namespace'] or '(cluster)'}/{kind}/{resource['name']}"


def selected(path):
    return (path in ("scripts/infra.py", "scripts/tls.py") or
            (path.startswith(("apps/", "plugins/kong/manifests/", "manifests/"))
             and path.endswith((".yaml", ".yml", ".yaml.tmpl", ".yml.tmpl"))))


def yaml_identities(text, path):
    """Read only literal top-level Kubernetes identity fields, not YAML spec/data."""
    resources = []
    for document in re.split(r"(?m)^---\s*$", text):
        if not document.strip() or all(not line.strip() or line.lstrip().startswith("#")
                                       for line in document.splitlines()):
            continue
        def field(pattern):
            match = re.search(pattern, document, re.MULTILINE)
            return match.group(1).strip().strip('"\'') if match else None
        api = field(r"^apiVersion:\s*([^#\n]+)")
        kind = field(r"^kind:\s*([^#\n]+)")
        block = re.search(r"(?ms)^metadata:\s*\n(.*?)(?=^\S|\Z)", document)
        if not api or not kind or not block:
            raise ValueError(f"履歴マニフェストの識別情報を解決できません: {path}")
        def metadata(name):
            match = re.search(r"^  " + name + r":\s*([^#\n]+)", block.group(1), re.MULTILINE)
            return match.group(1).strip().strip('"\'') if match else ""
        resources.append(identity(api, kind, metadata("name"), metadata("namespace")))
    return resources


def values(node, bindings):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name):
        return bindings.get(node.id, set())
    return set()


def dictionary(node):
    if not isinstance(node, ast.Dict):
        return {}
    return {k.value: v for k, v in zip(node.keys, node.values)
            if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def python_identities(text, path):
    tree = ast.parse(text, filename=path)
    resources = []
    if path == "scripts/infra.py":
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "NAMESPACES" for t in node.targets):
                for name in ast.literal_eval(node.value):
                    resources.append(identity("v1", "Namespace", name))
        return resources
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    bindings = {name: {} for name in functions}
    # Resolve literal loop names used to generate Certificate objects.
    def bind(target, item, known):
        if isinstance(target, ast.Name):
            known.setdefault(target.id, set()).update(values(item, known))
        elif isinstance(target, (ast.Tuple, ast.List)) and isinstance(item, (ast.Tuple, ast.List)):
            for left, right in zip(target.elts, item.elts):
                bind(left, right, known)
    for name, function in functions.items():
        for node in ast.walk(function):
            if isinstance(node, ast.For) and isinstance(node.iter, (ast.Tuple, ast.List)):
                for item in node.iter.elts:
                    bind(node.target, item, bindings[name])
    # Propagate literal arguments to helpers such as apply_secret/copy_secret.
    for _ in range(len(functions) + 1):
        changed = False
        for name, function in functions.items():
            for call in ast.walk(function):
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name) or call.func.id not in functions:
                    continue
                target = call.func.id
                for parameter, argument in zip(functions[target].args.args, call.args):
                    known = bindings[target].setdefault(parameter.arg, set())
                    before = len(known)
                    known.update(values(argument, bindings[name]))
                    changed |= before != len(known)
        if not changed:
            break
    for name, function in functions.items():
        namespaces = set()
        for node in ast.walk(function):
            obj = dictionary(node)
            metadata = dictionary(obj.get("metadata"))
            if not {"apiVersion", "kind", "metadata"} <= obj.keys():
                continue
            apis, kinds = values(obj["apiVersion"], bindings[name]), values(obj["kind"], bindings[name])
            names = values(metadata.get("name"), bindings[name])
            ns = values(metadata.get("namespace"), bindings[name]) or {""}
            if not apis or not kinds or not names:
                raise ValueError(f"動的なリソース名を履歴から解決できません: {path}:{name}")
            namespaces.update(ns)
            for api in apis:
                for kind in kinds:
                    for resource_name in names:
                        for namespace in ns:
                            resources.append(identity(api, kind, resource_name, namespace))
        # Issuer accounts / API tokens / certificate Secrets are generated resources too.
        for node in ast.walk(function):
            obj = dictionary(node)
            names = values(obj.get("secretName"), bindings[name])
            for reference in ("privateKeySecretRef", "apiTokenSecretRef"):
                names |= values(dictionary(obj.get(reference)).get("name"), bindings[name])
            for resource_name in names:
                for namespace in namespaces:
                    resources.append(identity("v1", "Secret", resource_name, namespace))
    return resources


def at_commit(commit, cache=None):
    cache = {} if cache is None else cache
    resources = {}
    for line in git("ls-tree", "-r", commit).splitlines():
        header, path = line.split("\t", 1)
        mode, kind, blob = header.split()
        if not selected(path):
            continue
        if kind != "blob" or mode not in ("100644", "100755"):
            raise ValueError(f"通常ファイルではない管理対象です: {path}")
        cache_key = (blob, path)
        if cache_key not in cache:
            text = git("show", blob)
            cache[cache_key] = (python_identities(text, path) if path.endswith(".py")
                                else yaml_identities(text, path))
        for resource in cache[cache_key]:
            resources[key(resource)] = resource
    return resources


def history(from_ref=None):
    if not from_ref and git("rev-parse", "--is-shallow-repository").strip() == "true":
        raise ValueError("Git 履歴が浅いため全差分を復元できません。git fetch --unshallow origin を実行してください")
    head = resolve("HEAD")
    commits = git("rev-list", "--reverse", head).splitlines()
    if from_ref:
        start = resolve(from_ref)
        if start not in commits:
            raise ValueError("from-ref は現在の HEAD の祖先にしてください")
        commits = commits[commits.index(start):]
    resources, cache = {}, {}
    for commit in commits:
        for resource_key, resource in at_commit(commit, cache).items():
            if resource_key not in resources:
                resources[resource_key] = dict(resource, first_seen=commit)
            resources[resource_key]["last_seen"] = commit
    return head, resources, at_commit(head, cache)


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def cluster_uid(cluster):
    result = cluster.call("get", "namespace/kube-system", "-o", "go-template={{.metadata.uid}}",
                          "--request-timeout=10s", capture_output=True, text=True)
    uid = result.stdout.strip()
    if not uid:
        raise ValueError("クラスタの UID を取得できません")
    return uid


def record(cluster, config):
    if any(selected(path) for path in git("diff", "--name-only", "HEAD").splitlines()):
        print("配備は完了しましたが、管理対象定義が未コミットのため一覧の記録は省略します。確定後に再配備してください。", flush=True)
        return
    commit = resolve("HEAD")
    now = datetime.now(timezone.utc).isoformat()
    data = {"version": 1, "commit": commit, "recorded_at": now,
            "cluster_uid": cluster_uid(cluster), "tls_enabled": config["tls"]["enabled"],
            "resources": list(at_commit(commit).values())}
    path = STATE / "deployments" / (now.replace(":", "-") + "-" + commit[:12] + ".json")
    write_json(path, data)
    print(f"配備の管理対象一覧を記録しました: {path}", flush=True)
