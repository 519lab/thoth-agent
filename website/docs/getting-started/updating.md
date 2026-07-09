---
sidebar_position: 3
title: "Updating & Uninstalling"
description: "How to update Thoth Agent to the latest version or uninstall it"
---

# Updating & Uninstalling

## Updating

### Git installs

Update to the latest version with a single command:

```bash
thoth update
```

This pulls the latest code from `main`, updates dependencies, and prompts you to configure any new options that were added since your last update.

### pip installs

PyPI releases track **tagged versions** (major and minor releases), not every commit on `main`. Check for updates and upgrade with:

```bash
thoth update --check    # see if a newer release is on PyPI
thoth update            # runs pip install --upgrade thoth-agent
```

Or manually:

```bash
pip install --upgrade thoth-agent    # or: uv pip install --upgrade thoth-agent
```

:::tip
`thoth update` automatically detects new configuration options and prompts you to add them. If you skipped that prompt, you can manually run `thoth config check` to see missing options, then `thoth config migrate` to interactively add them.
:::

### What happens during an update (git installs)

When you run `thoth update`, the following steps occur:

1. **Pairing-data snapshot** — a lightweight pre-update state snapshot is saved (covers `~/.thoth/pairing/` and other state files that get modified at runtime). Recoverable via the snapshot restore flow described under [Snapshots and rollback](../user-guide/checkpoints-and-rollback.md), or by extracting the most recent quick-snapshot zip Thoth wrote next to your `~/.thoth/` directory.
2. **Git pull** — pulls the latest code from the `main` branch and updates submodules
3. **Dependency install** — runs `uv pip install -e ".[all]"` to pick up new or changed dependencies
4. **Config migration** — detects new config options added since your version and prompts you to set them
5. **Gateway auto-restart** — running gateways are refreshed after the update completes so the new code takes effect immediately. Service-managed gateways (systemd on Linux, launchd on macOS) are restarted through the service manager. Manual gateways are relaunched automatically when Thoth can map the running PID back to a profile.

### Preview-only: `thoth update --check`

Want to know if an update is available before pulling? Run `thoth update --check` — for git installs it fetches and compares commits against `origin/main`; for pip installs it queries PyPI for the latest release. No files are modified, no gateway is restarted. Useful in scripts and cron jobs that gate on "is there an update".

### Full pre-update backup: `--backup`

For high-value profiles (production gateways, shared team installs) you can opt into a full pre-pull backup of `THOTH_HOME` (config, auth, sessions, skills, pairing):

```bash
thoth update --backup
```

Or make it the default for every run:

```yaml
# ~/.thoth/config.yaml
updates:
  pre_update_backup: true
```

`--backup` was the always-on behavior in earlier builds, but it was adding minutes to every update on large homes, so it's now opt-in. The lightweight pairing-data snapshot above still runs unconditionally.

### Windows: another `thoth.exe` is running

On Windows, `thoth update` will refuse to run if it detects another `thoth.exe` process holding the venv's entry-point executable open — most commonly the Thoth Desktop app's spawned backend, an open `thoth` REPL in another terminal, or a running gateway:

```
$ thoth update
✗ Another thoth.exe is running:
    PID 12345  thoth.exe

  Updating now would fail to overwrite ...\venv\Scripts\thoth.exe because
  Windows blocks REPLACE on a running executable.

  Close Thoth Desktop, exit any open `thoth` REPLs, and
  stop the gateway (`thoth gateway stop`) before retrying.
  Override with `thoth update --force` if you've already
  confirmed those processes will not write to the venv.
```

Close the listed processes and re-run. If you're sure the concurrent process won't interfere (rare — usually only useful when an antivirus shim is mis-attributed), pass `--force` to skip the check. In that case the updater will still retry the `.exe` rename with exponential backoff and, on stubborn locks, schedule the replacement for next reboot via `MoveFileEx(MOVEFILE_DELAY_UNTIL_REBOOT)` so the update can complete.

Expected output looks like:

