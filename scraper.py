#!/usr/bin/env python3
"""
Scraper za stanove u Beogradu
Prati oglase na Halo Oglasi, 4zida.rs i City Expert.
Šalje Telegram notifikaciju kad nađe stan u željenim lokacijama ispod zadate cene/m².
"""

import requests
import json
import os
import re
import time
import logging
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from bs4 import BeautifulSoup

# ============================================================
# SETUP
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / 'config.json'
SEEN_FILE = BASE_DIR / 'seen.json'

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept-Language': 'sr-RS,sr;q=0.9,en;q=0.8',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
}

# SSL verify: lokalno isključen (antivirus/proxy intercept HTTPS-a),
# na GitHub Actions (CI=true) uključen — tamo nema proxy-ja i SSL radi normalno
SSL_VERIFY = os.environ.get('CI') == 'true'

# Zaobiđi lokalni proxy (antivirus/korporativni) koji blokira HTTPS tunel
# Ovo ne utiče na GitHub Actions gde nema proxy-ja
os.environ['NO_PROXY'] = '*'
os.environ['no_proxy'] = '*'

# ============================================================
# CONFIG & STATE
# ============================================================

def load_config():
    """Učitaj konfiguraciju iz config.json ili env varijabli (GitHub Actions)."""
    config = {
        'telegram_token': os.environ.get('TELEGRAM_TOKEN', ''),
        'telegram_chat_id': os.environ.get('TELEGRAM_CHAT_ID', ''),
        'max_price_per_m2': int(os.environ.get('MAX_PRICE_PER_M2', 1500)),
        'target_locations': ['Novi Beograd', 'Zemun', 'Ledine', 'Bezanija'],
    }
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, encoding='utf-8') as f:
            file_config = json.load(f)
        config.update(file_config)
        # Env varijable imaju prednost nad config.json
        if os.environ.get('TELEGRAM_TOKEN'):
            config['telegram_token'] = os.environ['TELEGRAM_TOKEN']
        if os.environ.get('TELEGRAM_CHAT_ID'):
            config['telegram_chat_id'] = os.environ['TELEGRAM_CHAT_ID']
        if os.environ.get('TELEGRAM_EXTRA_CHAT_IDS'):
            # Može biti više ID-ova razdvojenih zarezom: "123,456"
            extra = [x.strip() for x in os.environ['TELEGRAM_EXTRA_CHAT_IDS'].split(',') if x.strip()]
            config['telegram_extra_chat_ids'] = extra
    return config

# Koliko dana čuvamo ID u seen.json otkad je POSLEDNJI PUT viđen u feedu.
# Timestamp se osvežava svaki run dok je oglas živ, pa se brišu samo
# oglasi koji su skinuti sa sajta pre više od N dana.
SEEN_MAX_AGE_DAYS = 30

def load_seen():
    """seen.json format: {"listing_id": unix_timestamp}. Stari format (lista) se migrira."""
    if SEEN_FILE.exists():
        try:
            with open(SEEN_FILE, encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            return {}
        if isinstance(data, list):  # migracija starog formata
            now = time.time()
            return {str(lid): now for lid in data}
        return {str(k): float(v) for k, v in data.items()}
    return {}

def save_seen(seen):
    cutoff = time.time() - SEEN_MAX_AGE_DAYS * 86400
    pruned = {k: v for k, v in seen.items() if v >= cutoff}
    removed = len(seen) - len(pruned)
    if removed:
        logger.info(f"🧹 seen.json: obrisano {removed} unosa starijih od {SEEN_MAX_AGE_DAYS} dana")
    with open(SEEN_FILE, 'w', encoding='utf-8') as f:
        json.dump(pruned, f, indent=2, sort_keys=True)

# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(token, chat_id, message, retries=3):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {
        'chat_id': chat_id,
        'text': message,
        'parse_mode': 'HTML',
        'disable_web_page_preview': False,
    }
    for attempt in range(1, retries + 1):
        try:
            r = requests.post(url, data=data, timeout=10, verify=SSL_VERIFY,
                              proxies={'http': '', 'https': ''})
            r.raise_for_status()
            logger.info("✅ Telegram poruka poslata")
            return True
        except Exception as e:
            logger.error(f"❌ Greška pri slanju Telegram poruke (pokušaj {attempt}/{retries}): {e}")
            if attempt < retries:
                time.sleep(3 * attempt)
    return False

