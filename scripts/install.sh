#!/bin/bash
# ============================================================================
# Thoth Agent installer
# ============================================================================
# Thoth uses PostgreSQL 17 + pgvector for all state (session transcripts,
# kanban, substrate slices). No SQLite.
#
# Defaults:
#
#   - INSTALL_DIR:  ~/.thoth/app
#   - THOTH_HOME:  ~/.thoth
#   - CLI command:  thoth
#   - PostgreSQL:   docker compose service on port 5432, db `thoth`
#
# If you are installing on a machine that already has an upstream
# 519lab/thoth-agent install and want to coexist without overwriting
# it, override the defaults explicitly:
#
#   curl ... | bash -s -- --cli-name thoth-substrate --thoth-home ~/.thoth-substrate
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/519lab/thoth-agent/main/scripts/install.sh | bash
#
# Or with options:
#   curl -fsSL ... | bash -s -- --skip-postgres --skip-setup
#
# ============================================================================

set -e

# ── Environment guards ──────────────────────────────────────────────────────
# A pre-set PYTHONPATH can force pip/entrypoints to import a different
# checkout than the one being installed, which makes fresh installs appear
# broken or stale. Same idea as upstream — preserved here.
if [ -n "${PYTHONPATH:-}" ]; then
    echo "⚠ Ignoring inherited PYTHONPATH during install to avoid module shadowing"
    unset PYTHONPATH
fi
if [ -n "${PYTHONHOME:-}" ]; then
    echo "⚠ Ignoring inherited PYTHONHOME during install"
    unset PYTHONHOME
fi
# Prevent uv from discovering config files (uv.toml, pyproject.toml) from the
# wrong user's home when running under sudo -u <user>.
export UV_NO_CONFIG=1

# ── Colors ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
MAGENTA='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m'
BOLD='\033[1m'

# ── Configuration ───────────────────────────────────────────────────────────
REPO_URL_SSH="git@github.com:519lab/thoth-agent.git"
REPO_URL_HTTPS="https://github.com/519lab/thoth-agent.git"

THOTH_HOME_DEFAULT="$HOME/.thoth"
CLI_NAME_DEFAULT="thoth"

THOTH_HOME="${THOTH_HOME:-$THOTH_HOME_DEFAULT}"
CLI_NAME="${THOTH_CLI_NAME:-${THOTH_CLI_NAME:-$CLI_NAME_DEFAULT}}"

# INSTALL_DIR resolved after arg parsing + OS detection.
if [ -n "${THOTH_INSTALL_DIR:-}" ]; then
    INSTALL_DIR="$THOTH_INSTALL_DIR"
    INSTALL_DIR_EXPLICIT=true
else
    INSTALL_DIR=""
    INSTALL_DIR_EXPLICIT=false
fi

PYTHON_VERSION="3.11"
NODE_VERSION="22"
# Minimum system Node we'll accept before falling back to a managed install.
# package.json engines require >=20; older distro node → EBADENGINE and
# silently-broken browser tools / TUI / dashboard.
NODE_MIN_MAJOR="20"
# Minimum uv that reliably supports `uv python find` + `uv sync --locked`.
# Older uv self-updates / gets reinstalled rather than being trusted.
UV_MIN_VERSION="0.4.0"

# PostgreSQL — substrate's source of truth.
# Defaults match the docker-compose.yml shipped with this repo.
PG_HOST_DEFAULT="localhost"
PG_PORT_DEFAULT="5432"
PG_USER_DEFAULT="thoth"
PG_PASSWORD_DEFAULT="thoth"
PG_DATABASE_DEFAULT="thoth"

# ── FHS-style root install layout (set by resolve_install_layout) ──────────
ROOT_FHS_LAYOUT=false
DETECTED_BROWSER_EXECUTABLE=""

# ── Update vs. fresh install (set by detect_install_mode after arg parsing) ─
# IS_UPDATE=true when the installer is re-running against an existing install
# (detected by ``$THOTH_HOME/.install_log`` or an existing git checkout at
# $INSTALL_DIR). On updates we preserve user-customized config, skip the
# setup wizard when API keys are already configured, and back up .env
# before any in-place mutation.
IS_UPDATE=false

# ── Options ─────────────────────────────────────────────────────────────────
USE_VENV=true
RUN_SETUP=true
SKIP_BROWSER=false
SKIP_POSTGRES=false        # NEW: skip docker compose up + alembic upgrade
RESET_DB=false             # NEW: drop the postgres data volume before compose-up
SKIP_NODE=false            # NEW: skip web dashboard npm install
# Force-rewrite preserves nothing: THOTH_PG_DSN, browser env, etc. get
# rewritten even if the user customized them. Existing values are backed
# up to ``$THOTH_HOME/.install-backup/`` first. Off by default — the
# installer auto-detects updates and keeps user config intact.
FORCE_REWRITE_CONFIG=false
BRANCH="main"

# Detect non-interactive mode (curl | bash)
if [ -t 0 ]; then
    IS_INTERACTIVE=true
else
    IS_INTERACTIVE=false
fi

# ── Argument parsing ───────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case $1 in
        --no-venv)         USE_VENV=false; shift ;;
        --skip-setup)      RUN_SETUP=false; shift ;;
        --skip-browser|--no-playwright) SKIP_BROWSER=true; shift ;;
        --skip-node)       SKIP_NODE=true; shift ;;
        --skip-postgres|--no-postgres) SKIP_POSTGRES=true; shift ;;
        --reset-db)        RESET_DB=true; shift ;;
        --branch)          BRANCH="$2"; shift 2 ;;
        --dir)             INSTALL_DIR="$2"; INSTALL_DIR_EXPLICIT=true; shift 2 ;;
        --thoth-home)      THOTH_HOME="$2"; shift 2 ;;
        --cli-name)        CLI_NAME="$2"; shift 2 ;;
        --pg-dsn)          PG_DSN_OVERRIDE="$2"; shift 2 ;;
        --force-rewrite-config) FORCE_REWRITE_CONFIG=true; shift ;;
        -h|--help)
            cat <<HELP_EOF
Thoth Agent installer

Usage: install.sh [OPTIONS]

Options:
  --no-venv           Don't create virtual environment
  --skip-setup        Skip interactive setup wizard
  --skip-browser      Skip Playwright/Chromium install
  --skip-node         Skip web dashboard npm install (no dashboard)
  --skip-postgres     Skip docker compose up + Alembic migrations
                        Use this if you have your own PostgreSQL and will
                        set THOTH_PG_DSN + run 'alembic upgrade head' yourself
  --reset-db          Destroy the existing PostgreSQL data volume before
                        starting Postgres, so the install begins from a clean
                        database. WITHOUT this flag the installer NEVER drops
                        data — it reuses any existing volume (and warns loudly
                        that it is doing so). Use --reset-db when a leftover
                        volume from a previous install has a stale schema /
                        alembic_version that breaks fresh migrations.
                        DESTRUCTIVE: all substrate state in that volume is lost.
  --branch NAME       Git branch to install (default: main)
  --dir PATH          Installation directory
                        default (non-root): ~/.thoth/app
                        default (root, Linux): /usr/local/lib/thoth
  --thoth-home PATH   Data directory
                        default: ~/.thoth
                        (Override env: THOTH_HOME)
  --cli-name NAME     Name for the CLI shim
                        default: thoth
                        Pass a different name (e.g. thoth-substrate) to
                        coexist with another Thoth install on the same machine.
                        (Override env: THOTH_CLI_NAME)
  --pg-dsn URL        PostgreSQL DSN to use
                        default: postgresql://thoth:thoth@localhost:5432/thoth
                        (matches the docker-compose service shipped with this repo)
  --force-rewrite-config
                      On updates, rewrite THOTH_PG_DSN and other installer-
                      managed entries in .env even when they have been
                      customized (a timestamped backup is written to
                      $THOTH_HOME/.install-backup/ first). Default: preserve
                      user-customized values.
  -h, --help          Show this help

Side-by-side install (coexist with another Thoth install):
  curl ... | bash -s -- --cli-name thoth-substrate --thoth-home ~/.thoth-substrate

Custom PostgreSQL (e.g. your own cluster, Neon, Supabase):
  curl ... | bash -s -- --skip-postgres --pg-dsn 'postgresql://user:pw@host:5432/db'

Configurable embedding dimension (THOTH_EMBEDDING_DIM, default 1536):
  The substrate stores text embeddings in a fixed-dim pgvector column.
  1536 matches OpenAI text-embedding-3-small / ada-002 and proxies thereof.
  Local models output different dims (Ollama nomic-embed-text = 768,
  mxbai-embed-large = 1024). Set THOTH_EMBEDDING_DIM at install time to
  shape the column for your chosen model:
    THOTH_EMBEDDING_DIM=768 curl ... | bash
  See substrate/recall/embeddings.py for the runtime auxiliary.embedding
  config schema (provider/model/base_url/api_key).

HELP_EOF
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ── Helper functions ───────────────────────────────────────────────────────
print_banner() {
    echo ""
    echo -e "${MAGENTA}${BOLD}"
    echo "┌─────────────────────────────────────────────────────────┐"
    echo "│   ⚕ Thoth Agent                                         │"
    echo "└─────────────────────────────────────────────────────────┘"
    echo -e "${NC}"
}

log_info()    { echo -e "${CYAN}→${NC} $1"; }
log_success() { echo -e "${GREEN}✓${NC} $1"; }
log_warn()    { echo -e "${YELLOW}⚠${NC} $1"; }
log_error()   { echo -e "${RED}✗${NC} $1"; }

# _version_ge A B → succeeds when version A is >= version B. Uses `sort -V`
# so multi-component versions (0.4.18 vs 0.4.0) compare correctly. Falls
# back to a string compare if `sort -V` is unavailable.
_version_ge() {
    [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V 2>/dev/null | head -1)" = "$2" ]
}

# Portable in-place sed. GNU sed accepts `sed -i EXPR FILE`; BSD/macOS sed
# requires a backup-suffix arg (`sed -i '' EXPR FILE`) and otherwise errors —
# which, bare under `set -e`, aborts the whole install on re-runs (#214).
# Sidestep the incompatibility with a temp-file rewrite.
_sed_inplace() {
    local expr="$1" file="$2" tmp
    tmp="$(mktemp 2>/dev/null || echo "${file}.tmp.$$")"
    sed "$expr" "$file" > "$tmp" && mv "$tmp" "$file"
}

prompt_yes_no() {
    local question="$1"
    local default="${2:-yes}"
    local prompt_suffix
    local answer=""

    # bash 3.2-compatible case (macOS /bin/bash)
    case "$default" in
        [yY]|[yY][eE][sS]|[tT][rR][uU][eE]|1) prompt_suffix="[Y/n]" ;;
        *) prompt_suffix="[y/N]" ;;
    esac

    if [ "$IS_INTERACTIVE" = true ]; then
        read -r -p "$question $prompt_suffix " answer || answer=""
    elif [ -r /dev/tty ] && [ -w /dev/tty ]; then
        printf "%s %s " "$question" "$prompt_suffix" > /dev/tty
        IFS= read -r answer < /dev/tty || answer=""
    else
        answer=""
    fi

    answer="${answer#"${answer%%[![:space:]]*}"}"
    answer="${answer%"${answer##*[![:space:]]}"}"

    if [ -z "$answer" ]; then
        case "$default" in
            [yY]|[yY][eE][sS]|[tT][rR][uU][eE]|1) return 0 ;;
            *) return 1 ;;
        esac
    fi

    case "$answer" in
        [yY]|[yY][eE][sS]) return 0 ;;
        *) return 1 ;;
    esac
}

