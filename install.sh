#!/usr/bin/env bash
# pmsbridge installer: installs pmsbridge on a fresh Ubuntu or Debian server with one command.
#
#   curl -fsSL https://raw.githubusercontent.com/faridmostafaei259/pmsbridge/main/install.sh | sudo bash
#   sudo ./install.sh                       (from a downloaded copy of the project)
#   sudo ./install.sh --help                (all options)
#
# It is safe to run again: an existing encryption key and existing data are never replaced.
# The log is /var/log/pmsbridge-install.log. It never holds the encryption key or the setup link.
#
# The script is written as functions so the tests can load it without running it (main runs only when
# the file is executed). The PMSBRIDGE_* variables below exist for those tests and for the rare operator
# who needs a different location; none of them is needed for a normal install.

set -euo pipefail

TOTAL_STEPS=10
RAW_URL="https://raw.githubusercontent.com/faridmostafaei259/pmsbridge/main/install.sh"
DOCS_URL="https://github.com/faridmostafaei259/pmsbridge/blob/main/docs"

REPO_URL="${PMSBRIDGE_REPO_URL:-https://github.com/faridmostafaei259/pmsbridge.git}"
LOG_FILE="${PMSBRIDGE_INSTALL_LOG:-/var/log/pmsbridge-install.log}"
OS_RELEASE_FILE="${PMSBRIDGE_OS_RELEASE:-/etc/os-release}"
TTY_PATH="${PMSBRIDGE_TTY:-/dev/tty}"
KEYRING_DIR="${PMSBRIDGE_KEYRING_DIR:-/etc/apt/keyrings}"
APT_SOURCES_DIR="${PMSBRIDGE_APT_SOURCES_DIR:-/etc/apt/sources.list.d}"

# How long to wait, and how often to look (seconds). The tests shorten these.
DNS_WAIT_SECONDS="${PMSBRIDGE_DNS_WAIT_SECONDS:-1800}"
DNS_POLL_SECONDS="${PMSBRIDGE_DNS_POLL_SECONDS:-30}"
HEALTH_WAIT_SECONDS="${PMSBRIDGE_HEALTH_WAIT_SECONDS:-300}"
HEALTH_POLL_SECONDS="${PMSBRIDGE_HEALTH_POLL_SECONDS:-5}"

# Where this script lives. Empty when the script is piped into bash.
SCRIPT_PATH="${BASH_SOURCE[0]:-}"
SCRIPT_DIR=""
if [ -n "$SCRIPT_PATH" ]; then
  SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" 2>/dev/null && pwd)" || SCRIPT_DIR=""
fi

# Options (set by parse_args).
OPT_DOMAIN=""
OPT_EMAIL=""
OPT_ADMIN_EMAIL=""
OPT_DIR="/opt/pmsbridge"
OPT_DIR_SET=0
OPT_REF="main"
OPT_REF_SET=0
OPT_TLS=""
OPT_INSTALL_DOCKER=1
OPT_FIREWALL=1
OPT_WAIT_DNS=1
OPT_YES=0

# Run state.
CURRENT_STEP=0
CURRENT_TITLE="starting"
FAIL_SHOWN=0
INSTALL_DIR=""
USING_CHECKOUT=0
ENV_FILE=""
DOMAIN=""
EMAIL=""
ADMIN_EMAIL=""
TLS_MODE="auto"
EXISTING_DOMAIN=""
EXISTING_EMAIL=""
EXISTING_TLS=""
KEY_CREATED=0
SETUP_LINK=""
ADMIN_NOTE=""
DISTRO_ID=""
DISTRO_NAME=""
DISTRO_VERSION=""
DISTRO_CODENAME=""
ARCH=""
PLATFORM_SUPPORTED=0
DNS_STATUS=""
DNS_RESOLVED_V4=""
DNS_RESOLVED_V6=""
DNS_PUBLIC_V4=""
DNS_PUBLIC_V6=""
TTY_OPEN=0

# ---------------------------------------------------------------------------------------------------
# Messages and the log
# ---------------------------------------------------------------------------------------------------

log_line() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$LOG_FILE"
}

say() {
  printf '%s\n' "$*"
  log_line "$*"
}

warn() {
  printf 'Warning: %s\n' "$*" >&2
  log_line "WARNING: $*"
}

err() {
  printf '%s\n' "$*" >&2
  log_line "$*"
}

# An error before the numbered steps begin (bad option, wrong system, not root).
die() {
  FAIL_SHOWN=1
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

die_usage() {
  FAIL_SHOWN=1
  printf 'Error: %s\n\nRun with --help to see the options.\n' "$*" >&2
  exit 2
}

# A failure inside a numbered step: which step, why, and what to do.
fail() {
  local why="$1" fix="${2:-}"
  FAIL_SHOWN=1
  err ""
  err "Step $CURRENT_STEP of $TOTAL_STEPS ($CURRENT_TITLE) failed."
  err "Why: $why"
  if [ -n "$fix" ]; then
    err "What to do: $fix"
  fi
  err "Details are in $LOG_FILE. The installer is safe to run again."
  exit 1
}

on_exit() {
  local code="$1"
  if [ "$code" -ne 0 ] && [ "$FAIL_SHOWN" -eq 0 ]; then
    err ""
    err "The installer stopped unexpectedly during step $CURRENT_STEP of $TOTAL_STEPS ($CURRENT_TITLE)."
    err "Details are in $LOG_FILE. It is safe to run the installer again."
  fi
}

step() {
  CURRENT_STEP="$1"
  CURRENT_TITLE="$2"
  say ""
  say "Step $CURRENT_STEP of $TOTAL_STEPS: $CURRENT_TITLE"
}

# Run a command, show its output and add it to the log. The command never reads the script's input.
run_logged() {
  log_line "RUN: $*"
  "$@" </dev/null 2>&1 | tee -a "$LOG_FILE"
}

init_log() {
  if ! ( umask 077; : >>"$LOG_FILE" ) 2>/dev/null; then
    printf 'Warning: cannot write the log file %s, so no log will be kept.\n' "$LOG_FILE" >&2
    LOG_FILE=/dev/null
    return 0
  fi
  chmod 600 "$LOG_FILE" 2>/dev/null || true
  log_line "pmsbridge installer started"
}

usage() {
  cat <<EOF
pmsbridge installer

Installs pmsbridge on an Ubuntu (22.04 or newer) or Debian (12 or newer) server. It asks for your
domain name and an email address; everything else is automatic. Run it as root (with sudo).

Usage:
  sudo ./install.sh [options]
  curl -fsSL $RAW_URL | sudo bash -s -- [options]

Options:
  --domain NAME        The domain people will use, for example mcp.example.com.
  --email ADDRESS      Your email address: used for certificate notices and for the first administrator.
  --admin-email ADDR   Email address of the first administrator (default: the --email address).
  --dir PATH           Where to install (default /opt/pmsbridge). Ignored when you run the installer
                       from a downloaded copy of the project: that folder is used.
  --ref NAME           The branch or version to install (default main).
  --tls auto|internal  auto (default) gets a real certificate. internal is for testing only: assistants
                       and browsers will not trust it.
  --no-install-docker  Do not install Docker; stop if it is missing.
  --no-firewall        Do not change the server's firewall.
  --no-wait-dns        Do not wait for the domain's DNS record to point at this server.
  --yes                Accept the defaults without asking. Missing required values are still asked for.
  --help               Show this text.

Everything is logged to $LOG_FILE (the encryption key and the setup link are never written there).
EOF
}

# ---------------------------------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------------------------------

set_option() {
  local name="$1" value="$2"
  case "$name" in
    --domain) OPT_DOMAIN="$value" ;;
    --email) OPT_EMAIL="$value" ;;
    --admin-email) OPT_ADMIN_EMAIL="$value" ;;
    --dir)
      [ -n "$value" ] || die_usage "--dir needs a folder name."
      OPT_DIR="$value"
      OPT_DIR_SET=1
      ;;
    --ref)
      [ -n "$value" ] || die_usage "--ref needs a branch or version name."
      OPT_REF="$value"
      OPT_REF_SET=1
      ;;
    --tls)
      case "$value" in
        auto | internal) OPT_TLS="$value" ;;
        *) die_usage "--tls must be auto or internal (you gave: $value)." ;;
      esac
      ;;
  esac
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --domain | --email | --admin-email | --dir | --ref | --tls)
        [ $# -ge 2 ] || die_usage "$1 needs a value."
        set_option "$1" "$2"
        shift 2
        ;;
      --domain=* | --email=* | --admin-email=* | --dir=* | --ref=* | --tls=*)
        set_option "${1%%=*}" "${1#*=}"
        shift
        ;;
      --no-install-docker)
        OPT_INSTALL_DOCKER=0
        shift
        ;;
      --no-firewall)
        OPT_FIREWALL=0
        shift
        ;;
      --no-wait-dns)
        OPT_WAIT_DNS=0
        shift
        ;;
      --yes | -y)
        OPT_YES=1
        shift
        ;;
      --help | -h)
        usage
        exit 0
        ;;
      *)
        die_usage "Unknown option: $1"
        ;;
    esac
  done
  if [ -n "$OPT_TLS" ]; then
    TLS_MODE="$OPT_TLS"
  fi
}

