from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import os
import sys
import time

URL = "https://www.enargas.gob.ar/secciones/gas-natural-comprimido/estadisticas.php"

TIPO_ESTADISTICA = "Prácticas informadas por Tipo de Operación"
PERIODO = os.getenv("ENARGAS_PERIODO", "2026")
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"

CUADROS = [
    "Conversiones de vehículos",
    "Desmontajes de equipos en vehículos",
    "Revisiones periódicas de vehículos",
    "Modificaciones de equipos en vehículos",
    "Revisiones de Cilindros",
    "Cilindro de GNC revisiones CRPC",
]

ARTIFACTS_DIR = Path("artifacts")
DOWNLOAD_DIR = ARTIFACTS_DIR / "descargas_enargas"
DEBUG_DIR = ARTIFACTS_DIR / "debug"

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

WINDOWS_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def log(msg: str) -> None:
    print(msg, flush=True)


def safe_name(texto: str) -> str:
    reemplazos = {
        " ": "_",
        "/": "-",
        "\\": "-",
        ":": "-",
        "á": "a",
        "é": "e",
        "í": "i",
        "ó": "o",
        "ú": "u",
        "ñ": "n",
    }
    for a, b in reemplazos.items():
        texto = texto.replace(a, b)
    return texto


def seleccionar_opcion(page, texto_label: str, opcion: str, indice_fallback: int) -> None:
    try:
        locator = page.get_by_label(texto_label)
        locator.select_option(label=opcion)
        locator.dispatch_event("change")
        locator.dispatch_event("blur")
        log(f"OK - Seleccionado '{opcion}' en '{texto_label}'")
        return
    except Exception:
        pass

    selects = page.locator("select")
    cantidad = selects.count()

    if cantidad <= indice_fallback:
        raise RuntimeError(
            f"No encontré el select esperado para '{texto_label}'. "
            f"Cantidad de <select> encontrados: {cantidad}"
        )

    locator = selects.nth(indice_fallback)
    locator.select_option(label=opcion)
    locator.dispatch_event("change")
    locator.dispatch_event("blur")
    log(f"OK - Seleccionado '{opcion}' en fallback índice {indice_fallback}")


def click_ver_xls(page) -> None:
    intentos = [
        lambda: page.get_by_role("button", name="Ver .xls").click(timeout=10000, delay=100),
        lambda: page.get_by_text("Ver .xls", exact=True).click(timeout=10000, delay=100),
        lambda: page.locator("text=Ver .xls").first.click(timeout=10000, delay=100),
    ]

    ultimo_error = None
    for intento in intentos:
        try:
            intento()
            log("OK - Click en 'Ver .xls'")
            return
        except Exception as e:
            ultimo_error = e

    raise RuntimeError(f"No pude hacer click en 'Ver .xls'. Error: {ultimo_error}")


def guardar_texto(path: Path, texto: str) -> None:
    path.write_text(texto, encoding="utf-8", errors="ignore")


def diagnosticar_popup_o_error(page, popup, cuadro: str) -> str:
    """
    Devuelve una descripción del error si encontró una página HTML de error.
    Si no encuentra un error claro, devuelve una descripción genérica.
    """
    nombre = safe_name(cuadro)

    objetivo = popup if popup is not None else page

    try:
        objetivo.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass

    try:
        screenshot_path = DEBUG_DIR / f"popup_{nombre}.png"
        objetivo.screenshot(path=str(screenshot_path), full_page=True)
    except Exception:
        screenshot_path = None

    try:
        html = objetivo.content()
        html_path = DEBUG_DIR / f"popup_{nombre}.html"
        guardar_texto(html_path, html)
    except Exception:
        html = ""
        html_path = None

    try:
        body_text = objetivo.locator("body").inner_text(timeout=5000)
    except Exception:
        body_text = ""

    try:
        url_actual = objetivo.url
    except Exception:
        url_actual = "(sin URL)"

    if "La solicitud no pudo ser procesada correctamente" in body_text:
        return f"ENARGAS devolvió página de error. URL: {url_actual}"

    if "Array to string conversion" in body_text:
        return f"ENARGAS devolvió warning PHP. URL: {url_actual}"

    if screenshot_path or html_path:
        return f"No hubo descarga. Se guardó evidencia de popup/página. URL: {url_actual}"

    return f"No hubo descarga y no pude extraer detalle. URL: {url_actual}"


