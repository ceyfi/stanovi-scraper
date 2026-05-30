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

# SSL verify: False za lokalne mreže sa proxy/antivirus interceptom
# Na GitHub Actions ovo nema efekta (tamo SSL radi normalno)
SSL_VERIFY = False

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

def load_seen():
    if SEEN_FILE.exists():
        try:
            with open(SEEN_FILE, encoding='utf-8') as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def save_seen(seen):
    with open(SEEN_FILE, 'w', encoding='utf-8') as f:
        json.dump(sorted(list(seen)), f, indent=2)

# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(token, chat_id, message):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {
        'chat_id': chat_id,
        'text': message,
        'parse_mode': 'HTML',
        'disable_web_page_preview': False,
    }
    try:
        r = requests.post(url, data=data, timeout=10, verify=SSL_VERIFY,
                          proxies={'http': '', 'https': ''})
        r.raise_for_status()
        logger.info("✅ Telegram poruka poslata")
        return True
    except Exception as e:
        logger.error(f"❌ Greška pri slanju Telegram poruke: {e}")
        return False

def format_message(listing):
    emoji_source = {
        'Halo Oglasi': '🟡',
        '4zida.rs': '🟢',
        'City Expert': '🔴',
    }
    icon = emoji_source.get(listing.get('source', ''), '🏠')

    lines = [
        f"{icon} <b>{listing.get('title', 'Stan na prodaju')}</b>",
        f"📍 {listing.get('location', 'N/A')}",
    ]
    if listing.get('price'):
        lines.append(f"💶 Cena: <b>{listing['price']:,.0f} €</b>".replace(',', '.'))
    if listing.get('area'):
        lines.append(f"📐 Površina: <b>{listing['area']} m²</b>")
    if listing.get('price_per_m2'):
        lines.append(f"📊 Cena/m²: <b>{listing['price_per_m2']:,.0f} €/m²</b>".replace(',', '.'))
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

def is_target_location(location_text, targets):
    loc = location_text.lower()
    return any(t.lower() in loc for t in targets)

def is_good_price(ppm2, max_ppm2):
    return ppm2 is not None and ppm2 <= max_ppm2