# ---------------------------------------------------------------------------------------------------
# Questions (always asked on the terminal, never on the script's own input, so piping works)
# ---------------------------------------------------------------------------------------------------

open_tty() {
  if [ "$TTY_OPEN" -eq 1 ]; then
    return 0
  fi
  if ! { : <"$TTY_PATH"; } 2>/dev/null; then
    return 1
  fi
  exec 8<"$TTY_PATH"
  TTY_OPEN=1
}

can_prompt() {
  open_tty
}

# Print a question and read one line into REPLY_VALUE. Fails when there is no terminal or no answer.
prompt_line() {
  REPLY_VALUE=""
  open_tty || return 1
  printf '%s' "$1" >&2
  IFS= read -r REPLY_VALUE <&8 || return 1
  REPLY_VALUE="${REPLY_VALUE#"${REPLY_VALUE%%[![:space:]]*}"}"
  REPLY_VALUE="${REPLY_VALUE%"${REPLY_VALUE##*[![:space:]]}"}"
}

# confirm "question" default(y|n): returns 0 for yes. With --yes the default is taken.
confirm() {
  local question="$1" default="${2:-y}" hint="[Y/n]"
  if [ "$default" = "n" ]; then
    hint="[y/N]"
  fi
  if [ "$OPT_YES" -eq 1 ]; then
    [ "$default" = "y" ]
    return
  fi
  if ! prompt_line "$question $hint "; then
    [ "$default" = "y" ]
    return
  fi
  case "$REPLY_VALUE" in
    y | Y | yes | YES | Yes) return 0 ;;
    n | N | no | NO | No) return 1 ;;
    *) [ "$default" = "y" ] ;;
  esac
}

# The command line an operator would use to pass a missing value without a terminal.
rerun_hint() {
  if [ -n "$SCRIPT_PATH" ]; then
    printf 'sudo %s %s' "$SCRIPT_PATH" "$1"
  else
    printf 'curl -fsSL %s | sudo bash -s -- %s' "$RAW_URL" "$1"
  fi
}

# ---------------------------------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------------------------------

lowercase() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

# Prints the reason a domain name is not acceptable, or nothing when it is fine.
domain_error() {
  local d="$1"
  case "$d" in
    "") printf 'The domain name is empty.'; return 0 ;;
    *://* | */* | *:* | *@* | *\?* | *\#*)
      printf 'Enter only the host name, for example mcp.example.com, without https://, a port or a path.'
      return 0
      ;;
    *[[:space:]]*) printf 'The domain name cannot contain spaces.'; return 0 ;;
  esac
  if [ "${#d}" -gt 253 ]; then
    printf 'The domain name is too long.'
    return 0
  fi
  case "$d" in
    *.*) ;;
    *) printf 'The domain name needs at least one dot, for example mcp.example.com.'; return 0 ;;
  esac
  if printf '%s' "$d" | grep -Eq '^[0-9.]+$'; then
    printf 'Use a domain name, not an IP address: certificates are issued for names.'
    return 0
  fi
  if ! printf '%s' "$d" | grep -Eq '^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z0-9]([a-z0-9-]*[a-z0-9])?$'; then
    printf 'The domain name may contain only letters, digits, hyphens and dots, and each part must not start or end with a hyphen.'
    return 0
  fi
  local label IFS=.
  for label in $d; do
    if [ "${#label}" -gt 63 ]; then
      printf 'Each part of the domain name (between the dots) can have at most 63 characters.'
      return 0
    fi
  done
  return 0
}

# Prints the reason an email address is not acceptable, or nothing when it is fine.
email_error() {
  local e="$1"
  if [ -z "$e" ]; then
    printf 'The email address is empty.'
    return 0
  fi
  if [ "${#e}" -gt 254 ]; then
    printf 'The email address is too long.'
    return 0
  fi
  if ! printf '%s' "$e" | grep -Eq '^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$'; then
    printf 'That does not look like an email address (for example you@example.com).'
    return 0
  fi
  return 0
}

# ---------------------------------------------------------------------------------------------------
# The settings file (deploy/.env)
# ---------------------------------------------------------------------------------------------------

