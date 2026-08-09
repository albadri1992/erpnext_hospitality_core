#!/usr/bin/env bash
# Idempotent setup for the Hospitality Core (ERPNext v15) dev environment.
# Builds a Frappe Bench with Frappe + ERPNext, links this repo as the
# `hospitality_core` app, and provisions a ready-to-use site.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BENCH_DIR="$HOME/frappe-bench"
SITE="hotel.localhost"
FRAPPE_BRANCH="version-15"
DB_ROOT_PW="frappe"
ADMIN_PW="admin"
export PATH="$HOME/.local/bin:$PATH"
export DEBIAN_FRONTEND=noninteractive

echo "==> [1/8] System packages"
if ! command -v mariadbd >/dev/null 2>&1 && ! command -v mysqld >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq \
    git python3-dev python3-venv python3-setuptools redis-server \
    mariadb-server mariadb-client libmariadb-dev pkg-config libssl-dev \
    xvfb libfontconfig1 libxrender1 fontconfig cron
fi
if ! command -v wkhtmltopdf >/dev/null 2>&1; then
  curl -fsSL -o /tmp/wkhtmltox.deb \
    "https://github.com/wkhtmltopdf/packaging/releases/download/0.12.6.1-3/wkhtmltox_0.12.6.1-3.jammy_amd64.deb"
  sudo apt-get install -y -qq /tmp/wkhtmltox.deb
fi

echo "==> [2/8] MariaDB configuration + services"
sudo tee /etc/mysql/mariadb.conf.d/99-frappe.cnf >/dev/null <<'CNF'
[mysqld]
character-set-client-handshake = FALSE
character-set-server = utf8mb4
collation-server = utf8mb4_unicode_ci
[mysql]
default-character-set = utf8mb4
CNF
sudo service mariadb start || true
sudo service redis-server start || true
for _ in $(seq 1 30); do sudo mysqladmin ping >/dev/null 2>&1 && break; sleep 1; done
if ! mysql -uroot -p"$DB_ROOT_PW" -e "SELECT 1" >/dev/null 2>&1; then
  sudo mysql -e "ALTER USER 'root'@'localhost' IDENTIFIED VIA mysql_native_password USING PASSWORD('$DB_ROOT_PW'); FLUSH PRIVILEGES;"
fi

echo "==> [3/8] frappe-bench CLI"
command -v bench >/dev/null 2>&1 || pip install --break-system-packages --quiet frappe-bench

echo "==> [4/8] bench init (Frappe $FRAPPE_BRANCH)"
if [ ! -d "$BENCH_DIR/apps/frappe" ]; then
  bench init "$BENCH_DIR" --frappe-branch "$FRAPPE_BRANCH" --python python3 --skip-redis-config-generation
fi
cd "$BENCH_DIR"

echo "==> [5/8] Point Redis at the system instance + default site"
python3 - "$SITE" <<'PY'
import json, pathlib, sys
site = sys.argv[1]
p = pathlib.Path("sites/common_site_config.json")
c = json.loads(p.read_text())
c["redis_cache"] = "redis://127.0.0.1:6379/0"
c["redis_queue"] = "redis://127.0.0.1:6379/1"
c["redis_socketio"] = "redis://127.0.0.1:6379/2"
c["default_site"] = site
p.write_text(json.dumps(c, indent=1))
PY

echo "==> [6/8] ERPNext + link this repo as hospitality_core"
[ -d "$BENCH_DIR/apps/erpnext" ] || bench get-app --branch "$FRAPPE_BRANCH" erpnext
ln -sfn "$REPO_DIR" "$BENCH_DIR/apps/hospitality_core"
"$BENCH_DIR/env/bin/pip" install --quiet -e "$BENCH_DIR/apps/hospitality_core"
printf 'frappe\nerpnext\nhospitality_core\n' > "$BENCH_DIR/sites/apps.txt"

echo "==> [7/8] Create site + install apps (first run only)"
if [ ! -d "$BENCH_DIR/sites/$SITE" ]; then
  bench new-site "$SITE" --mariadb-root-password "$DB_ROOT_PW" --admin-password "$ADMIN_PW" --no-mariadb-socket
  bench --site "$SITE" install-app erpnext

  # Run the ERPNext setup wizard so a Company and default fixtures
  # (Item Groups, UOMs, Warehouses, Chart of Accounts) exist before
  # hospitality_core's after_install runs.
  cat > /tmp/hc_setup_wizard.py <<'PY'
import frappe
from frappe.utils import now_datetime
from frappe.desk.page.setup_wizard.setup_wizard import setup_complete
y = now_datetime().year
if not frappe.db.a_row_exists("Company"):
    setup_complete({
        "currency": "USD", "full_name": "Hotel Admin",
        "company_name": "Grand Hotel", "company_abbr": "GH",
        "timezone": "America/New_York", "industry": "Services",
        "country": "United States", "language": "english",
        "fy_start_date": f"{y}-01-01", "fy_end_date": f"{y}-12-31",
        "company_tagline": "Hospitality Core Demo",
        "email": "admin@example.com", "password": "admin",
        "chart_of_accounts": "Standard",
    })
    frappe.db.commit()
    print("SETUP_WIZARD_DONE")
PY
  bench --site "$SITE" console < /tmp/hc_setup_wizard.py

  bench --site "$SITE" install-app hospitality_core
  bench --site "$SITE" enable-scheduler
  bench --site "$SITE" set-config developer_mode 1
fi

echo "==> [8/8] Migrate + build assets"
bench --site "$SITE" migrate
bench build --app hospitality_core

echo "==> Install complete. Site: http://localhost:8000  (Administrator / $ADMIN_PW)"
