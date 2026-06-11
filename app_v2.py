"""
pjud-consulta v2 — Buscador masivo de causas PJUD por nombre.

Uso:
    python app_v2.py          → web en http://localhost:8091
    python app_v2.py --cli    → modo terminal: python app_v2.py --cli "Juan Pérez"
"""

import argparse
import asyncio
import csv
import json
import os
import random
import sqlite3
import sys
import textwrap
import threading
import time
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

from pjud import PJUDClient, COMPETENCIAS, COMPETENCIAS_DISPONIBLES

# ── Config ──────────────────────────────────────────────────────────────

DB_PATH = Path(__file__).with_suffix(".db")
EMAIL_FROM = os.getenv("PJUD_EMAIL_FROM", "jacinta@prioridad.cl")
DELAY_MIN = float(os.getenv("PJUD_DELAY_MIN", 3))   # segundos entre consultas
DELAY_MAX = float(os.getenv("PJUD_DELAY_MAX", 8))

COMPETENCIAS_DEFAULT = ["civil", "laboral", "penal", "cobranza", "familia",
                         "apelaciones", "suprema"]

# ── Base de datos ───────────────────────────────────────────────────────

def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS busquedas (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre      TEXT    NOT NULL,
                email       TEXT    NOT NULL,
                fecha       TEXT    NOT NULL,
                estado      TEXT    DEFAULT 'pending',
                total       INTEGER DEFAULT 0,
                completadas INTEGER DEFAULT 0,
                error       TEXT
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS resultados (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                busqueda_id   INTEGER NOT NULL,
                competencia   TEXT,
                rit           TEXT,
                ruc           TEXT,
                tribunal      TEXT,
                caratulado    TEXT,
                fecha_ingreso TEXT,
                estado        TEXT,
                FOREIGN KEY (busqueda_id) REFERENCES busquedas(id)
            )
        """)
        db.commit()

def crear_busqueda(nombre: str, email: str) -> int:
    with sqlite3.connect(DB_PATH) as db:
        cur = db.execute(
            "INSERT INTO busquedas (nombre, email, fecha, estado) VALUES (?, ?, ?, 'pending')",
            (nombre.strip(), email.strip(), datetime.now().isoformat()),
        )
        db.commit()
        return cur.lastrowid

def actualizar_busqueda(busqueda_id: int, **kwargs):
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [busqueda_id]
    with sqlite3.connect(DB_PATH) as db:
        db.execute(f"UPDATE busquedas SET {sets} WHERE id=?", vals)
        db.commit()

def insertar_resultados(busqueda_id: int, competencia: str, causas: list):
    with sqlite3.connect(DB_PATH) as db:
        for c in causas:
            d = c.to_dict() if hasattr(c, "to_dict") else c
            db.execute(
                """INSERT INTO resultados
                   (busqueda_id, competencia, rit, ruc, tribunal, caratulado, fecha_ingreso, estado)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (busqueda_id, competencia, d.get("rit", ""), d.get("ruc", ""),
                 d.get("tribunal", ""), d.get("caratulado", ""),
                 d.get("fecha_ingreso", ""), d.get("estado", "")),
            )
        db.commit()

def get_busqueda(busqueda_id: int) -> dict:
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM busquedas WHERE id=?", (busqueda_id,)).fetchone()
        return dict(row) if row else {}