# env_get FILE KEY: the value of KEY, or nothing.
env_get() {
  local file="$1" key="$2" line
  [ -f "$file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      "$key="*)
        printf '%s' "${line#*=}"
        return 0
        ;;
    esac
  done <"$file"
  return 0
}

# env_set FILE KEY VALUE: replace the first KEY line (or add one), keep every other line, mode 600.
env_set() {
  local file="$1" key="$2" value="$3" tmp line found=0
  tmp="$(umask 077; mktemp "$file.XXXXXX")"
  if [ -f "$file" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in
        "$key="*)
          if [ "$found" -eq 0 ]; then
            printf '%s=%s\n' "$key" "$value" >>"$tmp"
            found=1
          fi
          ;;
        *) printf '%s\n' "$line" >>"$tmp" ;;
      esac
    done <"$file"
  fi
  if [ "$found" -eq 0 ]; then
    printf '%s=%s\n' "$key" "$value" >>"$tmp"
  fi
  chmod 600 "$tmp"
  chown root:root "$tmp" 2>/dev/null || warn "could not set the owner of $file to root."
  mv -f "$tmp" "$file"
}

# env_set_if_changed FILE KEY VALUE: only write when the value differs.
env_set_if_changed() {
  if [ "$(env_get "$1" "$2")" != "$3" ]; then
    env_set "$1" "$2" "$3"
  fi
}

# A new key entry for ENCRYPTION_KEYS: 32 random bytes, base64. Returned on stdout to the caller,
# which writes it to the settings file; never shown on the terminal or logged.
generate_encryption_key() {
  local raw
  if command -v openssl >/dev/null 2>&1; then
    raw="$(openssl rand -base64 32)"
  else
    raw="$(head -c 32 /dev/urandom | base64)"
  fi
  raw="$(printf '%s' "$raw" | tr -d '\n')"
  [ "${#raw}" -eq 44 ] || return 1
  printf 'k1:%s' "$raw"
}

# ---------------------------------------------------------------------------------------------------
# Step 1: check the server
# ---------------------------------------------------------------------------------------------------

check_platform() {
  local system
  system="$(uname -s)"
  if [ "$system" != "Linux" ]; then
    die "this installer is for Linux servers (Ubuntu or Debian). This computer runs $system. To try pmsbridge here, follow docs/install/test-server.md."
  fi
  if [ "$(id -u)" -ne 0 ]; then
    die "the installer must run as root. Run it again with sudo:
  sudo ./install.sh
or, from the web:
  curl -fsSL $RAW_URL | sudo bash"
  fi
}

os_field() {
  local value=""
  if [ -r "$OS_RELEASE_FILE" ]; then
    value="$(sed -n "s/^$1=//p" "$OS_RELEASE_FILE" | head -n 1 | tr -d "\"'")"
  fi
  printf '%s' "$value"
}

detect_platform() {
  local machine major
  DISTRO_ID="$(os_field ID)"
  DISTRO_NAME="$(os_field NAME)"
  DISTRO_VERSION="$(os_field VERSION_ID)"
  DISTRO_CODENAME="$(os_field UBUNTU_CODENAME)"
  if [ -z "$DISTRO_CODENAME" ]; then
    DISTRO_CODENAME="$(os_field VERSION_CODENAME)"
  fi
  machine="$(uname -m)"
  case "$machine" in
    x86_64 | amd64) ARCH="amd64" ;;
    aarch64 | arm64) ARCH="arm64" ;;
    *) ARCH="" ;;
  esac
  PLATFORM_SUPPORTED=0
  major="${DISTRO_VERSION%%.*}"
  case "$major" in
    "" | *[!0-9]*) return 0 ;;
  esac
  if [ -z "$ARCH" ] || [ -z "$DISTRO_CODENAME" ]; then
    return 0
  fi
  case "$DISTRO_ID" in
    ubuntu) [ "$major" -ge 22 ] && PLATFORM_SUPPORTED=1 ;;
    debian) [ "$major" -ge 12 ] && PLATFORM_SUPPORTED=1 ;;
  esac
  return 0
}

docker_ready() {
  command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1
}

check_resources() {
  local mem_kb="" free_kb=""
  if [ -r /proc/meminfo ]; then
    mem_kb="$(sed -n 's/^MemTotal:[[:space:]]*\([0-9]*\).*/\1/p' /proc/meminfo | head -n 1)"
  fi
  if [ -n "$mem_kb" ] && [ "$mem_kb" -lt 900000 ]; then
    warn "this server has less than 1 GB of memory. Building pmsbridge may fail; 2 GB is comfortable."
  fi
  free_kb="$(df -Pk / 2>/dev/null | awk 'NR==2 {print $4}')"
  case "$free_kb" in
    "" | *[!0-9]*) ;;
    *)
      if [ "$free_kb" -lt 4000000 ]; then
        warn "less than 4 GB of disk space is free. Building the images needs a few GB."
      fi
      ;;
  esac
}

# The install folder is known before anything is installed, so the early checks can use it.
resolve_install_dir() {
  USING_CHECKOUT=0
  if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/deploy/compose.yaml" ] && [ -f "$SCRIPT_DIR/Dockerfile" ]; then
    INSTALL_DIR="$SCRIPT_DIR"
    USING_CHECKOUT=1
  else
    INSTALL_DIR="${OPT_DIR%/}"
    if [ -z "$INSTALL_DIR" ]; then
      INSTALL_DIR="/"
    fi
  fi
  ENV_FILE="$INSTALL_DIR/deploy/.env"
}

load_existing_settings() {
  EXISTING_DOMAIN="$(env_get "$ENV_FILE" DOMAIN)"
  EXISTING_EMAIL="$(env_get "$ENV_FILE" ACME_EMAIL)"
  EXISTING_TLS="$(env_get "$ENV_FILE" TLS_MODE)"
  if [ -z "$OPT_TLS" ] && [ -n "$EXISTING_TLS" ]; then
    case "$EXISTING_TLS" in
      auto | internal) TLS_MODE="$EXISTING_TLS" ;;
    esac
  fi
}

# Stop early, before installing anything, when a required value is missing and nobody can be asked.
ensure_inputs_possible() {
  if [ -z "$OPT_DOMAIN" ] && [ -z "$EXISTING_DOMAIN" ] && ! can_prompt; then
    fail "the domain name is needed, but there is no terminal to ask on." \
      "run the installer again and give the domain: $(rerun_hint "--domain mcp.example.com --email you@example.com")"
  fi
  if [ -z "$OPT_EMAIL" ] && [ -z "$EXISTING_EMAIL" ] && ! can_prompt; then
    fail "an email address is needed, but there is no terminal to ask on." \
      "run the installer again and give your email address: $(rerun_hint "--domain mcp.example.com --email you@example.com")"
  fi
}

