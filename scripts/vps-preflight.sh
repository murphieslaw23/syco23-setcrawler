#!/usr/bin/env bash
set -euo pipefail

domain="${1:-api.syco23.org}"

section() {
  printf '\n[%s]\n' "$1"
}

section "host"
hostnamectl 2>/dev/null || hostname
uptime
df -h / /opt 2>/dev/null || df -h /

section "docker"
if command -v docker >/dev/null 2>&1; then
  docker --version
  docker compose version 2>/dev/null || true
  docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
  docker compose ls 2>/dev/null || true
else
  printf 'docker: not installed\n'
fi

section "process managers"
systemctl is-active docker caddy nginx apache2 2>/dev/null || true
if command -v pm2 >/dev/null 2>&1; then
  pm2 list
else
  printf 'pm2: not installed\n'
fi

section "listening ports"
ss -lntup

section "domain routes"
grep -R -n -F -- "$domain" \
  /etc/caddy \
  /etc/nginx/sites-enabled \
  /etc/nginx/conf.d \
  /opt \
  2>/dev/null || true

section "public health"
curl -fsS -D - --max-time 15 "https://${domain}/health" -o /tmp/syco23-health.txt \
  || true
if [[ -s /tmp/syco23-health.txt ]]; then
  sed -n '1,40p' /tmp/syco23-health.txt
fi