def resultados_csv(busqueda_id: int) -> str:
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM resultados WHERE busqueda_id=? ORDER BY competencia, tribunal",
            (busqueda_id,),
        ).fetchall()
    out = StringIO()
    if rows:
        w = csv.DictWriter(out, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows([dict(r) for r in rows])
    return out.getvalue()

# ── Motor de búsqueda (background) ──────────────────────────────────────

def _ejecutar_busqueda(busqueda_id: int, nombre: str, email: str,
                       competencias: list[str]):
    """Se ejecuta en un thread separado para no bloquear Flask."""
    total = len(competencias)

    async def _buscar():
        completadas = 0
        try:
            async with PJUDClient(headless=False) as pjud:
                async for comp, _, causas in pjud.stream_por_nombre(
                    nombre=nombre,
                    apellido_paterno="",
                    apellido_materno="",
                    competencias=competencias,
                ):
                    if causas:
                        insertar_resultados(busqueda_id, comp, causas)
                    completadas += 1
                    actualizar_busqueda(
                        busqueda_id,
                        estado="running",
                        total=total,
                        completadas=completadas,
                    )
                    # Delay humano entre competencias
                    delay = DELAY_MIN + random.random() * (DELAY_MAX - DELAY_MIN)
                    # No dormir después de la última
                    if completadas < total:
                        await asyncio.sleep(delay)

            actualizar_busqueda(
                busqueda_id, estado="done", total=total, completadas=completadas
            )
            _enviar_email(busqueda_id, nombre, email)
        except Exception as e:
            actualizar_busqueda(
                busqueda_id, estado="error", error=str(e),
                total=total, completadas=completadas,
            )

    asyncio.run(_buscar())


def _enviar_email(busqueda_id: int, nombre: str, email: str):
    """Envía resultados por email usando himalaya."""
    info = get_busqueda(busqueda_id)
    csv_data = resultados_csv(busqueda_id)
    n_resultados = info.get("total_resultados", 0) if "total_resultados" in info else \
        sum(1 for _ in StringIO(csv_data)) - 1

    # Guardar CSV temporal
    csv_path = f"/tmp/pjud-resultados-{busqueda_id}.csv"
    Path(csv_path).write_text(csv_data, encoding="utf-8")

    mml = f"""From: {EMAIL_FROM}
To: {email}
Subject: PJUD Consulta — "{nombre}" — {n_resultados} resultados
attach: {csv_path}

Resultados de la búsqueda de "{nombre}" en PJUD.

Fecha: {info.get('fecha', 'N/A')}
Competencias revisadas: {info.get('completadas', 0)}/{info.get('total', 0)}
Total resultados encontrados: {n_resultados}

El CSV adjunto contiene el detalle de todas las causas.

Saludos,
Jacinta 🌸
"""
    mml_path = f"/tmp/pjud-mail-{busqueda_id}.mml"
    Path(mml_path).write_text(mml, encoding="utf-8")
    os.system(f"cat {mml_path} | himalaya template send -a jacinta 2>/dev/null")

# ── Flask app ───────────────────────────────────────────────────────────

app = Flask(__name__)

HTML = """\
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PJUD Consulta Masiva</title>
<style>
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         background:#f4f5f7; color:#1a1a2e; min-height:100vh; }
  .container { max-width:700px; margin:2rem auto; padding:1rem; }
  .card { background:#fff; border-radius:12px; box-shadow:0 1px 3px rgba(0,0,0,.1);
          padding:2rem; margin-bottom:1.5rem; }
  h1 { font-size:1.5rem; color:#16213e; margin-bottom:.25rem; }
  .sub { color:#666; font-size:.9rem; margin-bottom:1.5rem; }
  label { display:block; font-weight:600; margin:1rem 0 .3rem; font-size:.9rem; }
  input[type=text], input[type=email], select {
    width:100%; padding:.7rem; border:1px solid #d1d5db; border-radius:8px;
    font-size:1rem; transition:border-color .2s; }
  input:focus, select:focus { outline:none; border-color:#2563eb; box-shadow:0 0 0 3px rgba(37,99,235,.15); }
  .checkbox-group { display:flex; flex-wrap:wrap; gap:.5rem .1rem; }
  .checkbox-group label { font-weight:400; display:inline-flex; align-items:center;
    gap:.25rem; background:#f0f4ff; padding:.35rem .75rem; border-radius:6px;
    cursor:pointer; font-size:.85rem; }
  .checkbox-group input { width:auto; }
  button { width:100%; padding:.85rem; background:#2563eb; color:#fff; border:none;
    border-radius:8px; font-size:1.05rem; font-weight:600; cursor:pointer;
    margin-top:1.5rem; transition:background .2s; }
  button:hover { background:#1d4ed8; }
  button:disabled { background:#9ca3af; cursor:not-allowed; }
  .progress { display:none; margin-top:1rem; }
  .progress.active { display:block; }
  .bar { height:10px; background:#e5e7eb; border-radius:5px; overflow:hidden; }
  .bar-fill { height:100%; background:#2563eb; border-radius:5px;
    transition:width .4s ease; width:0%; }
  .status { font-size:.85rem; color:#666; margin-top:.4rem; display:flex;
    justify-content:space-between; }
  .done-msg { display:none; padding:1rem; background:#ecfdf5; border-radius:8px;
    margin-top:1rem; color:#065f46; }
  .done-msg.active { display:block; }
  .error-msg { display:none; padding:1rem; background:#fef2f2; border-radius:8px;
    margin-top:1rem; color:#991b1b; }
  .error-msg.active { display:block; }
  a { color:#2563eb; }
  .footer { text-align:center; color:#999; font-size:.8rem; margin-top:2rem; }
</style>
</head>
<body>
<div class="container">
  <div class="card">
    <h1>⚖️ PJUD Consulta Masiva</h1>
    <p class="sub">Busca causas por nombre en todas las cortes y competencias</p>

    <form id="form-buscar">
      <label>Nombre a buscar *</label>
      <input type="text" id="nombre" placeholder="Ej: Juan Pérez González" required>

      <label>Email para recibir resultados *</label>
      <input type="email" id="email" placeholder="tu@email.com" required>

      <label>Competencias</label>
      <div class="checkbox-group">
        {% for comp, label in competencias_labels %}
        <label><input type="checkbox" name="comp" value="{{ comp }}" checked> {{ label }}</label>
        {% endfor %}
      </div>

      <button type="submit" id="btn">🔍 Iniciar búsqueda masiva</button>
    </form>

    <div class="progress" id="progreso">
      <div class="bar"><div class="bar-fill" id="bar-fill"></div></div>
      <div class="status">
        <span id="prog-text">Iniciando...</span>
        <span id="prog-pct">0%</span>
      </div>
    </div>

    <div class="done-msg" id="done">
      ✅ Búsqueda completada. Revisa tu correo <strong id="done-email"></strong>.
      <br><a id="descargar-link" href="#">Descargar CSV ahora</a>
    </div>

    <div class="error-msg" id="error-msg"></div>
  </div>
  <p class="footer">Las consultas se hacen con pausas para simular uso humano y evitar bloqueos.</p>
</div>

<script>
const form = document.getElementById('form-buscar');
const btn = document.getElementById('btn');
const progreso = document.getElementById('progreso');
const barFill = document.getElementById('bar-fill');
const progText = document.getElementById('prog-text');
const progPct = document.getElementById('prog-pct');
const doneDiv = document.getElementById('done');
const doneEmail = document.getElementById('done-email');
const errorDiv = document.getElementById('error-msg');
const descLink = document.getElementById('descargar-link');

let busquedaId = null;
let polling = null;

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const nombre = document.getElementById('nombre').value.trim();
  const email = document.getElementById('email').value.trim();
  const comps = [...document.querySelectorAll('input[name=comp]:checked')].map(c => c.value);

  if (!nombre || !email) return alert('Completa nombre y email');
  if (comps.length === 0) return alert('Selecciona al menos una competencia');

  btn.disabled = true;
  btn.textContent = '⏳ Iniciando...';
  doneDiv.classList.remove('active');
  errorDiv.classList.remove('active');
  progreso.classList.add('active');
  barFill.style.width = '0%';
  progText.textContent = 'Conectando con PJUD...';
  progPct.textContent = '0%';

  try {
    const resp = await fetch('/buscar', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({nombre, email, competencias: comps})
    });
    const data = await resp.json();
    busquedaId = data.id;
    descLink.href = '/descargar/' + busquedaId;
    doneEmail.textContent = email;
    polling = setInterval(poll, 2000);
  } catch(err) {
    btn.disabled = false;
    btn.textContent = '🔍 Iniciar búsqueda masiva';
    errorDiv.textContent = 'Error al conectar: ' + err.message;
    errorDiv.classList.add('active');
  }
});

async function poll() {
  if (!busquedaId) return;
  try {
    const resp = await fetch('/progreso/' + busquedaId);
    const data = await resp.json();

    if (data.total > 0) {
      const pct = Math.round((data.completadas / data.total) * 100);
      barFill.style.width = pct + '%';
      progPct.textContent = pct + '%';
      progText.textContent = data.completadas + '/' + data.total + ' competencias';
    }

    if (data.estado === 'done') {
      clearInterval(polling);
      btn.disabled = false;
      btn.textContent = '🔍 Nueva búsqueda';
      barFill.style.width = '100%';
      progPct.textContent = '100%';
      progText.textContent = 'Completado';
      doneDiv.classList.add('active');
    }

    if (data.estado === 'error') {
      clearInterval(polling);
      btn.disabled = false;
      btn.textContent = '🔍 Reintentar';
      errorDiv.textContent = 'Error: ' + (data.error || 'desconocido');
      errorDiv.classList.add('active');
    }
  } catch(err) {
    // Silencio
  }
}
</script>
</body>
</html>
"""

@app.route("/")
def index():
    labels = [
        ("civil", "Civil"), ("laboral", "Laboral"), ("penal", "Penal"),
        ("cobranza", "Cobranza"), ("familia", "Familia"),
        ("apelaciones", "Apelaciones"), ("suprema", "Suprema"),
    ]
    return render_template_string(HTML, competencias_labels=labels)


@app.route("/buscar", methods=["POST"])
def buscar():
    data = request.get_json(force=True)
    nombre = data.get("nombre", "").strip()
    email = data.get("email", "").strip()
    competencias = data.get("competencias", COMPETENCIAS_DEFAULT)

    if not nombre or not email:
        return jsonify({"error": "nombre y email requeridos"}), 400

    competencias = [c for c in competencias if c in COMPETENCIAS]
    if not competencias:
        return jsonify({"error": "competencias inválidas"}), 400

    busqueda_id = crear_busqueda(nombre, email)
    actualizar_busqueda(busqueda_id, estado="running", total=len(competencias))

    t = threading.Thread(
        target=_ejecutar_busqueda,
        args=(busqueda_id, nombre, email, competencias),
        daemon=True,
    )
    t.start()

    return jsonify({"id": busqueda_id, "total": len(competencias)})


@app.route("/progreso/<int:busqueda_id>")
def progreso(busqueda_id: int):
    info = get_busqueda(busqueda_id)
    if not info:
        return jsonify({"error": "no encontrado"}), 404
    return jsonify(info)


@app.route("/descargar/<int:busqueda_id>")
def descargar(busqueda_id: int):
    csv_data = resultados_csv(busqueda_id)
    info = get_busqueda(busqueda_id)
    nombre = info.get("nombre", "consulta").replace(" ", "_")
    return (
        csv_data,
        200,
        {
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": f'attachment; filename="pjud_{nombre}.csv"',
        },
    )


# ── Modo CLI ────────────────────────────────────────────────────────────

def modo_cli(nombre: str, email: str):
    """Ejecuta búsqueda directamente desde terminal."""
    print(f"\n⚖️  PJUD Consulta — Buscando: {nombre}\n")
    competencias = COMPETENCIAS_DEFAULT
    busqueda_id = crear_busqueda(nombre, email)
    actualizar_busqueda(busqueda_id, estado="running", total=len(competencias))

    completadas = 0
    async def _run():
        nonlocal completadas
        async with PJUDClient(headless=False) as pjud:
            async for comp, _, causas in pjud.stream_por_nombre(
                nombre=nombre,
                apellido_paterno="",
                apellido_materno="",
                competencias=competencias,
            ):
                if causas:
                    insertar_resultados(busqueda_id, comp, causas)
                completadas += 1
                pct = completadas / len(competencias) * 100
                bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
                print(f"  [{bar}] {comp:<15} {len(causas):>4} resultados  ({completadas}/{len(competencias)})")
                actualizar_busqueda(busqueda_id, estado="running",
                                    total=len(competencias), completadas=completadas)
                if completadas < len(competencias):
                    delay = DELAY_MIN + random.random() * (DELAY_MAX - DELAY_MIN)
                    await asyncio.sleep(delay)

        actualizar_busqueda(busqueda_id, estado="done",
                            total=len(competencias), completadas=completadas)
        total_res = sum(1 for _ in StringIO(resultados_csv(busqueda_id))) - 1
        print(f"\n✅ Completado. {total_res} resultados en {completadas} competencias.")
        print(f"   CSV: /tmp/pjud-resultados-{busqueda_id}.csv")
        if email:
            _enviar_email(busqueda_id, nombre, email)
            print(f"   Email enviado a {email}")

    asyncio.run(_run())


# ── Entry point ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PJUD Consulta Masiva")
    parser.add_argument("--cli", nargs="?", const=True, metavar="NOMBRE",
                        help="Modo terminal. Ej: --cli 'Juan Pérez'")
    parser.add_argument("--email", default="", help="Email para recibir resultados (modo CLI)")
    args = parser.parse_args()

    init_db()

    if args.cli:
        nombre = args.cli if isinstance(args.cli, str) else ""
        if not nombre:
            nombre = input("Nombre a buscar: ")
        email = args.email or input("Email para resultados (opcional): ")
        modo_cli(nombre, email)
    else:
        print(f"\n⚖️  PJUD Consulta v2 → http://localhost:8091\n")
        app.run(host="127.0.0.1", port=8091, debug=False)
