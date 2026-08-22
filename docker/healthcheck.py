#!/usr/bin/env python3
"""Container healthcheck for the gateway service.

The gateway exposes no inbound HTTP port by default (messaging is outbound,
and the OpenAI-compatible API server is opt-in), so the compose healthcheck
cannot probe an endpoint. Instead, verify the gateway process itself: read
the PID from ``$THOTH_HOME/gateway.pid`` (written by ``gateway/status.py``
on startup, JSON with a ``pid`` field) and signal-0 it. Same PID namespace —
the gateway and this probe run in the same container.

Deliberately stdlib-only with no thoth imports: a healthcheck fires every
interval and must not pay the CLI's import cost.
"""
import json
import os
import sys


def main() -> int:
    home = os.environ.get("THOTH_HOME", "/opt/data")
    pid_path = os.path.join(home, "gateway.pid")
    try:
        with open(pid_path, encoding="utf-8") as fh:
            pid = int(json.load(fh)["pid"])
    except Exception as exc:  # missing/corrupt pid file — gateway not up
        print(f"unhealthy: cannot read gateway pid from {pid_path}: {exc}")
        return 1
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        print(f"unhealthy: gateway pid {pid} is not running (stale pid file)")
        return 1
    except PermissionError:
        pass  # process exists under another uid — alive is what matters
    print(f"healthy: gateway pid {pid} alive")
    return 0


if __name__ == "__main__":
    sys.exit(main())
