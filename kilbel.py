#!/usr/bin/env python3
"""
Recolector de precios de Kilbel Online.

Uso normal (descarga las páginas listadas en urls_kilbel.txt):
    python kilbel.py

Modo prueba con un HTML guardado en tu computadora:
    python kilbel.py --html-file muestra.html

Salida (carpeta ./datos):
    - kilbel_ultimo.json       -> foto actual de precios
    - precios_historial.csv    -> se agrega una fila por producto en cada corrida
"""
import argparse
import csv
import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

SUPER = "Kilbel"
BASE = "https://www.kilbelonline.com"
USER_AGENT = "ComparadorPreciosPersonal/0.1 (proyecto personal, pocas consultas)"
PAUSA_SEGUNDOS = 3  # espera entre páginas, para no molestar al sitio

# Kilbel autorizó por escrito (respuesta de la gerencia de la sucursal Urquiza 3327)
# la consulta automatizada de su tienda online para uso personal, sin fines comerciales,
# con pocas consultas diarias y mencionando la fuente. Con esa autorización se omite la
# revisión del robots.txt. Mantener: 1 corrida por día, pocas categorías y la cita de la fuente.
AUTORIZACION_KILBEL = True

ART_RE = re.compile(r"/art_(\d+)/?")
PRECIO_RE = re.compile(r"\$\s*([\d.,]+)")
UNIDAD_RE = re.compile(r"Precio x\s+([^:\n]+):\s*\$\s*([\d.,]+)", re.I)
SIN_IMP_RE = re.compile(r"Prec\.s/Imp\.Nac\.:\s*\$\s*([\d.,]+)", re.I)
PROMO_RE = re.compile(r"Llevando\s+(\d+)", re.I)

CAMPOS = [
    "fecha", "super", "id", "nombre", "precio_lista", "precio_actual",
    "unidad_ref", "precio_unidad", "precio_sin_imp", "promo_llevando",
    "sin_stock", "url",
]


def a_numero(texto):
    """Convierte '1.020,00' o '2000.00' (formatos que usa el sitio) en float."""
    t = texto.strip().rstrip(".,")
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d+\.\d{2}", t):
        pass  # ya viene como decimal con punto ("Antes $2000.00")
    else:
        t = t.replace(".", "")
    return float(t)


def buscar_tarjeta(enlace):
    """Sube por el HTML hasta el contenedor más grande que tenga UN solo producto."""
    nodo = enlace
    while nodo.parent is not None and nodo.parent.name not in ("body", "html", "[document]"):
        padre = nodo.parent
        ids = set()
        for a in padre.find_all("a", href=True):
            m = ART_RE.search(a["href"])
            if m:
                ids.add(m.group(1))
        if len(ids) > 1:
            break
        nodo = padre
    return nodo


def parsear_pagina(html, url_base=BASE):
    """Devuelve una lista de productos (dicts) encontrados en el HTML."""
    sopa = BeautifulSoup(html, "html.parser")
    productos = {}
    for enlace in sopa.find_all("a", href=True):
        m = ART_RE.search(enlace["href"])
        if not m:
            continue
        pid = m.group(1)
        if pid in productos:
            continue
        tarjeta = buscar_tarjeta(enlace)
        texto = tarjeta.get_text("\n", strip=True)

        # Nombre: primer enlace del producto con texto
        nombre = ""
        for a in tarjeta.find_all("a", href=True):
            if ART_RE.search(a["href"]) and a.get_text(strip=True):
                nombre = a.get_text(strip=True)
                break
        if not nombre:
            continue

        # Precios: todo lo que aparece antes de "Precio x ..." (lista y actual)
        unidad = UNIDAD_RE.search(texto)
        zona_precios = texto[: unidad.start()] if unidad else texto.split("Prec.s/Imp")[0]
        precios = [a_numero(p) for p in PRECIO_RE.findall(zona_precios)]
        if not precios:
            continue

        sin_imp = SIN_IMP_RE.search(texto)
        promo = PROMO_RE.search(texto)
        productos[pid] = {
            "id": pid,
            "nombre": nombre,
            "precio_lista": precios[0],
            "precio_actual": precios[-1],
            "unidad_ref": unidad.group(1).strip() if unidad else "",
            "precio_unidad": a_numero(unidad.group(2)) if unidad else None,
            "precio_sin_imp": a_numero(sin_imp.group(1)) if sin_imp else None,
            "promo_llevando": int(promo.group(1)) if promo else None,
            "sin_stock": "sin stock" in texto.lower(),
            "url": urljoin(url_base, enlace["href"]),
        }
    return list(productos.values())


def permitido_por_robots(url):
    rp = RobotFileParser()
    try:
        resp = requests.get(urljoin(BASE, "/robots.txt"), headers={"User-Agent": USER_AGENT}, timeout=15)
        if resp.status_code != 200:
            return True  # sin robots.txt, no hay restricciones declaradas
        rp.parse(resp.text.splitlines())
        return rp.can_fetch(USER_AGENT, url)
    except requests.RequestException:
        return True


def descargar(url):
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return resp.text


def guardar(productos, carpeta):
    carpeta.mkdir(parents=True, exist_ok=True)
    fecha = datetime.now().isoformat(timespec="seconds")
    filas = [{"fecha": fecha, "super": SUPER, **p} for p in productos]

    with open(carpeta / "kilbel_ultimo.json", "w", encoding="utf-8") as f:
        json.dump(filas, f, ensure_ascii=False, indent=2)

    historial = carpeta / "precios_historial.csv"
    nuevo = not historial.exists()
    with open(historial, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS)
        if nuevo:
            w.writeheader()
        w.writerows(filas)
    return len(filas)


def main():
    ap = argparse.ArgumentParser(description="Recolector de precios de Kilbel Online")
    ap.add_argument("--urls", default="urls_kilbel.txt", help="archivo con una URL por línea")
    ap.add_argument("--html-file", help="analizar un HTML guardado en vez de descargar")
    ap.add_argument("--out", default="datos", help="carpeta de salida")
    args = ap.parse_args()

    productos = {}
    if args.html_file:
        html = Path(args.html_file).read_text(encoding="utf-8")
        for p in parsear_pagina(html):
            productos[p["id"]] = p
    else:
        archivo = Path(args.urls)
        urls = [l.strip() for l in archivo.read_text(encoding="utf-8").splitlines()
                if l.strip() and not l.startswith("#")] if archivo.exists() else [BASE + "/"]
        for i, url in enumerate(urls):
            if not AUTORIZACION_KILBEL and not permitido_por_robots(url):
                print(f"[omitida por robots.txt] {url}")
                continue
            try:
                encontrados = parsear_pagina(descargar(url))
            except requests.RequestException as e:
                print(f"[error] {url}: {e}")
                continue
            print(f"{url} -> {len(encontrados)} productos")
            for p in encontrados:
                productos[p["id"]] = p
            if i < len(urls) - 1:
                time.sleep(PAUSA_SEGUNDOS)

    if not productos:
        print("No se encontraron productos. Puede que la estructura de la página sea distinta.")
        return
    n = guardar(list(productos.values()), Path(args.out))
    print(f"Listo: {n} productos guardados en '{args.out}/'")


if __name__ == "__main__":
    main()