def format_message(listing):
    emoji_source = {
        'Halo Oglasi': '🟡',
        '4zida.rs': '🟢',
        'City Expert': '🔴',
        'Halo Zemljište': '🌳',
    }
    icon = emoji_source.get(listing.get('source', ''), '🏠')

    # Zemljišta: 'area' su ARI, 'price_per_m2' je cena PO ARU
    is_land = listing.get('source') == 'Halo Zemljište'
    area_unit = 'ari' if is_land else 'm²'
    ppm_label = 'Cena/ar' if is_land else 'Cena/m²'
    ppm_unit = '€/ar' if is_land else '€/m²'

    lines = [
        f"{icon} <b>{listing.get('title', 'Stan na prodaju')}</b>",
        f"📍 {listing.get('location', 'N/A')}",
    ]
    if listing.get('price'):
        lines.append(f"💶 Cena: <b>{listing['price']:,.0f} €</b>".replace(',', '.'))
    if listing.get('area'):
        lines.append(f"📐 Površina: <b>{listing['area']} {area_unit}</b>")
    if listing.get('price_per_m2'):
        lines.append(f"📊 {ppm_label}: <b>{listing['price_per_m2']:,.0f} {ppm_unit}</b>".replace(',', '.'))
    if listing.get('rooms'):
        lines.append(f"🚪 Sobnost: {listing['rooms']}")
    lines.append(f"🔗 {listing.get('url', '')}")
    lines.append(f"🕐 {listing.get('source', '')} | {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    return '\n'.join(lines)

# ============================================================
# HELPERS
# ============================================================

def parse_price(text):
    if not text:
        return None
    text = text.strip()
    cleaned = re.sub(r'[^\d.,]', '', text)
    if '.' in cleaned and ',' in cleaned:
        cleaned = cleaned.replace('.', '').replace(',', '.')
    elif cleaned.count('.') == 1 and len(cleaned.split('.')[-1]) == 2:
        pass
    else:
        cleaned = cleaned.replace('.', '').replace(',', '.')
    try:
        val = float(cleaned)
        if 10_000 <= val <= 10_000_000:
            return val
        return None
    except ValueError:
        return None

def parse_area(text):
    if not text:
        return None
    m = re.search(r'(\d+[\.,]?\d*)\s*m[²2]', text, re.IGNORECASE)
    if m:
        try:
            val = float(m.group(1).replace(',', '.'))
            if 10 <= val <= 1000:
                return val
        except ValueError:
            pass
    return None

def calc_ppm2(price, area):
    if price and area and area > 0:
        return round(price / area, 0)
    return None

_DIACRITICS_MAP = str.maketrans('čćšžđ', 'ccszd')

def normalize_text(text):
    """Lowercase + skini srpske dijakritike, da 'Bežanija' matchuje 'Bezanija' i obrnuto.
    'dj' → 'd' izjednačava dva zapisa slova đ (Đurđevo == Djurdjevo)."""
    return text.lower().translate(_DIACRITICS_MAP).replace('dj', 'd')

def is_target_location(location_text, targets):
    loc = normalize_text(location_text)
    return any(normalize_text(t) in loc for t in targets)

def is_good_price(ppm2, max_ppm2):
    return ppm2 is not None and ppm2 <= max_ppm2

def fetch_json(url, extra_headers=None):
    """Fetch JSON bez URL re-encodinga i bez proxy-ja (fix za lokalni antivirus intercept)."""
    import ssl
    headers = {**HEADERS, 'Accept': 'application/json'}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    # SSL context: certifikati se ignorišu samo lokalno (SSL_VERIFY=False)
    ctx = ssl.create_default_context()
    if not SSL_VERIFY:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    # Eksplicitno zaobiđi proxy — ProxyHandler({}) = bez proxy-ja
    proxy_handler = urllib.request.ProxyHandler({})
    https_handler = urllib.request.HTTPSHandler(context=ctx)
    opener = urllib.request.build_opener(proxy_handler, https_handler)
    try:
        with opener.open(req, timeout=20) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        body = ''
        try:
            body = e.read().decode('utf-8')
        except Exception:
            pass
        raise Exception(f"fetch_json greška: HTTP {e.code} {e.reason} | body: {body[:500]}")
    except Exception as e:
        raise Exception(f"fetch_json greška: {e}")

# ============================================================
# SCRAPER: HALO OGLASI
# ============================================================

# Jedan URL za sve lokacije: Novi Beograd, Zemun, Ledine, Bezanija
HALO_URL = (
    "https://www.halooglasi.com/nekretnine/prodaja-stanova"
    "?grad_id_l-lokacija_id_l-mikrolokacija_id_l=40574,40787,535592,55297,538989,525206,525208,525211,40776,40772,40784"
    "&sort=ValidFromMoment_desc"
)

def scrape_halooglasi(config):
    results = []
    session = requests.Session()
    session.trust_env = False
    session.headers.update({
        **HEADERS,
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
    })

    logger.info(f"[Halo Oglasi] {HALO_URL}")
    try:
        r = session.get(HALO_URL, timeout=20, verify=SSL_VERIFY)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')

        items = soup.select('.product-item')
        logger.info(f"[Halo Oglasi] sve lokacije: {len(items)} oglasa")

        for item in items:
            try:
                link = item.select_one('h3.product-title a, .product-title a, a.ga-title')
                if not link:
                    link = item.select_one('a[href*="/prodaja-stanova/"]')
                if not link:
                    continue
                href = link.get('href', '')
                raw_id = href.rstrip('/').split('/')[-1].split('?')[0]
                listing_id = f"halo_{raw_id}"
                title = link.get_text(strip=True) or "Stan na prodaju"

                price = None
                # '.central-feature' je trenutna klasa za ukupnu cenu na halooglasi.com
                # (19.7.2026: sajt promenio markup, stari '.price-box-main' vise ne pogadja ništa).
                # Stari selektori ostaju kao fallback ako se sajt opet promeni.
                price_el = item.select_one('.central-feature, .price-box-main, [class*="price-main"]')
                if price_el:
                    price = parse_price(price_el.get_text())

                area = None
                for feat in item.select('.product-features li, .features-container li'):
                    a = parse_area(feat.get_text(strip=True))
                    if a:
                        area = a
                        break
                if not area:
                    area = parse_area(item.get_text())

                ppm2 = calc_ppm2(price, area)
                full_url = href if href.startswith('http') else f"https://www.halooglasi.com{href}"

                # Pokušaj da izvučeš lokaciju iz teksta oglasa
                location_str = 'Novi Beograd / Zemun'
                loc_el = item.select_one('.subtitle-places, [class*="subtitle"]')
                if loc_el:
                    location_str = loc_el.get_text(strip=True)

                results.append({
                    'id': listing_id,
                    'title': title,
                    'location': location_str,
                    'price': price,
                    'area': area,
                    'price_per_m2': ppm2,
                    'url': full_url,
                    'source': 'Halo Oglasi',
                })
            except Exception as e:
                logger.debug(f"[Halo Oglasi] oglas greška: {e}")

    except Exception as e:
        logger.error(f"[Halo Oglasi] greška: {e}")

    return results

# ============================================================
# SCRAPER: HALO OGLASI — ZEMLJIŠTA
# ============================================================

HALO_ZEMLJISTE_URL = (
    "https://www.halooglasi.com/nekretnine/prodaja-zemljista"
    "?grad_id_l-lokacija_id_l-mikrolokacija_id_l=525206,525208,525211,55297,538989"
    "&sort=ValidFromMoment_desc"
)

def scrape_halooglasi_zemljiste(config):
    """Scrape zemljišta na prodaju u Surčinu, Jakovu, Bečmenu i Ledinama."""
    results = []
    max_total = config.get('max_total_price_zemljiste', 60000)
    max_per_ar = config.get('max_price_per_ar', 7000)

    session = requests.Session()
    session.trust_env = False
    session.headers.update({
        **HEADERS,
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
    })

    logger.info(f"[Halo Zemljište] {HALO_ZEMLJISTE_URL}")
    try:
        r = session.get(HALO_ZEMLJISTE_URL, timeout=20, verify=SSL_VERIFY)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')

        items = soup.select('.product-item')
        logger.info(f"[Halo Zemljište] {len(items)} oglasa")

        for item in items:
            try:
                link = item.select_one('h3.product-title a, .product-title a, a.ga-title')
                if not link:
                    link = item.select_one('a[href*="/prodaja-zemljista/"]')
                if not link:
                    continue
                href = link.get('href', '')
                raw_id = href.rstrip('/').split('/')[-1].split('?')[0]
                listing_id = f"halo_z_{raw_id}"
                title = link.get_text(strip=True) or "Zemljište na prodaju"

                price = None
                # '.central-feature' je trenutna klasa za ukupnu cenu na halooglasi.com
                # (19.7.2026: sajt promenio markup, stari '.price-box-main' vise ne pogadja ništa).
                # Stari selektori ostaju kao fallback ako se sajt opet promeni.
                price_el = item.select_one('.central-feature, .price-box-main, [class*="price-main"]')
                if price_el:
                    price = parse_price(price_el.get_text())

                # Površina u arima — tražimo "ar" ili "m²" u features
                area_ar = None
                area_m2 = None
                for feat in item.select('.product-features li, .features-container li'):
                    txt = feat.get_text(strip=True)
                    # Pokušaj ar
                    m = re.search(r'(\d+[\.,]?\d*)\s*ar', txt, re.IGNORECASE)
                    if m:
                        try:
                            area_ar = float(m.group(1).replace(',', '.'))
                        except ValueError:
                            pass
                    # Pokušaj m²
                    a = parse_area(txt)
                    if a:
                        area_m2 = a

                # Konvertuj m² u are ako nemamo direktno u arima
                if area_ar is None and area_m2:
                    area_ar = round(area_m2 / 100, 2)

                # Cena po aru
                price_per_ar = None
                if price and area_ar and area_ar > 0:
                    price_per_ar = round(price / area_ar, 0)

                full_url = href if href.startswith('http') else f"https://www.halooglasi.com{href}"

                location_str = 'Surcin / Ledine'
                loc_el = item.select_one('.subtitle-places, [class*="subtitle"]')
                if loc_el:
                    location_str = loc_el.get_text(strip=True)

                # Filter: ukupna cena i cena po aru
                if price and price > max_total:
                    continue
                if price_per_ar and price_per_ar > max_per_ar:
                    continue

                results.append({
                    'id': listing_id,
                    'title': title,
                    'location': location_str,
                    'price': price,
                    'area': area_ar,
                    'price_per_m2': price_per_ar,  # ovde cuvamo cenu/ar
                    'url': full_url,
                    'source': 'Halo Zemljište',
                    'rooms': f"{area_ar} ari" if area_ar else '',
                })
            except Exception as e:
                logger.debug(f"[Halo Zemljište] oglas greška: {e}")

    except Exception as e:
        logger.error(f"[Halo Zemljište] greška: {e}")

    return results

# ============================================================
# SCRAPER: 4ZIDA.RS (JSON API) — fix: urllib da ne enkodira []
# ============================================================

def scrape_4zida(config):
    """
    Dohvati stanove na prodaju iz 4zida.rs API-ja.
    API ne prihvata filtere (vraća 422) — koristimo ?limit=60&page=N
    i filtriramo u Pythonu po: for==sale, placeNames, pricePerM2.
    """
    results = []
    targets = config.get('target_locations', ['Novi Beograd', 'Zemun', 'Ledine', 'Bezanija'])

    # Probaj različite URL formate — API menja šta prima
    url_candidates = [
        "https://api.4zida.rs/v6/search/apartments",
        "https://api.4zida.rs/v6/search/apartments?for=sale",
        "https://api.4zida.rs/v5/search/apartments",
    ]

    working_url = None
    first_page_data = None  # čuvamo odgovor iz probe da ne tražimo stranu 1 dva puta
    for candidate in url_candidates:
        logger.info(f"[4zida.rs] Testiram URL: {candidate}")
        try:
            test_data = fetch_json(candidate, extra_headers={
                'Accept': 'application/json, text/plain, */*',
                'Origin': 'https://4zida.rs',
                'Referer': 'https://4zida.rs/',
            })
            if isinstance(test_data, dict) or isinstance(test_data, list):
                working_url = candidate
                first_page_data = test_data
                logger.info(f"[4zida.rs] Radi URL: {candidate}")
                break
        except Exception as e:
            logger.warning(f"[4zida.rs] Ne radi {candidate}: {e}")

    if not working_url:
        logger.error("[4zida.rs] Nijedan URL ne radi!")
        return results

    for page in range(1, 16):  # max 15 strana = 300 oglasa
        # Dodaj paginaciju samo ako base URL radi
        if page == 1:
            api_url = working_url
        else:
            sep = '&' if '?' in working_url else '?'
            api_url = f"{working_url}{sep}page={page}"
        logger.info(f"[4zida.rs] strana {page}: {api_url}")
        try:
            if page == 1 and first_page_data is not None:
                data = first_page_data  # reuse iz probe, bez dodatnog HTTP zahteva
            else:
                data = fetch_json(api_url, extra_headers={
                    'Accept': 'application/json, text/plain, */*',
                    'Origin': 'https://4zida.rs',
                    'Referer': 'https://4zida.rs/',
                })
            ads = data.get('ads', [])
            if not ads:
                logger.info(f"[4zida.rs] strana {page}: nema više oglasa, stajemo")
                break

            logger.info(f"[4zida.rs] strana {page}: {len(ads)} oglasa")

            for ad in ads:
                try:
                    # Samo prodaja
                    if ad.get('for') != 'sale':
                        continue

                    # Lokacija — placeNames je array npr. ["Ledine", "Novi Beograd", "Beograd"]
                    place_names = ad.get('placeNames', [])
                    location_str = ', '.join(place_names) if place_names else 'Beograd'

                    # Proveri da li je u traženim lokacijama.
                    # VAŽNO: koristi is_target_location() (normalizuje dijakritike),
                    # ne sirovi .lower() — inače 'Bežanija' iz API-ja ne matchuje
                    # 'Bezanija' iz config-a i oglas tiho nestane pre main() filtera.
                    if not is_target_location(location_str, targets):
                        continue

                    ad_id = str(ad.get('id', ''))
                    listing_id = f"4zida_{ad_id}"

                    price = ad.get('price')
                    area = ad.get('m2')

                    # API već računa pricePerM2 — koristimo direktno
                    ppm2 = ad.get('pricePerM2') or calc_ppm2(price, area)

                    url_path = ad.get('urlPath', '')
                    full_url = (
                        f"https://4zida.rs{url_path}" if url_path and url_path.startswith('/')
                        else f"https://4zida.rs/{url_path}" if url_path
                        else f"https://4zida.rs/stan-na-prodaju/{ad_id}"
                    )

                    rooms = ad.get('structureName', '') or ''
                    title = ad.get('detailedTitle') or ad.get('title') or f"Stan {area}m² – {place_names[0] if place_names else 'Beograd'}"

                    results.append({
                        'id': listing_id,
                        'title': title,
                        'location': location_str,
                        'price': price,
                        'area': area,
                        'price_per_m2': ppm2,
                        'url': full_url,
                        'source': '4zida.rs',
                        'rooms': rooms,
                    })
                except Exception as e:
                    logger.debug(f"[4zida.rs] oglas greška: {e}")

        except Exception as e:
            logger.error(f"[4zida.rs] API greška strana {page}: {e}")
            break

        time.sleep(1)

    logger.info(f"[4zida.rs] Ukupno u traženim lokacijama: {len(results)}")
    return results

# ============================================================
# SCRAPER: CITY EXPERT (novi API format: ?req=JSON)
# ============================================================

def scrape_cityexpert(config):
    """
    City Expert novi API: GET /api/Search?req={JSON}
    """
    import urllib.parse
    results = []
    MAX_PAGES = 5  # 5 x 60 = 300 najnovijih oglasa, kao 4zida

    for page in range(1, MAX_PAGES + 1):
        req_params = {
            "cityId": 1,
            "rentOrSale": "s",
            "searchSource": "regular",
            "sort": "datedsc",
            "currentPage": page,
            "resultsPerPage": 60,
        }
        req_json = json.dumps(req_params, separators=(',', ':'))
        api_url = f"https://cityexpert.rs/api/Search?req={urllib.parse.quote(req_json)}"
        logger.info(f"[City Expert] strana {page}: {api_url}")

        try:
            data = fetch_json(api_url, extra_headers={
                'Accept': 'application/json, text/plain, */*',
                'Origin': 'https://cityexpert.rs',
                'Referer': 'https://cityexpert.rs/prodaja-nekretnina/beograd',
            })

            ads = data.get('result', data.get('results', data.get('data', [])))
            logger.info(f"[City Expert] strana {page}: {len(ads)} oglasa")
            if not ads:
                break

            for ad in ads:
                try:
                    prop_id = str(ad.get('propId', ad.get('id', '')))
                    listing_id = f"ce_{prop_id}"

                    price = ad.get('price') or ad.get('totalPrice')
                    area = ad.get('size') or ad.get('m2')

                    mun_info = ad.get('municipality', {}) or {}
                    mun_name = mun_info.get('title', '') if isinstance(mun_info, dict) else str(mun_info)
                    micro = ad.get('microlocation', {}) or {}
                    micro_name = micro.get('title', '') if isinstance(micro, dict) else ''
                    street = ad.get('street', '') or ''
                    location_str = ', '.join(filter(None, [mun_name, micro_name, street])) or 'Beograd'

                    structure = str(ad.get('structure', '') or '')
                    slug = ad.get('slug', '') or ''
                    full_url = (
                        f"https://cityexpert.rs/prodaja/{slug}" if slug
                        else f"https://cityexpert.rs/prodaja/stan-{prop_id}"
                    )

                    ppm2 = calc_ppm2(price, area)
                    title = f"Stan {area}m² – {mun_name or 'Beograd'}"

                    results.append({
                        'id': listing_id,
                        'title': title,
                        'location': location_str,
                        'price': price,
                        'area': area,
                        'price_per_m2': ppm2,
                        'url': full_url,
                        'source': 'City Expert',
                        'rooms': structure,
                    })
                except Exception as e:
                    logger.debug(f"[City Expert] oglas greška: {e}")

        except Exception as e:
            logger.error(f"[City Expert] API greška strana {page}: {e}")
            break

        time.sleep(1)

    logger.info(f"[City Expert] Ukupno: {len(results)} oglasa")
    return results

# ============================================================
# SCRAPER: NEKRETNINE.RS (HTML)
# ============================================================

def scrape_nekretnine(config):
    """
    Scrape nekretnine.rs — podaci su u __NEXT_DATA__ JSON-u unutar HTML-a.
    URL: /prodaja-stanova/beograd/?pag={page}, sortiranje po najnovijem.
    Lokacija: properties[0].location.macrozone / microzone
    """
    results = []
    targets = config.get('target_locations', ['Novi Beograd', 'Zemun', 'Ledine', 'Bezanija'])
    session = requests.Session()
    session.trust_env = False
    session.headers.update({
        **HEADERS,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    })

    # Scrape prvih 5 strana (125 oglasa) sortiranih po najnovijem
    MAX_PAGES = 5

    for page in range(1, MAX_PAGES + 1):
        if page == 1:
            url = "https://www.nekretnine.rs/prodaja-stanova/beograd/"
        else:
            url = f"https://www.nekretnine.rs/prodaja-stanova/beograd/?pag={page}"

        logger.info(f"[Nekretnine.rs] strana {page}: {url}")
        try:
            # Sajt ume da vrati HTTP 103 (Early Hints) koji novije urllib3 verzije
            # isporuče kao finalni odgovor sa praznim telom. Pokušaj par puta —
            # sledeći pokušaj obično vrati pravi 200 sa sadržajem.
            r = None
            for attempt in range(1, 4):
                r = session.get(url, timeout=20, verify=SSL_VERIFY)
                if r.status_code == 200 and r.text:
                    break
                logger.warning(f"[Nekretnine.rs] status {r.status_code} (pokušaj {attempt}/3), ponavljam...")
                time.sleep(2 * attempt)

            if r is None or not r.text:
                logger.warning("[Nekretnine.rs] prazan odgovor posle 3 pokušaja")
                break

            import re as _re
            m = _re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, _re.DOTALL)
            if not m:
                logger.warning(f"[Nekretnine.rs] Nema __NEXT_DATA__ (status {r.status_code}, "
                               f"{len(r.text)} karaktera) — sajt je verovatno promenio strukturu")
                break

            page_data = json.loads(m.group(1))
            query_data = page_data['props']['pageProps']['dehydratedState']['queries'][0]['state']['data']
            listings_raw = query_data.get('results', [])

            logger.info(f"[Nekretnine.rs] strana {page}: {len(listings_raw)} oglasa")
            if not listings_raw:
                break

            for item in listings_raw:
                try:
                    re_data = item.get('realEstate', {})
                    seo = item.get('seo', {})

                    listing_id = f"nek_{re_data.get('id', '')}"
                    price = re_data.get('price', {}).get('value')
                    props = (re_data.get('properties') or [{}])[0]
                    location = props.get('location', {})

                    # Lokacija: macrozone + microzone
                    macrozone = location.get('macrozone', '')
                    microzone = location.get('microzone', '')
                    location_str = ', '.join(filter(None, [macrozone, microzone, location.get('city', '')]))

                    # Filter po lokaciji — is_target_location normalizuje dijakritike
                    # (vidi komentar u scrape_4zida)
                    if not is_target_location(location_str, targets):
                        continue

                    # Površina: "114 m²"
                    surface_str = props.get('surface', '')
                    area = parse_area(surface_str)

                    ppm2 = calc_ppm2(price, area)
                    full_url = seo.get('url', f"https://www.nekretnine.rs/oglasi/{re_data.get('id', '')}/")
                    title = seo.get('anchor', props.get('caption', f"Stan – {macrozone}"))

                    results.append({
                        'id': listing_id,
                        'title': title,
                        'location': location_str,
                        'price': price,
                        'area': area,
                        'price_per_m2': ppm2,
                        'url': full_url,
                        'source': 'Nekretnine.rs',
                        'rooms': props.get('rooms', ''),
                    })
                except Exception as e:
                    logger.debug(f"[Nekretnine.rs] oglas greška: {e}")

        except Exception as e:
            logger.error(f"[Nekretnine.rs] greška strana {page}: {e}")
            break

        time.sleep(2)

    logger.info(f"[Nekretnine.rs] Ukupno u traženim lokacijama: {len(results)}")
    return results

