#!/usr/bin/env bash
# Start Hearth on Linux or macOS.
# Checks Docker, creates .env on the first run, builds and starts the containers,
# waits for the panel to answer, and opens it in your browser. Safe to run again any time.
# Without a terminal (or with HEARTH_NONINTERACTIVE=1) it takes every default without asking.
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '\033[1m%s\033[0m\n' "$*"; }
die() { printf '\n\033[31m%s\033[0m\n' "$*" >&2; exit 1; }
is_mac() { [[ "$(uname)" == Darwin ]]; }

# ----- Docker ------------------------------------------------------------------------------------
command -v docker >/dev/null 2>&1 \
  || die "Docker isn't installed. Get it from https://docs.docker.com/get-docker/ and run this again."

if ! docker info >/dev/null 2>&1; then
  if is_mac; then
    say "Starting Docker Desktop…"
    open -a Docker 2>/dev/null || true
    for _ in $(seq 60); do docker info >/dev/null 2>&1 && break; sleep 2; done
  fi
  docker info >/dev/null 2>&1 || die "Docker isn't running, or your user can't use it.
Start Docker, or add yourself to the docker group:  sudo usermod -aG docker \$USER  (then log out and back in)."
fi

docker compose version >/dev/null 2>&1 \
  || die "Docker Compose v2 is missing. On Linux install the 'docker-compose-plugin' package."

# ----- First run: .env ---------------------------------------------------------------------------
random_token() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 32
  else head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; fi
}

detect_ip() {
  if is_mac; then ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true
  else hostname -I 2>/dev/null | awk '{print $1}' || true; fi
}

free_gb() {  # free space on the disk that holds $1 (or its nearest existing parent)
  local p="$1"
  while [[ ! -e "$p" && "$p" != / ]]; do p="$(dirname "$p")"; done
  df -Pk "$p" 2>/dev/null | awk 'NR==2 {printf "%.0f", $4/1048576}'
}

