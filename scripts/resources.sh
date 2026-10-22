#!/bin/sh
# How much CPU and RAM SpoolSteward uses right now: its containers, and Docker itself, added up.
#   scripts/resources.sh           one reading
#   watch -n 5 scripts/resources.sh   to follow it for a while (print something, scan something, and look)
#
# Why a script: `docker stats` shows one line per container and no total, and it leaves out what Docker itself costs
# (dockerd, containerd and one small process per container), which on a small machine is not little.
#
#   * RAM is what the processes hold, without the files the kernel keeps in memory for speed: it is what `docker stats` calls MEM USAGE.
#   * CPU: 100% = one core (a Raspberry Pi 4 has four: the most is 400%). The containers' CPU is what `docker stats` measures; Docker's
#     own is measured here over the same few seconds.
#   * The Docker line also serves any other container on the same machine: on a machine used only for SpoolSteward it is all ours.
#
# Needs: docker compose, awk, ps. Run it from any folder: it finds the SpoolSteward folder (the one with docker-compose.yml).
set -eu
cd "$(dirname "$0")/.."

ids="$(docker compose ps -q 2>/dev/null || true)"
[ -n "$ids" ] || { echo "No SpoolSteward container is running here. Start them with: docker compose up -d" >&2; exit 1; }

ticks_per_second="$(getconf CLK_TCK 2>/dev/null || echo 100)"
now() { awk '{print $1}' /proc/uptime; }
# the Docker processes: the daemon, containerd, one "shim" per container, and one docker-proxy per published port
docker_pids() { ps -eo pid=,comm= | awk '$2 ~ /^(dockerd|containerd|containerd-shim|docker-proxy)$/ { print $1 }'; }
# CPU time (in clock ticks) used so far by all of them
docker_ticks() {
    for p in $(docker_pids); do awk '{ print $14 + $15 }' "/proc/$p/stat" 2>/dev/null || true; done | awk '{ s += $1 } END { print s + 0 }'
}

t0="$(now)"; c0="$(docker_ticks)"
stats="$(docker stats --no-stream --format '{{.Name}} {{.CPUPerc}} {{.MemUsage}}' $ids)"
t1="$(now)"; c1="$(docker_ticks)"

# the figures of docker stats are "89.2MiB", the number stuck to its unit: they are turned into MB (MiB)
MB='function mb(s,   n, u) {
        n = s + 0; u = s; sub(/^[0-9.]+/, "", u); u = tolower(u)
        if (u == "gib" || u == "gb") return n * 1024
        if (u == "mib" || u == "mb") return n
        if (u == "kib" || u == "kb") return n / 1024
        return n / 1048576
    }'
printf '%s\n' "$stats" | awk "$MB"'{ gsub("%", "", $2); printf "  %-34s CPU %6.1f%%   RAM %6.0f MB\n", $1, $2, mb($3) }'
sums="$(printf '%s\n' "$stats" | awk "$MB"'{ gsub("%", "", $2); c += $2; r += mb($3) } END { printf "%.1f %.0f", c, r }')"
cont_cpu="${sums% *}"; cont_ram="${sums#* }"
printf '\n%-36s CPU %6.1f%%   RAM %6.0f MB\n' "Containers (sum)" "$cont_cpu" "$cont_ram"

dock_ram="$(ps -eo comm=,rss= | awk '$1 ~ /^(dockerd|containerd|containerd-shim|docker-proxy)$/ { s += $2 } END { printf "%.0f", s / 1024 }')"
dock_cpu="$(awk -v a="$c0" -v b="$c1" -v t0="$t0" -v t1="$t1" -v hz="$ticks_per_second" 'BEGIN { d = t1 - t0; if (d <= 0) d = 1; printf "%.1f", (b - a) / hz / d * 100 }')"
printf '%-36s CPU %6.1f%%   RAM %6.0f MB\n' "Docker (daemon, containerd, shims)" "$dock_cpu" "$dock_ram"
awk -v cc="$cont_cpu" -v cr="$cont_ram" -v dc="$dock_cpu" -v dr="$dock_ram" \
    'BEGIN { printf "%-36s CPU %6.1f%%   RAM %6.0f MB\n", "Total", cc + dc, cr + dr }'

# a machine whose kernel does not count memory per container (Raspberry Pi OS, until the memory cgroup is turned on) says 0 for all
if [ "$cont_ram" = "0" ]; then
    echo
    echo "The containers show 0 MB of RAM: this kernel does not measure memory per container. On a Raspberry Pi add" \
         "cgroup_enable=memory cgroup_memory=1 to the line in /boot/firmware/cmdline.txt (one line, with a space before) and reboot." >&2
fi