```
$ thoth update
Updating Thoth Agent...
📥 Pulling latest code...
Already up to date.  (or: Updating abc1234..def5678)
📦 Updating dependencies...
✅ Dependencies updated
🔍 Checking for new config options...
✅ Config is up to date  (or: Found 2 new options — running migration...)
🔄 Restarting gateways...
✅ Gateway restarted
✅ Thoth Agent updated successfully!
```

### Recommended Post-Update Validation

`thoth update` handles the main update path, but a quick validation confirms everything landed cleanly:

1. `git status --short` — if the tree is unexpectedly dirty, inspect before continuing
2. `thoth doctor` — checks config, dependencies, and service health
3. `thoth --version` — confirm the version bumped as expected
4. If you use the gateway: `thoth gateway status`
5. If `doctor` reports npm audit issues: run `npm audit fix` in the flagged directory

:::warning Dirty working tree after update
If `git status --short` shows unexpected changes after `thoth update`, stop and inspect them before continuing. This usually means local modifications were reapplied on top of the updated code, or a dependency step refreshed lockfiles.
:::

### If your terminal disconnects mid-update

`thoth update` protects itself against accidental terminal loss:

- The update ignores `SIGHUP`, so closing your SSH session or terminal window no longer kills it mid-install. `pip` and `git` child processes inherit this protection, so the Python environment cannot be left half-installed by a dropped connection.
- All output is mirrored to `~/.thoth/logs/update.log` while the update runs. If your terminal disappears, reconnect and inspect the log to see whether the update finished and whether the gateway restart succeeded:

```bash
tail -f ~/.thoth/logs/update.log
```

- `Ctrl-C` (SIGINT) and system shutdown (SIGTERM) are still honored — those are deliberate cancellations, not accidents.

You no longer need to wrap `thoth update` in `screen` or `tmux` to survive a terminal drop.

### Checking your current version

```bash
thoth version
```

Compare against the latest release at the [GitHub releases page](https://github.com/519lab/thoth-agent/releases).

### Updating from Messaging Platforms

You can also update directly from Telegram, Discord, Slack, WhatsApp, or Teams by sending:

```
/update
```

This pulls the latest code, updates dependencies, and restarts running gateways. The bot will briefly go offline during the restart (typically 5–15 seconds) and then resume.

### Manual Update

If you installed manually (not via the quick installer):

```bash
cd /path/to/thoth-agent
export VIRTUAL_ENV="$(pwd)/venv"

# Pull latest code
git pull origin main

# Reinstall (picks up new dependencies)
uv pip install -e ".[all]"

# Check for new config options
thoth config check
thoth config migrate   # Interactively add any missing options
```

### Rollback instructions

If an update introduces a problem, you can roll back to a previous version:

```bash
cd /path/to/thoth-agent

# List recent versions
git log --oneline -10

# Roll back to a specific commit
git checkout <commit-hash>
git submodule update --init --recursive
uv pip install -e ".[all]"

# Restart the gateway if running
thoth gateway restart
```

To roll back to a specific release tag:

```bash
git checkout v0.6.0
git submodule update --init --recursive
uv pip install -e ".[all]"
```

:::warning
Rolling back may cause config incompatibilities if new options were added. Run `thoth config check` after rolling back and remove any unrecognized options from `config.yaml` if you encounter errors.
:::


---

## Uninstalling

### Git installs

```bash
thoth uninstall
```

The uninstaller gives you the option to keep your configuration files (`~/.thoth/`) for a future reinstall.

### pip installs

```bash
pip uninstall thoth-agent
rm -rf ~/.thoth            # Optional — keep if you plan to reinstall
```

### Manual Uninstall

```bash
rm -f ~/.local/bin/thoth
rm -rf /path/to/thoth-agent
rm -rf ~/.thoth            # Optional — keep if you plan to reinstall
```

:::info
If you installed the gateway as a system service, stop and disable it first:
```bash
thoth gateway stop
# Linux: systemctl --user disable thoth-gateway
# macOS: launchctl remove ai.thoth.gateway
```
:::
