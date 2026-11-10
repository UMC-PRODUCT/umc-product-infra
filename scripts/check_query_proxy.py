#!/usr/bin/env python3
"""조회 proxy를 실제 nginx image로 띄워 쓰기·관리 경로가 upstream에 닿지 않는지 확인한다.

정적 검사는 nginx 설정을 끝까지 해석하지 못한다. 그래서 같은 image 안에 받은 요청줄을 그대로 돌려주는
가짜 upstream을 둔다. 막아야 할 경로는 upstream에 닿지 않아야 하고, 허용 경로는 정규화된 경로 그대로
upstream에 닿아야 한다.

    python3 scripts/check_query_proxy.py IMAGE NGINX_CONF
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

STUB_UPSTREAM = "127.0.0.1:9099"
STUB_SERVER = f"""
  server {{
    listen {STUB_UPSTREAM};
    location / {{
      return 200 "UPSTREAM $request_method $request_uri\\n";
    }}
  }}
}}
"""

PASSED = (
    ("GET", "/api/v1/query?query=up", "/api/v1/query?query=up"),
    ("POST", "/api/v1/query", "/api/v1/query"),
    ("POST", "/api/v1/query_range", "/api/v1/query_range"),
    ("GET", "/api/v1/label/__name__/values", "/api/v1/label/__name__/values"),
    ("GET", "/api/v1/status/buildinfo", "/api/v1/status/buildinfo"),
    ("POST", "//api/v1//query", "/api/v1/query"),
    ("POST", "/api/v1/write/../query", "/api/v1/query"),
    ("POST", "/api/v1/%71uery", "/api/v1/query"),
)

BLOCKED = tuple(
    (method, path)
    for path in (
        "/api/v1/write",
        "/api/v1/otlp/v1/metrics",
        "/api/v1/admin/tsdb/snapshot",
        "/api/v1/admin/tsdb/delete_series",
        "/-/quit",
        "/-/reload",
        "/federate",
        "/api/v1/status/config",
        "/api/v1/%77rite",
        "/api/v1/query/../write",
        "/api/v1/query%2F..%2Fwrite",
        "//api/v1/write",
        "/api/v1//write",
        "/API/v1/write",
        "/api/v1/write/",
        "/api/v1/query%0A",
        "/api/v1/label/..%2F..%2Fwrite/values",
    )
    for method in ("GET", "POST")
) + (("POST", "/api/v1/rules"), ("POST", "/api/v1/status/buildinfo"))


def probe_config(config: str) -> str:
    config = re.sub(r"^\s*resolver .*$", "", config, flags=re.MULTILINE)
    config, replaced = re.subn(r"set \$prometheus \S+;", f"set $prometheus {STUB_UPSTREAM};", config)
    if replaced != 1:
        raise SystemExit("query proxy check: expected one upstream variable")
    closing = config.rstrip().rfind("}")
    return config[:closing] + STUB_SERVER


def request(container: str, method: str, path: str) -> str:
    args = ["docker", "exec", container, "wget", "-q", "-O", "-", "-T", "5"]
    if method == "POST":
        args += ["--post-data", "query=up"]
    result = subprocess.run([*args, f"http://127.0.0.1:8080{path}"],
                            capture_output=True, text=True, check=False)
    return result.stdout


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: check_query_proxy.py IMAGE NGINX_CONF")
    image, source = sys.argv[1], Path(sys.argv[2])
    probe = source.with_name("prometheus-query-proxy-probe.conf")
    probe.write_text(probe_config(source.read_text(encoding="utf-8")), encoding="utf-8")
    container = subprocess.run(
        ["docker", "run", "-d", "--network", "none", "--read-only", "--tmpfs", "/tmp",
         "-v", f"{probe.parent}:/validation:ro", "--entrypoint", "nginx", image,
         "-e", "stderr", "-c", f"/validation/{probe.name}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    try:
        for _ in range(50):
            if request(container, "GET", "/healthz") == "ok\n":
                break
            time.sleep(0.2)
        else:
            raise SystemExit("query proxy check: proxy did not start")
        failures = [f"{method} {path} must reach upstream as {expected}"
                    for method, path, expected in PASSED
                    if request(container, method, path) != f"UPSTREAM {method} {expected}\n"]
        failures += [f"{method} {path} reached upstream"
                     for method, path in BLOCKED if "UPSTREAM" in request(container, method, path)]
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)
    if failures:
        raise SystemExit("query proxy check failed:\n  " + "\n  ".join(failures))
    print(f"query proxy check passed ({len(PASSED)} passed, {len(BLOCKED)} blocked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
