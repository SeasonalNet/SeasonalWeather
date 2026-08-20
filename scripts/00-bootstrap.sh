#!/usr/bin/env bash
# SeasonalWeather bootstrap script
# Run as root from inside a fresh clone of the repo:
#
#   git clone https://git.seasonalnet.org/Seasonal_Currency/SeasonalWeather /tmp/SeasonalWeather
#   cd /tmp/SeasonalWeather
#   sudo bash scripts/00-bootstrap.sh
#
# The script rsyncs the repo to /opt/seasonalweather/app/ automatically.
# Do NOT clone directly into /opt/seasonalweather/app/ — clone elsewhere and let
# this script handle the deploy.
#
# Interactive by default when run from a TTY. Use --non-interactive for
# automation/CI. Environment variables remain supported:
#   SEASONAL_INSTALL_PROFILE=x  — minimal|standard|voicetext-paul|dectalk|custom
#   SEASONAL_ESPEAK=0|1        — install espeak-ng fallback TTS packages
#   SEASONAL_FESTIVAL=0|1      — install Festival TTS packages
#   SEASONAL_PIPER=0|1         — install Piper Python TTS requirements
#   SEASONAL_VOICETEXT_PAUL=1  — install the VoiceText Paul (Wine) TTS backend
#   SEASONAL_DECTALK=1         — install DECtalk (builds from source; slow)
#   SEASONAL_DECTALK_UPDATE=1  — pull latest DECtalk source before rebuilding
#   SEASONAL_SAMEDEC=0         — skip installing the Rust samedec decoder
#   SEASONAL_SAMEDEC_VERSION=x — override pinned samedec crate version
#
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log()  { echo "[+] $*"; }
warn() { echo "[!] $*" >&2; }

ui_notice() { echo "[/]: $*"; }

BOOTSTRAP_NONINTERACTIVE="${SEASONAL_BOOTSTRAP_NONINTERACTIVE:-0}"
BOOTSTRAP_TUI="${SEASONAL_BOOTSTRAP_TUI:-auto}"
BOOTSTRAP_PROFILE="${SEASONAL_INSTALL_PROFILE:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --non-interactive)
      BOOTSTRAP_NONINTERACTIVE=1
      shift
      ;;
    --profile)
      BOOTSTRAP_PROFILE="${2:-}"
      shift 2
      ;;
    --no-tui)
      BOOTSTRAP_TUI=0
      shift
      ;;
    --tui)
      BOOTSTRAP_TUI=1
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage: sudo bash scripts/00-bootstrap.sh [options]

Options:
  --non-interactive       Do not prompt; use env vars/defaults only.
  --profile NAME          Install profile: minimal, standard, voicetext-paul, dectalk, custom.
  --tui                   Require a dialog/whiptail TUI when interactive.
  --no-tui                Use stdout/stdin prompts when interactive.

Environment overrides remain supported for automation:
  SEASONAL_INSTALL_PROFILE=minimal|standard|voicetext-paul|dectalk|custom
  SEASONAL_ESPEAK=0|1
  SEASONAL_FESTIVAL=0|1
  SEASONAL_PIPER=0|1
  SEASONAL_DECTALK=0|1
  SEASONAL_VOICETEXT_PAUL=0|1
  SEASONAL_SAMEDEC=0|1
EOF
      exit 0
      ;;
    *)
      warn "Unknown option: $1"
      exit 2
      ;;
  esac
done

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Run as root: sudo bash $0"
  exit 1
fi

is_interactive() {
  [[ "${BOOTSTRAP_NONINTERACTIVE}" != "1" && -t 0 && -t 1 ]]
}

UI_TOOL="plain"
init_ui() {
  if ! is_interactive || [[ "${BOOTSTRAP_TUI}" == "0" ]]; then
    return 0
  fi

  if command -v dialog >/dev/null 2>&1; then
    UI_TOOL="dialog"
    ui_notice "using dialog Terminal User Interface"
    return 0
  fi
  if command -v whiptail >/dev/null 2>&1; then
    UI_TOOL="whiptail"
    ui_notice "using whiptail Terminal User Interface"
    return 0
  fi

  ui_notice "dialog/whiptail not found, falling back to stdout/stdin and disabling Terminal User Interface"
  if [[ "${BOOTSTRAP_TUI}" == "1" ]]; then
    warn "--tui was requested but neither dialog nor whiptail is installed"
    exit 1
  fi
}