step_check_server() {
  step 1 "Check this server"
  detect_platform
  resolve_install_dir
  load_existing_settings
  say "System: ${DISTRO_NAME:-unknown} ${DISTRO_VERSION}, ${ARCH:-unsupported processor} ($(uname -m))."
  if [ "$PLATFORM_SUPPORTED" -eq 1 ]; then
    say "This system is supported."
  elif docker_ready; then
    warn "this system is not one pmsbridge is tested on (Ubuntu 22.04 or newer, Debian 12 or newer, amd64 or arm64). Docker is already installed, so the installer will continue."
  else
    fail "this system is not supported (the installer needs Ubuntu 22.04 or newer or Debian 12 or newer, on amd64 or arm64) and Docker with the Compose plugin is not installed." \
      "install Docker Engine and the Compose plugin yourself (https://docs.docker.com/engine/install/), then run the installer again. Or use a supported system."
  fi
  check_resources
  if [ "$TLS_MODE" = "internal" ]; then
    warn "the internal certificate mode is for testing only. AI assistants and browsers will not trust the certificate."
  fi
  ensure_inputs_possible
}

# ---------------------------------------------------------------------------------------------------
# Step 2: Docker
# ---------------------------------------------------------------------------------------------------

install_docker_packages() {
  local have_docker="$1" packages
  export DEBIAN_FRONTEND=noninteractive
  run_logged apt-get update || return 1
  run_logged apt-get install -y ca-certificates curl || return 1
  install -m 0755 -d "$KEYRING_DIR"
  run_logged curl -fsSL --proto '=https' --tlsv1.2 "https://download.docker.com/linux/$DISTRO_ID/gpg" \
    -o "$KEYRING_DIR/docker.asc" || return 1
  chmod a+r "$KEYRING_DIR/docker.asc"
  mkdir -p "$APT_SOURCES_DIR"
  printf 'deb [arch=%s signed-by=%s] https://download.docker.com/linux/%s %s stable\n' \
    "$ARCH" "$KEYRING_DIR/docker.asc" "$DISTRO_ID" "$DISTRO_CODENAME" >"$APT_SOURCES_DIR/docker.list"
  run_logged apt-get update || return 1
  if [ "$have_docker" -eq 1 ]; then
    packages="docker-compose-plugin"
  else
    packages="docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin"
  fi
  # shellcheck disable=SC2086  # the package names are plain words
  run_logged apt-get install -y $packages || return 1
}

step_docker() {
  local have_docker=0
  step 2 "Docker"
  if command -v docker >/dev/null 2>&1; then
    have_docker=1
  fi
  if docker_ready; then
    say "Docker and the Compose plugin are already installed."
  elif [ "$OPT_INSTALL_DOCKER" -eq 0 ]; then
    fail "Docker or its Compose plugin is missing, and --no-install-docker was given." \
      "install them (https://docs.docker.com/engine/install/) or run the installer again without --no-install-docker."
  elif [ "$PLATFORM_SUPPORTED" -ne 1 ]; then
    fail "Docker or its Compose plugin is missing, and this system is not one the installer can install it on." \
      "install Docker Engine and the Compose plugin yourself (https://docs.docker.com/engine/install/), then run the installer again."
  else
    say "Installing Docker Engine and the Compose plugin from Docker's official package repository (download.docker.com). This takes a few minutes."
    if ! install_docker_packages "$have_docker"; then
      fail "Docker could not be installed." \
        "check that this server can reach download.docker.com and its package mirrors, then run the installer again. Or install Docker yourself (https://docs.docker.com/engine/install/) and run again."
    fi
    say "Docker is installed."
  fi
  if command -v systemctl >/dev/null 2>&1; then
    if ! run_logged systemctl enable --now docker; then
      warn "could not turn Docker on with systemctl. Checking whether it is already running."
    fi
  fi
  if ! docker info >/dev/null 2>&1; then
    fail "Docker is installed but not running, or this account cannot use it." \
      "start it with: sudo systemctl enable --now docker, then run the installer again."
  fi
  if ! docker compose version >/dev/null 2>&1; then
    fail "the Docker Compose plugin is not working." \
      "install it with: sudo apt-get install docker-compose-plugin (see https://docs.docker.com/compose/install/linux/), then run the installer again."
  fi
  say "Docker is running."
}

# ---------------------------------------------------------------------------------------------------
# Step 3: firewall
# ---------------------------------------------------------------------------------------------------

step_firewall() {
  local ufw_status="" fw_state="" handled=0
  step 3 "Firewall"
  if [ "$OPT_FIREWALL" -eq 0 ]; then
    say "Skipped (--no-firewall). Make sure ports 80 and 443 are open."
  else
    if command -v ufw >/dev/null 2>&1; then
      ufw_status="$(ufw status 2>/dev/null || true)"
      case "$ufw_status" in
        "Status: active"*)
          handled=1
          say "ufw is active: allowing ports 80 and 443."
          run_logged ufw allow 80/tcp || warn "could not open port 80. Run: sudo ufw allow 80/tcp"
          run_logged ufw allow 443/tcp || warn "could not open port 443. Run: sudo ufw allow 443/tcp"
          ;;
      esac
    fi
    if command -v firewall-cmd >/dev/null 2>&1; then
      fw_state="$(firewall-cmd --state 2>/dev/null || true)"
      if [ "$fw_state" = "running" ]; then
        handled=1
        say "firewalld is running: allowing http and https."
        if run_logged firewall-cmd --permanent --add-service=http --add-service=https; then
          run_logged firewall-cmd --reload || warn "could not reload firewalld. Run: sudo firewall-cmd --reload"
        else
          warn "could not open the ports. Run: sudo firewall-cmd --permanent --add-service=http --add-service=https && sudo firewall-cmd --reload"
        fi
      fi
    fi
    if [ "$handled" -eq 0 ]; then
      say "No active ufw or firewalld found on this server, so nothing was changed."
    fi
  fi
  say "Note: if your hosting provider has its own firewall (a security group), the installer cannot see it. Ports 80 and 443 must be open there too."
}

# ---------------------------------------------------------------------------------------------------
# Step 4: the code
# ---------------------------------------------------------------------------------------------------

ensure_git() {
  if command -v git >/dev/null 2>&1; then
    return 0
  fi
  if ! command -v apt-get >/dev/null 2>&1; then
    fail "git is not installed and this system has no apt-get to install it." \
      "install git, then run the installer again."
  fi
  say "Installing git."
  export DEBIAN_FRONTEND=noninteractive
  if ! { run_logged apt-get update && run_logged apt-get install -y git; }; then
    fail "git could not be installed." "install git (sudo apt-get install git), then run the installer again."
  fi
}

update_checkout() {
  local ref="$OPT_REF"
  run_logged git -C "$INSTALL_DIR" fetch --tags --prune origin || return 1
  if git -C "$INSTALL_DIR" rev-parse --verify --quiet "refs/remotes/origin/$ref" >/dev/null 2>&1; then
    run_logged git -C "$INSTALL_DIR" checkout -q -B "$ref" "origin/$ref" || return 1
  else
    run_logged git -C "$INSTALL_DIR" checkout -q "$ref" || return 1
  fi
}