# ============================================================
# ZAJEDNIČKI SPISAK SCRAPERA + FILTER (koristi main, --debug i --listen)
# ============================================================

# Halo Oglasi (stanovi) radi lokalno ali GitHub Actions IP dobija 403,
# zato je van SCRAPERS_ACTIONS. --debug i --listen (koji se pokreću lokalno)
# koriste SCRAPERS_ALL i time ga uključuju.
SCRAPERS_ALL = [
    ('Halo Oglasi', scrape_halooglasi),
    ('4zida.rs', scrape_4zida),
    ('City Expert', scrape_cityexpert),
    ('Nekretnine.rs', scrape_nekretnine),
    ('Halo Zemljište', scrape_halooglasi_zemljiste),
]
SCRAPERS_ACTIONS = [s for s in SCRAPERS_ALL if s[0] != 'Halo Oglasi']


def run_scrapers_collect(config, scrapers):
    """Pokreni listu (naziv, funkcija) scrapera i skupi sve rezultate. Pad jednog ne ruši ostale."""
    all_listings = []
    for name, fn in scrapers:
        try:
            found = fn(config)
            logger.info(f"✔ {name}: {len(found)} oglasa")
            all_listings.extend(found)
        except Exception as e:
            logger.error(f"✘ {name} pao: {e}")
    return all_listings


