#!/usr/bin/env bash
# =============================================================================
# deploy.sh — VPS deployment script
# =============================================================================
# Usage:
#   chmod +x deploy.sh
#   ./deploy.sh                  # Default deploy (port 8050)
#   MD2PDF_PORT=9000 ./deploy.sh # Custom port
# =============================================================================

set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

echo "============================================="
echo "  md2pdf — Deploy"
echo "============================================="

# Pull latest changes (if this is a git repo)
if [ -d .git ]; then
    echo "→ Pulling latest code via git..."
    git pull --ff-only
fi

# Build and restart
echo "→ Building and restarting containers..."
docker compose down --remove-orphans 2>/dev/null || true
docker compose up -d --build

# Cleanup old dangling images
echo "→ Pruning unused images..."
docker image prune -f

echo ""
echo "✅ Deploy complete!"
echo "   Access: http://$(hostname -I | awk '{print $1}'):${MD2PDF_PORT:-8050}"
echo ""

# Show status
docker compose ps
