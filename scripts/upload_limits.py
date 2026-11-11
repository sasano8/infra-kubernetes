"""Validate and render the upload limits applied by Kong's NGINX proxy."""
DEFAULTS = {
    "max_body_bytes": 105 * 1024 * 1024,
    "client_body_idle_timeout_seconds": 30,
    "upstream_connect_timeout_seconds": 5,
    "upstream_send_idle_timeout_seconds": 30,
    "upstream_read_idle_timeout_seconds": 330,
    "max_concurrent_upload_requests": 5,
}
LEGACY_SELECTORS = {"upload_path_prefixes", "s3_hosts"}
MAXIMUM = {
    "max_body_bytes": 1024**4,
    "client_body_idle_timeout_seconds": 86400,
    "upstream_connect_timeout_seconds": 75,
    "upstream_send_idle_timeout_seconds": 86400,
    "upstream_read_idle_timeout_seconds": 86400,
    "max_concurrent_upload_requests": 65535,
}


NGINX_CONFIG = '''map "$content_length:$http_transfer_encoding" $infra_upload_key {
    default "";
    "~^[0-9]*[1-9][0-9]*:$" uploads;
    "~*^:chunked$" uploads;
}

limit_conn_zone $infra_upload_key zone=infra_uploads:1m;
'''


def normalize(config):
    supplied = config.get("upload_limits", {})
    if not isinstance(supplied, dict) or supplied.keys() - DEFAULTS.keys() - LEGACY_SELECTORS:
        raise ValueError("upload_limits は対応する設定項目のオブジェクトを指定してください")
    # Older environment files can keep the former selectors during migration.
    limits = {key: supplied.get(key, default) for key, default in DEFAULTS.items()}
    for key, maximum in MAXIMUM.items():
        value = limits[key]
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f"upload_limits.{key} は 1〜{maximum} の整数を指定してください")
    config["upload_limits"] = limits


def upstream_annotations(config):
    limits = config["upload_limits"]
    return {
        "konghq.com/connect-timeout": str(limits["upstream_connect_timeout_seconds"] * 1000),
        "konghq.com/write-timeout": str(limits["upstream_send_idle_timeout_seconds"] * 1000),
        "konghq.com/read-timeout": str(limits["upstream_read_idle_timeout_seconds"] * 1000),
    }