step_code() {
  step 4 "Get the code"
  if [ "$USING_CHECKOUT" -eq 1 ]; then
    say "Using the pmsbridge folder you ran the installer from: $INSTALL_DIR"
    if [ "$OPT_DIR_SET" -eq 1 ] && [ "${OPT_DIR%/}" != "$INSTALL_DIR" ]; then
      warn "--dir is ignored because the installer runs from a downloaded copy. Using $INSTALL_DIR."
    fi
    if [ "$OPT_REF_SET" -eq 1 ]; then
      warn "--ref is ignored because the installer runs from a downloaded copy."
    fi
  else
    ensure_git
    if [ -d "$INSTALL_DIR/.git" ]; then
      say "pmsbridge is already downloaded in $INSTALL_DIR. Updating it to $OPT_REF. Your settings and data are not touched."
      if ! update_checkout; then
        fail "the existing download in $INSTALL_DIR could not be updated to $OPT_REF." \
          "check the messages above (for example a version name that does not exist, or files you changed in that folder), fix them, then run the installer again."
      fi
    elif [ -e "$INSTALL_DIR" ] && [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]; then
      fail "$INSTALL_DIR exists and is not a pmsbridge download." \
        "choose another folder with --dir, or move that folder away. Nothing was changed."
    else
      say "Downloading pmsbridge ($OPT_REF) into $INSTALL_DIR."
      mkdir -p "$(dirname "$INSTALL_DIR")"
      if ! run_logged git clone --quiet "$REPO_URL" "$INSTALL_DIR"; then
        fail "pmsbridge could not be downloaded from $REPO_URL." \
          "check that this server can reach github.com, then run the installer again."
      fi
      if ! run_logged git -C "$INSTALL_DIR" checkout -q "$OPT_REF"; then
        fail "the version $OPT_REF does not exist." "run the installer again with a valid --ref (or leave it out to use main)."
      fi
    fi
  fi
  if [ ! -f "$INSTALL_DIR/deploy/compose.yaml" ]; then
    fail "$INSTALL_DIR does not contain deploy/compose.yaml, so it is not a complete pmsbridge download." \
      "remove that folder if it holds nothing you need, or choose another with --dir, then run the installer again."
  fi
}

# ---------------------------------------------------------------------------------------------------
# Step 5: domain and email
# ---------------------------------------------------------------------------------------------------

# choose_value NAME PROMPT OPTION EXISTING ERROR_FUNCTION NORMALISE(1 = lowercase)
choose_value() {
  local label="$1" prompt="$2" given="$3" existing="$4" checker="$5" lower="$6"
  local value="" problem tries=0
  CHOSEN=""
  if [ -n "$given" ]; then
    value="$given"
    if [ "$lower" -eq 1 ]; then value="$(lowercase "$value")"; fi
    problem="$($checker "$value")"
    if [ -n "$problem" ]; then
      fail "$label: $problem" "run the installer again with a corrected value."
    fi
    CHOSEN="$value"
    return 0
  fi
  if [ -n "$existing" ]; then
    CHOSEN="$existing"
    return 0
  fi
  while [ "$tries" -lt 5 ]; do
    tries=$((tries + 1))
    if ! prompt_line "$prompt"; then
      fail "$label is needed, but there is no terminal to ask on." \
        "run the installer again and pass it as an option (see --help)."
    fi
    value="$REPLY_VALUE"
    if [ "$lower" -eq 1 ]; then value="$(lowercase "$value")"; fi
    problem="$($checker "$value")"
    if [ -z "$problem" ]; then
      CHOSEN="$value"
      return 0
    fi
    say "  $problem"
  done
  fail "no valid $label was entered." "run the installer again."
}

step_inputs() {
  local cert_note
  step 5 "Domain and email"
  if [ "${OPT_DOMAIN%.}" != "$OPT_DOMAIN" ]; then
    OPT_DOMAIN="${OPT_DOMAIN%.}"
  fi
  if [ -z "$OPT_DOMAIN" ] && [ -z "$EXISTING_DOMAIN" ]; then
    if [ "$TLS_MODE" != "internal" ]; then
      print_dns_guidance
    fi
    say "Enter the full domain name people will use to reach pmsbridge, for example mcp.yourcompany.com."
  fi
  choose_value "the domain name" "Domain name: " "$OPT_DOMAIN" "$EXISTING_DOMAIN" domain_error 1
  DOMAIN="$CHOSEN"
  choose_value "the email address" "Your email address (for certificate notices and the first administrator): " \
    "$OPT_EMAIL" "$EXISTING_EMAIL" email_error 0
  EMAIL="$CHOSEN"
  if [ -n "$OPT_ADMIN_EMAIL" ]; then
    choose_value "the administrator email address" "" "$OPT_ADMIN_EMAIL" "" email_error 0
    ADMIN_EMAIL="$CHOSEN"
  else
    ADMIN_EMAIL="$EMAIL"
  fi
  if [ "$TLS_MODE" = "internal" ]; then
    cert_note="a private test certificate (not trusted by AI assistants or browsers)"
  else
    cert_note="a real certificate, obtained automatically"
  fi
  say ""
  say "pmsbridge will be set up with these values:"
  say "  Domain:                $DOMAIN"
  say "  Email for notices:     $EMAIL"
  say "  First administrator:   $ADMIN_EMAIL"
  say "  Certificate:           $cert_note"
  say "  Install folder:        $INSTALL_DIR"
  if [ "$OPT_YES" -eq 0 ]; then
    if ! confirm "Continue?" y; then
      err ""
      err "Stopped at your request. Nothing has been started. Run the installer again with --domain and --email to change these values."
      FAIL_SHOWN=1
      exit 1
    fi
  fi
}

# ---------------------------------------------------------------------------------------------------
# Step 6: DNS
# ---------------------------------------------------------------------------------------------------

is_ipv4() {
  printf '%s' "$1" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$'
}