def fetch_json(url, extra_headers=None):
    """Fetch JSON bez URL re-encodinga i bez proxy-ja (fix za lokalni antivirus intercept)."""
    import ssl
    headers = {**HEADERS, 'Accept': 'application/json'}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    # SSL context koji ignoriše certifikate
    ctx = ssl.create_default_context()
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
HALO_URL = "https://www.halooglasi.com/nekretnine/prodaja-stanova/beograd?sort=ValidFromMoment_desc"

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
                price_el = item.select_one('.price-box-main, [class*="price-main"]')
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
    max_ppm2 = int(config.get('max_price_per_m2', 1500))

    # Probaj različite URL formate — API menja šta prima
    url_candidates = [
        "https://api.4zida.rs/v6/search/apartments",
        "https://api.4zida.rs/v6/search/apartments?for=sale",
        "https://api.4zida.rs/v5/search/apartments",
    ]

    working_url = None
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

                    # Proveri da li je u traženim lokacijama
                    if not any(t.lower() in pn.lower() for t in targets for pn in place_names):
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

    req_params = {
        "cityId": 1,
        "rentOrSale": "s",
        "searchSource": "regular",
        "sort": "datedsc",
        "currentPage": 1,
        "resultsPerPage": 60,
    }
    req_json = json.dumps(req_params, separators=(',', ':'))
    api_url = f"https://cityexpert.rs/api/Search?req={urllib.parse.quote(req_json)}"
    logger.info(f"[City Expert] {api_url}")

    try:
        data = fetch_json(api_url, extra_headers={
            'Accept': 'application/json, text/plain, */*',
            'Origin': 'https://cityexpert.rs',
            'Referer': 'https://cityexpert.rs/prodaja-nekretnina/beograd',
        })

        ads = data.get('result', data.get('results', data.get('data', [])))
        logger.info(f"[City Expert] Beograd: {len(ads)} oglasa")

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
        logger.error(f"[City Expert] API greška: {e}")

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
            r = session.get(url, timeout=20, verify=SSL_VERIFY)
            if r.status_code != 200:
                logger.warning(f"[Nekretnine.rs] status {r.status_code}")
                break

            import re as _re
            m = _re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, _re.DOTALL)
            if not m:
                logger.warning("[Nekretnine.rs] Nema __NEXT_DATA__")
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

                    # Filter po lokaciji
                    if not any(t.lower() in location_str.lower() for t in targets):
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

    scrapers = [
        # Halo Oglasi je uklonjen — blokira GitHub Actions IP (403)
        ('4zida.rs', scrape_4zida),
        ('City Expert', scrape_cityexpert),
        ('Nekretnine.rs', scrape_nekretnine),
    ]

    all_listings = []
    for name, fn in scrapers:
        try:
            found = fn(config)
            logger.info(f"✔ {name}: {len(found)} oglasa")
            all_listings.extend(found)
        except Exception as e:
            logger.error(f"✘ {name} pao: {e}")

    logger.info(f"\n📦 Ukupno: {len(all_listings)} oglasa")

    new_total = 0
    sent_total = 0

    for listing in all_listings:
        lid = listing.get('id')
        if not lid:
            continue

        is_new = lid not in seen
        seen.add(lid)

        if not is_new:
            continue

        new_total += 1

        loc_ok = is_target_location(listing.get('location', ''), targets)
        price_ok = is_good_price(listing.get('price_per_m2'), max_ppm2)
        price_val = listing.get('price')
        total_ok = max_total is None or (price_val is not None and price_val <= max_total)

        if loc_ok and price_ok and total_ok:
            ppm2 = listing.get('price_per_m2', 0)
            logger.info(
                f"🎯 MATCH: [{listing['source']}] {listing['title']} | "
                f"{ppm2:.0f}€/m² | {listing['url']}"
            )
            if telegram_token and telegram_chat_id:
                msg = format_message(listing)
                all_chat_ids = [telegram_chat_id] + config.get('telegram_extra_chat_ids', [])
                for cid in all_chat_ids:
                    ok = send_telegram(telegram_token, cid, msg)
                    if ok:
                        sent_total += 1
                    time.sleep(1.5)

    logger.info(f"\n📊 Rezultati:")
    logger.info(f"   Novi oglasi: {new_total}")
    logger.info(f"   Notifikacije poslate: {sent_total}")

    save_seen(seen)
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
        save_seen(set())
        print("✅ seen.json je obrisan — sledeći run će poslati sve oglase koji prođu filter.")
        sys.exit(0)

    if '--debug' in sys.argv:
        config = load_config()
        max_ppm2 = int(config.get('max_price_per_m2', 1500))
        targets = config.get('target_locations', ['Novi Beograd', 'Zemun', 'Ledine', 'Bezanija'])
        scrapers = [
            ('Halo Oglasi', scrape_halooglasi),
            ('4zida.rs', scrape_4zida),
            ('City Expert', scrape_cityexpert),
            ('Nekretnine.rs', scrape_nekretnine),
        ]
        all_listings = []
        for name, fn in scrapers:
            try:
                found = fn(config)
                all_listings.extend(found)
            except Exception as e:
                print(f"✘ {name} pao: {e}")

        print(f"\n{'='*60}")
        print(f"Ukupno nađeno: {len(all_listings)} oglasa")
        matches = [l for l in all_listings if
                   is_target_location(l.get('location', ''), targets) and
                   is_good_price(l.get('price_per_m2'), max_ppm2)]
        print(f"Prolazi filter (lokacija + cena ≤ {max_ppm2}€/m²): {len(matches)}")
        print(f"{'='*60}")
        for l in matches:
            print(f"  [{l['source']}] {l['title']} | {l.get('price_per_m2', 0):.0f}€/m² | {l['url']}")
        if not matches:
            print("\nNema oglasa koji prolaze filter. Distribucija cena/m²:")
            loc_listings = [l for l in all_listings if is_target_location(l.get('location', ''), targets)]
            print(f"  Oglasi u tražnim lokacijama: {len(loc_listings)}")
            prices = [l['price_per_m2'] for l in loc_listings if l.get('price_per_m2')]
            if prices:
                print(f"  Min: {min(prices):.0f}€/m² | Max: {max(prices):.0f}€/m² | Prosek: {sum(prices)/len(prices):.0f}€/m²")
        sys.exit(0)

    main()