is_termux() {
    [ -n "${TERMUX_VERSION:-}" ] || [[ "${PREFIX:-}" == *"com.termux/files/usr"* ]]
}

# Resolve installation layout. Substrate edition keeps the same layout
# decision tree as upstream but with new defaults.
detect_install_mode() {
    # An "update" is when ANY of these markers exist from a prior install
    # against this $THOTH_HOME / $INSTALL_DIR pair:
    #   - $THOTH_HOME/.install_log   — written at the end of every install
    #   - $THOTH_HOME/.thoth_install — written by copy_config_templates
    #   - $INSTALL_DIR/.git          — repo is already cloned
    # Any one of these indicates the user has run the installer before, so
    # we preserve their config/state and skip first-run wizards.
    if [ -f "$THOTH_HOME/.install_log" ] \
       || [ -f "$THOTH_HOME/.thoth_install" ] \
       || [ -d "$INSTALL_DIR/.git" ]; then
        IS_UPDATE=true
        log_info "Existing installation detected — running in UPDATE mode."
        log_info "  Your config (.env, config.yaml, SOUL.md) will be preserved."
        log_info "  Setup wizard will be skipped if API keys are already configured."
        log_info "  Pass --force-rewrite-config to override (rare; backs up existing first)."
    fi
}

resolve_install_layout() {
    if [ "$INSTALL_DIR_EXPLICIT" = true ]; then
        log_info "Install directory: $INSTALL_DIR (explicit)"
        return 0
    fi

    if is_termux; then
        INSTALL_DIR="$THOTH_HOME/app"
        return 0
    fi

    # Root on Linux: FHS layout, unless an install already exists at THOTH_HOME.
    if [ "$OS" = "linux" ] && [ "$(id -u)" -eq 0 ]; then
        if [ -d "$THOTH_HOME/app/.git" ]; then
            INSTALL_DIR="$THOTH_HOME/app"
            log_info "Existing install detected at $INSTALL_DIR — keeping layout"
            return 0
        fi
        INSTALL_DIR="/usr/local/lib/thoth"
        ROOT_FHS_LAYOUT=true
        log_info "Root install on Linux — using FHS layout"
        log_info "  Code:    $INSTALL_DIR"
        log_info "  Command: /usr/local/bin/$CLI_NAME"
        log_info "  Data:    $THOTH_HOME (unchanged)"
        return 0
    fi

    # Non-root default: the ``app`` checkout under THOTH_HOME.
    INSTALL_DIR="$THOTH_HOME/app"
}

get_command_link_dir() {
    if is_termux && [ -n "${PREFIX:-}" ]; then
        echo "$PREFIX/bin"
    elif [ "$ROOT_FHS_LAYOUT" = true ]; then
        echo "/usr/local/bin"
    else
        echo "$HOME/.local/bin"
    fi
}

get_command_link_display_dir() {
    if is_termux && [ -n "${PREFIX:-}" ]; then
        echo '$PREFIX/bin'
    elif [ "$ROOT_FHS_LAYOUT" = true ]; then
        echo '/usr/local/bin'
    else
        echo '~/.local/bin'
    fi
}

get_thoth_command_path() {
    local link_dir
    link_dir="$(get_command_link_dir)"
    if [ -x "$link_dir/$CLI_NAME" ]; then
        echo "$link_dir/$CLI_NAME"
    else
        echo "$CLI_NAME"
    fi
}

# Warn ONLY when we'd actually overwrite a foreign install, not on a
# normal re-install of our own launcher. One check:
#
#   * An existing ``thoth`` on PATH resolves to a different real file
#     than the one we're about to write. ``command -v`` plus
#     ``readlink -f`` canonicalize both sides so re-installing the
#     same launcher from a path that includes ``~`` vs ``/home/user``
#     vs a symlinked dir compares equal and the check stays quiet.
#
# When the check doesn't fire (the common case — first-time install OR a
# re-install of the same Thoth), the function exits silently.
warn_upstream_collision() {
    local saw_collision=false

    if [ "$CLI_NAME" = "thoth" ] && command -v thoth >/dev/null 2>&1; then
        local existing existing_canon target_link target_canon
        existing="$(command -v thoth)"
        target_link="$(get_command_link_dir)/thoth"
        # Canonicalize both sides so the same physical file (reached via
        # different paths — symlinks, ``~`` vs ``$HOME``, /usr/local
        # shims) compares equal. ``readlink -f`` returns the path even
        # for regular files (just resolves any symlinks in path
        # components). Falls back to the raw path if readlink fails.
        existing_canon="$(readlink -f "$existing" 2>/dev/null || echo "$existing")"
        target_canon="$(readlink -f "$target_link" 2>/dev/null || echo "$target_link")"
        if [ "$existing_canon" != "$target_canon" ]; then
            log_warn "CLI_NAME=thoth will install a launcher at $(get_command_link_display_dir)/thoth"
            log_warn "  which shadows the existing 'thoth' command at: $existing"
            saw_collision=true
        fi
    fi

    if [ "$saw_collision" = true ]; then
        if [ "$IS_INTERACTIVE" = true ] || [ -r /dev/tty ]; then
            if ! prompt_yes_no "Continue anyway?" "no"; then
                echo "Aborted. Re-run with --thoth-home and/or --cli-name to install side-by-side."
                exit 1
            fi
        else
            log_warn "Non-interactive — proceeding (set --thoth-home and --cli-name explicitly if this is wrong)."
        fi
    fi
}

# ── System detection ───────────────────────────────────────────────────────
detect_os() {
    case "$(uname -s)" in
        Linux*)
            if is_termux; then
                OS="android"; DISTRO="termux"
            else
                OS="linux"
                if [ -f /etc/os-release ]; then
                    . /etc/os-release
                    DISTRO="$ID"
                else
                    DISTRO="unknown"
                fi
            fi
            ;;
        Darwin*) OS="macos"; DISTRO="macos" ;;
        CYGWIN*|MINGW*|MSYS*)
            OS="windows"; DISTRO="windows"
            log_error "Windows detected. Please use the PowerShell installer:"
            log_info "  iex (irm https://raw.githubusercontent.com/519lab/thoth-agent/main/scripts/install.ps1)"
            exit 1
            ;;
        *) OS="unknown"; DISTRO="unknown"; log_warn "Unknown operating system" ;;
    esac
    log_success "Detected: $OS ($DISTRO)"
}

# ── Dependency checks ──────────────────────────────────────────────────────
install_uv() {
    if [ "$DISTRO" = "termux" ]; then
        log_info "Termux detected — using stdlib venv + pip instead of uv"
        UV_CMD=""
        return 0
    fi
    log_info "Checking for uv package manager..."
    # Accept an existing uv only if it meets the minimum version; `uv python
    # find` and `uv sync --locked` need a reasonably recent uv (#222). An old
    # uv falls through and gets (re)installed via the astral installer below.
    local _uv_candidate="" _uv_where=""
    if command -v uv &> /dev/null; then
        _uv_candidate="uv"; _uv_where=""
    elif [ -x "$HOME/.local/bin/uv" ]; then
        _uv_candidate="$HOME/.local/bin/uv"; _uv_where=" at ~/.local/bin"
    elif [ -x "$HOME/.cargo/bin/uv" ]; then
        _uv_candidate="$HOME/.cargo/bin/uv"; _uv_where=" at ~/.cargo/bin"
    fi
    if [ -n "$_uv_candidate" ]; then
        local _uv_ver
        _uv_ver="$("$_uv_candidate" --version 2>/dev/null | awk '{print $2}')"
        if [ -n "$_uv_ver" ] && _version_ge "$_uv_ver" "$UV_MIN_VERSION"; then
            UV_CMD="$_uv_candidate"; log_success "uv found$_uv_where ($($UV_CMD --version 2>/dev/null))"; return 0
        fi
        log_warn "uv ${_uv_ver:-unknown} is older than $UV_MIN_VERSION — upgrading via the astral installer"
    fi
    log_info "Installing uv (fast Python package manager)..."
    local _log _inst
    _log="$(mktemp 2>/dev/null || echo "/tmp/thoth-uv-install.$$.log")"
    _inst="$(mktemp 2>/dev/null || echo "/tmp/thoth-uv-installer.$$.sh")"
    if ! curl -LsSf https://astral.sh/uv/install.sh -o "$_inst" 2>"$_log"; then
        log_error "Failed to download uv installer"; sed 's/^/    /' "$_log" >&2
        rm -f "$_log" "$_inst"; exit 1
    fi
    if sh "$_inst" >>"$_log" 2>&1; then
        rm -f "$_inst"
        if [ -x "$HOME/.local/bin/uv" ]; then UV_CMD="$HOME/.local/bin/uv"
        elif [ -x "$HOME/.cargo/bin/uv" ]; then UV_CMD="$HOME/.cargo/bin/uv"
        elif command -v uv &> /dev/null; then UV_CMD="uv"
        else log_error "uv installer reported success but binary missing"; sed 's/^/    /' "$_log" >&2; rm -f "$_log"; exit 1
        fi
        rm -f "$_log"
        log_success "uv installed ($($UV_CMD --version 2>/dev/null))"
    else
        log_error "Failed to install uv"; sed 's/^/    /' "$_log" >&2
        rm -f "$_log" "$_inst"; exit 1
    fi
}

check_python() {
    if [ "$DISTRO" = "termux" ]; then
        log_info "Checking Termux Python..."
        if command -v python >/dev/null 2>&1; then
            PYTHON_PATH="$(command -v python)"
            if "$PYTHON_PATH" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
                log_success "Python found: $("$PYTHON_PATH" --version 2>/dev/null)"; return 0
            fi
        fi
        log_info "Installing Python via pkg..."
        pkg install -y python >/dev/null
        PYTHON_PATH="$(command -v python)"
        log_success "Python installed: $("$PYTHON_PATH" --version 2>/dev/null)"
        return 0
    fi

    log_info "Checking Python $PYTHON_VERSION..."
    if PYTHON_PATH="$("$UV_CMD" python find "$PYTHON_VERSION" 2>/dev/null)"; then
        log_success "Python found: $("$PYTHON_PATH" --version 2>/dev/null)"; return 0
    fi
    log_info "Python $PYTHON_VERSION not found, installing via uv..."
    if "$UV_CMD" python install "$PYTHON_VERSION"; then
        PYTHON_PATH="$("$UV_CMD" python find "$PYTHON_VERSION")"
        log_success "Python installed: $("$PYTHON_PATH" --version 2>/dev/null)"
    else
        log_error "Failed to install Python $PYTHON_VERSION"; exit 1
    fi
}

check_git() {
    log_info "Checking Git..."
    if command -v git &> /dev/null; then
        log_success "Git $(git --version | awk '{print $3}') found"
        return 0
    fi
    log_error "Git not found"
    if [ "$DISTRO" = "termux" ]; then
        log_info "Installing Git via pkg..."
        pkg install -y git >/dev/null
        command -v git >/dev/null 2>&1 && { log_success "Git installed"; return 0; }
    fi
    case "$OS" in
        linux)
            case "$DISTRO" in
                ubuntu|debian) log_info "  sudo apt install git" ;;
                fedora)        log_info "  sudo dnf install git" ;;
                arch)          log_info "  sudo pacman -S git" ;;
                *)             log_info "  Use your package manager to install git" ;;
            esac
            ;;
        android) log_info "  pkg install git" ;;
        macos)   log_info "  xcode-select --install  (or: brew install git)" ;;
    esac
    exit 1
}