choose_data_dir() {  # prints the chosen folder for game servers
  local -a paths=()
  local v mnt
  if is_mac; then
    paths+=("$HOME/Hearth")
    for v in /Volumes/*; do [[ -d "$v" && ! -L "$v" ]] && paths+=("$v/Hearth"); done
  else
    paths+=("/var/lib/hearth")
    while read -r mnt; do
      case "$mnt" in /|/boot*|/efi*|/snap*|/run*|/dev*|/sys*|/proc*|/var/lib/docker*) continue ;; esac
      paths+=("$mnt/hearth")
    done < <(df -P -x tmpfs -x devtmpfs -x squashfs -x overlay -x efivarfs 2>/dev/null | awk 'NR>1 && $2 > 2097152 {print $6}')
  fi
  if [[ ! -t 0 || -n "${HEARTH_NONINTERACTIVE:-}" ]]; then printf '%s\n' "${paths[0]}"; return; fi

  echo "Where should game servers be installed? (worlds, mods and configs live here)" >&2
  local i
  for i in "${!paths[@]}"; do
    printf '  %d) %-36s %6s GB free\n' "$((i + 1))" "${paths[$i]}" "$(free_gb "${paths[$i]}")" >&2
  done
  local answer
  while true; do
    read -r -p "Choose 1-${#paths[@]}, or type a folder path [1]: " answer || answer=""
    answer="${answer:-1}"
    if [[ "$answer" =~ ^[0-9]+$ ]] && (( answer >= 1 && answer <= ${#paths[@]} )); then
      printf '%s\n' "${paths[$((answer - 1))]}"; return
    fi
    # shellcheck disable=SC2088  # a literal ~/ typed by the user
    [[ "$answer" == "~/"* ]] && answer="$HOME/${answer#\~/}"
    if [[ "$answer" == /* && "$answer" != *[\|\&\\]* ]]; then
      printf '%s\n' "${answer%/}"; return
    fi
    echo "Enter a number from the list, or a full path starting with /" >&2
  done
}

if [[ ! -f .env ]]; then
  say "First run: setting up .env"
  data_dir="$(choose_data_dir)"
  mkdir -p "$data_dir" 2>/dev/null || true  # Docker creates it if this needs root
  say "Game servers will be installed in $data_dir"
  detected="$(detect_ip)"
  detected="${detected:-localhost}"
  host=""
  if [[ -t 0 && -z "${HEARTH_NONINTERACTIVE:-}" ]]; then
    echo "Friends join your servers at this machine's address (a domain, public IP, or LAN IP)."
    read -r -p "Address to show players [$detected]: " host || true
  fi
  host="${host:-$detected}"
  [[ "$host" =~ ^[A-Za-z0-9.:-]+$ ]] || die "\"$host\" doesn't look like a hostname or IP address."


  token="$(random_token)"
  # umask in a subshell: .env (which holds the agent token) is never readable by others, not even
  # for the moment between creating it and a chmod.
  (umask 077 && sed -e "s|^HEARTH_AGENT_TOKEN=.*|HEARTH_AGENT_TOKEN=$token|" \
      -e "s|^HEARTH_PUBLIC_HOST=.*|HEARTH_PUBLIC_HOST=$host|" \
      -e "s|^HEARTH_DATA_DIR=.*|HEARTH_DATA_DIR=$data_dir|" \
      .env.example > .env)
  chmod 600 .env
  say "Saved settings to .env (edit it later to change them)."
fi

# A .env copied by hand from .env.example has an empty token; Compose would refuse to start.
if grep -qE '^HEARTH_AGENT_TOKEN=[[:space:]]*$' .env || ! grep -qE '^HEARTH_AGENT_TOKEN=' .env; then
  say "Generating the agent token in .env"
  token="$(random_token)"
  (umask 077 && { grep -vE '^HEARTH_AGENT_TOKEN=' .env; echo "HEARTH_AGENT_TOKEN=$token"; } > .env.tmp)
  mv .env.tmp .env
  chmod 600 .env
fi

port="$(grep -E '^HEARTH_PANEL_PORT=' .env | cut -d= -f2 | tr -d '[:space:]' || true)"
port="${port:-8000}"

# ----- Start ---------------------------------------------------------------------------------------
say "Building and starting Hearth (the first run takes a few minutes)…"
# --pull fetches the latest Python base image, so its security fixes reach the panel and agent.
# Offline, fall back to the copy already on this machine.
docker compose build --pull || { say "Couldn't check for a newer base image; using the one on this machine."; docker compose build; } \
  || die "Building Hearth failed. The messages above say why."
docker compose up -d || die "Starting the containers failed. The messages above say why."

state() {  # the panel's /api/auth/state, or nothing if it isn't answering (yet)
  if command -v curl >/dev/null 2>&1; then curl -fs "http://localhost:$port/api/auth/state"
  elif command -v wget >/dev/null 2>&1; then wget -q -O - "http://localhost:$port/api/auth/state"
  else return 2; fi
}
say "Waiting for the panel…"
ready=0
for _ in $(seq 90); do
  rc=0; reply="$(state 2>/dev/null)" || rc=$?
  if [[ $rc == 2 ]]; then ready=2; break; fi  # no curl or wget: can't check, so don't wait
  if [[ $rc == 0 && -n "$reply" ]]; then ready=1; break; fi
  sleep 2
done
[[ $ready != 0 ]] || die "The panel didn't answer within 3 minutes. See what happened with:  docker compose logs panel"
[[ $ready == 2 ]] && echo "(Install curl to have this script wait until the panel is ready.)"

url="http://localhost:$port"
host="$(grep -E '^HEARTH_PUBLIC_HOST=' .env | cut -d= -f2 || true)"
echo
say "Hearth is running → $url"
[[ -n "$host" && "$host" != localhost ]] && echo "From other devices:  http://$host:$port"
echo "Game servers are stored in $(grep -E '^HEARTH_DATA_DIR=' .env | cut -d= -f2- || echo /var/lib/hearth)"
echo "Open game ports in your firewall/router: 25565-25575/tcp (Minecraft Java), 19132/udp (Bedrock), 2456-2457/udp (Valheim),"
echo "  7777/tcp (Terraria), 7777-7778 (Satisfactory), 34197/udp (Factorio), 8211/udp (Palworld), 28015-28017 (Rust): only the games you run."
echo "Stop with ./stop.sh   ·   Logs: docker compose logs -f"
if [[ "${reply:-}" == *'"needs_setup":true'* ]]; then
  echo
  say "Next: create your admin account at $url"
  echo "Do it from this computer or your local network (Hearth only allows the first account from there)."
fi

if [[ -n "${HEARTH_NONINTERACTIVE:-}" ]]; then :
elif is_mac; then open "$url" >/dev/null 2>&1 || true
elif [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]] && command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$url" >/dev/null 2>&1 || true
fi
