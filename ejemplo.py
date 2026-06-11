"""
Ejemplos de uso de la librería pjud.py
"""

import asyncio
import json
from pjud import PJUDClient


# ── Ejemplo 1: búsqueda asíncrona por RUT persona natural ─────────────────

async def ejemplo_rut_natural():
    async with PJUDClient(headless=False) as pjud:
        causas = await pjud.buscar_por_rut(
            "12345678", "9",
            competencias=["civil", "laboral", "penal"],
        )
        for c in causas:
            print(c.to_dict())
        print(f"Total: {len(causas)}")


# ── Ejemplo 2: búsqueda por RUT persona jurídica ──────────────────────────

async def ejemplo_rut_juridica():
    async with PJUDClient(headless=False) as pjud:
        causas = await pjud.buscar_por_rut(
            "76335837-2",
            competencias=["civil"],
            tipo="juridica",
        )
        for c in causas:
            print(json.dumps(c.to_dict(), ensure_ascii=False, indent=2))


# ── Ejemplo 3: búsqueda por nombre ────────────────────────────────────────

async def ejemplo_nombre():
    async with PJUDClient(headless=False) as pjud:
        causas = await pjud.buscar_por_nombre(
            "Juan",
            apellido_paterno="Pérez",
            apellido_materno="González",
            competencias=["civil", "penal"],
        )
        for c in causas:
            print(c.to_dict())


# ── Ejemplo 4: API sincrónica (sin async/await) ───────────────────────────

def ejemplo_sincrono():
    # Por RUT
    causas = PJUDClient.buscar_rut_sync("12345678-9", competencias=["civil"])
    print(json.dumps(causas, ensure_ascii=False, indent=2))

    # Por nombre
    causas = PJUDClient.buscar_nombre_sync(
        "María", "González",
        competencias=["laboral", "civil"],
    )
    print(json.dumps(causas, ensure_ascii=False, indent=2))


# ── Ejemplo 5: múltiples búsquedas en la misma sesión ────────────────────

async def ejemplo_multiples():
    ruts = [
        ("12345678", "9"),
        ("98765432", "1"),
        ("11111111", "1"),
    ]

    async with PJUDClient(headless=False) as pjud:
        for rut, dv in ruts:
            causas = await pjud.buscar_por_rut(rut, dv, competencias=["civil"])
            print(f"RUT {rut}-{dv}: {len(causas)} causas")
            for c in causas:
                print(f"  [{c.competencia}] {c.rit} — {c.caratulado[:60]} ({c.estado})")


if __name__ == "__main__":
    # Cambiar por el ejemplo que quieras probar:
    asyncio.run(ejemplo_rut_natural())
    # asyncio.run(ejemplo_nombre())
    # ejemplo_sincrono()
    # asyncio.run(ejemplo_multiples())