# Docker is required for the bundled PostgreSQL service. (Or pass
# --skip-postgres and set THOTH_PG_DSN to your own cluster.)
check_docker() {
    if [ "$SKIP_POSTGRES" = true ]; then
        log_info "Skipping Docker check (--skip-postgres)"
        return 0
    fi
    log_info "Checking Docker (for PostgreSQL)..."
    if ! command -v docker >/dev/null 2>&1; then
        log_error "Docker not found"
        log_info "Thoth needs PostgreSQL. Install Docker Desktop or Docker Engine, then re-run."
        case "$OS" in
            linux)   log_info "  https://docs.docker.com/engine/install/" ;;
            macos)   log_info "  https://docs.docker.com/desktop/install/mac-install/" ;;
            android) log_warn "Docker is not available on Termux. Pass --skip-postgres and use a remote PG." ;;
        esac
        log_info ""
        log_info "Or skip Docker and provide your own PostgreSQL:"
        log_info "  --skip-postgres --pg-dsn 'postgresql://user:pw@host:5432/db'"
        log_info "  (you'll still need to run 'alembic upgrade head' yourself)"
        exit 1
    fi
    # Capture stderr (drop stdout) so we can tell "daemon down" apart from
    # "daemon up but you lack permission". For rootless / user-not-in-docker-
    # group the daemon IS running and `systemctl start docker` is the wrong
    # remedy — the fix is the docker group (#219).
    local _docker_err
    if ! _docker_err="$(docker info 2>&1 >/dev/null)"; then
        case "$_docker_err" in
            *"permission denied"*|*"dial unix"*|*"connect: permission denied"*)
                log_error "Docker is running but you don't have permission to access it"
                log_info "  Add your user to the docker group, then re-run:"
                log_info "    sudo usermod -aG docker $USER && newgrp docker"
                ;;
            *)
                log_error "Docker is installed but not running"
                case "$OS" in
                    macos) log_info "  Launch Docker Desktop" ;;
                    linux) log_info "  sudo systemctl start docker  (or: sudo service docker start)" ;;
                esac
                ;;
        esac
        exit 1
    fi
    DOCKER_VERSION="$(docker --version | awk '{print $3}' | tr -d ',')"
    log_success "Docker $DOCKER_VERSION found"
    # Compose v2 ships as `docker compose` (subcommand); v1 was `docker-compose`.
    if docker compose version >/dev/null 2>&1; then
        DOCKER_COMPOSE="docker compose"
    elif command -v docker-compose >/dev/null 2>&1; then
        DOCKER_COMPOSE="docker-compose"
    else
        log_error "Docker Compose not found (need v2 'docker compose' or v1 'docker-compose')"
        log_info "Install: https://docs.docker.com/compose/install/"
        exit 1
    fi
    log_success "Docker Compose: $DOCKER_COMPOSE"
}

check_node() {
    if [ "$SKIP_NODE" = true ]; then
        log_info "Skipping Node.js check (--skip-node)"
        HAS_NODE=false
        return 0
    fi
    log_info "Checking Node.js (for dashboard + browser tools)..."
    if command -v node &> /dev/null; then
        # Gate on the major version: package.json engines require >=20, and an
        # old distro node otherwise breaks browser tools silently (#218).
        local node_major
        node_major="$(node --version 2>/dev/null | sed 's/^v//' | cut -d. -f1)"
        if [ -n "$node_major" ] && [ "$node_major" -ge "$NODE_MIN_MAJOR" ] 2>/dev/null; then
            log_success "Node.js $(node --version) found"
            HAS_NODE=true
            return 0
        fi
        log_warn "Node.js $(node --version 2>/dev/null) is older than v$NODE_MIN_MAJOR (required) — installing a Thoth-managed Node.js $NODE_VERSION LTS"
    fi
    if [ -x "$THOTH_HOME/node/bin/node" ]; then
        export PATH="$THOTH_HOME/node/bin:$PATH"
        log_success "Node.js $("$THOTH_HOME/node/bin/node" --version) found (Thoth-managed)"
        HAS_NODE=true
        return 0
    fi
    log_info "Node.js not found — installing Node.js $NODE_VERSION LTS..."
    install_node
}

install_node() {
    if [ "$DISTRO" = "termux" ]; then
        log_info "Installing Node.js via pkg..."
        if pkg install -y nodejs >/dev/null; then
            log_success "Node.js $(node --version) installed via pkg"; HAS_NODE=true
        else
            HAS_NODE=false
        fi
        return 0
    fi
    local arch=$(uname -m) node_arch node_os
    case "$arch" in
        x86_64)        node_arch="x64"    ;;
        aarch64|arm64) node_arch="arm64"  ;;
        armv7l)        node_arch="armv7l" ;;
        *) log_warn "Unsupported architecture ($arch)"; HAS_NODE=false; return 0 ;;
    esac
    case "$OS" in
        linux) node_os="linux"  ;;
        macos) node_os="darwin" ;;
        *) log_warn "Unsupported OS"; HAS_NODE=false; return 0 ;;
    esac
    local index_url="https://nodejs.org/dist/latest-v${NODE_VERSION}.x/"
    local tarball_name
    tarball_name=$(curl -fsSL "$index_url" | grep -oE "node-v${NODE_VERSION}\.[0-9]+\.[0-9]+-${node_os}-${node_arch}\.tar\.xz" | head -1)
    [ -z "$tarball_name" ] && tarball_name=$(curl -fsSL "$index_url" | grep -oE "node-v${NODE_VERSION}\.[0-9]+\.[0-9]+-${node_os}-${node_arch}\.tar\.gz" | head -1)
    if [ -z "$tarball_name" ]; then
        log_warn "Could not find Node.js $NODE_VERSION binary for $node_os-$node_arch"; HAS_NODE=false; return 0
    fi
    local tmp_dir; tmp_dir=$(mktemp -d)
    log_info "Downloading $tarball_name..."
    if ! curl -fsSL "${index_url}${tarball_name}" -o "$tmp_dir/$tarball_name"; then
        log_warn "Download failed"; rm -rf "$tmp_dir"; HAS_NODE=false; return 0
    fi
    log_info "Extracting to $THOTH_HOME/node/..."
    if [[ "$tarball_name" == *.tar.xz ]]; then
        tar xf "$tmp_dir/$tarball_name" -C "$tmp_dir"
    else
        tar xzf "$tmp_dir/$tarball_name" -C "$tmp_dir"
    fi
    local extracted_dir=$(ls -d "$tmp_dir"/node-v* 2>/dev/null | head -1)
    [ ! -d "$extracted_dir" ] && { log_warn "Extraction failed"; rm -rf "$tmp_dir"; HAS_NODE=false; return 0; }
    rm -rf "$THOTH_HOME/node"
    mkdir -p "$THOTH_HOME"
    mv "$extracted_dir" "$THOTH_HOME/node"
    rm -rf "$tmp_dir"
    mkdir -p "$HOME/.local/bin"
    ln -sf "$THOTH_HOME/node/bin/node" "$HOME/.local/bin/node"
    ln -sf "$THOTH_HOME/node/bin/npm"  "$HOME/.local/bin/npm"
    ln -sf "$THOTH_HOME/node/bin/npx"  "$HOME/.local/bin/npx"
    export PATH="$THOTH_HOME/node/bin:$PATH"
    log_success "Node.js $("$THOTH_HOME/node/bin/node" --version) installed to $THOTH_HOME/node/"
    HAS_NODE=true
}

check_network_prerequisites() {
    log_info "Checking internet connectivity..."
    if ! command -v curl >/dev/null 2>&1; then
        log_warn "curl not found; skipping connectivity probes"; return 0
    fi
    local failed=false url
    for url in "https://pypi.org/simple/" "https://github.com/"; do
        curl -fsSI --max-time 8 "$url" >/dev/null 2>&1 || { failed=true; log_warn "Could not reach $url"; }
    done
    if [ "$failed" = false ]; then
        log_success "Internet connectivity looks good"
    else
        log_warn "Network checks failed — install may not complete cleanly"
    fi
}

install_system_packages() {
    HAS_RIPGREP=false
    HAS_FFMPEG=false
    log_info "Checking ripgrep (fast file search)..."
    if command -v rg &> /dev/null; then
        log_success "$(rg --version | head -1) found"; HAS_RIPGREP=true
    fi
    log_info "Checking ffmpeg (TTS voice messages)..."
    if command -v ffmpeg &> /dev/null; then
        log_success "ffmpeg $(ffmpeg -version 2>/dev/null | head -1 | awk '{print $3}') found"
        HAS_FFMPEG=true
    fi

    [ "$HAS_RIPGREP" = true ] && [ "$HAS_FFMPEG" = true ] && return 0

    # Termux always needs the build toolchain too.
    if [ "$DISTRO" = "termux" ]; then
        local pkgs=(clang rust make pkg-config libffi openssl ca-certificates curl)
        [ "$HAS_RIPGREP" = false ] && pkgs+=(ripgrep)
        [ "$HAS_FFMPEG" = false ] && pkgs+=(ffmpeg)
        log_info "Installing Termux packages: ${pkgs[*]}"
        pkg install -y "${pkgs[@]}" >/dev/null && {
            command -v rg &>/dev/null && HAS_RIPGREP=true
            command -v ffmpeg &>/dev/null && HAS_FFMPEG=true
        } || log_warn "Could not auto-install all Termux packages"
        return 0
    fi

    local missing=()
    [ "$HAS_RIPGREP" = false ] && missing+=("ripgrep")
    [ "$HAS_FFMPEG" = false ] && missing+=("ffmpeg")

    if [ "$OS" = "macos" ] && command -v brew &> /dev/null; then
        log_info "Installing ${missing[*]} via Homebrew..."
        brew install "${missing[@]}" && {
            command -v rg &>/dev/null && HAS_RIPGREP=true
            command -v ffmpeg &>/dev/null && HAS_FFMPEG=true
        }
        return 0
    fi

    local pkg_install=""
    case "$DISTRO" in
        ubuntu|debian) pkg_install="apt install -y" ;;
        fedora)        pkg_install="dnf install -y" ;;
        arch)          pkg_install="pacman -S --noconfirm" ;;
    esac
    if [ -n "$pkg_install" ]; then
        case "$DISTRO" in
            ubuntu|debian) export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a ;;
        esac
        local install_cmd="$pkg_install ${missing[*]}"
        if [ "$(id -u)" -eq 0 ]; then
            $install_cmd && {
                command -v rg &>/dev/null && HAS_RIPGREP=true
                command -v ffmpeg &>/dev/null && HAS_FFMPEG=true
            }
        elif command -v sudo &> /dev/null && sudo -n true 2>/dev/null; then
            sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a $install_cmd && {
                command -v rg &>/dev/null && HAS_RIPGREP=true
                command -v ffmpeg &>/dev/null && HAS_FFMPEG=true
            }
        elif command -v sudo &> /dev/null && [ "$IS_INTERACTIVE" = true ]; then
            log_info "sudo needed to install optional system packages: ${missing[*]}"
            if prompt_yes_no "Install via sudo?" "yes"; then
                sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a $install_cmd && {
                    command -v rg &>/dev/null && HAS_RIPGREP=true
                    command -v ffmpeg &>/dev/null && HAS_FFMPEG=true
                }
            fi
        fi
    fi

    [ "$HAS_RIPGREP" = false ] && log_warn "ripgrep missing (file search falls back to grep)"
    [ "$HAS_FFMPEG"  = false ] && log_warn "ffmpeg missing (TTS voice messages limited)"
}

