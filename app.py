import asyncio
import json
import os
import queue as queue_mod
import threading
from datetime import datetime

from flask import Flask, render_template, request, Response, stream_with_context
from werkzeug.middleware.proxy_fix import ProxyFix

from pjud import PJUDClient, COMPETENCIAS

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_prefix=1)

ALL_COMPETENCIAS = list(COMPETENCIAS.keys())
BASE_PATH = os.getenv('BASE_PATH', '/pjud').rstrip('/') or ''


def _split_rut(rut: str) -> tuple[str, str]:
    rut = rut.strip().replace('.', '')
    if '-' in rut:
        base, dv = rut.split('-', 1)
        return base, dv
    rut = rut.replace('-', '')
    if len(rut) < 2:
        return rut, ''
    return rut[:-1], rut[-1]


def _headless() -> bool:
    val = os.getenv('PJUD_HEADLESS', '0').lower().strip()
    return val not in ('0', 'false', 'no')


def _years_for_range(periodo: str):
    actual = datetime.now().year
    if periodo == '5':
        return list(range(actual, actual - 5, -1))
    if periodo == '10':
        return list(range(actual, actual - 10, -1))
    return None


def _buscar_rut_por_periodo(rut: str, dv: str, competencias: list[str], persona_tipo: str, periodo: str):
    years = _years_for_range(periodo)
    if years is None:
        return PJUDClient.buscar_rut_sync(
            rut,
            dv,
            competencias=competencias,
            year=None,
            tipo=persona_tipo,
            headless=_headless(),
        )

    acumulado = []
    vistos = set()
    for year_val in years:
        items = PJUDClient.buscar_rut_sync(
            rut,
            dv,
            competencias=competencias,
            year=year_val,
            tipo=persona_tipo,
            headless=_headless(),
        )
        for item in items:
            key = (
                item.get('competencia', ''),
                item.get('rit', ''),
                item.get('ruc', ''),
                item.get('tribunal', ''),
                item.get('caratulado', ''),
                item.get('fecha_ingreso', ''),
            )
            if key not in vistos:
                vistos.add(key)
                acumulado.append(item)
    return acumulado


def _ctx(**extra):
    data = {'competencias': ALL_COMPETENCIAS, 'base_path': BASE_PATH}
    data.update(extra)
    return data


@app.route('/', methods=['GET'])
def index():
    return render_template('index.html', **_ctx())


@app.route('/buscar', methods=['POST'])
def buscar():
    tipo_busqueda = request.form.get('tipo_busqueda', 'rut')
    competencias = request.form.getlist('competencias') or ALL_COMPETENCIAS

    resultados = []
    error = ''

    try:
        if tipo_busqueda == 'rut':
            rut = request.form.get('rut', '').strip()
            dv = request.form.get('dv', '').strip()
            periodo = request.form.get('periodo', '5').strip() or '5'
            persona_tipo = request.form.get('persona_tipo', 'natural')
            if not rut:
                raise ValueError('Ingrese RUT')
            if not dv:
                rut, dv = _split_rut(rut)
            resultados = _buscar_rut_por_periodo(
                rut,
                dv,
                competencias,
                persona_tipo,
                periodo,
            )
        else:
            nombre = request.form.get('nombre', '').strip()
            apellido_p = request.form.get('apellido_p', '').strip()
            apellido_m = request.form.get('apellido_m', '').strip()
            if not nombre:
                raise ValueError('Ingrese nombre')
            resultados = PJUDClient.buscar_nombre_sync(
                nombre,
                apellido_p,
                apellido_m,
                competencias=competencias,
                headless=_headless(),
            )
    except Exception as e:
        error = str(e)

    return render_template(
        'results.html',
        **_ctx(resultados=resultados, error=error, tipo_busqueda=tipo_busqueda),
    )


@app.route('/buscar-stream')
def buscar_stream():
    tipo_busqueda = request.args.get('tipo_busqueda', 'rut')
    competencias_sel = request.args.getlist('competencias') or ALL_COMPETENCIAS
    periodo      = request.args.get('periodo', '5')
    rut          = request.args.get('rut', '').strip()
    dv           = request.args.get('dv', '').strip()
    persona_tipo = request.args.get('persona_tipo', 'natural')
    nombre       = request.args.get('nombre', '').strip()
    apellido_p   = request.args.get('apellido_p', '').strip()
    apellido_m   = request.args.get('apellido_m', '').strip()

    def sse_error(msg):
        return Response(
            f"data: {json.dumps({'type': 'error', 'msg': msg})}\n\n",
            mimetype='text/event-stream',
            headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
        )

    if tipo_busqueda == 'rut':
        if not rut:
            return sse_error('Ingrese RUT')
        if not dv:
            rut, dv = _split_rut(rut)
        years = _years_for_range(periodo)
        total = (len(years) if years else 1) * len(competencias_sel)
    else:
        if not nombre:
            return sse_error('Ingrese nombre')
        years = None
        total = len(competencias_sel)

    q = queue_mod.Queue()

    def run_playwright():
        async def _inner():
            try:
                async with PJUDClient(headless=_headless()) as cliente:
                    if tipo_busqueda == 'rut':
                        async for comp, year, causas in cliente.stream_por_rut(
                            rut, dv, competencias_sel, years, persona_tipo
                        ):
                            q.put(('result', comp, year, [c.to_dict() for c in causas]))
                    else:
                        async for comp, year, causas in cliente.stream_por_nombre(
                            nombre, apellido_p, apellido_m, competencias_sel
                        ):
                            q.put(('result', comp, year, [c.to_dict() for c in causas]))
                q.put(('done', None, None, None))
            except Exception as exc:
                q.put(('error', str(exc), None, None))

        asyncio.run(_inner())

    threading.Thread(target=run_playwright, daemon=True).start()

    def generate():
        import time
        # Comentario SSE inmediato: rompe el buffer de Apache/nginx y establece
        # la conexión antes de que Playwright termine de arrancar.
        yield ": connected\n\n"

        done_count = 0
        seen: set = set()
        deadline = time.monotonic() + 300

        while True:
            if time.monotonic() >= deadline:
                yield f"data: {json.dumps({'type': 'error', 'msg': 'Timeout esperando resultados'})}\n\n"
                break

            try:
                kind, comp, year, causas_raw = q.get(timeout=10)
            except queue_mod.Empty:
                # Keepalive para que el proxy no cierre la conexión inactiva
                yield ": keepalive\n\n"
                continue

            if kind == 'done':
                yield f"data: {json.dumps({'type': 'done', 'done': done_count, 'total': total})}\n\n"
                break
            if kind == 'error':
                yield f"data: {json.dumps({'type': 'error', 'msg': comp})}\n\n"
                break

            # kind == 'result'
            done_count += 1
            new_causas = []
            for c in (causas_raw or []):
                key = (
                    c.get('competencia', ''), c.get('rit', ''),
                    c.get('ruc', ''), c.get('tribunal', ''),
                    c.get('caratulado', ''), c.get('fecha_ingreso', ''),
                )
                if key not in seen:
                    seen.add(key)
                    new_causas.append(c)

            payload = {
                'type': 'result',
                'competencia': comp,
                'year': year,
                'causas': new_causas,
                'done': done_count,
                'total': total,
            }
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'X-Content-Type-Options': 'nosniff',
        },
    )


@app.route('/health', methods=['GET'])
def health():
    return {'ok': True, 'base_path': BASE_PATH, 'headless': _headless()}


if __name__ == '__main__':
    host = os.getenv('HOST', '127.0.0.1')
    port = int(os.getenv('PORT', '8091'))
    app.run(host=host, port=port, debug=False, threaded=True)