def filter_match(listing, targets, max_ppm2, max_total):
    """Da li oglas prolazi lokacijski + cenovni filter.
    max_ppm2=None → cenovni cap po m² se ne primenjuje (koristi se za /sviN komandu).
    Zemljišta su već filtrirana unutar scrapera (price_per_m2 kod njih je cena PO ARU,
    ne po m² — zato se cenovni filteri za stanove ne primenjuju na njih ovde)."""
    loc_ok = is_target_location(listing.get('location', ''), targets)
    if listing.get('source') == 'Halo Zemljište':
        price_ok = True
        total_ok = True
    else:
        price_ok = max_ppm2 is None or is_good_price(listing.get('price_per_m2'), max_ppm2)
        price_val = listing.get('price')
        total_ok = max_total is None or (price_val is not None and price_val <= max_total)
    return loc_ok and price_ok and total_ok


# ============================================================
# TELEGRAM KOMANDE (/svi, /svi<broj>) — na zahtev, ne čeka se seen.json
# ============================================================

TELEGRAM_CMD_RE = re.compile(r'^/svi(\d+)?\b')


def get_telegram_updates(token, offset=None, timeout=30):
    """Long-poll Telegram getUpdates. Vraća listu update objekata.
    Ako Telegram vrati 409 (Conflict), znači da druga instanca već sluša
    (npr. lokalni --listen dok Actions pokušava) — tada tiho vraćamo praznu listu."""
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    params = {'timeout': timeout}
    if offset is not None:
        params['offset'] = offset
    try:
        r = requests.get(url, params=params, timeout=timeout + 10, verify=SSL_VERIFY,
                         proxies={'http': '', 'https': ''})
        if r.status_code == 409:
            logger.info("ℹ️  getUpdates 409 — druga instanca (lokalni listener) već sluša, preskačem.")
            return []
        r.raise_for_status()
        return r.json().get('result', [])
    except Exception as e:
        logger.error(f"❌ Greška pri getUpdates: {e}")
        return []