# ── Installation ───────────────────────────────────────────────────────────
clone_repo() {
    log_info "Installing to $INSTALL_DIR..."
    if [ -d "$INSTALL_DIR" ]; then
        if [ -d "$INSTALL_DIR/.git" ]; then
            log_info "Existing installation found, updating..."
            cd "$INSTALL_DIR"
            git fetch origin
            # The code dir is managed and DISPOSABLE — config + state live in
            # THOTH_HOME, never in this checkout. If it has diverged (hand-edits
            # from debugging, partially-deleted files, etc.), do NOT stash/replay:
            # that produces catastrophic modify/delete conflicts on nearly every
            # file. Instead back up the diff and hard-reset to the upstream branch.
            if [ -n "$(git status --porcelain)" ]; then
                local backup_dir="${THOTH_HOME:-$HOME/.thoth}/.install-backup"
                local stamp; stamp="$(date -u +%Y%m%dT%H%M%SZ)"
                local patch_file="$backup_dir/code-local-changes-$stamp.patch"
                mkdir -p "$backup_dir" 2>/dev/null || true
                git diff HEAD > "$patch_file" 2>/dev/null || true
                git status --porcelain > "$backup_dir/code-local-status-$stamp.txt" 2>/dev/null || true
                log_warn "Local changes in the code dir detected — resetting to a clean"
                log_warn "  upstream copy (the code dir is managed; your config + data in"
                log_warn "  ${THOTH_HOME:-$HOME/.thoth} are untouched)."
                [ -s "$patch_file" ] && log_warn "  Saved a diff of your changes to: $patch_file"
            fi
            # Force the local branch to exactly match upstream — conflict-proof.
            git checkout -q "$BRANCH" 2>/dev/null || git checkout -q -B "$BRANCH" "origin/$BRANCH"
            git reset --hard "origin/$BRANCH"
            # Remove stray now-untracked files (e.g. modules deleted upstream) but
            # KEEP gitignored build artifacts: no -x, so venv/ and node_modules/
            # (gitignored) are preserved; -e is belt-and-suspenders for old checkouts.
            git clean -fd -e venv -e node_modules >/dev/null 2>&1 || true
        else
            log_error "Directory exists but is not a git repository: $INSTALL_DIR"
            log_info "Remove it or choose a different directory with --dir"
            exit 1
        fi
    elif [ "${THOTH_INSTALL_SSH:-}" = "1" ]; then
        # Opt-in SSH (maintainers who want to push later). Falls back to HTTPS.
        log_info "Trying SSH clone (THOTH_INSTALL_SSH=1)..."
        if GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=5" \
           git clone --branch "$BRANCH" "$REPO_URL_SSH" "$INSTALL_DIR" 2>/dev/null; then
            log_success "Cloned via SSH"
        else
            rm -rf "$INSTALL_DIR" 2>/dev/null
            log_info "SSH failed, falling back to HTTPS..."
            git clone --branch "$BRANCH" "$REPO_URL_HTTPS" "$INSTALL_DIR" || { log_error "git clone failed"; exit 1; }
            log_success "Cloned via HTTPS"
        fi
    else
        # Public project: clone over HTTPS by default — no SSH keys or GitHub
        # auth required, works for anyone. (Set THOTH_INSTALL_SSH=1 to use SSH.)
        log_info "Cloning from the public repo over HTTPS..."
        git clone --branch "$BRANCH" "$REPO_URL_HTTPS" "$INSTALL_DIR" || { log_error "git clone failed"; exit 1; }
        log_success "Cloned via HTTPS"
    fi
    cd "$INSTALL_DIR"
    log_success "Repository ready"
}

setup_venv() {
    if [ "$USE_VENV" = false ]; then
        log_info "Skipping virtual environment (--no-venv)"; return 0
    fi
    if [ -d "venv" ]; then
        log_info "Virtual environment already exists, recreating..."
        rm -rf venv
    fi
    if [ "$DISTRO" = "termux" ]; then
        log_info "Creating virtual environment with Termux Python..."
        "$PYTHON_PATH" -m venv venv
    else
        log_info "Creating virtual environment with Python $PYTHON_VERSION..."
        $UV_CMD venv venv --python "$PYTHON_VERSION"
    fi
    log_success "Virtual environment ready ($(./venv/bin/python --version 2>/dev/null))"
}

install_deps() {
    log_info "Installing Python dependencies (this can take 1-5 minutes on first run)..."
    if [ "$USE_VENV" = true ]; then
        export VIRTUAL_ENV="$INSTALL_DIR/venv"
    fi

    # Termux path keeps the upstream pip+constraints flow (uv's not viable there).
    if [ "$DISTRO" = "termux" ]; then
        local PIP_PYTHON
        [ "$USE_VENV" = true ] && PIP_PYTHON="$INSTALL_DIR/venv/bin/python" || PIP_PYTHON="$PYTHON_PATH"
        if [ -z "${ANDROID_API_LEVEL:-}" ]; then
            ANDROID_API_LEVEL="$(getprop ro.build.version.sdk 2>/dev/null || echo 24)"
            export ANDROID_API_LEVEL
        fi
        "$PIP_PYTHON" -m pip install --upgrade pip setuptools wheel >/dev/null
        if "$PIP_PYTHON" -c 'import sys; raise SystemExit(0 if sys.platform == "android" else 1)' 2>/dev/null; then
            log_info "Android Python detected: prebuilding psutil compatibility shim..."
            "$PIP_PYTHON" "$INSTALL_DIR/scripts/install_psutil_android.py" --pip "$PIP_PYTHON -m pip" \
                || log_warn "psutil Android prebuild failed"
        fi
        if ! "$PIP_PYTHON" -m pip install -e '.[termux-all]' -c constraints-termux.txt; then
            "$PIP_PYTHON" -m pip install -e '.[termux]' -c constraints-termux.txt \
                || "$PIP_PYTHON" -m pip install -e '.' -c constraints-termux.txt \
                || { log_error "Termux pip install failed"; exit 1; }
        fi
        log_success "Python deps installed (Termux profile)"
        return 0
    fi

    # Hash-verified install via uv.lock (preferred). Fork's lockfile is the
    # source of truth for substrate/asyncpg/pgvector resolution — never
    # resolve those transitives on the fly.
    if [ -f "uv.lock" ]; then
        log_info "Installing curated [all] extra (hash-verified via uv.lock)..."
        if UV_PROJECT_ENVIRONMENT="$INSTALL_DIR/venv" $UV_CMD sync --extra all --locked; then
            log_success "Python deps installed (hash-verified)"
            return 0
        fi
        log_warn "uv.lock sync failed (see uv output above), falling back to PyPI resolve"
    fi

    # Fallback: PyPI resolve. Won't hash-verify but keeps installs working
    # if uv.lock is stale relative to pyproject.toml.
    if $UV_CMD pip install -e '.[all]'; then
        log_success "Python deps installed (PyPI resolve)"
    elif $UV_CMD pip install -e '.'; then
        log_warn "Installed core only — optional extras failed; some features off"
    else
        log_error "Python install failed even at core level"
        log_info "Check build tools: sudo apt install build-essential python3-dev libffi-dev"
        exit 1
    fi
}

# Verify the dependency install actually produced an importable venv. An
# interrupted `uv sync` / pip run can leave a venv that exists (so the
# `[ -d venv ]` check passes) but is missing CORE deps — alembic, sqlalchemy
# and asyncpg are declared in pyproject.toml's base dependencies (NOT extras),
# and the very next step (`alembic upgrade head`) imports all three. If the
# venv is half-built we must abort here with a clear message rather than fail
# cryptically inside the migration step.
verify_core_deps() {
    if [ "$USE_VENV" = false ]; then
        # No managed venv to check; deps live in the system/Termux interpreter
        # and are validated by the install path itself.
        return 0
    fi
    local venv_py="$INSTALL_DIR/venv/bin/python"
    if [ ! -x "$venv_py" ]; then
        log_error "Virtual environment python not found at $venv_py"
        log_error "  Dependency install did not complete. Re-run the installer."
        exit 1
    fi
    log_info "Verifying core dependencies are importable..."
    if "$venv_py" -c "import alembic, sqlalchemy, asyncpg" >/dev/null 2>&1; then
        log_success "Core dependencies present (alembic, sqlalchemy, asyncpg)"
    else
        log_error "Virtual environment is INCOMPLETE — core dependencies failed to import."
        log_error "  Expected: alembic, sqlalchemy, asyncpg (base deps in pyproject.toml)."
        log_error "  This usually means a previous dependency sync was interrupted."
        log_info  "  Fix: re-run the installer. It will recreate the venv and re-sync:"
        log_info  "    cd $INSTALL_DIR && rm -rf venv && \"$UV_CMD\" sync --extra all --locked"
        log_info  "  (or re-run install.sh, which does the same.)"
        log_error "Aborting before migrations — refusing to continue with a half-built venv."
        exit 1
    fi
}

# ── PostgreSQL via docker compose ──────────────────────────────────────────

# Detect a non-substrate PostgreSQL listening on the chosen port. If a native
# pg server (apt-installed `postgresql` is common on Ubuntu) is bound to
# 5432 *and* it doesn't accept our `thoth/thoth` creds, our docker
# container will silently fail to bind (or bind on a different interface)
# and every connection from the host will hit the native one and bounce
# with InvalidPasswordError. Probe first; if the port is taken by something
# other than our container, bump to the next free port and pin everything
# downstream to that port.
# Inspect / optionally destroy the named postgres data volume that the
# docker-compose `postgres` service mounts at /var/lib/postgresql/data
# (declared as `thoth_pg_data` in docker-compose.yml; the actual docker
# volume is prefixed with the compose project name, e.g.
# `thoth_thoth_pg_data`).
#
#   * Default (no --reset-db): NEVER deletes data. Logs LOUDLY that an
#     existing database is being reused, and — when cheaply obtainable —
#     prints its current alembic_version so the operator can see at a
#     glance whether the inherited schema matches this checkout.
#   * With --reset-db: drops the volume (via `docker compose down -v`,
#     which is project-aware and removes the named volume even when the
#     prefixed name varies) BEFORE compose-up, so the install starts from
#     a genuinely clean database.
_warn_or_reset_pg_volume() {
    # $1 (optional): name of the existing, still-running Thoth pg container,
    # used only to probe the inherited alembic_version before we tear it down.
    local probe_container="${1:-}"
    # Resolve the compose-prefixed volume name. `docker compose` knows the
    # project; ask it for the volume's full name. Fall back to the common
    # `thoth_thoth_pg_data` if the lookup yields nothing.
    local vol_name=""
    if command -v docker >/dev/null 2>&1; then
        vol_name=$(docker volume ls --format '{{.Name}}' 2>/dev/null \
            | grep -E '_thoth_pg_data$' | head -n1)
    fi

    if [ "$RESET_DB" = true ]; then
        log_warn "PostgreSQL: --reset-db given — DESTROYING the existing data volume"
        log_warn "  (all substrate state in it will be lost) before compose-up."
        # `down -v` is the project-aware, name-agnostic way to drop the
        # named volume. Run it from the install dir so compose resolves the
        # right project.
        ( cd "$INSTALL_DIR" 2>/dev/null && $DOCKER_COMPOSE down -v >/dev/null 2>&1 ) || true
        # Belt-and-suspenders: if a stray prefixed volume survived (e.g. the
        # container was removed out-of-band so compose no longer tracks it),
        # remove it directly.
        if [ -n "$vol_name" ]; then
            docker volume rm "$vol_name" >/dev/null 2>&1 || true
        fi
        log_info "PostgreSQL: data volume reset; install will start from a clean database."
        return 0
    fi

    # Reuse path — do NOT delete anything. Warn loudly.
    log_warn "PostgreSQL: REUSING existing database data (named volume '${vol_name:-thoth_pg_data}')."
    log_warn "  The new container re-attaches the OLD volume, so its schema, rows,"
    log_warn "  and alembic_version are inherited from the previous install."
    # Try to surface the inherited alembic_version cheaply. Best-effort: only
    # works if we were handed a still-running container to probe (the caller
    # invokes us before tearing the old container down).
    if [ -n "$probe_container" ] && command -v docker >/dev/null 2>&1; then
        local av
        av=$(docker exec "$probe_container" \
            psql -U "${POSTGRES_USER:-thoth}" -d "${POSTGRES_DB:-thoth}" \
            -tAc 'SELECT version_num FROM alembic_version' 2>/dev/null | head -n1 | tr -d '[:space:]')
        if [ -n "$av" ]; then
            log_warn "  Inherited alembic_version: $av"
        fi
    fi
    log_warn "  If this database has a stale/incompatible schema and migrations"
    log_warn "  fail, re-run the installer with --reset-db to start clean"
    log_warn "  (DESTRUCTIVE: wipes the inherited database)."
}

