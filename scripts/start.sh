#!/bin/sh
set -eu

alembic upgrade head
exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --no-access-log \
  --proxy-headers \
  --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16}"
