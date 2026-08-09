#!/usr/bin/env bash
# Per-boot service reconciliation: bring up MariaDB and Redis so the bench
# terminal (which runs `bench start`) can connect. Idempotent and returns.
set -euo pipefail

sudo service mariadb start || true
sudo service redis-server start || true

for _ in $(seq 1 30); do
  sudo mysqladmin ping >/dev/null 2>&1 && break
  sleep 1
done

redis-cli ping >/dev/null 2>&1 || echo "warning: redis not responding yet"
echo "start.sh: MariaDB and Redis are up."