def handle_svi_command(config, chat_id, telegram_token, override_total=None, prefetched=None):
    """/svi → svi trenutni matches po config filterima (bez seen.json ograničenja).
    /svi<N> → isto, ali max ukupna cena = N*1000 €, bez ppm2 ograničenja (brzi pregled).
    prefetched: već prikupljeni oglasi (main() ih ima posle svog run-a) — tada se
    ne skrejpuje ponovo, odgovor je trenutan."""
    targets = config.get('target_locations', ['Beograd'])
    if override_total is not None:
        max_total = override_total
        ppm2_cap = None
    else:
        max_total = config.get('max_total_price')
        ppm2_cap = int(config.get('max_price_per_m2', 2000))

    cena_txt = f" do {max_total:,.0f} €".replace(',', '.') if max_total else ""

    if prefetched is not None:
        all_listings = prefetched
    else:
        send_telegram(telegram_token, chat_id, f"🔍 Tražim sve oglase{cena_txt}... (30-60s)")
        all_listings = run_scrapers_collect(config, SCRAPERS_ALL)

    matches = [l for l in all_listings if filter_match(l, targets, ppm2_cap, max_total)]

    if not matches:
        send_telegram(telegram_token, chat_id, "😕 Nema oglasa koji ispunjavaju kriterijume trenutno.")
        return

    matches.sort(key=lambda l: l.get('price') or float('inf'))

    header = f"📋 Nađeno {len(matches)} oglasa{cena_txt}"
    send_telegram(telegram_token, chat_id, header)
    time.sleep(1)

    # Kompaktna lista (ne pun format_message po oglasu — previše poruka za spam).
    # Chunk-uje se da ne pređe Telegram limit od 4096 karaktera po poruci.
    chunk = ""
    for l in matches:
        price = f"{l['price']:,.0f}€".replace(',', '.') if l.get('price') else '?'
        is_land = l.get('source') == 'Halo Zemljište'
        ppm2_val = l.get('price_per_m2')
        ppm2 = f" ({ppm2_val:,.0f}€/{'ar' if is_land else 'm²'})".replace(',', '.') if ppm2_val else ''
        title = (l.get('title') or '')[:45]
        line = f"• <b>{price}</b>{ppm2} | {title} | {l.get('location', '')}\n{l.get('url', '')}\n"
        if len(chunk) + len(line) > 3500:
            send_telegram(telegram_token, chat_id, chunk)
            time.sleep(1)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        send_telegram(telegram_token, chat_id, chunk)