choose_pg_port() {
    if [ "$SKIP_POSTGRES" = true ]; then
        return 0
    fi

    # If --pg-dsn was passed, extract its host:port and use that for compose
    # binding too. The DSN tells us where to *connect*; if it points at
    # localhost:5434, our docker container must also bind to host port 5434
    # or alembic will hit a closed socket.
    if [ -n "${PG_DSN_OVERRIDE:-}" ]; then
        # Parse port from postgresql://user:pw@host:PORT/db. Tolerates missing
        # port (postgresql defaults to 5432) and missing user/pw segments.
        local dsn_port
        dsn_port=$(echo "$PG_DSN_OVERRIDE" | sed -nE 's|.*@[^:/]+:([0-9]+)/.*|\1|p')
        if [ -n "$dsn_port" ]; then
            PG_PORT_DEFAULT="$dsn_port"
            export POSTGRES_PORT="$dsn_port"
            log_info "PostgreSQL: --pg-dsn pins host port $dsn_port"
        fi
        return 0
    fi

    # Helper: does *something* answer a TCP connect on this port?
    _port_in_use() {
        local p="$1"
        if command -v ss >/dev/null 2>&1; then
            ss -tlnH "( sport = :$p )" 2>/dev/null | grep -q ':'
        elif command -v lsof >/dev/null 2>&1; then
            lsof -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1
        else
            (timeout 2 bash -c "exec 3<>/dev/tcp/127.0.0.1/$p" 2>/dev/null) && return 0 || return 1
        fi
    }

    # Upgrade-aware path: if a prior Thoth Postgres container exists
    # (running OR stopped), reclaim its port and remove the old container
    # so the new compose-up can bind cleanly. Without this, a container
    # left on port 5432 but unreferenceable by the new compose project
    # would push the next install to 5433, drifting further up the port
    # range on every re-install.
    #
    # Container name candidates, in priority order. First match wins:
    #   1. ``${COMPOSE_PROJECT_NAME}-postgres-1`` — the pinned project (thoth)
    #      resolved in resolve_compose_project
    #   2. ``thoth-postgres-1`` — stable fresh-install name
    local existing_container=""
    for name in "${COMPOSE_PROJECT_NAME:-thoth}-postgres-1" thoth-postgres-1; do
        if docker inspect "$name" >/dev/null 2>&1; then
            existing_container="$name"
            break
        fi
    done

    if [ -n "$existing_container" ]; then
        # Extract the host port the existing container was bound to.
        # ``docker port`` only works when the container is running;
        # ``docker inspect`` works in any state. Format of the inspect
        # output: ``5432/tcp:0.0.0.0:5432`` (port mapping line).
        local existing_port
        # Containers with both IPv4 and IPv6 bindings produce TWO
        # ``HostPort`` entries (e.g. 0.0.0.0:5433 + [::]:5433); the inner
        # ``{{range $conf}}`` concatenates them into "54335433" if we
        # don't separate them. Inject whitespace between values + take
        # the first field via awk so we always get a single port number.
        existing_port=$(docker inspect \
            --format='{{range $p, $conf := .NetworkSettings.Ports}}{{if eq $p "5432/tcp"}}{{range $conf}}{{.HostPort}} {{end}}{{end}}{{end}}' \
            "$existing_container" 2>/dev/null | awk '{print $1}')

        if [ -n "$existing_port" ]; then
            log_info "PostgreSQL upgrade: found existing container '$existing_container' on port $existing_port"
            # CRITICAL: removing the container does NOT remove its named data
            # volume (thoth_pg_data). The new compose-up re-attaches that same
            # volume, so the "fresh" install inherits the OLD database — schema,
            # rows, and alembic_version included. If that alembic_version is
            # ahead of (or inconsistent with) the migrations in this checkout,
            # `alembic upgrade head` blows up mid-migration with a cryptic
            # UndefinedTableError. Warn loudly that data is being reused (and
            # surface the inherited alembic_version while the old container is
            # still running, so the probe can reach it), and only ever drop
            # the volume when the operator explicitly opts in via --reset-db.
            _warn_or_reset_pg_volume "$existing_container"
            log_info "  Stopping + removing it so the new compose-up reuses the same port."
            docker rm -f "$existing_container" >/dev/null 2>&1 || true
            PG_PORT_DEFAULT="$existing_port"
            export POSTGRES_PORT="$existing_port"
            return 0
        fi

        # Container exists but has no published 5432 mapping (weird
        # config). Remove it; fall through to normal port selection.
        log_warn "PostgreSQL: found existing '$existing_container' with no 5432 mapping; removing"
        docker rm -f "$existing_container" >/dev/null 2>&1 || true
    fi

    # No existing Thoth container found. A named data volume can still
    # survive on its own (e.g. the container was removed out-of-band while
    # the volume persisted). If --reset-db was passed, drop it now so the
    # fresh compose-up starts clean; otherwise, if such a volume exists,
    # warn that it will be reused. We cannot probe alembic_version here (no
    # running server), hence no probe-container arg.
    if command -v docker >/dev/null 2>&1; then
        local orphan_vol
        orphan_vol=$(docker volume ls --format '{{.Name}}' 2>/dev/null \
            | grep -E '_thoth_pg_data$' | head -n1)
        if [ -n "$orphan_vol" ]; then
            # Reuses (warns) by default; drops only under --reset-db.
            _warn_or_reset_pg_volume
        fi
    fi

    # Fall back to the original collision-detection path. Probe the default
    # port; bump if something else is holding it.
    local port="$PG_PORT_DEFAULT"
    if _port_in_use "$port"; then
        log_warn "PostgreSQL: port $port is taken by something else (likely a native"
        log_warn "  Postgres install — apt-installed postgresql, system service, etc.)"
        local p
        for p in $(seq 5433 5450); do
            if ! _port_in_use "$p"; then
                port="$p"
                log_info "PostgreSQL: bumping to port $port to avoid collision"
                break
            fi
        done
        if [ "$port" = "$PG_PORT_DEFAULT" ]; then
            log_error "No free port in 5433-5450; aborting."
            log_info "Free a port or pass --pg-dsn pointing at an existing cluster."
            exit 1
        fi
    fi

    PG_PORT_DEFAULT="$port"
    export POSTGRES_PORT="$port"
}

# Pin a STABLE docker-compose project name, decoupled from the install dir.
# Compose otherwise derives the project (and thus the DB volume + container
# names) from the code-dir basename — so `~/.thoth/app` yields project `app`
# and volume `app_thoth_pg_data`. That couples the database's identity to a
# directory name, which would silently point a "fresh" install at a NEW empty
# volume and orphan the real data.
#
# Resolution (exported so EVERY `docker compose` call below is consistent,
# regardless of the `cd "$INSTALL_DIR"` the compose steps do):
#   1. An explicit COMPOSE_PROJECT_NAME in the environment always wins.
#   2. Otherwise → the stable Thoth name `thoth`.
resolve_compose_project() {
    if [ -n "${COMPOSE_PROJECT_NAME:-}" ]; then
        export COMPOSE_PROJECT_NAME
        log_info "PostgreSQL: compose project '$COMPOSE_PROJECT_NAME' (from environment)"
        return 0
    fi
    COMPOSE_PROJECT_NAME="thoth"
    export COMPOSE_PROJECT_NAME
    log_info "PostgreSQL: compose project '$COMPOSE_PROJECT_NAME'"
}

setup_postgres() {
    if [ "$SKIP_POSTGRES" = true ]; then
        log_info "Skipping PostgreSQL setup (--skip-postgres)"
        if [ -z "${PG_DSN_OVERRIDE:-}" ]; then
            log_warn "You'll need to set THOTH_PG_DSN yourself and run 'alembic upgrade head'"
        fi
        return 0
    fi

    # Pin the compose project BEFORE any docker-compose / container lookup so
    # the DB volume + container names are stable and dir-independent.
    resolve_compose_project
    choose_pg_port

    log_info "Starting PostgreSQL via docker compose (host port $PG_PORT_DEFAULT → container 5432)..."
    cd "$INSTALL_DIR"

    # docker-compose.yml ships with `postgres` (port 5432) + `postgres-test`
    # (port 5433, under `test` profile, only used by pytest). We only want
    # the real `postgres` service running for production use.
    if ! $DOCKER_COMPOSE up -d postgres; then
        log_error "Failed to start postgres service"
        log_info "Inspect: $DOCKER_COMPOSE logs postgres"
        exit 1
    fi

    log_info "Waiting for PostgreSQL to be healthy..."
    local i
    for i in $(seq 1 60); do
        if $DOCKER_COMPOSE ps postgres 2>/dev/null | grep -q "healthy"; then
            log_success "PostgreSQL is ready"
            break
        fi
        if [ "$i" -eq 60 ]; then
            log_error "PostgreSQL did not become healthy within 60s"
            log_info "Inspect: $DOCKER_COMPOSE logs postgres"
            exit 1
        fi
        sleep 1
    done
}

