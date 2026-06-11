#!/usr/bin/env bash
# pjud-consulta v2 — Arranque rápido
# Uso:  bash start.sh       (modo web en http://localhost:8091)
#       bash start.sh cli   (modo terminal interactivo)

set -euo pipefail
cd "$(dirname "$0")"

# ── Verificar dependencias ──────────────────────────────────────────────
echo "==> Verificando dependencias..."

if ! python3 -c "import flask" 2>/dev/null; then
    echo "    Instalando Flask..."
    python3 -m pip install --break-system-packages flask 2>/dev/null || \
    python3 -m pip install flask
fi

if ! python3 -c "import playwright" 2>/dev/null; then
    echo "    Instalando Playwright..."
    python3 -m pip install --break-system-packages playwright 2>/dev/null || \
    python3 -m pip install playwright
    python3 -m playwright install chromium 2>/dev/null || true
fi

if ! command -v himalaya &>/dev/null; then
    echo "    ⚠️  himalaya no encontrado — no se podrá enviar email."
    echo "    Instala: brew install himalaya  o  cargo install himalaya"
fi

# ── Arrancar ────────────────────────────────────────────────────────────
if [ "${1:-}" = "cli" ]; then
    python3 app_v2.py --cli
else
    echo ""
    echo "    ⚖️  PJUD Consulta v2"
    echo "    → Abre http://localhost:8091 en tu navegador"
    echo ""
    python3 app_v2.py
fi
