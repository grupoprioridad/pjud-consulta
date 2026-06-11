"""
pjud.py — Cliente Python para consulta de causas en el Poder Judicial de Chile.

Requiere:
    pip install playwright
    playwright install chromium

IMPORTANTE: El sitio usa Imperva WAF que bloquea navegadores headless desde
servidores. En la máquina local del usuario, usar headless=False (por defecto)
funciona correctamente porque Chrome real tiene el fingerprint adecuado.

Uso básico:
    import asyncio
    from pjud import PJUDClient

    async def main():
        async with PJUDClient() as pjud:
            causas = await pjud.buscar_por_rut("12345678", "9")
            for c in causas:
                print(c)

    asyncio.run(main())
"""

import asyncio
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal, Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

BASE_URL = "https://oficinajudicialvirtual.pjud.cl"
HOME_URL = f"{BASE_URL}/home/index.php"
INDEX_URL = f"{BASE_URL}/indexN.php"
SESSION_URL = f"{BASE_URL}/includes/sesion-invitado.php"

COMPETENCIAS: dict[str, int] = {
    "suprema":     1,
    "apelaciones": 2,
    "civil":       3,
    "laboral":     4,
    "penal":       5,
    "cobranza":    6,
    "familia":     7,
}

# Competencias disponibles en búsqueda pública sin login
COMPETENCIAS_DISPONIBLES = {"civil", "laboral", "penal", "cobranza", "apelaciones", "suprema"}

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _detectar_chrome_path() -> Optional[str]:
    candidatos = [
        os.getenv("PJUD_CHROME_PATH", "").strip(),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/opt/homebrew/bin/chromium",
    ]
    for ruta in candidatos:
        if ruta and os.path.exists(ruta):
            return ruta
    return None


# ---------------------------------------------------------------------------
# Modelos de datos
# ---------------------------------------------------------------------------

@dataclass
class Causa:
    rit: str = ""
    ruc: str = ""
    tribunal: str = ""
    caratulado: str = ""
    fecha_ingreso: str = ""
    estado: str = ""
    competencia: str = ""
    ubicacion: str = ""
    tipo_recurso: str = ""
    litigantes: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "rit": self.rit,
            "ruc": self.ruc,
            "tribunal": self.tribunal,
            "caratulado": self.caratulado,
            "fecha_ingreso": self.fecha_ingreso,
            "estado": self.estado,
            "competencia": self.competencia,
            "ubicacion": self.ubicacion,
            "tipo_recurso": self.tipo_recurso,
            "litigantes": self.litigantes,
        }


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _validar_competencias(competencias: list[str]) -> list[str]:
    invalidas = [c for c in competencias if c not in COMPETENCIAS]
    if invalidas:
        raise ValueError(f"Competencias inválidas: {invalidas}. Válidas: {sorted(COMPETENCIAS)}")
    return competencias


def _rut_sin_puntos(rut: str) -> str:
    return rut.replace(".", "").replace("-", "").strip()


def _separar_rut_dv(rut_completo: str) -> tuple[str, str]:
    """Acepta '12345678-9', '123456789', '12.345.678-9'."""
    limpio = _rut_sin_puntos(rut_completo)
    if "-" in rut_completo.replace(".", ""):
        partes = rut_completo.replace(".", "").split("-")
        return partes[0].strip(), partes[1].strip()
    return limpio[:-1], limpio[-1]


# ---------------------------------------------------------------------------
# Cliente principal
# ---------------------------------------------------------------------------