run_migrations() {
    if [ "$SKIP_POSTGRES" = true ] && [ -z "${PG_DSN_OVERRIDE:-}" ]; then
        log_info "Skipping Alembic migrations (no DSN configured)"
        return 0
    fi
    local dsn="${PG_DSN_OVERRIDE:-postgresql://${PG_USER_DEFAULT}:${PG_PASSWORD_DEFAULT}@${PG_HOST_DEFAULT}:${PG_PORT_DEFAULT}/${PG_DATABASE_DEFAULT}}"
    log_info "Running Alembic migrations against:"
    log_info "  $dsn"
    cd "$INSTALL_DIR"
    if THOTH_PG_DSN="$dsn" ./venv/bin/alembic -c migrations/alembic.ini upgrade head; then
        log_success "Substrate schema migrated to head"
    else
        log_error "Alembic upgrade failed"
        log_info "Check connectivity: THOTH_PG_DSN=\"$dsn\" ./venv/bin/python -c 'import asyncpg, asyncio; asyncio.run(asyncpg.connect(\"$dsn\"))'"
        exit 1
    fi
}

# ── PATH wiring (CLI launcher) ─────────────────────────────────────────────
setup_path() {
    log_info "Setting up $CLI_NAME command..."

    if [ "$USE_VENV" = true ]; then
        THOTH_BIN="$INSTALL_DIR/venv/bin/thoth"
    else
        THOTH_BIN="$(which thoth 2>/dev/null || echo "")"
        [ -z "$THOTH_BIN" ] && { log_warn "thoth not on PATH"; return 0; }
    fi

    if [ ! -x "$THOTH_BIN" ]; then
        log_warn "thoth entry point not found at $THOTH_BIN"
        log_info "Re-run: cd $INSTALL_DIR && $UV_CMD pip install -e '.[all]'"
        return 0
    fi

    local link_dir=$(get_command_link_dir)
    local link_disp=$(get_command_link_display_dir)
    mkdir -p "$link_dir"
    rm -f "$link_dir/$CLI_NAME"

    # Launcher injects THOTH_PG_DSN only when we actually have one.
    # Clears PYTHONPATH/PYTHONHOME so a parent process can't shadow this venv.
    # With --skip-postgres and no --pg-dsn there is no local cluster, so baking
    # the localhost:5432 default would just point the agent at a non-existent
    # DB — leave THOTH_PG_DSN unset and let the user's own env/.env win (#227).
    local pg_dsn_export=""
    if [ -n "${PG_DSN_OVERRIDE:-}" ]; then
        pg_dsn_export="export THOTH_PG_DSN=\"\${THOTH_PG_DSN:-$PG_DSN_OVERRIDE}\""
    elif [ "$SKIP_POSTGRES" != true ]; then
        local pg_dsn="postgresql://${PG_USER_DEFAULT}:${PG_PASSWORD_DEFAULT}@${PG_HOST_DEFAULT}:${PG_PORT_DEFAULT}/${PG_DATABASE_DEFAULT}"
        pg_dsn_export="export THOTH_PG_DSN=\"\${THOTH_PG_DSN:-$pg_dsn}\""
    fi
    # Pin the embedding dim choice (if the operator set one at install
    # time) into the launcher so re-installs and subsequent ``alembic
    # upgrade head`` invocations preserve the schema shape. Unset env →
    # nothing exported → migration 0009's default (1536) wins.
    local embed_dim_export=""
    if [ -n "${THOTH_EMBEDDING_DIM:-}" ]; then
        embed_dim_export="export THOTH_EMBEDDING_DIM=\"\${THOTH_EMBEDDING_DIM:-$THOTH_EMBEDDING_DIM}\""
    fi
    cat > "$link_dir/$CLI_NAME" <<EOF
#!/usr/bin/env bash
# Launcher generated by Thoth Substrate installer.
# Do not edit by hand — re-run install.sh to regenerate.
unset PYTHONPATH
unset PYTHONHOME
export THOTH_HOME="\${THOTH_HOME:-$THOTH_HOME}"
$pg_dsn_export
# Echo the user-facing launcher name into resume/setup hints. The venv
# console script is itself named "thoth" so argv[0] can't carry this.
export THOTH_CLI_NAME="\${THOTH_CLI_NAME:-$CLI_NAME}"
export THOTH_CLI_NAME="\${THOTH_CLI_NAME:-$CLI_NAME}"
$embed_dim_export
exec "$THOTH_BIN" "\$@"
EOF
    chmod +x "$link_dir/$CLI_NAME"
    log_success "Launcher installed → $link_disp/$CLI_NAME"

    if [ "$DISTRO" = "termux" ]; then
        export PATH="$link_dir:$PATH"
        return 0
    fi

    if [ "$ROOT_FHS_LAYOUT" = true ]; then
        export PATH="$link_dir:$PATH"
        if env -i HOME="$HOME" TERM="${TERM:-dumb}" bash -i -c "command -v $CLI_NAME" >/dev/null 2>&1; then
            log_info "/usr/local/bin is on PATH for all shells"
            return 0
        fi
        log_info "/usr/local/bin missing from non-login shells (RHEL-family); fixing ~/.bashrc"
        local PATH_LINE='export PATH="/usr/local/bin:$PATH"'
        for cfg in "$HOME/.bashrc" "$HOME/.bash_profile"; do
            [ -f "$cfg" ] || continue
            grep -v '^[[:space:]]*#' "$cfg" 2>/dev/null | grep -qE 'PATH=.*(/usr/local/bin|\$link_dir)' || {
                printf '\n# Thoth Substrate — ensure /usr/local/bin is on PATH\n%s\n' "$PATH_LINE" >> "$cfg"
                log_success "Added /usr/local/bin to $cfg"
            }
        done
        return 0
    fi

    # User-scoped: ensure ~/.local/bin on PATH for the user's actual login shell.
    if ! echo "$PATH" | tr ':' '\n' | grep -q "^$link_dir$"; then
        local LOGIN_SHELL="$(basename "${SHELL:-/bin/bash}")"
        local PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
        local cfgs=()
        case "$LOGIN_SHELL" in
            zsh)  [ -f "$HOME/.zshrc"     ] && cfgs+=("$HOME/.zshrc")
                  [ -f "$HOME/.zprofile"  ] && cfgs+=("$HOME/.zprofile")
                  [ ${#cfgs[@]} -eq 0 ] && { touch "$HOME/.zshrc"; cfgs+=("$HOME/.zshrc"); } ;;
            bash) [ -f "$HOME/.bashrc"       ] && cfgs+=("$HOME/.bashrc")
                  [ -f "$HOME/.bash_profile" ] && cfgs+=("$HOME/.bash_profile")
                  # No rc file at all (e.g. a minimal/login-only environment):
                  # create ~/.bashrc so ~/.local/bin actually persists — mirror
                  # the zsh/fish create-if-missing branches (#215).
                  [ ${#cfgs[@]} -eq 0 ] && { touch "$HOME/.bashrc"; cfgs+=("$HOME/.bashrc"); } ;;
            fish)
                local FISH_CONFIG="$HOME/.config/fish/config.fish"
                mkdir -p "$(dirname "$FISH_CONFIG")"
                touch "$FISH_CONFIG"
                grep -q 'fish_add_path.*\.local/bin' "$FISH_CONFIG" || {
                    printf '\n# Thoth Substrate — ensure ~/.local/bin is on PATH\nfish_add_path "$HOME/.local/bin"\n' >> "$FISH_CONFIG"
                    log_success "Added ~/.local/bin to $FISH_CONFIG"
                }
                ;;
            *)    [ -f "$HOME/.bashrc" ] && cfgs+=("$HOME/.bashrc")
                  [ -f "$HOME/.zshrc"  ] && cfgs+=("$HOME/.zshrc") ;;
        esac
        [ -f "$HOME/.profile" ] && cfgs+=("$HOME/.profile")
        local cfg
        for cfg in "${cfgs[@]}"; do
            grep -v '^[[:space:]]*#' "$cfg" 2>/dev/null | grep -qE 'PATH=.*\.local/bin' || {
                printf '\n# Thoth Substrate — ensure ~/.local/bin is on PATH\n%s\n' "$PATH_LINE" >> "$cfg"
                log_success "Added ~/.local/bin to $cfg"
            }
        done
    fi
    export PATH="$link_dir:$PATH"
}

# Back up $THOTH_HOME/.env before any in-place mutation. Backup name
# embeds an ISO timestamp + a short reason tag so users can tell which
# rewrite produced each file. No-op if .env doesn't exist yet.
_backup_env_file() {
    local reason="${1:-rewrite}"
    local env_file="$THOTH_HOME/.env"
    [ -f "$env_file" ] || return 0
    local backup_dir="$THOTH_HOME/.install-backup"
    mkdir -p "$backup_dir"
    local ts
    ts=$(date -u +%Y%m%dT%H%M%SZ)
    local backup_path="$backup_dir/.env.$ts.$reason"
    cp "$env_file" "$backup_path"
    log_info "Backed up .env to $backup_path"
}

# Predicate: does the user already have a usable provider API key in .env?
# Used by run_setup_wizard to skip re-prompting on updates when the user
# clearly already finished setup. Conservative — only checks the most
# common provider keys; if none match we still run the wizard.
_env_has_provider_api_key() {
    local env_file="$THOTH_HOME/.env"
    [ -f "$env_file" ] || return 1
    grep -qE '^(OPENAI_API_KEY|ANTHROPIC_API_KEY|OPENROUTER_API_KEY|NOUS_API_KEY|GEMINI_API_KEY|GROQ_API_KEY|XAI_API_KEY|MISTRAL_API_KEY|DEEPSEEK_API_KEY|OLLAMA_BASE_URL|CUSTOM_API_KEY)=..*' "$env_file"
}

copy_config_templates() {
    log_info "Setting up configuration files in $THOTH_HOME..."
    mkdir -p "$THOTH_HOME"/{cron,sessions,logs,pairing,hooks,image_cache,audio_cache,memories,skills}

    if [ ! -f "$THOTH_HOME/.env" ]; then
        if [ -f "$INSTALL_DIR/.env.example" ]; then
            cp "$INSTALL_DIR/.env.example" "$THOTH_HOME/.env"
        else
            touch "$THOTH_HOME/.env"
        fi
        log_success "Created $THOTH_HOME/.env"
    else
        log_info "$THOTH_HOME/.env exists, keeping it"
    fi
    chmod 600 "$THOTH_HOME/.env"

    # Ensure THOTH_PG_DSN in .env matches THIS install's PG so non-launcher
    # entry points (gateway, cron jobs spawned outside the shim) can find
    # the database. On updates we preserve user customizations: only rewrite
    # when the existing DSN looks installer-managed (points at the local
    # docker-compose PG via localhost/127.0.0.1/postgres host) AND the port
    # drifted. Custom DSNs — remote PG, custom creds, hosted Postgres
    # (Neon/Supabase/RDS) — are left untouched.
    local pg_dsn="${PG_DSN_OVERRIDE:-postgresql://${PG_USER_DEFAULT}:${PG_PASSWORD_DEFAULT}@${PG_HOST_DEFAULT}:${PG_PORT_DEFAULT}/${PG_DATABASE_DEFAULT}}"
    if grep -q '^THOTH_PG_DSN=' "$THOTH_HOME/.env" 2>/dev/null; then
        local cur
        cur=$(grep '^THOTH_PG_DSN=' "$THOTH_HOME/.env" | head -1 | cut -d= -f2-)
        if [ "$cur" != "$pg_dsn" ]; then
            # Detect "looks installer-managed" via host segment matching one
            # of the docker-compose-friendly localhost aliases.
            local _looks_local=false
            case "$cur" in
                *@localhost:*|*@127.0.0.1:*|*@postgres:*) _looks_local=true ;;
            esac

            if [ "$FORCE_REWRITE_CONFIG" = true ]; then
                _backup_env_file "force-rewrite-config"
                _sed_inplace "s|^THOTH_PG_DSN=.*|THOTH_PG_DSN=$pg_dsn|" "$THOTH_HOME/.env"
                log_success "Updated THOTH_PG_DSN in $THOTH_HOME/.env ($cur → $pg_dsn) [--force-rewrite-config]"
            elif [ "$_looks_local" = true ]; then
                # Local DSN whose port drifted (typical after a port-bump
                # on an upgrade). Safe to rewrite — but back up first.
                _backup_env_file "pg-dsn-port-drift"
                _sed_inplace "s|^THOTH_PG_DSN=.*|THOTH_PG_DSN=$pg_dsn|" "$THOTH_HOME/.env"
                log_success "Updated THOTH_PG_DSN in $THOTH_HOME/.env ($cur → $pg_dsn)"
            else
                # Non-local DSN — almost certainly user-customized
                # (remote PG, hosted service, custom creds). Leave it.
                log_warn "THOTH_PG_DSN in $THOTH_HOME/.env points at a non-local cluster:"
                log_warn "  $cur"
                log_warn "  This install's local PG is at $pg_dsn — NOT rewriting."
                log_warn "  Pass --force-rewrite-config to overwrite (backs up .env first)."
            fi
        fi
    else
        printf '\n# Substrate PostgreSQL DSN (added by installer)\nTHOTH_PG_DSN=%s\n' "$pg_dsn" >> "$THOTH_HOME/.env"
        log_success "Wrote THOTH_PG_DSN to $THOTH_HOME/.env"
    fi

    if [ ! -f "$THOTH_HOME/config.yaml" ] && [ -f "$INSTALL_DIR/cli-config.yaml.example" ]; then
        cp "$INSTALL_DIR/cli-config.yaml.example" "$THOTH_HOME/config.yaml"
        log_success "Created $THOTH_HOME/config.yaml"
    fi

    if [ ! -f "$THOTH_HOME/SOUL.md" ]; then
        cat > "$THOTH_HOME/SOUL.md" <<'SOUL_EOF'
