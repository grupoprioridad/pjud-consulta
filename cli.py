#!/usr/bin/env python3
"""
CLI para consultar causas PJUD.

Ejemplos:
    python cli.py rut 12345678-9
    python cli.py rut 12345678-9 --competencias civil penal
    python cli.py rut 76335837-2 --tipo juridica
    python cli.py nombre Juan --apellido Perez --competencias civil laboral
    python cli.py nombre Juan --apellido Perez --apellido2 Gomez
"""

import argparse
import asyncio
import json
import sys

from pjud import PJUDClient, COMPETENCIAS


async def cmd_rut(args):
    rut_raw = args.rut
    dv = ""
    if "-" in rut_raw:
        partes = rut_raw.split("-")
        rut = partes[0].replace(".", "")
        dv = partes[1]
    else:
        rut = rut_raw.replace(".", "")

    competencias = args.competencias or ["civil", "penal", "laboral"]
    year = args.year

    print(f"Buscando RUT {rut}-{dv} en {competencias}...", file=sys.stderr)
    async with PJUDClient(headless=args.headless) as cliente:
        causas = await cliente.buscar_por_rut(
            rut, dv,
            competencias=competencias,
            year=year,
            tipo=args.tipo,
        )
    return [c.to_dict() for c in causas]


async def cmd_nombre(args):
    competencias = args.competencias or ["civil", "penal", "laboral"]
    print(
        f"Buscando nombre='{args.nombre}' apellido='{args.apellido}' "
        f"en {competencias}...",
        file=sys.stderr
    )
    async with PJUDClient(headless=args.headless) as cliente:
        causas = await cliente.buscar_por_nombre(
            args.nombre,
            args.apellido or "",
            args.apellido2 or "",
            competencias=competencias,
        )
    return [c.to_dict() for c in causas]


def main():
    parser = argparse.ArgumentParser(description="Consulta causas PJUD Chile")
    parser.add_argument("--headless", action="store_true",
                        help="Modo headless (solo funciona sin WAF activo)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # Subcomando: rut
    p_rut = sub.add_parser("rut", help="Buscar por RUT")
    p_rut.add_argument("rut", help="RUT con o sin DV (ej: 12345678-9 o 123456789)")
    p_rut.add_argument(
        "--competencias", nargs="+",
        choices=sorted(COMPETENCIAS),
        default=None,
        help="Competencias a consultar (default: civil penal laboral)",
    )
    p_rut.add_argument("--year", type=int, default=None, help="Año de ingreso")
    p_rut.add_argument(
        "--tipo", choices=["natural", "juridica"], default="natural",
        help="Tipo de persona (default: natural)",
    )

    # Subcomando: nombre
    p_nom = sub.add_parser("nombre", help="Buscar por nombre")
    p_nom.add_argument("nombre", help="Primer nombre")
    p_nom.add_argument("--apellido", default="", help="Apellido paterno")
    p_nom.add_argument("--apellido2", default="", help="Apellido materno")
    p_nom.add_argument(
        "--competencias", nargs="+",
        choices=sorted(COMPETENCIAS),
        default=None,
        help="Competencias a consultar (default: civil penal laboral)",
    )

    args = parser.parse_args()

    if args.cmd == "rut":
        resultado = asyncio.run(cmd_rut(args))
    else:
        resultado = asyncio.run(cmd_nombre(args))

    print(json.dumps(resultado, ensure_ascii=False, indent=2))
    print(f"\n# Total: {len(resultado)} causa(s) encontrada(s)", file=sys.stderr)


if __name__ == "__main__":
    main()
