# Changelog

All notable changes to this project are recorded here. Entries accumulate
under `[Unreleased]` on `develop`; promotion to `main` rolls them into a dated
version heading that matches the release tag.

## [Unreleased]

### Changed

- **`/health/detailed` now requires the API key when one is configured.** The
  endpoint reports the gateway's connected platforms and running agents, which
  was readable by any unauthenticated peer that could reach the API server.
  With `API_SERVER_KEY` set it now returns 401 without a matching bearer, so
  monitoring tools that scrape it must send `Authorization: Bearer <key>`.
  The plain `/health` liveness probe stays unauthenticated, and with no key
  configured both endpoints remain open. The dashboard sends the key when it
  has `API_SERVER_KEY` and otherwise falls back to `/health`.

### Added

- **Docker Compose healthcheck for the gateway.** The gateway container now
  reports `healthy`/`unhealthy` from its pid file (`docker/healthcheck.py`),
  so `docker compose ps` and restart policies reflect whether the gateway
  process is actually running. A stale pid file whose PID was reused by an
  unrelated process is detected as unhealthy.

### Fixed

- **Postgres-backed tests errored in CI** because `psycopg` floated to 3.3.6
  while `psycopg-binary` stayed pinned at 3.3.4, and the mismatched pair
  rejected a keyword argument on every connection wait. `psycopg` is now
  pinned to match.

### Infrastructure

- CI (tests, lint, lockfile, OSV scanning, history and contributor checks,
  Claude review) now runs on pull requests and pushes to `develop`, not only
  `main`.
- Bumped `astral-sh/setup-uv` to v10.0.1, `actions/deploy-pages` to v5.0.1,
  and `github/codeql-action/upload-sarif` to v4.38.2.
