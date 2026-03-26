from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import os
import sys

URL = "https://www.enargas.gob.ar/secciones/gas-natural-comprimido/estadisticas.php"

TIPO_ESTADISTICA = "Prácticas informadas por Tipo de Operación"
PERIODO = os.getenv("ENARGAS_PERIODO", "2026")

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


def log(msg: str) -> None:
    print(msg, flush=True)


def seleccionar_opcion(page, texto_label: str, opcion: str, indice_fallback: int) -> None:
    try:
        page.get_by_label(texto_label).select_option(label=opcion)
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

    selects.nth(indice_fallback).select_option(label=opcion)
    log(f"OK - Seleccionado '{opcion}' en fallback índice {indice_fallback}")


def click_ver_xls(page) -> None:
    intentos = [
        lambda: page.get_by_role("button", name="Ver .xls").click(),
        lambda: page.get_by_text("Ver .xls", exact=True).click(),
        lambda: page.locator("text=Ver .xls").first.click(),
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


def nombre_seguro(texto: str) -> str:
    return (
        texto.replace(" ", "_")
        .replace("/", "-")
        .replace("\\", "-")
        .replace(":", "-")
    )


def descargar_cuadro(page, cuadro: str) -> Path:
    log(f"\n--- Procesando: {cuadro} ---")

    page.goto(URL, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")

    seleccionar_opcion(page, "Tipo de estadistica", TIPO_ESTADISTICA, 0)
    page.wait_for_timeout(1200)

    seleccionar_opcion(page, "Cuadro", cuadro, 1)
    page.wait_for_timeout(1200)

    seleccionar_opcion(page, "Periodo", PERIODO, 2)
    page.wait_for_timeout(1500)

    with page.expect_download(timeout=60000) as download_info:
        click_ver_xls(page)

    download = download_info.value
    nombre_archivo = download.suggested_filename
    ruta_final = DOWNLOAD_DIR / nombre_archivo

    download.save_as(str(ruta_final))
    log(f"DESCARGADO: {ruta_final.resolve()}")

    return ruta_final


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
            headless=True
        )

        context = browser.new_context(
            accept_downloads=True,
            viewport={"width": 1400, "height": 900},
            locale="es-AR"
        )

        page = context.new_page()
        page.set_default_timeout(60000)

        try:
            for cuadro in CUADROS:
                try:
                    archivo = descargar_cuadro(page, cuadro)
                    descargados.append((cuadro, archivo))
                except PlaywrightTimeoutError:
                    screenshot = DEBUG_DIR / f"timeout_{nombre_seguro(cuadro)}.png"
                    page.screenshot(path=str(screenshot), full_page=True)
                    log(f"TIMEOUT en '{cuadro}'. Captura: {screenshot.resolve()}")
                    errores.append((cuadro, "Timeout"))
                except Exception as e:
                    screenshot = DEBUG_DIR / f"error_{nombre_seguro(cuadro)}.png"
                    page.screenshot(path=str(screenshot), full_page=True)
                    log(f"ERROR en '{cuadro}': {e}")
                    log(f"Captura: {screenshot.resolve()}")
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

    # hace fallar el job si no descargó nada o si hubo errores
    if len(descargados) == 0:
        log("No se descargó ningún archivo.")
        sys.exit(1)

    if len(errores) > 0:
        log("Hubo errores en uno o más cuadros.")
        sys.exit(1)


if __name__ == "__main__":
    main()