def get_allowed_chat_ids(config):
    """Chat ID-ovi kojima je dozvoljeno da šalju komande (glavni + extra iz config-a)."""
    ids = {str(config.get('telegram_chat_id', ''))}
    ids.update(str(x) for x in config.get('telegram_extra_chat_ids', []))
    ids.discard('')
    return ids


def handle_pending_commands(config, prefetched=None):
    """Jednokratna (bez long-polla) provera neodgovorenih /svi komandi.
    Poziva se iz main() na GitHub Actions, da komande rade i kad lokalni
    --listen nije pokrenut (npr. računar ugašen) — odgovor tada stiže
    na sledećem satnom run-u umesto za par sekundi.

    Nema dupliranja odgovora: Telegram svaki update isporučuje samo jednom,
    pa ko ga prvi pokupi (lokalni listener ili Actions) taj i odgovara.
    Ako lokalni listener trenutno long-polluje, Actions dobije 409 i preskoči."""
    token = config.get('telegram_token', '')
    if not token:
        return
    allowed = get_allowed_chat_ids(config)
    if not allowed:
        logger.warning("⚠️  Nema dozvoljenih chat ID-ova — preskačem proveru komandi.")
        return

    updates = get_telegram_updates(token, timeout=0)  # timeout=0 → ne čekamo, uzmi šta je u redu
    if not updates:
        return

    last_update_id = None
    handled = 0
    for upd in updates:
        last_update_id = upd['update_id']
        msg = upd.get('message') or upd.get('channel_post')
        if not msg:
            continue
        chat_id = str(msg.get('chat', {}).get('id', ''))
        text = (msg.get('text') or '').strip()
        if chat_id not in allowed:
            logger.warning(f"⚠️  Ignorišem poruku od nepoznatog chat_id: {chat_id}")
            continue
        m = TELEGRAM_CMD_RE.match(text)
        if not m:
            continue
        logger.info(f"📩 [Actions] Komanda '{text}' od {chat_id}")
        override_total = int(m.group(1)) * 1000 if m.group(1) else None
        try:
            handle_svi_command(config, chat_id, token, override_total, prefetched=prefetched)
            handled += 1
        except Exception as e:
            logger.error(f"❌ Greška u handle_svi_command: {e}")

    # Potvrdi Telegram-u da su ovi update-ovi obrađeni (da ih ne dobijemo ponovo)
    if last_update_id is not None:
        get_telegram_updates(token, offset=last_update_id + 1, timeout=0)
        logger.info(f"✅ Obrađeno komandi: {handled} (potvrđeno do update_id={last_update_id})")


