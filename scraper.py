#!/usr/bin/env python3
"""
Bot de OPORTUNIDADES · 3S Superficies

Consulta la API OCDS oficial del OECE (ex-OSCE), filtra los procesos de
contratación pública que coinciden con las palabras clave de 3S, y los inserta
en la tabla `opportunities` de Supabase (vía la función upsert_opportunity).

Corre una vez al día (Railway Cron, 20:00 hora Perú). Idempotente: si un proceso
ya existe, actualiza sus datos pero NO pisa el estado que puso el comercial.

Fuente:
  https://contratacionesabiertas.oece.gob.pe/api/v1/releasesAfter?format=json&order=desc
  (paginación por el enlace links.next; order=desc = lo más nuevo primero)

Config por variables de entorno (ver .env.example).
"""

import os
import sys
import json
import time
import unicodedata
from datetime import datetime, timezone, timedelta

import requests

# ── Config ───────────────────────────────────────────────────────────────────
OECE_API_BASE = os.environ.get(
    "OECE_API_BASE",
    "https://contratacionesabiertas.oece.gob.pe/api/v1/releasesAfter?format=json&order=desc",
)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "2"))
MAX_PAGES = int(os.environ.get("MAX_PAGES", "40"))
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
KEYWORDS_FILE = os.environ.get("KEYWORDS_FILE", "keywords.txt")
SEACE_BUSCADOR = "https://prod2.seace.gob.pe/seacebus-uiwd-pub/buscadorPublico/buscadorPublico.xhtml"

# Muchos WAF del Estado cortan a 'python-requests'. Nos presentamos como navegador.
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": os.environ.get("USER_AGENT", DEFAULT_UA),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-PE,es;q=0.9,en;q=0.8",
    "Referer": "https://contratacionesabiertas.oece.gob.pe/",
}

CATEGORY_ES = {"goods": "Bienes", "services": "Servicios", "works": "Obras"}


# ── Utilidades ───────────────────────────────────────────────────────────────
def norm(text: str) -> str:
    """minúsculas y sin acentos, para comparar sin sorpresas."""
    if not text:
        return ""
    t = unicodedata.normalize("NFD", text)
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return t.lower()


def load_keywords(path: str):
    kws = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                k = line.strip()
                if k and not k.startswith("#"):
                    kws.append((k, norm(k)))
    except FileNotFoundError:
        env = os.environ.get("KEYWORDS", "")
        for k in env.split(","):
            k = k.strip()
            if k:
                kws.append((k, norm(k)))
    if not kws:
        print("ERROR: no hay palabras clave (keywords.txt ni KEYWORDS).", file=sys.stderr)
        sys.exit(2)
    return kws


def first_match(haystack_norm: str, keywords):
    for original, kn in keywords:
        if kn and kn in haystack_norm:
            return original
    return None


def g(d, *path, default=None):
    """get anidado y seguro: g(rel, 'tender', 'value', 'amount')."""
    cur = d
    for key in path:
        if isinstance(cur, dict) and key in cur and cur[key] is not None:
            cur = cur[key]
        else:
            return default
    return cur


def region_of(release):
    for party in release.get("parties", []) or []:
        roles = party.get("roles", []) or []
        if "buyer" in roles or "procuringEntity" in roles:
            region = g(party, "address", "region")
            if region:
                return region
    # fallback: cualquier parte con región
    for party in release.get("parties", []) or []:
        region = g(party, "address", "region")
        if region:
            return region
    return None


def process_url(release):
    # Mejor esfuerzo: algún documento del tender con URL; si no, el buscador SEACE.
    for doc in g(release, "tender", "documents", default=[]) or []:
        if doc.get("url"):
            return doc["url"]
    return SEACE_BUSCADOR