plain_menu() {
  local title="$1" default="$2"
  shift 2
  local -a values=()
  local -a labels=()
  local i=1
  echo >&2
  echo "${title}" >&2
  while [[ $# -gt 0 ]]; do
    values+=("$1")
    labels+=("$2")
    printf '  %d) %s\n' "${i}" "$2" >&2
    shift 2
    i=$((i + 1))
  done
  local answer
  while true; do
    read -r -p "Select [${default}]: " answer
    answer="${answer:-${default}}"
    if [[ "${answer}" =~ ^[0-9]+$ ]] && (( answer >= 1 && answer <= ${#values[@]} )); then
      printf '%s\n' "${values[$((answer - 1))]}"
      return 0
    fi
    for i in "${!values[@]}"; do
      if [[ "${answer}" == "${values[$i]}" ]]; then
        printf '%s\n' "${values[$i]}"
        return 0
      fi
    done
    warn "Invalid selection: ${answer}"
  done
}

tui_menu() {
  local title="$1" default="$2"
  shift 2
  local -a args=()
  local selected=""
  while [[ $# -gt 0 ]]; do
    args+=("$1" "$2")
    shift 2
  done

  if [[ "${UI_TOOL}" == "dialog" ]]; then
    selected="$(dialog --stdout --title "SeasonalWeather bootstrap" --default-item "${default}" --menu "${title}" 18 76 8 "${args[@]}")" || return 1
  elif [[ "${UI_TOOL}" == "whiptail" ]]; then
    selected="$(whiptail --title "SeasonalWeather bootstrap" --default-item "${default}" --menu "${title}" 18 76 8 "${args[@]}" 3>&1 1>&2 2>&3)" || return 1
  else
    return 1
  fi
  printf '%s\n' "${selected}"
}

choose_menu() {
  local title="$1" default="$2"
  shift 2
  if [[ "${UI_TOOL}" != "plain" ]]; then
    tui_menu "${title}" "${default}" "$@" && return 0
  fi
  plain_menu "${title}" "${default}" "$@"
}

plain_yesno() {
  local prompt="$1" default="$2" answer
  while true; do
    read -r -p "${prompt} [${default}]: " answer
    answer="${answer:-${default}}"
    case "${answer,,}" in
      y|yes|1|true) return 0 ;;
      n|no|0|false) return 1 ;;
      *) warn "Enter yes or no." ;;
    esac
  done
}

tui_yesno() {
  local prompt="$1" default="$2"
  local extra=()
  [[ "${default,,}" == "n" || "${default,,}" == "no" ]] && extra+=(--defaultno)
  if [[ "${UI_TOOL}" == "dialog" ]]; then
    dialog --title "SeasonalWeather bootstrap" "${extra[@]}" --yesno "${prompt}" 8 76
  elif [[ "${UI_TOOL}" == "whiptail" ]]; then
    whiptail --title "SeasonalWeather bootstrap" "${extra[@]}" --yesno "${prompt}" 8 76
  else
    return 2
  fi
}

choose_yesno() {
  local prompt="$1" default="$2"
  if [[ "${UI_TOOL}" != "plain" ]]; then
    tui_yesno "${prompt}" "${default}" && return 0
    local rc=$?
    [[ ${rc} -eq 1 ]] && return 1
  fi
  plain_yesno "${prompt}" "${default}"
}

apply_install_profile() {
  local profile="$1"
  case "${profile}" in
    minimal)
      SEASONAL_ESPEAK="${SEASONAL_ESPEAK:-0}"
      SEASONAL_FESTIVAL="${SEASONAL_FESTIVAL:-0}"
      SEASONAL_PIPER="${SEASONAL_PIPER:-0}"
      SEASONAL_DECTALK="${SEASONAL_DECTALK:-0}"
      SEASONAL_VOICETEXT_PAUL="${SEASONAL_VOICETEXT_PAUL:-0}"
      SEASONAL_SAMEDEC="${SEASONAL_SAMEDEC:-0}"
      ;;
    standard)
      SEASONAL_ESPEAK="${SEASONAL_ESPEAK:-1}"
      SEASONAL_FESTIVAL="${SEASONAL_FESTIVAL:-0}"
      SEASONAL_PIPER="${SEASONAL_PIPER:-0}"
      SEASONAL_DECTALK="${SEASONAL_DECTALK:-0}"
      SEASONAL_VOICETEXT_PAUL="${SEASONAL_VOICETEXT_PAUL:-0}"
      SEASONAL_SAMEDEC="${SEASONAL_SAMEDEC:-1}"
      ;;
    voicetext-paul|voicetext_paul|vt-paul)
      SEASONAL_ESPEAK="${SEASONAL_ESPEAK:-0}"
      SEASONAL_FESTIVAL="${SEASONAL_FESTIVAL:-0}"
      SEASONAL_PIPER="${SEASONAL_PIPER:-0}"
      SEASONAL_DECTALK="${SEASONAL_DECTALK:-0}"
      SEASONAL_VOICETEXT_PAUL="${SEASONAL_VOICETEXT_PAUL:-1}"
      SEASONAL_SAMEDEC="${SEASONAL_SAMEDEC:-1}"
      ;;
    dectalk)
      SEASONAL_ESPEAK="${SEASONAL_ESPEAK:-0}"
      SEASONAL_FESTIVAL="${SEASONAL_FESTIVAL:-0}"
      SEASONAL_PIPER="${SEASONAL_PIPER:-0}"
      SEASONAL_DECTALK="${SEASONAL_DECTALK:-1}"
      SEASONAL_VOICETEXT_PAUL="${SEASONAL_VOICETEXT_PAUL:-0}"
      SEASONAL_SAMEDEC="${SEASONAL_SAMEDEC:-1}"
      ;;
    custom|"")
      ;;
    *)
      warn "Unknown install profile '${profile}', falling back to standard"
      apply_install_profile standard
      ;;
  esac
}

configure_interactive_features() {
  init_ui

  if ! is_interactive; then
    apply_install_profile "${BOOTSTRAP_PROFILE:-standard}"
    SEASONAL_ESPEAK="${SEASONAL_ESPEAK:-1}"
    SEASONAL_FESTIVAL="${SEASONAL_FESTIVAL:-0}"
    SEASONAL_PIPER="${SEASONAL_PIPER:-0}"
    SEASONAL_DECTALK="${SEASONAL_DECTALK:-0}"
    SEASONAL_VOICETEXT_PAUL="${SEASONAL_VOICETEXT_PAUL:-0}"
    SEASONAL_SAMEDEC="${SEASONAL_SAMEDEC:-1}"
    return 0
  fi

  local profile="${BOOTSTRAP_PROFILE}"
  if [[ -z "${profile}" ]]; then
    profile="$(choose_menu "Select install profile" "standard" \
      minimal "Minimal daemon (no optional TTS/SAME decoder)" \
      standard "Standard install (espeak-ng fallback + samedec)" \
      voicetext-paul "VoiceText Paul production path" \
      dectalk "DECtalk path" \
      custom "Custom feature selection")"
  fi

  apply_install_profile "${profile}"

  if [[ "${profile}" == "custom" ]]; then
    choose_yesno "Install espeak-ng fallback TTS?" "Y" && SEASONAL_ESPEAK=1 || SEASONAL_ESPEAK=0
    choose_yesno "Install Festival TTS packages?" "N" && SEASONAL_FESTIVAL=1 || SEASONAL_FESTIVAL=0
    choose_yesno "Install Piper Python TTS stack?" "N" && SEASONAL_PIPER=1 || SEASONAL_PIPER=0
    choose_yesno "Build/install DECtalk?" "N" && SEASONAL_DECTALK=1 || SEASONAL_DECTALK=0
    choose_yesno "Install VoiceText Paul/Wine backend?" "N" && SEASONAL_VOICETEXT_PAUL=1 || SEASONAL_VOICETEXT_PAUL=0
    choose_yesno "Install Rust samedec SAME decoder?" "Y" && SEASONAL_SAMEDEC=1 || SEASONAL_SAMEDEC=0
  fi

  cat <<EOF

Selected SeasonalWeather install features:
  espeak-ng fallback:   ${SEASONAL_ESPEAK:-0}
  Festival:             ${SEASONAL_FESTIVAL:-0}
  Piper Python stack:   ${SEASONAL_PIPER:-0}
  DECtalk:              ${SEASONAL_DECTALK:-0}
  VoiceText Paul:       ${SEASONAL_VOICETEXT_PAUL:-0}
  samedec decoder:      ${SEASONAL_SAMEDEC:-0}
EOF

  choose_yesno "Continue with these selections?" "Y" || exit 1
}

