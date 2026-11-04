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

- **The GitNexus explorer skill no longer exposes your files.** Its web proxy
  served any path, including `../` traversal outside the UI build, and listened
  on every network interface. While it ran, any host on the network, or anyone
  with the optional Cloudflare tunnel URL, could read any file the user could,
  including SSH keys (#421). The proxy now serves only files inside the build
  directory, binds to `127.0.0.1` by default (`HOST` overrides), and the skill
  copies the bundled script instead of writing its own copy.
- **Postgres-backed tests errored in CI** because `psycopg` floated to 3.3.6
  while `psycopg-binary` stayed pinned at 3.3.4, and the mismatched pair
  rejected a keyword argument on every connection wait. `psycopg` is now
  pinned to match.

### Security

- **Fixed every critical and high Dependabot alert (60 open; 3 expected to
  remain once Dependabot rescans `develop`).** PyJWT
  moves from 2.13.0 to 2.15.1, fixing 17 advisories that include HMAC
  key-confusion token forgery, an empty-HMAC-key bypass, and unauthenticated
  `RecursionError` DoS. anyio moves to 4.14.2 (TLS hostname spoofing) and
  setuptools to 84.2.1. In the website, web dashboard, and WhatsApp bridge
  lockfiles, transitive npm packages (tinypool RCE, websocket-driver, js-yaml,
  nanoid, postcss, svgo, image-size, browserslist, qs, and others) are updated,
  and the website now overrides tinypool to the patched 2.x line and
  webpack-dev-middleware to 7.4.6 (path traversal on the docs dev server). Docusaurus
  loads tinypool only when `future.faster.ssgWorkerThreads` is on. The three
  alerts still open are moderate or low and have no upstream fix that our
  dependents can take: minimatch below 11.0.1 (no 3.x or 10.x backport) and
  deepmerge (no patched release).
- **The website build no longer breaks when its lockfile is refreshed.**
  `@docusaurus/theme-mermaid` was a caret range while every other Docusaurus
  package was pinned, so any lockfile update moved it past core and Docusaurus
  refused to build ("Invalid name=docusaurus-theme-mermaid version"). All
  `@docusaurus/*` packages are now pinned to the same exact version (3.10.2).

### Infrastructure

- **Automated Claude PR reviews now post their findings.** The review workflow
  ran and found issues but never posted them. The action only starts its
  inline-comment server when the tool allow-list names it, and ours did not,
  so every review finished green and said nothing (#419). The workflow now
  allows the inline-comment tool and read-only `gh pr` commands.
- **Website, docs, and markdown-only pull requests can be merged again.** The
  lint and test workflows skipped such PRs with a path filter, so the required
  `ruff enforcement (blocking)`, `Windows footguns (blocking)`, and `test`
  checks never reported and the PR stayed pending forever (#407). The lint
  checks now always run on PRs (they take seconds). The test workflow still
  skips the full suite for docs/markdown-only PRs, but now via a skipped job,
  which satisfies the required check.
- The docs-only check that gates the test suite now reads the PR's file list
  from the GitHub API instead of cloning full history, removing ~2.5 minutes
  it had added before every PR's required `test` check could start (#412).
- The lint workflow no longer interpolates the PR branch name directly into a
  shell script, closing a script-injection vector through crafted branch
  names.
- **Fixed a random CI failure in the interrupt tests.** The test suite's
  live-system guard could mistake the test's own just-exited child for a
  foreign process and block a harmless liveness probe, failing
  `test_sigterm_trap_killed_within_2s` at random and blocking unrelated PRs
  on the required `test` check (#408).
- CI (tests, lint, lockfile, OSV scanning, history and contributor checks,
  Claude review) now runs on pull requests and pushes to `develop`, not only
  `main`.
- Bumped `astral-sh/setup-uv` to v10.0.1, `actions/deploy-pages` to v5.0.1,
  and `github/codeql-action/upload-sarif` to v4.38.2.
