#!/usr/bin/env python3
"""Container healthcheck for the gateway service.

The gateway exposes no inbound HTTP port by default (messaging is outbound,
and the OpenAI-compatible API server is opt-in), so the compose healthcheck
cannot probe an endpoint. Instead, verify the gateway process itself: read
the PID from ``$THOTH_HOME/gateway.pid`` (written by ``gateway/status.py``
on startup, JSON with a ``pid`` field) and signal-0 it. Same PID namespace —
the gateway and this probe run in the same container. When the record also
carries ``start_time``, it must match the live process's start time, so a
stale pid file whose PID was reused by another process reads as unhealthy.

Deliberately stdlib-only with no thoth imports: a healthcheck fires every
interval and must not pay the CLI's import cost.
"""
import json
import os
import sys


def _process_start_time(pid: int) -> int | None:
    """Kernel start time (field 22 of /proc/<pid>/stat), as gateway/status.py records it."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            return int(fh.read().split()[21])
    except (OSError, IndexError, ValueError):
        return None


def main() -> int:
    home = os.environ.get("THOTH_HOME", "/opt/data")
    pid_path = os.path.join(home, "gateway.pid")
    try:
        with open(pid_path, encoding="utf-8") as fh:
            record = json.load(fh)
        pid = int(record["pid"])
    except Exception as exc:
        print(f"unhealthy: cannot read gateway pid from {pid_path}: {exc}")
        return 1
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        print(f"unhealthy: gateway pid {pid} is not running (stale pid file)")
        return 1
    except PermissionError:
        # Exists, but this user cannot signal it. Treat that as alive.
        pass
    recorded = record.get("start_time")
    if recorded is not None:
        live = _process_start_time(pid)
        if live is not None and live != recorded:
            print(f"unhealthy: gateway pid {pid} was reused by another process")
            return 1
    print(f"healthy: gateway pid {pid} alive")
    return 0


if __name__ == "__main__":
    sys.exit(main())