# Thoth Agent Persona

<!--
This file defines the agent's personality and tone.
Edit to customize how Thoth communicates with you.
Loaded fresh each message — no restart needed.
-->
SOUL_EOF
        log_success "Created $THOTH_HOME/SOUL.md"
    fi

    # Marker so detect_install_mode can tell if THOTH_HOME has previously
    # been used by this installer (avoid first-run wizards on re-installs).
    touch "$THOTH_HOME/.thoth_install"

    log_info "Syncing bundled skills..."
    if [ -x "$INSTALL_DIR/venv/bin/python" ] && [ -f "$INSTALL_DIR/tools/skills_sync.py" ]; then
        "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/tools/skills_sync.py" 2>/dev/null \
            && log_success "Skills synced" \
            || log_info "Skills sync skipped (will run on first $CLI_NAME invocation)"
    fi

    configure_browser_env_from_system_browser
}

# ── Browser tools (Playwright) ─────────────────────────────────────────────
find_system_browser() {
    if [ -n "${AGENT_BROWSER_EXECUTABLE_PATH:-}" ]; then
        if [ -x "$AGENT_BROWSER_EXECUTABLE_PATH" ]; then echo "$AGENT_BROWSER_EXECUTABLE_PATH"; return 0; fi
        command -v "$AGENT_BROWSER_EXECUTABLE_PATH" 2>/dev/null && return 0
    fi
    local c
    for c in google-chrome google-chrome-stable chromium chromium-browser chrome; do
        command -v "$c" 2>/dev/null && return 0
    done
    if [ "$(uname)" = "Darwin" ]; then
        for app in \
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
            "/Applications/Chromium.app/Contents/MacOS/Chromium"; do
            [ -x "$app" ] && { echo "$app"; return 0; }
        done
    fi
    return 1
}

configure_browser_env_from_system_browser() {
    local env_file="$THOTH_HOME/.env"
    local browser_path="${DETECTED_BROWSER_EXECUTABLE:-$(find_system_browser 2>/dev/null || true)}"
    [ -z "$browser_path" ] && return 0
    [ -f "$env_file" ] || touch "$env_file"
    grep -q '^AGENT_BROWSER_EXECUTABLE_PATH=' "$env_file" 2>/dev/null && return 0
    printf '\n# Use the system Chrome/Chromium for browser tools.\nAGENT_BROWSER_EXECUTABLE_PATH=%s\n' "$browser_path" >> "$env_file"
    log_success "Browser tools will use $browser_path"
}

run_browser_install_with_timeout() {
    local seconds="$1"; shift
    if command -v timeout >/dev/null 2>&1; then
        timeout "$seconds" "$@"
    elif command -v gtimeout >/dev/null 2>&1; then
        # macOS coreutils (brew) installs GNU timeout as `gtimeout`.
        gtimeout "$seconds" "$@"
    else
        # Stock macOS ships neither `timeout` nor `gtimeout`. Run in the
        # background and kill it on overrun so Playwright can't hang the
        # installer forever (#222).
        "$@" &
        local _cmd_pid=$! _waited=0
        while kill -0 "$_cmd_pid" 2>/dev/null; do
            if [ "$_waited" -ge "$seconds" ]; then
                log_warn "Browser install exceeded ${seconds}s — terminating"
                kill "$_cmd_pid" 2>/dev/null
                wait "$_cmd_pid" 2>/dev/null
                return 124
            fi
            sleep 1
            _waited=$((_waited + 1))
        done
        wait "$_cmd_pid"
    fi
}

install_node_deps() {
    if [ "$HAS_NODE" = false ]; then
        log_info "Skipping Node.js dependencies (Node not installed)"
        return 0
    fi
    if [ "$DISTRO" = "termux" ]; then
        log_info "Termux: skipping web dashboard npm install (not part of tested Termux path)"
        return 0
    fi

    cd "$INSTALL_DIR"
    if [ -f "package.json" ]; then
        log_info "Installing root Node.js dependencies (browser tools)..."
        npm install --silent 2>/dev/null || log_warn "Root npm install failed (browser tools may not work)"
    fi

    if [ "$SKIP_BROWSER" = true ]; then
        log_info "Skipping Playwright/Chromium install (--skip-browser)"
    else
        log_info "Installing browser engine (Playwright Chromium)..."
        DETECTED_BROWSER_EXECUTABLE="$(find_system_browser 2>/dev/null || true)"
        if [ -n "$DETECTED_BROWSER_EXECUTABLE" ]; then
            log_success "Found system browser at $DETECTED_BROWSER_EXECUTABLE — skipping Chromium download"
        else
            case "$DISTRO" in
                ubuntu|debian|raspbian|pop|linuxmint|elementary|zorin|kali|parrot)
                    if [ "$(id -u)" -eq 0 ] || (command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null); then
                        run_browser_install_with_timeout 600 npx playwright install --with-deps chromium \
                            || log_warn "Playwright install failed — browser tools will not work"
                    else
                        log_warn "No sudo — installing Chromium only (admin must run later: sudo npx playwright install-deps chromium)"
                        run_browser_install_with_timeout 600 npx playwright install chromium \
                            || log_warn "Playwright install failed"
                    fi
                    ;;
                arch|manjaro|cachyos|endeavouros|garuda)
                    if [ "$(id -u)" -eq 0 ] || (command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null); then
                        local sudo_pfx=""; [ "$(id -u)" -ne 0 ] && sudo_pfx="sudo "
                        ${sudo_pfx}pacman -S --noconfirm --needed nss atk at-spi2-core cups libdrm libxkbcommon mesa pango cairo alsa-lib >/dev/null 2>&1 || true
                    fi
                    run_browser_install_with_timeout 600 npx playwright install chromium \
                        || log_warn "Playwright install failed"
                    ;;
                fedora|rhel|centos|rocky|alma)
                    log_warn "RPM distro: install system deps manually if missing:"
                    log_info "  sudo dnf install nss atk at-spi2-core cups-libs libdrm libxkbcommon mesa-libgbm pango cairo alsa-lib"
                    run_browser_install_with_timeout 600 npx playwright install chromium || true
                    ;;
                *)
                    run_browser_install_with_timeout 600 npx playwright install chromium || true
                    ;;
            esac
        fi
    fi

    log_success "Node dependencies installed"
}

# ── Substrate smoke ────────────────────────────────────────────────────────
# Verify the substrate actually boots against PG. Fails loudly if migrations
# didn't run or PG isn't reachable, which catches misconfigured DSNs before
# the setup wizard or first chat session does.
substrate_smoke() {
    if [ "$SKIP_POSTGRES" = true ] && [ -z "${PG_DSN_OVERRIDE:-}" ]; then
        log_info "Skipping substrate smoke (no DSN configured)"
        return 0
    fi
    log_info "Running substrate boot smoke test..."
    local dsn="${PG_DSN_OVERRIDE:-postgresql://${PG_USER_DEFAULT}:${PG_PASSWORD_DEFAULT}@${PG_HOST_DEFAULT}:${PG_PORT_DEFAULT}/${PG_DATABASE_DEFAULT}}"
    local script_out
    script_out=$(THOTH_PG_DSN="$dsn" "$INSTALL_DIR/venv/bin/python" - <<'PY' 2>&1
import asyncio, os, sys
async def main():
    import thoth_db
    from thoth_bootstrap import bootstrap_substrate
    await thoth_db.init(os.environ["THOTH_PG_DSN"])
    sub = await bootstrap_substrate()
    if sub is None:
        print("substrate-boot-FAIL: bootstrap_substrate returned None")
        sys.exit(2)
    print(f"substrate-boot-OK type={type(sub).__name__}")
    await thoth_db.close()
asyncio.run(main())
PY
)
    if echo "$script_out" | grep -q "substrate-boot-OK"; then
        log_success "Substrate boots cleanly against PostgreSQL"
    else
        log_warn "Substrate boot smoke FAILED — first chat session may emit warnings"
        echo "$script_out" | sed 's/^/    /' >&2
    fi
}