class PJUDClient:
    """
    Cliente asíncrono para el portal de consulta de causas del PJUD.

    Parámetros:
        headless:   False (default) → Chrome visible, bypassa WAF.
                    True → modo headless, solo funciona si el servidor no
                    está bloqueado por Imperva (necesita cookies válidas).
        timeout:    Timeout en ms para operaciones de navegación (default 30000).
        slow_mo:    Retardo entre acciones en ms (útil para depuración).
    """

    def __init__(
        self,
        headless: bool = False,
        timeout: int = 30_000,
        slow_mo: int = 0,
    ):
        self._headless = headless
        self._timeout = timeout
        self._slow_mo = slow_mo
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._session_ok = False

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "PJUDClient":
        self._playwright = await async_playwright().start()
        chrome_path = _detectar_chrome_path()
        launch_kwargs = {
            "headless": self._headless,
            "slow_mo": self._slow_mo,
            "args": [
                "--disable-crash-reporter",
                "--disable-crashpad",
                "--disable-features=Crashpad",
            ],
        }
        if chrome_path:
            launch_kwargs["executable_path"] = chrome_path
        else:
            raise RuntimeError(
                "No encontré Google Chrome/Chromium instalado. "
                "Instala Chrome o define PJUD_CHROME_PATH."
            )

        self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        self._context = await self._browser.new_context(
            user_agent=DEFAULT_USER_AGENT,
            viewport={"width": 1280, "height": 800},
            locale="es-CL",
        )
        self._page = await self._context.new_page()
        self._page.set_default_timeout(self._timeout)
        return self

    async def __aexit__(self, *_):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    # ------------------------------------------------------------------
    # Inicialización de sesión de invitado
    # ------------------------------------------------------------------

    async def _iniciar_sesion(self) -> None:
        """Navega al home, establece sesión de invitado CC y va a indexN."""
        if self._session_ok:
            return

        page = self._page

        # 1) Home page para obtener cookies WAF.
        #    Usar "load" en vez de "domcontentloaded" porque Imperva hace un
        #    JS challenge que genera redirects adicionales; si resolvemos muy
        #    pronto el siguiente evaluate encuentra el contexto destruido.
        await page.goto(HOME_URL, wait_until="load")
        await page.wait_for_timeout(2000)

        # 2) POST sesion-invitado (establece $_SESSION['ACCESO'] = 'CC').
        #    Usamos context.request.post() en vez de page.evaluate(fetch...)
        #    para evitar "Execution context was destroyed" cuando la página
        #    aún está navegando por el challenge WAF.
        try:
            await self._context.request.post(
                SESSION_URL,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data="nombreAcceso=CC",
            )
        except Exception:
            pass

        # 3) Navegar a indexN.php que muestra la interfaz de consulta
        await page.goto(INDEX_URL, wait_until="load")
        await page.wait_for_timeout(2000)

        # Verificar que cargó la página correcta
        title = await page.title()
        if "Request Rejected" in title:
            raise RuntimeError(
                "El WAF bloqueó la sesión. "
                "Asegúrate de usar headless=False en tu máquina local con Chrome instalado."
            )

        # 4) Esperar que jQuery esté disponible
        await page.wait_for_function("typeof jQuery !== 'undefined'", timeout=15000)

        # 5) Activar la sección de consulta de causas
        await page.evaluate("if(typeof accesoConsultaCausas === 'function') accesoConsultaCausas()")
        await page.wait_for_timeout(2000)

        self._session_ok = True

    # ------------------------------------------------------------------
    # Búsqueda por RUT (persona natural)
    # ------------------------------------------------------------------

    async def buscar_por_rut(
        self,
        rut: str,
        dv: str = "",
        *,
        competencias: Optional[list[str]] = None,
        year: Optional[int] = None,
        tipo: Literal["natural", "juridica"] = "natural",
    ) -> list[Causa]:
        """
        Busca causas por RUT.

        Parámetros:
            rut:         Número de RUT sin DV (ej: "12345678") o completo
                         con guión (ej: "12345678-9").
            dv:          Dígito verificador. Se puede omitir si rut incluye guión.
            competencias: Lista de competencias a consultar.
                          Opciones: "civil", "penal", "laboral", "cobranza",
                                    "apelaciones", "suprema".
                          Default: ["civil", "penal", "laboral"].
            year:        Año de ingreso de la causa (opcional).
            tipo:        "natural" (persona natural) o "juridica" (persona jurídica).

        Retorna:
            Lista de objetos Causa.
        """
        if not dv and "-" in rut:
            rut, dv = _separar_rut_dv(rut)
        rut = _rut_sin_puntos(rut).replace("-", "")

        if competencias is None:
            competencias = ["civil", "penal", "laboral"]
        _validar_competencias(competencias)

        await self._iniciar_sesion()

        todas: list[Causa] = []
        for comp in competencias:
            causas = await self._buscar_rut_competencia(rut, dv, comp, year, tipo)
            todas.extend(causas)

        return todas

    async def _buscar_rut_competencia(
        self,
        rut: str,
        dv: str,
        competencia: str,
        year: Optional[int],
        tipo: str,
    ) -> list[Causa]:
        page = self._page
        comp_val = str(COMPETENCIAS[competencia])

        if tipo == "natural":
            tab_selector = "a[href*='tabNatural'], #tabNatural, [data-tab='natural'], a:has-text('Persona Natural')"
            rut_field = "#rutNat"
            dv_field  = "#dvNat"
            era_field = "#eraNat"
            comp_select = "#natCompetencia"
            btn_buscar = "#btnConConsultaNat"
            tabla = "#verDetalleNatural"
            loader = "#loadPreNatural"
        else:
            tab_selector = "a[href*='tabJuridica'], #tabJuridica, [data-tab='juridica'], a:has-text('Persona Jurídica')"
            rut_field = "#rutJur"
            dv_field  = "#dvJur"
            era_field = "#eraJur"
            comp_select = "#jurCompetencia"
            btn_buscar = "#btnConConsultaJur"
            tabla = "#verDetalleJuridica"
            loader = "#loadPreJuridica"

        # Activar tab correcto
        try:
            await page.click(tab_selector, timeout=5000)
            await page.wait_for_timeout(500)
        except Exception:
            pass

        # Limpiar y llenar campos
        await page.fill(rut_field, "")
        await page.type(rut_field, rut)

        await page.fill(dv_field, "")
        await page.type(dv_field, dv)

        if year and await page.query_selector(era_field):
            await page.fill(era_field, "")
            await page.type(era_field, str(year))

        # Seleccionar competencia
        await page.select_option(comp_select, comp_val)
        await page.wait_for_timeout(500)

        # Pulsar buscar
        await page.click(btn_buscar)

        # Esperar que desaparezca el loader o que aparezca la tabla
        try:
            await page.wait_for_selector(f"{loader}.hide, {loader}[style*='none']", timeout=15000)
        except Exception:
            await page.wait_for_timeout(3000)

        return await self._extraer_causas_tabla(tabla, competencia)

    # ------------------------------------------------------------------
    # Búsqueda por nombre
    # ------------------------------------------------------------------

    async def buscar_por_nombre(
        self,
        nombre: str,
        apellido_paterno: str = "",
        apellido_materno: str = "",
        *,
        competencias: Optional[list[str]] = None,
    ) -> list[Causa]:
        """
        Busca causas por nombre de persona.

        Parámetros:
            nombre:           Primer nombre.
            apellido_paterno: Apellido paterno (recomendado para acotar).
            apellido_materno: Apellido materno (opcional).
            competencias:     Lista de competencias. Default: ["civil","penal","laboral"].

        Retorna:
            Lista de objetos Causa.
        """
        if competencias is None:
            competencias = ["civil", "penal", "laboral"]
        _validar_competencias(competencias)

        await self._iniciar_sesion()

        todas: list[Causa] = []
        for comp in competencias:
            causas = await self._buscar_nombre_competencia(
                nombre, apellido_paterno, apellido_materno, comp
            )
            todas.extend(causas)

        return todas

    async def _buscar_nombre_competencia(
        self,
        nombre: str,
        apellido_paterno: str,
        apellido_materno: str,
        competencia: str,
    ) -> list[Causa]:
        page = self._page
        comp_val = str(COMPETENCIAS[competencia])

        # Selectores posibles para el tab de búsqueda por nombre
        tab_nombre_selectors = [
            "a:has-text('Por Nombre')",
            "a:has-text('Nombre')",
            "#tabNombre",
            "[data-tab='nombre']",
            "a[href*='tabNombre']",
        ]

        for sel in tab_nombre_selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    await page.wait_for_timeout(500)
                    break
            except Exception:
                continue

        # Campos posibles según versión del formulario PJUD
        nombre_field_candidates    = ["#nombre", "#nombreNom", "#inputNombre"]
        apellido_p_candidates      = ["#apellidoPaterno", "#apPaternoNom", "#apellidoP"]
        apellido_m_candidates      = ["#apellidoMaterno", "#apMaternoNom", "#apellidoM"]
        comp_nombre_candidates     = ["#nomCompetencia", "#competenciaNom", "#nombreCompetencia"]
        btn_nombre_candidates      = ["#btnConConsultaNom", "#btnBuscarNombre", "#btnNombre"]
        tabla_nombre_candidates    = ["#verDetalleNombre", "#tablaResultadosNombre", "#resultadosNombre"]

        async def fill_first(candidates, value):
            for sel in candidates:
                el = await page.query_selector(sel)
                if el:
                    await page.fill(sel, "")
                    await page.type(sel, value)
                    return sel
            return None

        await fill_first(nombre_field_candidates, nombre)
        if apellido_paterno:
            await fill_first(apellido_p_candidates, apellido_paterno)
        if apellido_materno:
            await fill_first(apellido_m_candidates, apellido_materno)

        # Competencia
        for sel in comp_nombre_candidates:
            el = await page.query_selector(sel)
            if el:
                await page.select_option(sel, comp_val)
                break

        await page.wait_for_timeout(300)

        # Botón buscar
        btn_usado = None
        for sel in btn_nombre_candidates:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                btn_usado = sel
                break

        if not btn_usado:
            return []

        await page.wait_for_timeout(3000)

        # Tabla de resultados
        for sel in tabla_nombre_candidates:
            el = await page.query_selector(sel)
            if el:
                return await self._extraer_causas_tabla(sel, competencia)

        return []

    # ------------------------------------------------------------------
    # Extracción de tabla de resultados (con paginación)
    # ------------------------------------------------------------------

    async def _extraer_causas_tabla(
        self, tabla_selector: str, competencia: str
    ) -> list[Causa]:
        page = self._page
        causas: list[Causa] = []
        pagina = 1

        while True:
            tabla = await page.query_selector(tabla_selector)
            if not tabla:
                break

            filas = await tabla.query_selector_all("tbody tr")
            if not filas:
                break

            nuevas = await self._parsear_filas(filas, competencia)
            causas.extend(nuevas)

            # Intentar ir a la siguiente página
            siguiente = await self._ir_siguiente_pagina(tabla_selector, pagina, competencia)
            if not siguiente:
                break
            pagina += 1
            await page.wait_for_timeout(1500)

        return causas

    async def _parsear_filas(self, filas, competencia: str) -> list[Causa]:
        causas = []
        for fila in filas:
            celdas = await fila.query_selector_all("td")
            if not celdas:
                continue

            textos = [_clean(await c.inner_text()) for c in celdas]
            if not any(textos):
                continue

            # El número de columnas varía por competencia
            n = len(textos)
            causa = Causa(competencia=competencia)

            if competencia == "suprema":
                # RIT, TipoRecurso, Caratulado, Fecha, Estado
                causa.rit         = textos[0] if n > 0 else ""
                causa.tipo_recurso = textos[1] if n > 1 else ""
                causa.caratulado  = textos[2] if n > 2 else ""
                causa.fecha_ingreso = textos[3] if n > 3 else ""
                causa.estado      = textos[4] if n > 4 else ""
                causa.tribunal    = "Corte Suprema"

            elif competencia == "apelaciones":
                # RIT, Tribunal, Caratulado, Fecha, Estado
                causa.rit         = textos[0] if n > 0 else ""
                causa.tribunal    = textos[1] if n > 1 else ""
                causa.caratulado  = textos[2] if n > 2 else ""
                causa.fecha_ingreso = textos[3] if n > 3 else ""
                causa.estado      = textos[4] if n > 4 else ""

            elif competencia == "penal":
                # RIT, Tribunal, RUC, Caratulado, Fecha, Estado
                causa.rit         = textos[0] if n > 0 else ""
                causa.tribunal    = textos[1] if n > 1 else ""
                causa.ruc         = textos[2] if n > 2 else ""
                causa.caratulado  = textos[3] if n > 3 else ""
                causa.fecha_ingreso = textos[4] if n > 4 else ""
                causa.estado      = textos[5] if n > 5 else ""

            else:
                # Civil, Laboral, Cobranza, Familia: RIT, Tribunal, Caratulado, Fecha, Estado
                causa.rit         = textos[0] if n > 0 else ""
                causa.tribunal    = textos[1] if n > 1 else ""
                causa.caratulado  = textos[2] if n > 2 else ""
                causa.fecha_ingreso = textos[3] if n > 3 else ""
                causa.estado      = textos[4] if n > 4 else ""

            causas.append(causa)
        return causas

    async def _ir_siguiente_pagina(
        self, tabla_selector: str, pagina_actual: int, competencia: str
    ) -> bool:
        """
        Intenta navegar a la siguiente página de resultados.
        Los enlaces de paginación usan funciones JS como paginaNat(2), paginaJur(2).
        """
        page = self._page

        # Determinar prefijo de función de paginación según competencia
        if competencia in ("civil", "cobranza", "familia"):
            fn_prefix = "paginaCivil"
        elif competencia == "laboral":
            fn_prefix = "paginaLaboral"
        elif competencia == "penal":
            fn_prefix = "paginaPenal"
        elif competencia in ("apelaciones",):
            fn_prefix = "paginaApelaciones"
        elif competencia == "suprema":
            fn_prefix = "paginaSuprema"
        else:
            fn_prefix = "pagina"

        siguiente = pagina_actual + 1

        # Buscar enlace de paginación por texto o por onclick
        selectors_pagina = [
            f"a:has-text('{siguiente}')",
            f"[onclick*='pagina'][onclick*='{siguiente}']",
            f"a.pagina[data-page='{siguiente}']",
            "a.siguiente",
            "li.next a",
        ]

        for sel in selectors_pagina:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    await page.wait_for_timeout(2000)
                    return True
            except Exception:
                continue

        # Como fallback, intentar llamar la función JS directamente
        try:
            existe = await page.evaluate(
                f"typeof {fn_prefix} === 'function' || typeof paginaNat === 'function'"
            )
            if existe:
                fn_real = await page.evaluate(
                    f"typeof {fn_prefix} !== 'undefined' ? '{fn_prefix}' : "
                    f"(typeof paginaNat !== 'undefined' ? 'paginaNat' : 'paginaJur')"
                )
                await page.evaluate(f"{fn_real}({siguiente})")
                await page.wait_for_timeout(2000)
                return True
        except Exception:
            pass

        return False

    # ------------------------------------------------------------------
    # Generadores async para streaming por competencia
    # ------------------------------------------------------------------

    async def stream_por_rut(
        self,
        rut: str,
        dv: str,
        competencias: list[str],
        years: Optional[list[int]],
        tipo: str,
    ):
        """Yield (competencia, year_o_None, [Causa]) a medida que completa cada búsqueda."""
        await self._iniciar_sesion()
        if years is None:
            for comp in competencias:
                causas = await self._buscar_rut_competencia(rut, dv, comp, None, tipo)
                yield comp, None, causas
        else:
            for year in years:
                for comp in competencias:
                    causas = await self._buscar_rut_competencia(rut, dv, comp, year, tipo)
                    yield comp, year, causas

    async def stream_por_nombre(
        self,
        nombre: str,
        apellido_paterno: str,
        apellido_materno: str,
        competencias: list[str],
    ):
        """Yield (competencia, None, [Causa]) a medida que completa cada búsqueda."""
        await self._iniciar_sesion()
        for comp in competencias:
            causas = await self._buscar_nombre_competencia(
                nombre, apellido_paterno, apellido_materno, comp
            )
            yield comp, None, causas

    # ------------------------------------------------------------------
    # API sincrónica de conveniencia
    # ------------------------------------------------------------------

    @staticmethod
    def buscar_rut_sync(
        rut: str,
        dv: str = "",
        competencias: Optional[list[str]] = None,
        year: Optional[int] = None,
        tipo: str = "natural",
        headless: bool = False,
    ) -> list[dict]:
        """Versión sincrónica de buscar_por_rut. Retorna lista de dicts."""
        async def _run():
            async with PJUDClient(headless=headless) as cliente:
                causas = await cliente.buscar_por_rut(
                    rut, dv, competencias=competencias, year=year, tipo=tipo
                )
                return [c.to_dict() for c in causas]
        return asyncio.run(_run())

    @staticmethod
    def buscar_nombre_sync(
        nombre: str,
        apellido_paterno: str = "",
        apellido_materno: str = "",
        competencias: Optional[list[str]] = None,
        headless: bool = False,
    ) -> list[dict]:
        """Versión sincrónica de buscar_por_nombre. Retorna lista de dicts."""
        async def _run():
            async with PJUDClient(headless=headless) as cliente:
                causas = await cliente.buscar_por_nombre(
                    nombre, apellido_paterno, apellido_materno,
                    competencias=competencias
                )
                return [c.to_dict() for c in causas]
        return asyncio.run(_run())
