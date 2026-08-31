#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Cirqle backend — one-shot setup for a fresh EC2 instance.
# Works on Amazon Linux 2023 (dnf), Amazon Linux 2 (yum), and Ubuntu (apt).
#
# Run it ON THE SERVER with a single command:
#   curl -fsSL https://raw.githubusercontent.com/gkg-gkg/cirqle-proj/main/backend/deploy/setup.sh | bash
#
# Safe to re-run to deploy updates — it pulls the latest code and restarts the
# service WITHOUT touching your .env or database (so users are preserved).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_URL="https://github.com/gkg-gkg/cirqle-proj.git"
APP_DIR="$HOME/cirqle"
BACKEND_DIR="$APP_DIR/backend"
RUN_USER="$(whoami)"

echo "Detected OS:"; grep -E '^(NAME|VERSION)=' /etc/os-release 2>/dev/null || true
echo ""

echo "== 1/7 Installing system packages (python, pip, git) =="
if command -v dnf >/dev/null 2>&1; then
  sudo dnf install -y python3 python3-pip git
elif command -v yum >/dev/null 2>&1; then
  sudo yum install -y python3 python3-pip git
elif command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update -y
  sudo apt-get install -y python3 python3-venv python3-pip git
else
  echo "ERROR: no supported package manager (dnf/yum/apt-get) found." >&2
  exit 1
fi

echo "== 2/7 Fetching the code from GitHub =="
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull
else
  git clone "$REPO_URL" "$APP_DIR"
fi

echo "== 3/7 Creating the Python virtual environment + installing dependencies =="
echo "   Using $(python3 --version)"
python3 -m venv "$BACKEND_DIR/.venv"
"$BACKEND_DIR/.venv/bin/pip" install --upgrade pip
"$BACKEND_DIR/.venv/bin/pip" install -r "$BACKEND_DIR/requirements.txt"

echo "== 4/7 Creating .env (only if it does not exist yet) =="
if [ ! -f "$BACKEND_DIR/.env" ]; then
  SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
  cat > "$BACKEND_DIR/.env" <<EOF
CIRQLE_SECRET_KEY=$SECRET
CIRQLE_DB_PATH=$BACKEND_DIR/cirqle.db
CIRQLE_CORS_ORIGINS=https://gkg-gkg.github.io,https://cirqle.co.uk,https://www.cirqle.co.uk
# Fill this in to enable the Instagram feed (Apify → Settings → API tokens),
# then: sudo systemctl restart cirqle-api
APIFY_TOKEN=
CIRQLE_BRAND_HANDLE=cirqle.ltd
# Stripe — merchant memberships and top-ups. Without these, plans still show
# but checkout returns 503. Get them from the Stripe dashboard, then:
#   python scripts/setup_stripe_plans.py   (creates the plans in that mode)
#   sudo systemctl restart cirqle-api
STRIPE_SECRET_KEY=
STRIPE_WEBHOOK_SECRET=
EOF
  echo "   .env created with a fresh random secret key."
  echo "   NOTE: set APIFY_TOKEN in $BACKEND_DIR/.env to enable the feed, then restart."
else
  echo "   .env already exists — keeping it (and your users) untouched."
  echo "   NOTE: if the feed is new to you, add APIFY_TOKEN=... to $BACKEND_DIR/.env and restart."
fi

echo "== 5/7 Applying database migrations (alembic upgrade head) =="
( cd "$BACKEND_DIR" && "$BACKEND_DIR/.venv/bin/alembic" upgrade head )

echo "== 6/7 Installing + starting the systemd service =="
# Generated dynamically so the user/paths are correct for whatever OS this is.
sudo tee /etc/systemd/system/cirqle-api.service >/dev/null <<EOF
[Unit]
Description=Cirqle FastAPI backend
After=network.target

[Service]
User=$RUN_USER
WorkingDirectory=$BACKEND_DIR
EnvironmentFile=$BACKEND_DIR/.env
ExecStart=$BACKEND_DIR/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable cirqle-api
sudo systemctl restart cirqle-api

echo "== 7/7 Done. Current service status: =="
sleep 2
sudo systemctl --no-pager status cirqle-api | head -n 12
echo ""
echo "✅ The API is now listening on port 8000."
echo "   Test it from your laptop:  curl http://<YOUR_SERVER_PUBLIC_IP>:8000/"