install_packages() {
  local label="$1"
  shift
  [[ $# -eq 0 ]] && return 0
  log "Installing ${label}"
  apt-get install -y --no-install-recommends "$@"
}


install_repo_wrapper() {
  local name="$1"
  local src="/opt/seasonalweather/app/scripts/wrappers/${name}"
  local dest="/usr/local/bin/${name}"

  if [[ ! -f "${src}" ]]; then
    warn "Repo wrapper missing: ${src}"
    return 1
  fi

  bash -n "${src}"
  install -m 755 "${src}" "${dest}"
}

configure_interactive_features

export DEBIAN_FRONTEND=noninteractive

BASE_APT_PACKAGES=(
  ca-certificates
  curl
  git
  python3 python3-venv
  ffmpeg
  icecast2
  liquidsoap
  sox
  rsync
  tzdata
  plocate
  sudo
)
ESPEAK_APT_PACKAGES=(espeak-ng)
FESTIVAL_APT_PACKAGES=(festival festvox-kallpc16k)
DECTALK_APT_PACKAGES=(
  build-essential
  pkg-config
  autoconf
  automake
  libasound2-dev
  libpulse-dev
)

# -----------------------------------------------------------------------------------------
# OS packages
# -----------------------------------------------------------------------------------------
apt-get update -y
install_packages "base OS packages" "${BASE_APT_PACKAGES[@]}"

if [[ "${SEASONAL_ESPEAK:-1}" == "1" ]]; then
  install_packages "espeak-ng fallback TTS packages" "${ESPEAK_APT_PACKAGES[@]}"
else
  log "Skipping espeak-ng fallback TTS packages"
fi

if [[ "${SEASONAL_FESTIVAL:-0}" == "1" ]]; then
  install_packages "Festival TTS packages" "${FESTIVAL_APT_PACKAGES[@]}"
else
  log "Skipping Festival TTS packages"
fi

if [[ "${SEASONAL_DECTALK:-0}" == "1" ]]; then
  install_packages "DECtalk build dependencies" "${DECTALK_APT_PACKAGES[@]}"
fi

# -----------------------------------------------------------------------------------------
# System user + directories
# -----------------------------------------------------------------------------------------
log "Creating user + directories"
if ! id -u seasonalweather >/dev/null 2>&1; then
  useradd --system --home /var/lib/seasonalweather --shell /usr/sbin/nologin seasonalweather
fi

install -d -o seasonalweather -g seasonalweather /var/lib/seasonalweather
install -d -o seasonalweather -g seasonalweather /var/lib/seasonalweather/state
install -d -o seasonalweather -g seasonalweather /var/lib/seasonalweather/state/cache
install -d -o seasonalweather -g seasonalweather /var/lib/seasonalweather/jobs
install -d -o seasonalweather -g seasonalweather /var/lib/seasonalweather/artifacts
install -d -o seasonalweather -g seasonalweather /var/lib/seasonalweather/artifacts/audio
install -d -o seasonalweather -g seasonalweather /var/log/seasonalweather
install -d -o root            -g root            /etc/seasonalweather

# -----------------------------------------------------------------------------------------
# Deploy repo to /opt/seasonalweather/app via rsync
# -----------------------------------------------------------------------------------------
log "Syncing app to /opt/seasonalweather/app"
install -d -o root -g root /opt/seasonalweather
install -d -o root -g root /opt/seasonalweather/app
rsync -a --delete "${SRC_DIR}/" /opt/seasonalweather/app/

# -----------------------------------------------------------------------------------------
# Python venv
# -----------------------------------------------------------------------------------------
log "Creating Python venv"
if [[ ! -d /opt/seasonalweather/venv ]]; then
  python3 -m venv /opt/seasonalweather/venv
fi
/opt/seasonalweather/venv/bin/python -m pip install --upgrade pip wheel
/opt/seasonalweather/venv/bin/pip install -r /opt/seasonalweather/app/requirements.txt
if [[ "${SEASONAL_PIPER:-0}" == "1" ]]; then
  log "Installing Piper Python TTS stack"
  /opt/seasonalweather/venv/bin/pip install -r /opt/seasonalweather/app/requirements-piper.txt
else
  log "Skipping Piper Python TTS stack"
fi

# -----------------------------------------------------------------------------------------
# samedec SAME/EAS decoder
# -----------------------------------------------------------------------------------------
if [[ "${SEASONAL_SAMEDEC:-1}" == "1" ]]; then
  log "Installing samedec decoder"
  bash /opt/seasonalweather/app/scripts/install-samedec.sh
else
  log "Skipping samedec (set SEASONAL_SAMEDEC=1 to install)"
fi

# -----------------------------------------------------------------------------------------
# Config files — never overwrite existing operator-edited files
# -----------------------------------------------------------------------------------------
log "Installing default config (won't overwrite existing)"

if [[ ! -f /etc/seasonalweather/config.yaml ]]; then
  cp /opt/seasonalweather/app/config/config.yaml /etc/seasonalweather/config.yaml
  warn "Installed default config.yaml — edit it before starting the service!"
  warn "  sudo nano /etc/seasonalweather/config.yaml"
  warn "  NOTE: This is the LIVE config. Do NOT edit the repo copy at"
  warn "        /opt/seasonalweather/app/config/config.yaml — the service does not read it."
else
  log "config.yaml already exists — not overwriting"
  log "  REMINDER: Live config is /etc/seasonalweather/config.yaml"
  log "            /opt/seasonalweather/app/config/config.yaml is the repo template; editing it does nothing."
fi

if [[ ! -f /etc/seasonalweather/radio.liq ]]; then
  cp /opt/seasonalweather/app/liquidsoap/radio.liq /etc/seasonalweather/radio.liq
fi

if [[ ! -f /etc/seasonalweather/seasonalweather.env ]]; then
  cp /opt/seasonalweather/app/config/example.env /etc/seasonalweather/seasonalweather.env
  chmod 600 /etc/seasonalweather/seasonalweather.env
  warn "Installed example seasonalweather.env — fill in credentials before starting!"
  warn "  sudo nano /etc/seasonalweather/seasonalweather.env"
else
  log "seasonalweather.env already exists — not overwriting"
fi

# -----------------------------------------------------------------------------------------
# DECtalk (optional — set SEASONAL_DECTALK=1 to install)
#
#   Build from source: github.com/dectalk/dectalk (develop branch, autoconf/automake)
#   Requires: autoconf automake libasound2-dev libpulse-dev (installed when selected)
#
#   Built say binary:   /opt/dectalk/dectalk/src/dist/say
#   Installed to:       /opt/dectalk/dectalk/dist/say  (path wrapper scripts expect)
#
#   Usage: SEASONAL_DECTALK=1 sudo -E bash scripts/00-bootstrap.sh
# -----------------------------------------------------------------------------------------
if [[ "${SEASONAL_DECTALK:-0}" == "1" ]]; then
  DECTALK_BASE="/opt/dectalk"
  DECTALK_SRC="${DECTALK_BASE}/dectalk"
  DECTALK_SAY_BUILT="${DECTALK_SRC}/src/dist/say"  # where autoconf build outputs say
  DECTALK_DIST="${DECTALK_SRC}/dist"                # where wrapper scripts expect it
  DECTALK_SAY="${DECTALK_DIST}/say"

  log "Ensuring DECtalk is installed at ${DECTALK_SRC}"
  install -d -o root -g root "${DECTALK_BASE}"

  if [[ ! -d "${DECTALK_SRC}/.git" ]]; then
    log "Cloning DECtalk repo (develop branch)"
    rm -rf "${DECTALK_SRC}" || true
    git clone --depth=1 --branch develop https://github.com/dectalk/dectalk "${DECTALK_SRC}"
  else
    if [[ "${SEASONAL_DECTALK_UPDATE:-0}" == "1" ]]; then
      log "Updating DECtalk repo (SEASONAL_DECTALK_UPDATE=1)"
      git -C "${DECTALK_SRC}" pull --ff-only
    else
      log "DECtalk repo already present (not updating; set SEASONAL_DECTALK_UPDATE=1 to pull)"
    fi
  fi

  if [[ ! -x "${DECTALK_SAY}" ]]; then
    log "Building DECtalk from src/ (autogen + configure + make)"
    if [[ ! -f "${DECTALK_SRC}/src/autogen.sh" ]]; then
      warn "DECtalk src/autogen.sh not found — repo may be on wrong branch or incomplete."
      warn "Expected develop branch: github.com/dectalk/dectalk (branch: develop)"
      warn "Skipping DECtalk build."
    else
      (
        cd "${DECTALK_SRC}/src"
        ./autogen.sh
        ./configure
        make -j"$(nproc)"
      )

      if [[ -x "${DECTALK_SAY_BUILT}" ]]; then
        install -d -o root -g root "${DECTALK_DIST}"
        install -m 755 "${DECTALK_SAY_BUILT}" "${DECTALK_SAY}"
        if [[ -d "${DECTALK_SRC}/src/dist/lib" ]]; then
          cp -a "${DECTALK_SRC}/src/dist/lib" "${DECTALK_DIST}/"
        fi
        log "DECtalk built and installed to ${DECTALK_DIST}"
      else
        warn "Build ran but ${DECTALK_SAY_BUILT} not found — check build output above."
        warn "DECtalk backend will not function."
      fi
    fi
  else
    log "DECtalk already built (${DECTALK_SAY} exists)"
  fi

  log "Installing DECtalk wrapper scripts from repo"
  install_repo_wrapper dectalk-env
  install_repo_wrapper dectalk-text2wav
else
  log "Skipping DECtalk (set SEASONAL_DECTALK=1 to install)"
fi

# -----------------------------------------------------------------------------------------
# VoiceText Paul (optional — set SEASONAL_VOICETEXT_PAUL=1 to install)
#
#   Requires Wine. Downloads WeatherRadioSuite-LIB.zip from cdn.dondaplayer.com.
#   Source project: github.com/dondaplayer1/weather-radio-suite (open source)
#
#   After install, set  tts.backend: "voicetext_paul"  in /etc/seasonalweather/config.yaml.
#
#   Usage: SEASONAL_VOICETEXT_PAUL=1 sudo -E bash scripts/00-bootstrap.sh
# -----------------------------------------------------------------------------------------
if [[ "${SEASONAL_VOICETEXT_PAUL:-0}" == "1" ]]; then
  log "Installing VoiceText Paul backend"

  if ! dpkg --print-foreign-architectures | grep -qx i386; then
    log "Enabling i386 architecture for 32-bit Wine support"
    dpkg --add-architecture i386
    apt-get update
  fi
  if ! apt-get install -y --no-install-recommends wine wine32:i386 unzip xvfb x11-utils; then
    warn "wine32:i386 install failed; retrying with distro wine32 package name"
    apt-get install -y --no-install-recommends wine wine32 unzip xvfb x11-utils
  fi

  VTP_USER="voicetext"
  VTP_HOME="/home/${VTP_USER}"
  VTP_STATE_BASE="${SEASONALWEATHER_DATA_BASE:-/var/lib/seasonalweather}"
  VTP_BASE="${VTP_STATE_BASE}/voices/voicetext_paul"
  VTP_ENGINE_DIR="${VTP_BASE}/WeatherRadioSuite-LIB"
  VTP_BIN_DIR="${VTP_ENGINE_DIR}/binary"
  VTP_EXE="${VTP_BIN_DIR}/voicetext_paul.exe"
  VTP_DEFAULT_SOURCE="https://cdn.dondaplayer.com/WeatherRadioSuite-LIB.zip"
  VTP_SOURCE="${SEASONAL_VOICETEXT_PAUL_SOURCE:-${SEASONAL_VOICETEXT_PAUL_ZIP_URL:-${VTP_DEFAULT_SOURCE}}}"
  VTP_SHA256="${SEASONAL_VOICETEXT_PAUL_SHA256:-}"
  VTP_REFRESH="${SEASONAL_VOICETEXT_PAUL_REFRESH:-0}"
  VTP_SYNTH="/usr/local/bin/voicetext_paul_synth"
  VTP_WSKILL="/usr/local/bin/voicetext_paul_wineserver_kill"
  # VoiceText Paul is a 32-bit Windows runtime. Default fresh prefixes to win32
  # and keep Wine's 64-bit/WOW64 environment out of the primary install path.
  # Operators may set VOICETEXT_PAUL_WINEARCH=auto only for a known-good fallback.
  VTP_WINEARCH="${VOICETEXT_PAUL_WINEARCH:-win32}"
  VTP_WINEPREFIX="${VOICETEXT_PAUL_WINEPREFIX:-${VTP_STATE_BASE}/wineprefixes/voicetext_paul_voicetext}"
  VTP_SUDOERS="/etc/sudoers.d/seasonalweather-voicetext-paul"
  VTP_SYSTEMD_DROPIN_DIR="/etc/systemd/system/seasonalweather.service.d"
  VTP_SYSTEMD_DROPIN="${VTP_SYSTEMD_DROPIN_DIR}/10-voicetext-paul.conf"

  wine_prefix_arch() {
    local prefix="$1"
    if [[ -f "${prefix}/system.reg" ]]; then
      awk -F= '/^#arch=/{print $2; exit}' "${prefix}/system.reg" 2>/dev/null || true
    fi
  }

  wine_env_exports() {
    cat <<'WINEENVEOF'
set -euo pipefail
mkdir -p "${VOICETEXT_PAUL_WINEPREFIX}"
export WINEPREFIX="${VOICETEXT_PAUL_WINEPREFIX}"
if [[ "${VOICETEXT_PAUL_WINEARCH}" != "auto" ]]; then
  export WINEARCH="${VOICETEXT_PAUL_WINEARCH}"
else
  unset WINEARCH
fi
export DISPLAY="${VOICETEXT_PAUL_DISPLAY}"
export WINEDEBUG="${VOICETEXT_PAUL_WINEDEBUG}"
export WINEESYNC="${VOICETEXT_PAUL_WINEESYNC:-0}"
export WINEFSYNC="${VOICETEXT_PAUL_WINEFSYNC:-0}"
export WINE_DISABLE_WRITE_WATCH="${VOICETEXT_PAUL_DISABLE_WRITE_WATCH:-1}"
export WINEDLLOVERRIDES="${VOICETEXT_PAUL_WINEDLLOVERRIDES}"
wineboot --init
wineserver -w
WINEENVEOF
  }

  if ! id -u "${VTP_USER}" >/dev/null 2>&1; then
    useradd --system --home "${VTP_HOME}" --create-home --shell /usr/sbin/nologin "${VTP_USER}"
  fi
  install -d -o "${VTP_USER}" -g "${VTP_USER}" "${VTP_HOME}"

  if command -v loginctl >/dev/null 2>&1; then
    log "Enabling persistent systemd user runtime for ${VTP_USER}"
    loginctl enable-linger "${VTP_USER}"
  else
    warn "loginctl not found; VoiceText Paul may be unstable without lingering enabled for ${VTP_USER}"
  fi

  if ! id -nG "${VTP_USER}" | tr ' ' '\n' | grep -qx seasonalweather; then
    log "Adding ${VTP_USER} to seasonalweather group for shared VoiceText runtime access"
    usermod -aG seasonalweather "${VTP_USER}"
  fi

  install -d -o seasonalweather -g seasonalweather "${VTP_STATE_BASE}"
  install -d -o seasonalweather -g seasonalweather "${VTP_STATE_BASE}/audio"
  install -d -o seasonalweather -g seasonalweather "${VTP_STATE_BASE}/cache"
  install -d -o seasonalweather -g seasonalweather "${VTP_STATE_BASE}/voices"
  install -d -o seasonalweather -g seasonalweather -m 2775 "${VTP_BASE}"
  install -d -o "${VTP_USER}"    -g "${VTP_USER}"    "${VTP_STATE_BASE}/wineprefixes"
  install -d -m 700 -o "${VTP_USER}" -g "${VTP_USER}" "${VTP_STATE_BASE}/tmp"
  install -d -o seasonalweather -g seasonalweather -m 2775 "${VTP_ENGINE_DIR}"
  install -d -o seasonalweather -g seasonalweather -m 2775 "${VTP_BIN_DIR}"

  if [[ ! -f "${VTP_EXE}" || "${VTP_REFRESH}" == "1" ]]; then
    if [[ "${VTP_REFRESH}" == "1" ]]; then
      log "Refreshing VoiceText Paul runtime from configured source"
      rm -rf "${VTP_ENGINE_DIR:?}/"*
      install -d -o "${VTP_USER}" -g "${VTP_USER}" "${VTP_ENGINE_DIR}" "${VTP_BIN_DIR}"
    fi

    TMP_SRC=""
    if [[ -d "${VTP_SOURCE}" ]]; then
      log "Installing VoiceText Paul runtime from local directory: ${VTP_SOURCE}"
      cp -a "${VTP_SOURCE}/." "${VTP_ENGINE_DIR}/"
    else
      if [[ -f "${VTP_SOURCE}" ]]; then
        log "Installing VoiceText Paul runtime from local archive: ${VTP_SOURCE}"
        TMP_SRC="${VTP_SOURCE}"
      else
        log "Downloading VoiceText Paul runtime archive: ${VTP_SOURCE}"
        TMP_SRC="$(mktemp /tmp/WeatherRadioSuite-LIB.XXXXXX.archive)"
        curl -fsSL --max-time 120 "${VTP_SOURCE}" -o "${TMP_SRC}"
      fi

      if [[ -n "${VTP_SHA256}" ]]; then
        echo "${VTP_SHA256}  ${TMP_SRC}" | sha256sum -c -
      else
        warn "SEASONAL_VOICETEXT_PAUL_SHA256 is unset; runtime archive is not checksum-pinned"
      fi

      case "${TMP_SRC}" in
        *.tar.gz|*.tgz)
          tar -xzf "${TMP_SRC}" -C "${VTP_ENGINE_DIR}"
          ;;
        *.zip|*.archive|*)
          unzip -o "${TMP_SRC}" -d "${VTP_ENGINE_DIR}"
          ;;
      esac
      if [[ "${TMP_SRC}" == /tmp/WeatherRadioSuite-LIB.*.archive ]]; then
        rm -f "${TMP_SRC}"
      fi
    fi

    if [[ ! -f "${VTP_EXE}" && -f "${VTP_ENGINE_DIR}/WeatherRadioSuite-LIB/binary/voicetext_paul.exe" ]]; then
      log "Detected nested WeatherRadioSuite-LIB directory; normalizing runtime layout"
      cp -a "${VTP_ENGINE_DIR}/WeatherRadioSuite-LIB/." "${VTP_ENGINE_DIR}/"
      rm -rf "${VTP_ENGINE_DIR}/WeatherRadioSuite-LIB"
    fi

    chown -R seasonalweather:seasonalweather "${VTP_ENGINE_DIR}"
    chmod -R u+rwX,g+rwX,o-rwx "${VTP_ENGINE_DIR}"
    find "${VTP_ENGINE_DIR}" -type d -exec chmod 2775 {} +
    if [[ ! -f "${VTP_EXE}" ]]; then
      warn "VoiceText Paul archive did not provide ${VTP_EXE}"
      exit 1
    fi
    log "VoiceText Paul binary installed at ${VTP_EXE}"
  else
    log "VoiceText Paul binary already present - skipping runtime install"
  fi

  VTP_PREFIX_ARCH="$(wine_prefix_arch "${VTP_WINEPREFIX}")"
  if [[ "${VTP_WINEARCH}" == "win32" && -n "${VTP_PREFIX_ARCH}" && "${VTP_PREFIX_ARCH}" != "win32" && "${SEASONAL_VOICETEXT_PAUL_RECREATE_NON_WIN32_PREFIX:-1}" == "1" ]]; then
    warn "Existing VoiceText Paul Wine prefix is ${VTP_PREFIX_ARCH}; recreating it as a 32-bit prefix"
    warn "  Prefix: ${VTP_WINEPREFIX}"
    warn "  Set SEASONAL_VOICETEXT_PAUL_RECREATE_NON_WIN32_PREFIX=0 to preserve it for forensics."
    rm -rf "${VTP_WINEPREFIX:?}"
  fi

  log "Normalizing VoiceText Paul runtime permissions for seasonalweather + ${VTP_USER}"
  chown -R seasonalweather:seasonalweather "${VTP_ENGINE_DIR}"
  chmod -R u+rwX,g+rwX,o-rwx "${VTP_ENGINE_DIR}"
  find "${VTP_ENGINE_DIR}" -type d -exec chmod 2775 {} +
  rm -f "${VTP_BIN_DIR}/output.wav" "${VTP_BIN_DIR}/.voicetext_paul.flock"

  log "Installing VoiceText Paul wrapper scripts from repo"
  install_repo_wrapper voicetext_paul_synth
  install_repo_wrapper voicetext_paul_wineserver_kill

  cat > "${VTP_SUDOERS}" <<SUDOEOF
