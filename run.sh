#!/bin/sh
# Restart server.py after a crash. The environment, including OI_WEBHOOK_URL and
# OI_WEBHOOK_TOKEN, is inherited by every launch. Exit 0 and Ctrl+C (130) stop the loop.
cd "$(dirname "$0")" || exit 1
while true; do
  python3 server.py "$@"
  code=$?
  if [ "$code" -eq 0 ] || [ "$code" -eq 130 ]; then
    exit "$code"
  fi
  echo "server.py 退出 ${code}，2 秒后重启" >&2
  sleep 2
done