def parse_date(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ── Supabase ─────────────────────────────────────────────────────────────────
def upsert(opp: dict):
    if DRY_RUN:
        print("   [dry-run] upsert:", opp["p_external_id"], "·", opp["p_title"][:70])
        return True
    url = f"{SUPABASE_URL}/rest/v1/rpc/upsert_opportunity"
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    r = requests.post(url, headers=headers, json=opp, timeout=30)
    if r.status_code >= 300:
        print(f"   ! upsert falló ({r.status_code}): {r.text[:200]}", file=sys.stderr)
        return False
    return True


# ── Extracción de una release OCDS → fila de oportunidad ─────────────────────
def build_opportunity(release, matched_keyword):
    tender = release.get("tender", {}) or {}
    category = g(release, "tender", "mainProcurementCategory")
    method = g(release, "tender", "procurementMethodDetails") or g(release, "tender", "procurementMethod")
    pub = parse_date(g(release, "tender", "tenderPeriod", "startDate")) or parse_date(release.get("date"))
    end = parse_date(g(release, "tender", "tenderPeriod", "endDate"))
    return {
        "p_source": "OCDS",
        "p_external_id": release.get("ocid") or release.get("id"),
        "p_title": tender.get("title") or "(sin título)",
        "p_proceso_code": tender.get("id") or g(release, "tender", "procuringEntity", "id"),
        "p_entity_name": g(release, "buyer", "name") or g(release, "tender", "procuringEntity", "name"),
        "p_description": tender.get("description"),
        "p_procurement_method": method,
        "p_category": CATEGORY_ES.get(category, category),
        "p_value_amount": g(release, "tender", "value", "amount"),
        "p_value_currency": g(release, "tender", "value", "currency", default="PEN"),
        "p_department": region_of(release),
        "p_published_date": pub.date().isoformat() if pub else None,
        "p_submission_end": end.isoformat() if end else None,
        "p_url": process_url(release),
        "p_matched_keyword": matched_keyword,
    }


def haystack(release):
    tender = release.get("tender", {}) or {}
    parts = [tender.get("title", ""), tender.get("description", "")]
    for item in tender.get("items", []) or []:
        parts.append(item.get("description", "") or "")
        parts.append(g(item, "classification", "description", default="") or "")
    return norm(" ".join(parts))


def extract_releases(package):
    """La API puede devolver los datos con distintas formas. Las cubrimos todas."""
    if isinstance(package, list):
        out = []
        for el in package:
            out.extend(extract_releases(el))
        return out
    if not isinstance(package, dict):
        return []
    if isinstance(package.get("releases"), list):
        return package["releases"]
    if isinstance(package.get("records"), list):
        out = []
        for rec in package["records"]:
            cr = rec.get("compiledRelease")
            if isinstance(cr, dict):
                out.append(cr)
            elif isinstance(rec.get("releases"), list):
                out.extend(rec["releases"])
        return out
    for k in ("data", "results", "items"):
        if isinstance(package.get(k), list):
            return package[k]
    return []


def next_link(package):
    if isinstance(package, dict):
        links = package.get("links")
        if isinstance(links, dict) and links.get("next"):
            return links["next"]
        if package.get("next"):
            return package["next"]
    return None


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    keywords = load_keywords(KEYWORDS_FILE)
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    print(f"== Bot Oportunidades · {datetime.now(timezone.utc).isoformat()} ==")
    print(f"   keywords: {len(keywords)} · lookback: {LOOKBACK_DAYS}d · dry_run: {DRY_RUN}")

    if not DRY_RUN and (not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY):
        print("ERROR: faltan SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY.", file=sys.stderr)
        sys.exit(2)

    url = OECE_API_BASE
    seen, matched, saved, pages = 0, 0, 0, 0

    while url and pages < MAX_PAGES:
        pages += 1
        try:
            resp = requests.get(url, timeout=60, headers=HEADERS)
            resp.raise_for_status()
            package = resp.json()
        except Exception as e:  # noqa: BLE001
            print(f"   ! error al bajar página {pages}: {e}", file=sys.stderr)
            break

        releases = extract_releases(package)

        # Diagnóstico de la primera página: nos dice qué devolvió la API de verdad.
        if pages == 1:
            if isinstance(package, dict):
                shape = "dict keys=[" + ", ".join(list(package.keys())[:12]) + "]"
            elif isinstance(package, list):
                shape = f"list len={len(package)}"
            else:
                shape = type(package).__name__
            ct = resp.headers.get("content-type", "")
            print(f"   [diag] status={resp.status_code} ct={ct} · {shape} · releases={len(releases)}")
            if not releases:
                snippet = resp.text[:1200].replace("\n", " ").replace("\r", " ")
                print(f"   [diag] cuerpo (primeros 1200 car.): {snippet}")

        if not releases:
            break

        stop = False
        for rel in releases:
            seen += 1
            rel_date = parse_date(rel.get("date")) or parse_date(g(rel, "tender", "tenderPeriod", "startDate"))
            # order=desc: cuando pasamos el corte, dejamos de paginar.
            if rel_date and rel_date < cutoff:
                stop = True
                continue
            kw = first_match(haystack(rel), keywords)
            if not kw:
                continue
            matched += 1
            opp = build_opportunity(rel, kw)
            if not opp["p_external_id"]:
                continue
            if upsert(opp):
                saved += 1

        if stop:
            break
        url = next_link(package)
        time.sleep(0.5)  # cortesía con el servidor

    print(f"== Fin. páginas:{pages} vistas:{seen} coinciden:{matched} guardadas:{saved} ==")


if __name__ == "__main__":
    main()