# SeasonalWeather: voicetext_paul backend
# Allows the seasonalweather service user to invoke the Wine TTS wrappers as the voicetext user.
seasonalweather ALL=(${VTP_USER}) NOPASSWD: ${VTP_SYNTH}
seasonalweather ALL=(${VTP_USER}) NOPASSWD: ${VTP_WSKILL}
SUDOEOF
  chmod 440 "${VTP_SUDOERS}"
  if ! visudo -cf "${VTP_SUDOERS}" >/dev/null 2>&1; then
    warn "sudoers syntax check failed — removing ${VTP_SUDOERS}"
    rm -f "${VTP_SUDOERS}"
  else
    log "sudoers entry installed at ${VTP_SUDOERS}"
  fi

  install -d -o root -g root "${VTP_SYSTEMD_DROPIN_DIR}"
  cat > "${VTP_SYSTEMD_DROPIN}" <<'SYSTEMDEOF'
[Unit]
Wants=seasonalweather-voicetext-xvfb.service
After=seasonalweather-voicetext-xvfb.service
SYSTEMDEOF
  chmod 644 "${VTP_SYSTEMD_DROPIN}"

  log "VoiceText Paul install complete"
  log "  Installed systemd ordering drop-in: ${VTP_SYSTEMD_DROPIN}"
  log "  Activate: set  tts.backend: \"voicetext_paul\"  in /etc/seasonalweather/config.yaml"