def esperar_descarga_o_popup(context, page, cuadro: str, timeout_ms: int = 70000) -> Path:
    """
    Espera una descarga real. Si en lugar de eso aparece un popup o navegación con error HTML,
    guarda evidencia y levanta excepción.
    """
    descargas = []
    context.on("download", lambda d: descargas.append(d))

    paginas_antes = list(context.pages)
    ids_antes = {id(p) for p in paginas_antes}

    click_ver_xls(page)

    deadline = time.time() + (timeout_ms / 1000)

    while time.time() < deadline:
        # Caso 1: hubo descarga real
        if descargas:
            download = descargas[0]
            nombre_archivo = download.suggested_filename
            ruta_final = DOWNLOAD_DIR / nombre_archivo
            download.save_as(str(ruta_final))
            log(f"DESCARGADO: {ruta_final.resolve()}")
            return ruta_final

        # Caso 2: apareció popup nuevo
        popup = None
        for p in context.pages:
            if id(p) not in ids_antes:
                popup = p
                break

        if popup is not None:
            error = diagnosticar_popup_o_error(page, popup, cuadro)
            raise RuntimeError(error)

        # Caso 3: la misma página navegó a un HTML de error
        if "exportar-datos-operativos-gnc-xls-pdf" in page.url:
            error = diagnosticar_popup_o_error(page, None, cuadro)
            raise RuntimeError(error)

        page.wait_for_timeout(500)

    raise PlaywrightTimeoutError("No apareció descarga ni popup dentro del tiempo esperado")


def descargar_cuadro(page, context, cuadro: str) -> Path:
    log(f"\n--- Procesando: {cuadro} ---")

    page.goto(URL, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")

    seleccionar_opcion(page, "Tipo de estadistica", TIPO_ESTADISTICA, 0)
    page.wait_for_timeout(2000)

    seleccionar_opcion(page, "Cuadro", cuadro, 1)
    page.wait_for_timeout(2500)

    seleccionar_opcion(page, "Periodo", PERIODO, 2)
    page.wait_for_timeout(2500)

    # evidencia previa al click
    pre_name = safe_name(cuadro)
    page.screenshot(path=str(DEBUG_DIR / f"antes_click_{pre_name}.png"), full_page=True)

    return esperar_descarga_o_popup(context, page, cuadro, timeout_ms=70000)


def guardar_resumen(descargados, errores):
    resumen_path = ARTIFACTS_DIR / "resumen.txt"
    with resumen_path.open("w", encoding="utf-8") as f:
        f.write("RESUMEN ENARGAS\n")
        f.write("=" * 50 + "\n")
        f.write(f"Periodo: {PERIODO}\n")
        f.write(f"Descargados: {len(descargados)}\n")
        f.write(f"Errores: {len(errores)}\n\n")

        if descargados:
            f.write("Archivos descargados:\n")
            for cuadro, archivo in descargados:
                f.write(f"- {cuadro} -> {archivo.name}\n")

        if errores:
            f.write("\nErrores:\n")
            for cuadro, error in errores:
                f.write(f"- {cuadro} -> {error}\n")

    log(f"Resumen guardado en: {resumen_path.resolve()}")


def main():
    descargados = []
    errores = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        context = browser.new_context(
            accept_downloads=True,
            viewport={"width": 1400, "height": 900},
            locale="es-AR",
            timezone_id="America/Argentina/Buenos_Aires",
            user_agent=WINDOWS_CHROME_UA,
            extra_http_headers={
                "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
            },
        )

        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['es-AR', 'es', 'en-US', 'en'] });
            Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
        """)

        page = context.new_page()
        page.set_default_timeout(60000)

        try:
            for cuadro in CUADROS:
                try:
                    archivo = descargar_cuadro(page, context, cuadro)
                    descargados.append((cuadro, archivo))
                except Exception as e:
                    nombre = safe_name(cuadro)
                    try:
                        page.screenshot(path=str(DEBUG_DIR / f"error_{nombre}.png"), full_page=True)
                    except Exception:
                        pass

                    log(f"ERROR en '{cuadro}': {e}")
                    errores.append((cuadro, str(e)))

            guardar_resumen(descargados, errores)

            log("\n" + "=" * 60)
            log("RESUMEN FINAL")
            log("=" * 60)
            log(f"Descargados: {len(descargados)}")
            log(f"Errores: {len(errores)}")

            if descargados:
                log("\nArchivos descargados:")
                for cuadro, archivo in descargados:
                    log(f"- {cuadro} -> {archivo.name}")

            if errores:
                log("\nErrores:")
                for cuadro, error in errores:
                    log(f"- {cuadro} -> {error}")

        finally:
            browser.close()

    if len(descargados) == 0:
        sys.exit(1)

    if len(errores) > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