is_private_ipv4() {
  local a b
  IFS=. read -r a b _ _ <<EOF
$1
EOF
  case "$a" in "" | *[!0-9]*) return 1 ;; esac
  case "$b" in "" | *[!0-9]*) return 1 ;; esac
  a=$((10#$a))
  b=$((10#$b))
  [ "$a" -eq 10 ] && return 0
  [ "$a" -eq 127 ] && return 0
  [ "$a" -eq 0 ] && return 0
  [ "$a" -eq 172 ] && [ "$b" -ge 16 ] && [ "$b" -le 31 ] && return 0
  [ "$a" -eq 192 ] && [ "$b" -eq 168 ] && return 0
  [ "$a" -eq 169 ] && [ "$b" -eq 254 ] && return 0
  [ "$a" -eq 100 ] && [ "$b" -ge 64 ] && [ "$b" -le 127 ] && return 0
  return 1
}

# The addresses configured on this server's own network interfaces, one per line.
local_addresses() {
  ip -o addr show scope global 2>/dev/null | awk '{split($4, a, "/"); if (a[1] != "") print a[1]}' | sort -u || true
}

resolve_domain() {
  getent "$1" "$2" 2>/dev/null | awk '{print $1}' | sort -u || true
}

list_contains() {
  printf '%s\n' "$1" | grep -Fxq -- "$2"
}

# Sets DNS_STATUS: match, nat (cannot confirm), mismatch or unresolved.
dns_evaluate() {
  local locals address v4="none" v6="none"
  DNS_RESOLVED_V4="$(resolve_domain ahostsv4 "$DOMAIN")"
  DNS_RESOLVED_V6="$(resolve_domain ahostsv6 "$DOMAIN")"
  locals="$(local_addresses)"
  compute_server_addresses
  if [ -z "$DNS_RESOLVED_V4" ] && [ -z "$DNS_RESOLVED_V6" ]; then
    DNS_STATUS="unresolved"
    return 0
  fi
  if [ -n "$DNS_RESOLVED_V4" ]; then
    v4="bad"
    for address in $DNS_RESOLVED_V4; do
      if list_contains "$locals" "$address"; then
        v4="ok"
      fi
    done
    if [ "$v4" = "bad" ] && [ -z "$DNS_PUBLIC_V4" ]; then
      v4="nat"
    fi
  fi
  if [ -n "$DNS_RESOLVED_V6" ]; then
    v6="bad"
    for address in $DNS_RESOLVED_V6; do
      if list_contains "$locals" "$address"; then
        v6="ok"
      fi
    done
  fi
  if [ "$v6" = "bad" ]; then
    DNS_STATUS="mismatch"
  elif [ "$v4" = "ok" ] || { [ "$v4" = "none" ] && [ "$v6" = "ok" ]; }; then
    DNS_STATUS="match"
  elif [ "$v4" = "nat" ]; then
    DNS_STATUS="nat"
  else
    DNS_STATUS="mismatch"
  fi
}

# Sets DNS_PUBLIC_V4 and DNS_PUBLIC_V6 to this server's public addresses, found only on its own network
# interfaces (never by asking a web service). Private, loopback, link-local and unique-local addresses
# are skipped. Each is empty when there is none, which is common on cloud servers behind NAT.
compute_server_addresses() {
  local address
  DNS_PUBLIC_V4=""
  DNS_PUBLIC_V6=""
  for address in $(local_addresses); do
    case "$address" in
      *:*)
        case "$address" in
          ::1 | [fF][cdCD]* | [fF][eE][89abAB]*) ;;
          *)
            if [ -z "$DNS_PUBLIC_V6" ]; then DNS_PUBLIC_V6="$address"; fi
            ;;
        esac
        ;;
      *)
        if [ -z "$DNS_PUBLIC_V4" ] && is_ipv4 "$address" && ! is_private_ipv4 "$address"; then
          DNS_PUBLIC_V4="$address"
        fi
        ;;
    esac
  done
}

# print_record_table NAME: the DNS record to create, in a plain table. The values are this server's
# own addresses, or a placeholder when they cannot be found. An AAAA row appears only when the server
# has a public IPv6 address.
print_record_table() {
  local name="$1" v4 v6 ttl="Auto (or 300)" w_name w_value rule placeholder
  placeholder="<your server's public IP>"
  v4="${DNS_PUBLIC_V4:-$placeholder}"
  v6="$DNS_PUBLIC_V6"
  w_name=${#name}
  if [ "$w_name" -lt 11 ]; then w_name=11; fi
  w_value=${#v4}
  if [ "${#v6}" -gt "$w_value" ]; then w_value=${#v6}; fi
  if [ "$w_value" -lt 18 ]; then w_value=18; fi
  rule="$(printf '    +------+-%s-+-%s-+-%s-+' \
    "$(printf '%*s' "$w_name" '' | tr ' ' '-')" "$(printf '%*s' "$w_value" '' | tr ' ' '-')" \
    "$(printf '%*s' 13 '' | tr ' ' '-')")"
  say "$rule"
  say "$(printf '    | Type | %-*s | %-*s | %-13s |' "$w_name" 'Name (host)' "$w_value" 'Value (points to)' 'TTL')"
  say "$rule"
  say "$(printf '    | A    | %-*s | %-*s | %-13s |' "$w_name" "$name" "$w_value" "$v4" "$ttl")"
  if [ -n "$v6" ]; then
    say "$(printf '    | AAAA | %-*s | %-*s | %-13s |' "$w_name" "$name" "$w_value" "$v6" "$ttl")"
  fi
  say "$rule"
  if [ -z "$DNS_PUBLIC_V4" ]; then
    say "This server's own addresses are all private, which is common on cloud servers. Use the public IP"
    say "address shown in your hosting provider's control panel."
  fi
}

# The notes that go with the table: what "Name" means, A or CNAME, and how long to wait.
print_record_notes() {
  local example_name="$1" example_full="$2"
  say "\"Name\" is only the subdomain part. Some DNS providers want just \"$example_name\", others want the"
  say "full name \"$example_full\"."
  say ""
  say "Use an A record. A CNAME is only for when your server already has a hostname from your hosting"
  say "provider (for example server123.provider.net). Then use Type CNAME, Name $example_name,"
  say "Value server123.provider.net instead. Never use a CNAME on your bare main domain (for example"
  say "yourcompany.com itself)."
}

# Shown before the domain is asked for, because many people have never created a DNS record.
print_dns_guidance() {
  compute_server_addresses
  say ""
  say "First, decide which web address pmsbridge will have, and point it at this server in DNS."
  say ""
  say "Use a subdomain of a domain you already own, for example mcp.yourcompany.com, rather than the"
  say "main domain. Your main website keeps working untouched."
  say ""
  say "At the company that manages your domain's DNS (often where you bought the domain), create this"
  say "record. The example uses the name mcp; use your own:"
  say ""
  print_record_table "mcp"
  say ""
  print_record_notes "mcp" "mcp.yourcompany.com"
  say ""
  say "DNS changes usually take a few minutes, sometimes up to an hour. If the record is not ready yet,"
  say "the installer checks again and waits (step 6)."
  say ""
}

print_dns_instructions() {
  local seen name="${DOMAIN%%.*}"
  seen="$(printf '%s %s' "$DNS_RESOLVED_V4" "$DNS_RESOLVED_V6" | tr '\n' ' ' | sed 's/[[:space:]]*$//; s/^[[:space:]]*//')"
  if [ "$DNS_STATUS" = "unresolved" ]; then
    say "The name $DOMAIN does not point anywhere yet."
  else
    say "The name $DOMAIN points to: $seen"
    say "That is not an address of this server."
  fi
  say "Create this record at the company that manages your domain's DNS:"
  say ""
  print_record_table "$name"
  say ""
  print_record_notes "$name" "$DOMAIN"
  if [ -n "$DNS_RESOLVED_V6" ]; then
    say ""
    say "The name also has an IPv6 (AAAA) record that does not belong to this server. Remove it, or point it"
    say "at this server's IPv6 address. Otherwise the certificate authority may try IPv6 and fail."
  fi
  say ""
  say "DNS changes usually take a few minutes, sometimes up to an hour."
}

step_dns() {
  local attempts i
  step 6 "Check DNS"
  if [ "$TLS_MODE" = "internal" ]; then
    say "Skipped: the internal certificate mode does not need public DNS."
    return 0
  fi
  dns_evaluate
  case "$DNS_STATUS" in
    match)
      say "$DOMAIN points at this server."
      return 0
      ;;
    nat)
      say "$DOMAIN points to: $(printf '%s' "$DNS_RESOLVED_V4" | tr '\n' ' ')"
      say "This server's own network addresses are private, so it is probably behind your provider's address translation and the installer cannot confirm that the record is yours."
      say "If that address is this server's public address, you can continue."
      if confirm "Continue?" y; then
        return 0
      fi
      fail "stopped at your request because the DNS record could not be confirmed." \
        "check the A record for $DOMAIN, then run the installer again."
      ;;
  esac
  print_dns_instructions
  if [ "$OPT_WAIT_DNS" -eq 0 ]; then
    warn "continuing without waiting (--no-wait-dns). The certificate cannot be issued until the record is correct."
    return 0
  fi
  attempts=$((DNS_WAIT_SECONDS / DNS_POLL_SECONDS))
  say "Waiting for the record (checking every $DNS_POLL_SECONDS seconds, for up to $((DNS_WAIT_SECONDS / 60)) minutes). Press Ctrl+C to stop; run the installer again later."
  i=0
  while [ "$i" -lt "$attempts" ]; do
    sleep "$DNS_POLL_SECONDS"
    i=$((i + 1))
    dns_evaluate
    case "$DNS_STATUS" in
      match)
        say "$DOMAIN now points at this server."
        return 0
        ;;
      nat)
        say "$DOMAIN now resolves, but the installer cannot confirm it is this server (private network address). Continuing."
        return 0
        ;;
    esac
    if [ $((i % 4)) -eq 0 ]; then
      say "Still waiting for DNS ($((i * DNS_POLL_SECONDS / 60)) minutes so far)."
    fi
  done
  if can_prompt && confirm "The record did not appear. Continue anyway? The certificate will not be issued until it does." n; then
    return 0
  fi
  fail "the DNS record for $DOMAIN did not point at this server in time." \
    "create the record shown above, wait for it to spread (this can take a few minutes), then run the installer again."
}