else
  log "Skipping VoiceText Paul (set SEASONAL_VOICETEXT_PAUL=1 to install)"
fi

# -----------------------------------------------------------------------------------------
# Icecast config
# -----------------------------------------------------------------------------------------
log "Configuring Icecast"
if [[ -f /etc/default/icecast2 ]]; then
  sed -i 's/^ENABLE=.*/ENABLE=true/' /etc/default/icecast2 || true
  grep -q '^ENABLE=true' /etc/default/icecast2 || echo 'ENABLE=true' >> /etc/default/icecast2
fi

ICECAST_SOURCE_PASSWORD="$(grep -E '^ICECAST_SOURCE_PASSWORD=' /etc/seasonalweather/seasonalweather.env 2>/dev/null | head -n1 | cut -d= -f2- || true)"
ICECAST_SOURCE_PASSWORD="${ICECAST_SOURCE_PASSWORD:-seasonal-source}"
ICECAST_ADMIN_PASSWORD="seasonal-admin"
ICECAST_RELAY_PASSWORD="seasonal-relay"

if [[ -f /etc/icecast2/icecast.xml ]]; then
  sed -i "s#<source-password>.*</source-password>#<source-password>${ICECAST_SOURCE_PASSWORD}</source-password>#g" /etc/icecast2/icecast.xml || true
  sed -i "s#<admin-password>.*</admin-password>#<admin-password>${ICECAST_ADMIN_PASSWORD}</admin-password>#g" /etc/icecast2/icecast.xml || true
  sed -i "s#<relay-password>.*</relay-password>#<relay-password>${ICECAST_RELAY_PASSWORD}</relay-password>#g" /etc/icecast2/icecast.xml || true