# ── Substrate worker subprocess (Sentinel/Curator/etc.) ───────────────────
# The substrate sub-agents run in a dedicated process with their own
# event loop + asyncpg pool — see ``substrate/cli/worker.py`` for the
# rationale (2026-05-26 cross-loop pool incident). Without this unit
# running, slices stay ``pending`` forever and embeddings never
# backfill.
#
# Strategy:
#   * systemd --user available → write unit + daemon-reload + enable
#     (on fresh install) or restart (on update if active).
#   * sudo / system-mode FHS install → write to /etc/systemd/system,
#     enable + start system-wide.
#   * Termux / macOS launchd / no systemd → print clear manual steps
#     and continue. The worker is not strictly required for the
#     gateway to come up; recall just degrades to keyword Jaccard.
setup_substrate_worker_service() {
    if [ "$DISTRO" = "termux" ]; then
        log_info "Substrate worker: Termux has no systemd — run manually:"
        log_info "  $CLI_NAME substrate worker run &"
        return 0
    fi

    if [ "$OS" != "linux" ]; then
        log_info "Substrate worker: non-Linux ($OS) has no systemd here."
        log_info "  Run manually or daemonise via your platform's mechanism:"
        log_info "    $CLI_NAME substrate worker run"
        return 0
    fi

    if ! command -v systemctl >/dev/null 2>&1; then
        log_info "Substrate worker: systemctl not on PATH — run manually:"
        log_info "  $CLI_NAME substrate worker run &"
        return 0
    fi

    # Choose scope: system mode (FHS layout, root install) vs user mode.
    local scope=""
    local unit_dir=""
    local unit_name="thoth-substrate-worker.service"
    if [ "$ROOT_FHS_LAYOUT" = true ]; then
        scope="--system"
        unit_dir="/etc/systemd/system"
    else
        scope="--user"
        unit_dir="$HOME/.config/systemd/user"
        # systemd --user requires a logind session. Skip cleanly if not
        # available (CI containers, fresh non-interactive installs).
        if ! systemctl --user list-units >/dev/null 2>&1; then
            log_warn "Substrate worker: no systemd --user session detected."
            log_info "  After logging in interactively, enable the worker:"
            log_info "    systemctl --user daemon-reload"
            log_info "    systemctl --user enable --now $unit_name"
            log_info "  Without it, substrate sub-agents (Sentinel/Curator/embedding"
            log_info "  backfill) will NOT tick."
            return 0
        fi
    fi

    # Detect prior state BEFORE we touch anything so the update-vs-fresh
    # decision is honest.
    local was_active=false
    local was_enabled=false
    if systemctl $scope is-active "$unit_name" >/dev/null 2>&1; then
        was_active=true
    fi
    if systemctl $scope is-enabled "$unit_name" >/dev/null 2>&1; then
        was_enabled=true
    fi

    mkdir -p "$unit_dir"
    local unit_path="$unit_dir/$unit_name"

    # Render the unit. We don't copy ``scripts/thoth-substrate-worker.service``
    # verbatim because that file uses ``%h`` (systemd home substitution)
    # which only works for ``--user`` units AND assumes the standard
    # install layout — operators with custom ``--thoth-home`` /
    # ``--cli-name`` / system-mode installs need their actual paths
    # baked in. Template the values here so the unit is correct for
    # the install we just performed.
    local python_path="$INSTALL_DIR/venv/bin/python"
    local env_file="$THOTH_HOME/.env"

    # Pick ExecStart wrapper — system-mode FHS runs as the operator's
    # primary user if set; user-mode runs as the invoking user implicitly.
    local user_directive=""
    if [ "$ROOT_FHS_LAYOUT" = true ]; then
        local _run_as=""
        if [ -n "${SUDO_USER:-}" ]; then _run_as="$SUDO_USER"; fi
        if [ -n "$_run_as" ]; then
            user_directive=$'\nUser='"$_run_as"
        fi
    fi

    cat > "$unit_path" <<UNIT
[Unit]
# Thoth substrate sub-agent worker. Installed by install.sh; do NOT
# hand-edit (your changes will be overwritten on the next install
# run). For local overrides use a drop-in at
# ${unit_path}.d/override.conf
#
# Runs Sentinel + Curator + ForceRejectWorker + PartitionMaintenanceWorker
# in a process separate from the gateway so each owns its own asyncpg
# pool + event loop (no cross-loop contention — see
# substrate/cli/worker.py for the 2026-05-26 incident).

Description=Thoth Substrate Sub-Agent Worker (Sentinel, Curator, etc.)
Documentation=https://github.com/519lab/thoth-agent#substrate
After=network-online.target
Wants=network-online.target
After=thoth-gateway.service

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$env_file$user_directive
ExecStart=$python_path -m thoth_cli.main substrate worker run
TimeoutStopSec=15
KillSignal=SIGTERM
Restart=always
RestartSec=10
MemoryMax=512M
CPUWeight=50
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$THOTH_HOME

[Install]
WantedBy=$([ "$ROOT_FHS_LAYOUT" = true ] && echo "multi-user.target" || echo "default.target")
UNIT

    log_success "Substrate worker unit installed at $unit_path"

    # Reload + enable/restart per scenario.
    systemctl $scope daemon-reload

    if [ "$IS_UPDATE" = true ] && [ "$was_active" = true ]; then
        # Update path: pick up new unit + new code by restarting.
        log_info "Substrate worker: restarting (was active)"
        systemctl $scope restart "$unit_name" || \
            log_warn "systemctl $scope restart $unit_name failed — investigate"
    elif [ "$was_enabled" = true ] && [ "$was_active" = false ]; then
        # Enabled but stopped (operator paused it deliberately). Don't
        # second-guess; just reload and leave alone.
        log_info "Substrate worker: enabled but stopped — leaving as-is."
        log_info "  Start when ready: systemctl $scope start $unit_name"
    else
        # Fresh install (or update where the unit was never enabled):
        # enable + start now.
        log_info "Substrate worker: enabling + starting"
        if systemctl $scope enable --now "$unit_name" 2>&1 | sed 's/^/    /'; then
            log_success "Substrate worker active"
        else
            log_warn "systemctl $scope enable --now $unit_name failed."
            log_info "  Inspect: systemctl $scope status $unit_name"
            log_info "  Logs:    journalctl $scope -u $unit_name --since '5 minutes ago'"
        fi
    fi
}

# ── Setup wizard ───────────────────────────────────────────────────────────
run_setup_wizard() {
    if [ "$RUN_SETUP" = false ]; then
        log_info "Skipping setup wizard (--skip-setup) — run '$CLI_NAME setup' later"
        return 0
    fi
    if ! (: </dev/tty) 2>/dev/null; then
        log_info "No TTY — skipping wizard. Run '$CLI_NAME setup' interactively when you have one."
        return 0
    fi
    # On updates, only re-run the wizard if the user hasn't already
    # configured a provider. They almost certainly don't want to walk
    # through model/provider selection again on every upgrade.
    if [ "$IS_UPDATE" = true ] && _env_has_provider_api_key; then
        log_info "Update mode: provider API key already in .env — skipping wizard."
        log_info "  Re-run interactively any time with: $CLI_NAME setup"
        return 0
    fi
    echo ""
    log_info "Starting setup wizard..."
    cd "$INSTALL_DIR"
    local pg_dsn="${PG_DSN_OVERRIDE:-postgresql://${PG_USER_DEFAULT}:${PG_PASSWORD_DEFAULT}@${PG_HOST_DEFAULT}:${PG_PORT_DEFAULT}/${PG_DATABASE_DEFAULT}}"
    THOTH_HOME="$THOTH_HOME" THOTH_PG_DSN="$pg_dsn" \
        "$INSTALL_DIR/venv/bin/python" -m thoth_cli.main setup < /dev/tty
}

# ── Success message ────────────────────────────────────────────────────────
print_success() {
    echo ""
    echo -e "${GREEN}${BOLD}"
    echo "┌─────────────────────────────────────────────────────────┐"
    echo "│              ✓ Installation Complete!                   │"
    echo "└─────────────────────────────────────────────────────────┘"
    echo -e "${NC}"
    echo ""
    echo -e "${CYAN}${BOLD}📁 Your files:${NC}"
    echo -e "   ${YELLOW}Code:${NC}      $INSTALL_DIR"
    echo -e "   ${YELLOW}Data:${NC}      $THOTH_HOME"
    echo -e "   ${YELLOW}Config:${NC}    $THOTH_HOME/config.yaml"
    echo -e "   ${YELLOW}API keys:${NC}  $THOTH_HOME/.env"
    echo ""

    if [ "$SKIP_POSTGRES" = false ]; then
        echo -e "${CYAN}${BOLD}🗄  PostgreSQL (substrate):${NC}"
        echo -e "   $DOCKER_COMPOSE ps postgres   # check status"
        echo -e "   $DOCKER_COMPOSE logs postgres # inspect logs"
        echo -e "   $DOCKER_COMPOSE stop postgres # shut down (will not auto-restart)"
        echo ""

        # Substrate worker scope (system vs user) picked by
        # setup_substrate_worker_service() above.
        local _wscope="--user"
        if [ "$ROOT_FHS_LAYOUT" = true ]; then _wscope="--system"; fi
        echo -e "${CYAN}${BOLD}🧠 Substrate worker (Sentinel + Curator):${NC}"
        echo -e "   systemctl $_wscope status thoth-substrate-worker"
        echo -e "   journalctl $_wscope -u thoth-substrate-worker -f"
        echo -e "   systemctl $_wscope restart thoth-substrate-worker"
        echo ""
    fi

    echo -e "${CYAN}${BOLD}🚀 Commands:${NC}"
    echo -e "   ${GREEN}$CLI_NAME${NC}                Start chatting"
    echo -e "   ${GREEN}$CLI_NAME setup${NC}          Configure API keys & settings"
    echo -e "   ${GREEN}$CLI_NAME config${NC}         View/edit configuration"
    echo -e "   ${GREEN}$CLI_NAME substrate${NC}      Show running sub-agents + stream stats"
    echo -e "   ${GREEN}$CLI_NAME gateway install${NC}    Background gateway service (messaging + cron)"
    echo ""

    if [ "$CLI_NAME" != "thoth" ]; then
        echo -e "${BLUE}ℹ${NC}  Installed as ${BOLD}$CLI_NAME${NC} to avoid colliding with another"
        echo "    Thoth install. Pass ${BOLD}--cli-name thoth${NC} on a clean machine to use the natural name."
        echo ""
    fi

    if [ "$DISTRO" = "termux" ]; then
        echo -e "${YELLOW}⚡ '$CLI_NAME' was linked into $(get_command_link_display_dir) (on PATH in Termux).${NC}"
    elif [ "$ROOT_FHS_LAYOUT" = true ]; then
        echo -e "${YELLOW}⚡ '$CLI_NAME' linked into /usr/local/bin — ready to use, no reload needed.${NC}"
    else
        echo -e "${YELLOW}⚡ Reload your shell to use '$CLI_NAME':${NC}"
        case "$(basename "${SHELL:-/bin/bash}")" in
            zsh)  echo "   source ~/.zshrc" ;;
            bash) echo "   source ~/.bashrc" ;;
            fish) echo "   source ~/.config/fish/config.fish" ;;
            *)    echo "   source ~/.bashrc  # or ~/.zshrc" ;;
        esac
    fi
    echo ""

    [ "$HAS_NODE" = false ]    && echo -e "${YELLOW}Note: Node.js missing — browser tools / TUI / dashboard disabled.${NC}"
    [ "$HAS_RIPGREP" = false ] && echo -e "${YELLOW}Note: ripgrep missing — file search uses grep fallback.${NC}"

    # Non-interactive / CI installs can finish with no provider key (the
    # wizard was skipped or had nothing to read). Say so loudly so the user
    # isn't surprised by a chat that can't reach any model (#227).
    if ! _env_has_provider_api_key; then
        echo ""
        echo -e "${YELLOW}${BOLD}⚠  No provider key configured — run \`$CLI_NAME setup\` before chatting.${NC}"
    fi
}

# ── Main ───────────────────────────────────────────────────────────────────
main() {
    print_banner

    detect_os
    resolve_install_layout
    detect_install_mode
    warn_upstream_collision

    install_uv
    check_python
    check_git
    check_docker
    check_node
    check_network_prerequisites
    install_system_packages

    clone_repo
    setup_venv
    install_deps
    verify_core_deps
    setup_postgres
    run_migrations
    install_node_deps
    setup_path
    copy_config_templates
    substrate_smoke
    setup_substrate_worker_service
    run_setup_wizard

    print_success

    echo "git" > "$THOTH_HOME/.install_method"
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) cli_name=$CLI_NAME install_dir=$INSTALL_DIR" >> "$THOTH_HOME/.install_log"
}

main