def run_telegram_listener(config):
    """Beskonačna petlja: long-poll Telegram, odgovori na /svi i /svi<N> komande.
    Namenjeno za lokalno pokretanje (Task Scheduler), ne za GitHub Actions."""
    token = config.get('telegram_token', '')
    if not token:
        logger.error("❌ Telegram token nije podešen — ne mogu da pokrenem listener.")
        return

    allowed_chat_ids = get_allowed_chat_ids(config)

    logger.info("🤖 Telegram listener pokrenut — komande: /svi, /svi<broj> (npr. /svi100 = do 100.000€)")
    logger.info(f"   Dozvoljeni chat ID-ovi: {', '.join(allowed_chat_ids) or '(nijedan — proveri config!)'}")

    offset = None
    while True:
        try:
            updates = get_telegram_updates(token, offset=offset, timeout=30)
            for upd in updates:
                offset = upd['update_id'] + 1
                msg = upd.get('message') or upd.get('channel_post')
                if not msg:
                    continue
                chat_id = str(msg.get('chat', {}).get('id', ''))
                text = (msg.get('text') or '').strip()

                if chat_id not in allowed_chat_ids:
                    logger.warning(f"⚠️  Ignorišem poruku od nepoznatog chat_id: {chat_id}")
                    continue

                m = TELEGRAM_CMD_RE.match(text)
                if not m:
                    continue

                logger.info(f"📩 Komanda '{text}' od {chat_id}")
                override_total = int(m.group(1)) * 1000 if m.group(1) else None
                try:
                    handle_svi_command(config, chat_id, token, override_total)
                except Exception as e:
                    logger.error(f"❌ Greška u handle_svi_command: {e}")
                    send_telegram(token, chat_id, f"❌ Greška pri pretrazi: {e}")
        except KeyboardInterrupt:
            logger.info("🛑 Listener zaustavljen (Ctrl+C).")
            break
        except Exception as e:
            logger.error(f"❌ Listener greška: {e}")
            time.sleep(5)


# ============================================================
# MAIN
# ============================================================

