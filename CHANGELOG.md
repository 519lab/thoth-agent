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
- **API keys in CLI messages are masked consistently.** Setup prompts and
  auth diagnostics printed the first 8–12 characters of keys. A short key
  could show in full. They now use the same `sk-a...7890` / `***` masking as
  the rest of Thoth. Separately, the OpenClaw migration's
  `MIGRATION_JSON_OUTPUT` dump printed tokens found in migrated approval
  patterns unredacted. It is now redacted like `--json` (#438).

### Added

- **Docker Compose healthcheck for the gateway.** The gateway container now
  reports `healthy`/`unhealthy` from its pid file (`docker/healthcheck.py`),
  so `docker compose ps` and restart policies reflect whether the gateway
  process is actually running. A stale pid file whose PID was reused by an
  unrelated process is detected as unhealthy.

### Fixed

- **Profile names like `..` are rejected everywhere.** Two dashboard paths
  (the kanban profile-description endpoint and the profile auto-describer)
  normalised profile names but never validated them. `..` or `.` then
  resolved to `~/.thoth` itself, and a `profile.yaml` could be written there (#429).
  `get_profile_dir()` now rejects any name that isn't a valid profile id, and
  the endpoint returns 400.
- **The GitNexus explorer skill no longer exposes your files.** Its web proxy
  served any path, including `../` traversal outside the UI build, and listened
  on every network interface. While it ran, any host on the network, or anyone
  with the optional Cloudflare tunnel URL, could read any file the user could,
  including SSH keys (#421). The proxy now serves only files inside the build
  directory, binds to `127.0.0.1` by default (`HOST` overrides), and the skill
  copies the bundled script instead of writing its own copy.
- **The API server rejects session ids that could act as file paths.**
  `X-Thoth-Session-Id` accepted `..` and `/`, and session ids later become
  transcript filenames and delete targets (#435). Ids must now be a single safe
  segment of up to 128 letters, digits, and `._:-`. Generated ids and UUIDs are
  unaffected, and anything else gets a 400.
- **`POST /v1/runs` now checks the `session_id` in its body, and session ids
  can no longer contain `:`.** The run's `session_id` becomes a transcript
  filename the same way `X-Thoth-Session-Id` does, but it skipped the safe
  segment check, so `../` and `/` still reached the filesystem through this
  endpoint. It now gets the same check and returns 400 for anything else, a
  non-string included. `:` is dropped from the allowed characters on both
  endpoints because on Windows `session_a:b.json` writes an NTFS alternate data
  stream of `session_a` instead of a file of its own. Generated ids and UUIDs
  never contain `:`.
- **Postgres-backed tests errored in CI** because `psycopg` floated to 3.3.6
  while `psycopg-binary` stayed pinned at 3.3.4, and the mismatched pair
  rejected a keyword argument on every connection wait. `psycopg` is now
  pinned to match.
- **The one-time cleanup of empty TUI sessions now deletes their files too.**
  It removed the database rows but passed every session id to the file
  cleaner as one string, so no transcript or request-dump file was ever
  matched and they were left in `sessions/` (#433). Files already left behind
  by earlier runs are not removed retroactively.

### Security

- **The WhatsApp bridge can no longer be made to run shell commands.** When it
  converted voice replies to ogg, it ran ffmpeg through the shell with the
  file path quoted only by `JSON.stringify`. A file named `a$(cmd).mp3` passed
  to `/send-media` ran `cmd` as the bridge user (#423). ffmpeg is now called
  directly with an argument list.
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
- **The ComfyUI skill no longer writes your Comfy Cloud API key into transcripts.**
  Its websocket monitor logged the full connection URL, including
  `&token=<key>`, and skill output is saved to session history (#425). The log
  line now shows `token=<redacted>`.
- **The website build no longer breaks when its lockfile is refreshed.**
  `@docusaurus/theme-mermaid` was a caret range while every other Docusaurus
  package was pinned, so any lockfile update moved it past core and Docusaurus
  refused to build ("Invalid name=docusaurus-theme-mermaid version"). All
  `@docusaurus/*` packages are now pinned to the same exact version (3.10.2).
- **Hindsight and Honcho API keys are no longer readable by other local users.**
  Both memory plugins wrote their credential files (`.env`,
  `hindsight/config.json`, `~/.hindsight/profiles/*.env`, `honcho.json`) with
  default permissions, which is usually 0644 (#427). They are now created owner-only
  (0600) from the first byte, the same as Thoth's own `.env`. Files written before this fix keep their old
  permissions until the plugin's setup runs again.

### Infrastructure

- **Automated Claude PR reviews now post their findings.** The review workflow
  ran and found issues but never posted them. The action only starts its
  inline-comment server when the tool allow-list names it, and ours did not,
  so every review finished green and said nothing (#419). Once that was
  fixed, the review still couldn't tell which PR it was on, so it printed its
  findings to the job log instead of posting them (#431). The workflow now
  allows the inline-comment tool and read-only `gh pr` commands, and passes
  the PR number to the review.
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