fi

if [[ -f /etc/seasonalweather/radio.liq ]]; then
  sed -i "s#password=\"[^\"]*\"#password=\"${ICECAST_SOURCE_PASSWORD}\"#g" /etc/seasonalweather/radio.liq || true
fi

# -----------------------------------------------------------------------------------------
# systemd
# -----------------------------------------------------------------------------------------
log "Installing preflight helper scripts"
if [[ -f /opt/seasonalweather/app/scripts/preflight/seasonalweather-preflight.sh ]]; then
  bash -n /opt/seasonalweather/app/scripts/preflight/seasonalweather-preflight.sh
  install -m 755 /opt/seasonalweather/app/scripts/preflight/seasonalweather-preflight.sh /usr/local/sbin/seasonalweather-preflight.sh
fi


log "Installing systemd units"
cp /opt/seasonalweather/app/systemd/seasonalweather.service /etc/systemd/system/seasonalweather.service
cp /opt/seasonalweather/app/systemd/seasonalweather-liquidsoap.service /etc/systemd/system/seasonalweather-liquidsoap.service
if [[ -f /opt/seasonalweather/app/systemd/seasonalweather-voicetext-xvfb.service ]]; then
  cp /opt/seasonalweather/app/systemd/seasonalweather-voicetext-xvfb.service /etc/systemd/system/seasonalweather-voicetext-xvfb.service
