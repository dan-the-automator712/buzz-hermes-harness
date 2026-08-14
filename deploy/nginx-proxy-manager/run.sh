#!/usr/bin/env bash
# Nginx Proxy Manager (docker01) helper — replaces HAProxy for new services.
set -euo pipefail
cd "$(dirname "$0")"
case "${1:-help}" in
  start|up)   docker compose up -d ;;
  stop|down)  docker compose down ;;
  restart)    docker compose up -d --force-recreate ;;
  pull)       docker compose pull ;;
  logs)       docker compose logs -f "${2:-nginx-proxy-manager}" ;;
  status|ps)  docker compose ps ;;
  *) echo "Usage: $0 {start|stop|restart|pull|logs|status}" ;;
esac