# ---------------------------------------------------------------------------------------------------
# Step 7: settings
# ---------------------------------------------------------------------------------------------------

data_volume_exists() {
  docker volume inspect pmsbridge_app_data >/dev/null 2>&1
}

step_settings() {
  local existing_key existing_url
  step 7 "Write the settings"
  mkdir -p "$INSTALL_DIR/deploy"
  existing_key="$(env_get "$ENV_FILE" ENCRYPTION_KEYS)"
  existing_url="$(env_get "$ENV_FILE" PUBLIC_BASE_URL)"
  KEY_CREATED=0
  if [ -z "$existing_key" ]; then
    if data_volume_exists; then
      fail "pmsbridge data from an earlier installation exists on this server, but there is no encryption key in $ENV_FILE. A new key could not read the stored credentials." \
        "restore the original deploy/.env file from your backup into $INSTALL_DIR/deploy/ and run the installer again. If this was only a test and the data can be deleted, run: cd $INSTALL_DIR && sudo docker compose -f deploy/compose.yaml down --volumes, then run the installer again."
    fi
    local key
    if ! key="$(generate_encryption_key)"; then
      fail "a random encryption key could not be created." "check that openssl or /dev/urandom works on this server, then run the installer again."
    fi
    env_set "$ENV_FILE" ENCRYPTION_KEYS "$key"
    key=""
    KEY_CREATED=1
    say "Created a new encryption key."
  else
    say "Keeping the encryption key that is already in $ENV_FILE."
  fi
  if [ -z "$(env_get "$ENV_FILE" ENVIRONMENT)" ]; then
    env_set "$ENV_FILE" ENVIRONMENT production
  fi
  if [ "$DOMAIN" != "$EXISTING_DOMAIN" ] || [ -z "$existing_url" ]; then
    env_set "$ENV_FILE" DOMAIN "$DOMAIN"
    env_set "$ENV_FILE" PUBLIC_BASE_URL "https://$DOMAIN"
  fi
  env_set_if_changed "$ENV_FILE" ACME_EMAIL "$EMAIL"
  env_set_if_changed "$ENV_FILE" TLS_MODE "$TLS_MODE"
  # Make sure the file is private even if an older copy was not.
  chmod 600 "$ENV_FILE"
  chown root:root "$ENV_FILE" 2>/dev/null || true
  say "Settings are in $ENV_FILE (readable by root only)."
  if [ "$KEY_CREATED" -eq 1 ]; then
    print_key_warning
  fi
}

print_key_warning() {
  say ""
  say "================================================================================"
  say "  IMPORTANT: BACK UP YOUR ENCRYPTION KEY NOW"
  say ""
  say "  pmsbridge uses this key to protect the PMS credentials it stores. If you lose"
  say "  it, those credentials cannot be recovered and you would have to enter them"
  say "  again. Never share it."
  say ""
  say "  To show it, run:"
  say "    sudo grep ENCRYPTION_KEYS $ENV_FILE"
  say "  and save that line in a password manager. This message appears only once."
  say "================================================================================"
  say ""
}

# ---------------------------------------------------------------------------------------------------
# Step 8: start
# ---------------------------------------------------------------------------------------------------

compose() {
  docker compose -f deploy/compose.yaml "$@"
}

show_service_logs() {
  local service
  for service in app caddy; do
    say ""
    say "--- last 30 lines of the $service log ---"
    run_logged compose logs --no-color --tail 30 "$service" || true
  done
}

# Waits until https://DOMAIN/healthz answers ok, going to this server directly so it works before DNS settles.
wait_for_health() {
  local port url attempts i body
  port="$(env_get "$ENV_FILE" HTTPS_PORT)"
  if [ -z "$port" ]; then
    port=443
  fi
  if [ "$port" = "443" ]; then
    url="https://$DOMAIN/healthz"
  else
    url="https://$DOMAIN:$port/healthz"
  fi
  attempts=$((HEALTH_WAIT_SECONDS / HEALTH_POLL_SECONDS))
  i=0
  while [ "$i" -lt "$attempts" ]; do
    body=""
    if [ "$TLS_MODE" = "internal" ]; then
      body="$(curl -fsS -k --max-time 5 --resolve "$DOMAIN:$port:127.0.0.1" "$url" 2>/dev/null || true)"
    else
      body="$(curl -fsS --max-time 5 --resolve "$DOMAIN:$port:127.0.0.1" "$url" 2>/dev/null || true)"
    fi
    if printf '%s' "$body" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"'; then
      return 0
    fi
    i=$((i + 1))
    if [ $((i % 6)) -eq 0 ]; then
      say "Still waiting for $url ($((i * HEALTH_POLL_SECONDS)) seconds so far)."
    fi
    sleep "$HEALTH_POLL_SECONDS"
  done
  return 1
}