fi
systemctl daemon-reload

if [[ "${SEASONAL_VOICETEXT_PAUL:-0}" == "1" && -f /etc/systemd/system/seasonalweather-voicetext-xvfb.service ]]; then
  log "Enabling seasonalweather-voicetext-xvfb service"
  systemctl enable --now seasonalweather-voicetext-xvfb.service

  if [[ ! -f "${VTP_WINEPREFIX}/system.reg" || "${SEASONAL_VOICETEXT_PAUL_PREFIX_INIT:-1}" == "1" ]]; then
    log "Initializing VoiceText Paul Wine prefix (${VTP_WINEPREFIX}, WINEARCH=${VTP_WINEARCH})"
    VTP_BOOT_STDERR="$(mktemp /tmp/voicetext-paul-wineboot.XXXXXX.err)"
    if ! wine_env_exports | runuser -u "${VTP_USER}" -- env \
        SEASONALWEATHER_DATA_BASE="${VTP_STATE_BASE}" \
        HOME="${VTP_HOME}" \
        VOICETEXT_PAUL_WINEPREFIX="${VTP_WINEPREFIX}" \
        VOICETEXT_PAUL_WINEARCH="${VTP_WINEARCH}" \
        VOICETEXT_PAUL_DISPLAY="${VOICETEXT_PAUL_DISPLAY:-:99}" \
        VOICETEXT_PAUL_WINEDEBUG="${VOICETEXT_PAUL_WINEDEBUG:--all}" \
        VOICETEXT_PAUL_WINEESYNC="${VOICETEXT_PAUL_WINEESYNC:-0}" \
        VOICETEXT_PAUL_WINEFSYNC="${VOICETEXT_PAUL_WINEFSYNC:-0}" \
        VOICETEXT_PAUL_DISABLE_WRITE_WATCH="${VOICETEXT_PAUL_DISABLE_WRITE_WATCH:-1}" \
        VOICETEXT_PAUL_WINEDLLOVERRIDES="${VOICETEXT_PAUL_WINEDLLOVERRIDES:-mscoree,mshtml=}" \
        bash \
        2>"${VTP_BOOT_STDERR}"; then
      warn "VoiceText Paul Wine prefix initialization failed"
      sed 's/^/[voicetext-paul-wineboot] /' "${VTP_BOOT_STDERR}" >&2 || true
      rm -f "${VTP_BOOT_STDERR}"
      exit 1
    fi
    rm -f "${VTP_BOOT_STDERR}"
  fi

  if [[ "${SEASONAL_VOICETEXT_PAUL_SMOKE:-1}" == "1" ]]; then
    log "Running VoiceText Paul smoke test"
    for _ in $(seq 1 50); do
      [[ -S /tmp/.X11-unix/X99 ]] && break
      sleep 0.2
    done

    if ! runuser -u "${VTP_USER}" -- test -w "${VTP_BIN_DIR}" || ! runuser -u seasonalweather -- test -w "${VTP_BIN_DIR}"; then
      warn "VoiceText Paul runtime directory is not writable by both seasonalweather and ${VTP_USER}: ${VTP_BIN_DIR}"
      warn "Expected setgid seasonalweather group ownership; rerun bootstrap after checking user/group setup."
      exit 1
    fi

    VTP_STDERR="$(mktemp /tmp/voicetext-paul-smoke.XXXXXX.err)"
    rm -f "${VTP_BIN_DIR}/output.wav" "${VTP_BIN_DIR}/input1.txt"
    if ! printf '%s\n' 'VoiceText Paul deployment test.' | runuser -u "${VTP_USER}" -- env \
        HOME="${VTP_HOME}" \
        SEASONALWEATHER_DATA_BASE="${VTP_STATE_BASE}" \
        VOICETEXT_DEBUG="${VOICETEXT_DEBUG:-0}" \
        VOICETEXT_PAUL_ATTEMPTS="${VOICETEXT_PAUL_ATTEMPTS:-3}" \
        "${VTP_SYNTH}" >/dev/null 2>"${VTP_STDERR}"; then
      warn "VoiceText Paul smoke test failed; the backend is not safe to enable yet"
      warn "Smoke stderr follows:"
      sed 's/^/[voicetext-paul-smoke] /' "${VTP_STDERR}" >&2 || true
      warn "VoiceText Paul segfaults are often stale-Wine-server/stateful failures; the wrapper retries crash rc=134/139 after wineserver -k."
      warn "If all retry attempts still fail, provide a known-good runtime with SEASONAL_VOICETEXT_PAUL_SOURCE and optionally SEASONAL_VOICETEXT_PAUL_SHA256."
      rm -f "${VTP_STDERR}"
      exit 1
    fi
    rm -f "${VTP_STDERR}"
    if [[ ! -s "${VTP_BIN_DIR}/output.wav" ]]; then
      warn "VoiceText Paul smoke test did not leave a usable output.wav for cross-user cleanup validation"
      exit 1
    fi
    if ! runuser -u seasonalweather -- rm -f "${VTP_BIN_DIR}/output.wav"; then
      warn "VoiceText Paul smoke test synthesized audio, but seasonalweather could not clean up output.wav"
      warn "Check group ownership and setgid permissions on ${VTP_BIN_DIR}"
      exit 1
    fi
    rm -f "${VTP_BIN_DIR}/input1.txt" "${VTP_BIN_DIR}/.voicetext_paul.flock" 2>/dev/null || true
    log "VoiceText Paul smoke test passed"
  else
    warn "Skipping VoiceText Paul smoke test (SEASONAL_VOICETEXT_PAUL_SMOKE=0)"
  fi
