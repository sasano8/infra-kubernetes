"""Validate and render the upload limits applied by Kong's NGINX proxy."""
import re

DEFAULTS = {
    "max_body_bytes": 105 * 1024 * 1024,
    "client_body_idle_timeout_seconds": 30,
    "upstream_connect_timeout_seconds": 5,
    "upstream_send_idle_timeout_seconds": 30,
    "upstream_read_idle_timeout_seconds": 330,
    "max_concurrent_upload_requests": 5,
    "upload_path_prefixes": ["/upload", "/uploads"],
    "s3_hosts": [],
}
MAXIMUM = {
    "max_body_bytes": 1024**4,
    "client_body_idle_timeout_seconds": 86400,
    "upstream_connect_timeout_seconds": 75,
    "upstream_send_idle_timeout_seconds": 86400,
    "upstream_read_idle_timeout_seconds": 86400,
    "max_concurrent_upload_requests": 65535,
}


def normalize(config, dns_name):
    supplied = config.get("upload_limits", {})
    if not isinstance(supplied, dict) or supplied.keys() - DEFAULTS.keys():
        raise ValueError("upload_limits は対応する設定項目のオブジェクトを指定してください")
    limits = {**DEFAULTS, **supplied}
    for key, maximum in MAXIMUM.items():
        value = limits[key]
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f"upload_limits.{key} は 1〜{maximum} の整数を指定してください")
    for key in ("upload_path_prefixes", "s3_hosts"):
        entries = limits[key]
        if (not isinstance(entries, list)
                or any(not isinstance(entry, str) for entry in entries)
                or len(set(entries)) != len(entries)):
            raise ValueError(f"upload_limits.{key} は重複のない文字列の配列を指定してください")
        limits[key] = list(entries)
    for path in limits["upload_path_prefixes"]:
        if not re.fullmatch(r"(?:/[A-Za-z0-9._~-]+)+", path) or {".", ".."} & set(path.split("/")):
            raise ValueError("upload_limits.upload_path_prefixes は末尾 / のない絶対パスを指定してください")
    for host in limits["s3_hosts"]:
        dns_name(host, "upload_limits.s3_hosts")
        if config["sso"]["enabled"] and host == config["sso"]["auth_host"]:
            raise ValueError("upload_limits.s3_hosts に認証ホストは指定できません")
    config["upload_limits"] = limits


def nginx_config(config):
    limits = config["upload_limits"]
    # Exact host entries win over regex entries. Keep the shared login host
    # and the public wildcard out of the application upload counter.
    lines = ["map $host $infra_upload_scope {", "    default none;"]
    if config["sso"]["enabled"]:
        lines.append(f'    {config["sso"]["auth_host"]} none;')
    lines.extend(f"    {host} s3;" for host in limits["s3_hosts"])
    public = re.escape(config["routes"]["knative_public"]["domain"])
    lines.append(f'    "~^(?:[^.]+\\.)?{public}$" none;')
    for route_id, route in config["routes"].items():
        if route_id != "knative_public":
            domain = re.escape(route["domain"])
            lines.append(f'    "~^(?:[^.]+\\.)?{domain}$" app;')
    lines.extend(["}", "", 'map "$infra_upload_scope:$request_method:$uri" $infra_upload_key {',
                  '    default "";', '    "~^s3:PUT:" uploads;'])
    for prefix in limits["upload_path_prefixes"]:
        lines.append(f'    "~^app:(?:POST|PUT|PATCH):{re.escape(prefix)}(?:/|$)" uploads;')
    lines.extend(["}", "", "limit_conn_zone $infra_upload_key zone=infra_uploads:1m;"])
    return "\n".join(lines) + "\n"


def upstream_annotations(config):
    limits = config["upload_limits"]
    return {
        "konghq.com/connect-timeout": str(limits["upstream_connect_timeout_seconds"] * 1000),
        "konghq.com/write-timeout": str(limits["upstream_send_idle_timeout_seconds"] * 1000),
        "konghq.com/read-timeout": str(limits["upstream_read_idle_timeout_seconds"] * 1000),
    }