step_start() {
  step 8 "Start pmsbridge"
  cd "$INSTALL_DIR"
  say "Building and starting the services. The first time this takes a few minutes."
  if ! run_logged compose up -d --build --wait; then
    show_service_logs
    fail "the services did not start." \
      "read the messages above. If one says a port is already in use, stop the program that uses ports 80 and 443 (for example a web server) and run the installer again. If it says 'Pool overlaps', see $DOCS_URL/tls-and-domains.md. Then run the installer again."
  fi
  say "The services are running. Waiting for the secure address to answer."
  if ! wait_for_health; then
    show_service_logs
    if [ "$TLS_MODE" = "auto" ]; then
      fail "https://$DOMAIN/healthz did not answer within $((HEALTH_WAIT_SECONDS / 60)) minutes. The most common cause is that the certificate could not be issued yet." \
        "check that the DNS record for $DOMAIN points at this server and that ports 80 and 443 are open (also in your provider's firewall), then run the installer again. $DOCS_URL/tls-and-domains.md lists what to check."
    fi
    fail "https://$DOMAIN/healthz did not answer within $((HEALTH_WAIT_SECONDS / 60)) minutes." \
      "read the logs above and run the installer again."
  fi
  say "pmsbridge is up and answering on https://$DOMAIN."
}

# ---------------------------------------------------------------------------------------------------
# Step 9: first administrator
# ---------------------------------------------------------------------------------------------------

admin_exists_for_someone_else() {
  local listing
  if ! listing="$(compose exec -T app python -m pmsbridge.admin users list 2>>"$LOG_FILE" </dev/null)"; then
    return 1
  fi
  printf '%s\n' "$listing" | awk -v me="$(lowercase "$ADMIN_EMAIL")" '
    NR > 1 && $2 == "admin" { found = 1; if (tolower($1) == me) mine = 1 }
    END { exit !(found && !mine) }'
}

step_admin() {
  local out="" errfile rc=0 link
  step 9 "Create the first administrator"
  SETUP_LINK=""
  ADMIN_NOTE=""
  if admin_exists_for_someone_else; then
    ADMIN_NOTE="An administrator account already exists, so no new one was created. To see who has an account, run:
  cd $INSTALL_DIR && sudo docker compose -f deploy/compose.yaml exec app python -m pmsbridge.admin users list
To give an administrator a new password, run the same command with 'reset-admin-password --email <their email address>' instead of 'list'."
    say "An administrator already exists. No new one is created."
    return 0
  fi
  errfile="$(mktemp)"
  if out="$(compose exec -T app python -m pmsbridge.admin users create-admin --email "$ADMIN_EMAIL" 2>"$errfile" </dev/null)"; then
    rc=0
  else
    rc=$?
  fi
  if [ "$rc" -eq 0 ]; then
    link="$(printf '%s\n' "$out" | grep -E '^https?://' | tail -n 1 || true)"
    out=""
    rm -f "$errfile"
    if [ -z "$link" ]; then
      fail "the administrator command did not return a setup link." \
        "run it yourself to see why: cd $INSTALL_DIR && sudo docker compose -f deploy/compose.yaml exec app python -m pmsbridge.admin users create-admin --email $ADMIN_EMAIL"
    fi
    SETUP_LINK="$link"
    link=""
    log_line "A setup link was created for the first administrator (not logged)."
    say "The first administrator is ready to be set up."
    return 0
  fi
  out=""
  log_line "create-admin failed with exit code $rc: $(tr '\n' ' ' <"$errfile")"
  if grep -q 'already exists' "$errfile"; then
    rm -f "$errfile"
    ADMIN_NOTE="An account for $ADMIN_EMAIL already exists, so no new setup link was made. To give this administrator a new password, run:
  cd $INSTALL_DIR && sudo docker compose -f deploy/compose.yaml exec app python -m pmsbridge.admin users reset-admin-password --email $ADMIN_EMAIL"
    say "An account for $ADMIN_EMAIL already exists. No new one is created."
    return 0
  fi
  err "$(cat "$errfile")"
  rm -f "$errfile"
  fail "the first administrator could not be created." \
    "read the message above, then run the installer again. The services are running; nothing else is needed."
}

# ---------------------------------------------------------------------------------------------------
# Step 10: finish
# ---------------------------------------------------------------------------------------------------

step_finish() {
  step 10 "Done"
  say ""
  say "pmsbridge is installed."
  say ""
  say "  Dashboard:  https://$DOMAIN"
  say "  MCP address (for AI assistants):  https://$DOMAIN/mcp"
  say ""
  say "What to do next:"
  if [ -n "$SETUP_LINK" ]; then
    say "  1. Open the setup link below in your browser and choose a password."
    say "  2. Set up two-factor sign-in with an authenticator app and save the recovery codes."
  else
    say "  1. Sign in to the dashboard with your administrator account."
    say "  2. Make sure two-factor sign-in is set up."
  fi
  say "  3. Add your Guesty connection (Connections, then Add a Guesty connection)."
  say "     $DOCS_URL/user-guide.md"
  say "  4. Connect an AI assistant to the MCP address above."
  say "     $DOCS_URL/connect-ai-clients.md"
  say ""
  say "Day-to-day commands (run them in $INSTALL_DIR):"
  say "  See the services:   sudo docker compose -f deploy/compose.yaml ps"
  say "  Read the log:       sudo docker compose -f deploy/compose.yaml logs app"
  say "  Stop:               sudo docker compose -f deploy/compose.yaml stop"
  say "  Start again:        sudo docker compose -f deploy/compose.yaml up -d"
  say ""
  say "Your encryption key is in $ENV_FILE. Keep a copy of it in a password manager."
  say "To install again or repair this installation, run the installer again: your key and data are kept."
  if [ -n "$ADMIN_NOTE" ]; then
    say ""
    say "$ADMIN_NOTE"
  fi
  if [ -n "$SETUP_LINK" ]; then
    say ""
    say "Your one-time setup link (valid for 24 hours; it is shown only here and is not saved in the log):"
    printf '\n  %s\n\n' "$SETUP_LINK"
  fi
}

# ---------------------------------------------------------------------------------------------------

main() {
  # The script may have been piped into bash: nothing started below may read the rest of it.
  exec </dev/null
  parse_args "$@"
  check_platform
  init_log
  trap 'on_exit $?' EXIT
  say "pmsbridge installer"
  step_check_server
  step_docker
  step_firewall
  step_code
  step_inputs
  step_dns
  step_settings
  step_start
  step_admin
  step_finish
}

# Run only when executed (or piped into bash), not when the file is loaded with `source`.
if [ -z "${BASH_SOURCE[0]:-}" ] || [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