def main():
    logger.info("=" * 50)
    logger.info("🔍 Pokretanje scrapera za stanove")
    logger.info(f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
    logger.info("=" * 50)

    config = load_config()
    seen = load_seen()

    telegram_token = config.get('telegram_token', '')
    telegram_chat_id = config.get('telegram_chat_id', '')
    max_ppm2 = int(config.get('max_price_per_m2', 2000))
    max_total = config.get('max_total_price', None)
    targets = config.get('target_locations', ['Beograd'])

    if not telegram_token:
        logger.warning("⚠️  Telegram token nije podešen!")
    if not telegram_chat_id:
        logger.warning("⚠️  Telegram chat ID nije podešen!")

    logger.info(f"🎯 Lokacije: {', '.join(targets)}")
    logger.info(f"💶 Max cena/m²: {max_ppm2} €")
    logger.info(f"👁️  Već viđeno: {len(seen)} oglasa")

    all_listings = run_scrapers_collect(config, SCRAPERS_ACTIONS)

    logger.info(f"\n📦 Ukupno: {len(all_listings)} oglasa")

    # Ako je pokrenut --listen u pozadini (drugi proces), proveri Telegram komande
    # ovde nije potrebno — main() i listener su odvojeni CLI modovi.

    new_total = 0
    sent_total = 0

    for listing in all_listings:
        lid = listing.get('id')
        if not lid:
            continue

        is_new = lid not in seen
        if not is_new:
            # Osveži timestamp — oglas je i dalje živ u feedu, ne sme da
            # ispadne iz seen.json posle 30 dana pa da stigne duplikat.
            seen[lid] = time.time()
            continue

        new_total += 1

        if filter_match(listing, targets, max_ppm2, max_total):
            # 'or 0' a ne default u .get(): ključ postoji sa vrednošću None
            # (npr. zemljište bez cene) → .get(key, 0) vraća None → crash na :.0f
            ppm2 = listing.get('price_per_m2') or 0
            unit = '€/ar' if listing.get('source') == 'Halo Zemljište' else '€/m²'
            logger.info(
                f"🎯 MATCH: [{listing['source']}] {listing['title']} | "
                f"{ppm2:.0f}{unit} | {listing['url']}"
            )
            if telegram_token and telegram_chat_id:
                msg = format_message(listing)
                extra = [str(x) for x in config.get('telegram_extra_chat_ids', [])]
                all_chat_ids = list(dict.fromkeys([str(telegram_chat_id)] + extra))  # deduplikacija
                any_sent = False
                for cid in all_chat_ids:
                    ok = send_telegram(telegram_token, cid, msg)
                    if ok:
                        sent_total += 1
                        any_sent = True
                    time.sleep(1.5)
                if any_sent:
                    seen[lid] = time.time()
                else:
                    # Nijedno slanje nije uspelo — NE upisujemo u seen,
                    # sledeći run će ponovo pokušati da pošalje ovaj match.
                    logger.warning(f"⚠️  Slanje nije uspelo, oglas ostaje za sledeći run: {lid}")
            else:
                seen[lid] = time.time()
        else:
            # Nije match — zapamti da ga ne procenjujemo ponovo.
            # (Napomena: posle labavljenja filtera pokreni --clear-seen)
            seen[lid] = time.time()

    logger.info(f"\n📊 Rezultati:")
    logger.info(f"   Novi oglasi: {new_total}")
    logger.info(f"   Notifikacije poslate: {sent_total}")

    save_seen(seen)

    # Odgovori na eventualne /svi komande poslate dok lokalni listener nije radio.
    # Koristi oglase iz ovog run-a (prefetched) — bez ponovnog skrejpovanja.
    # Greška ovde ne sme da obori ceo run, zato try/except.
    try:
        handle_pending_commands(config, prefetched=all_listings)
    except Exception as e:
        logger.error(f"❌ Greška pri obradi Telegram komandi: {e}")

    logger.info("✅ Scraping završen.\n")


if __name__ == '__main__':
    import sys

    # Fix za Windows konzolu koja ne podržava UTF-8 po defaultu
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ('utf-8', 'utf8'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if sys.stderr.encoding and sys.stderr.encoding.lower() not in ('utf-8', 'utf8'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    if '--test-telegram' in sys.argv:
        config = load_config()
        token = config.get('telegram_token', '')
        chat_id = config.get('telegram_chat_id', '')
        print(f"Token: {token[:10]}... | Chat ID: {chat_id}")
        if not token or not chat_id:
            print("❌ Token ili chat ID nisu podešeni u config.json!")
        else:
            ok = send_telegram(token, chat_id, "✅ Test poruka — scraper radi!")
            print("✅ Poruka poslata!" if ok else "❌ Greška pri slanju!")
        sys.exit(0)

    if '--clear-seen' in sys.argv:
        save_seen({})
        print("✅ seen.json je obrisan — sledeći run će poslati sve oglase koji prođu filter.")
        sys.exit(0)

    if '--listen' in sys.argv:
        config = load_config()
        run_telegram_listener(config)
        sys.exit(0)

    if '--debug' in sys.argv:
        config = load_config()
        max_ppm2 = int(config.get('max_price_per_m2', 1500))
        max_total = config.get('max_total_price')
        targets = config.get('target_locations', ['Novi Beograd', 'Zemun', 'Ledine', 'Bezanija'])
        all_listings = run_scrapers_collect(config, SCRAPERS_ALL)

        print(f"\n{'='*60}")
        print(f"Ukupno nađeno: {len(all_listings)} oglasa")

        matches = [l for l in all_listings if filter_match(l, targets, max_ppm2, max_total)]
        print(f"Prolazi filter (lokacija + cena ≤ {max_ppm2}€/m² za stanove): {len(matches)}")
        print(f"{'='*60}")
        for l in matches:
            print(f"  [{l['source']}] {l['title']} | {l.get('price_per_m2') or 0:.0f}€/m² | {l['url']}")
        if not matches:
            print("\nNema oglasa koji prolaze filter. Distribucija cena/m²:")
            loc_listings = [l for l in all_listings if is_target_location(l.get('location', ''), targets)]
            print(f"  Oglasi u tražnim lokacijama: {len(loc_listings)}")
            prices = [l['price_per_m2'] for l in loc_listings if l.get('price_per_m2')]
            if prices:
                print(f"  Min: {min(prices):.0f}€/m² | Max: {max(prices):.0f}€/m² | Prosek: {sum(prices)/len(prices):.0f}€/m²")
        sys.exit(0)

    main()