fi

log "Installing helper scripts (if present in repo)"
if [[ -f "/opt/seasonalweather/app/scripts/configure-seasonalweather" ]]; then
  bash -n /opt/seasonalweather/app/scripts/configure-seasonalweather
  install -m 755 /opt/seasonalweather/app/scripts/configure-seasonalweather /usr/local/bin/seasonalweather-configure
fi
for f in seasonalweather-audio-prune.sh seasonalweather-prune-audio.sh; do
  if [[ -f "/opt/seasonalweather/app/scripts/${f}" ]]; then
    install -m 755 "/opt/seasonalweather/app/scripts/${f}" "/usr/local/sbin/${f}"
  fi
done
for f in seasonalweather-inject; do
  if [[ -f "/opt/seasonalweather/app/scripts/${f}" ]]; then
    install -m 755 "/opt/seasonalweather/app/scripts/${f}" "/usr/local/bin/${f}"
  fi
done
# -----------------------------------------------------------------------------------------
# Permissions
# -----------------------------------------------------------------------------------------
log "Permissions"
chown seasonalweather:seasonalweather /var/lib/seasonalweather /var/lib/seasonalweather/state /var/lib/seasonalweather/state/cache /var/lib/seasonalweather/jobs /var/lib/seasonalweather/artifacts /var/lib/seasonalweather/artifacts/audio /var/log/seasonalweather
chmod 755 /var/lib/seasonalweather /var/lib/seasonalweather/state /var/lib/seasonalweather/state/cache /var/lib/seasonalweather/jobs /var/lib/seasonalweather/artifacts /var/lib/seasonalweather/artifacts/audio /var/log/seasonalweather

echo
echo "======================================================="
echo " SeasonalWeather bootstrap complete"
echo "======================================================="
echo
echo " IMPORTANT — two config files exist; they are NOT the same:"
echo
echo "   LIVE (service reads this):  /etc/seasonalweather/config.yaml"
echo "   REPO TEMPLATE (ignored):    /opt/seasonalweather/app/config/config.yaml"
echo
echo "   Always edit the LIVE config. Editing the repo template has no effect"
echo "   on the running service."
echo
echo " Configuration:"
echo "   Assistant  →  sudo seasonalweather-configure"
echo "   Manual     →  sudo nano /etc/seasonalweather/config.yaml"
echo "   Secrets    →  sudo nano /etc/seasonalweather/seasonalweather.env"
echo
echo " Next steps:"
echo "   1) Generate/review a live config candidate:"
echo "      sudo seasonalweather-configure"
echo "      # or edit manually: sudo nano /etc/seasonalweather/config.yaml"
echo
echo "   2) Fill in credentials (NWWS_JID, NWWS_PASSWORD, ICECAST_SOURCE_PASSWORD):"
echo "      sudo nano /etc/seasonalweather/seasonalweather.env"
echo
echo "   3) Enable and start services:"
echo "      sudo systemctl enable --now icecast2"
echo "      sudo systemctl enable --now seasonalweather-liquidsoap"
echo "      sudo systemctl enable --now seasonalweather"
echo
echo "   4) Check logs:"
echo "      journalctl -u seasonalweather -f"
echo
echo " Listen:"
echo "   http://<your-ip>:8000/seasonalweather.ogg"
echo
echo " Optional/re-run install profiles:"
echo "   Standard:       sudo bash scripts/00-bootstrap.sh --profile standard"
echo "   DECtalk:        sudo bash scripts/00-bootstrap.sh --profile dectalk"
echo "   VoiceText Paul: sudo bash scripts/00-bootstrap.sh --profile voicetext-paul"
echo "   Automation:     sudo bash scripts/00-bootstrap.sh --non-interactive"
echo "======================================================="
