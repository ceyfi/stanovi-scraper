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
        r = requests.post(url, data=data, timeout=10, verify=SSL_VERIFY)
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
    """Fetch JSON bez URL re-encodinga (fix za 4zida [] problem)."""
    headers = {**HEADERS, 'Accept': 'application/json'}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        raise Exception(f"fetch_json greška: {e}")

# ============================================================
# SCRAPER: HALO OGLASI
# ============================================================

HALO_LOCATION_SLUGS = [
    ('novi-beograd', 'Novi Beograd'),
    ('zemun', 'Zemun'),
    ('ledine', 'Ledine'),
    ('bezanija', 'Bezanija'),
]

def scrape_halooglasi(config):
    results = []
    session = requests.Session()
    session.headers.update({
        **HEADERS,
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
    })

    for slug, location_name in HALO_LOCATION_SLUGS:
        url = f"https://www.halooglasi.com/nekretnine/prodaja-stanova/{slug}?sort=ValidFromMoment_desc"
        logger.info(f"[Halo Oglasi] {url}")
        try:
            r = session.get(url, timeout=20, verify=SSL_VERIFY)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, 'html.parser')

            items = soup.select('.product-item')
            logger.info(f"[Halo Oglasi] {location_name}: {len(items)} oglasa")

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
                    title = link.get_text(strip=True) or f"Stan - {location_name}"

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

                    results.append({
                        'id': listing_id,
                        'title': title,
                        'location': location_name,
                        'price': price,
                        'area': area,
                        'price_per_m2': ppm2,
                        'url': full_url,
                        'source': 'Halo Oglasi',
                    })
                except Exception as e:
                    logger.debug(f"[Halo Oglasi] oglas greška: {e}")

        except Exception as e:
            logger.error(f"[Halo Oglasi] greška za {slug}: {e}")

        time.sleep(2)

    return results

# ============================================================
# SCRAPER: 4ZIDA.RS (JSON API) — fix: urllib da ne enkodira []
# ============================================================

def scrape_4zida(config):
    """
    Dohvati sve stanove na prodaju u Beogradu.
    Koristimo cityId=1 umesto placeIds[] koji vraća 422.
    Filtriranje po lokaciji se radi naknadno u main().
    """
    results = []
    api_url = (
        "https://api.4zida.rs/v6/search/apartments"
        "?for=sale&cityId=1&sort=-createdAt&page=1&limit=60"
    )
    logger.info(f"[4zida.rs] {api_url}")
    try:
        data = fetch_json(api_url)
        ads = data.get('ads', data.get('results', data if isinstance(data, list) else []))
        logger.info(f"[4zida.rs] Beograd: {len(ads)} oglasa")

        for ad in ads:
            try:
                ad_id = str(ad.get('id', ''))
                listing_id = f"4zida_{ad_id}"

                price = ad.get('price') or ad.get('totalPrice')
                area = ad.get('m2') or ad.get('size')

                addr = ad.get('address', {}) or {}
                city_part = addr.get('cityPart', {}) or {}
                street = addr.get('street', {}) or {}
                nb_name = city_part.get('name', '') if isinstance(city_part, dict) else ''
                st_name = street.get('name', '') if isinstance(street, dict) else ''
                location_str = f"{nb_name}, {st_name}".strip(', ') or 'Beograd'

                structure = ad.get('structure', {}) or {}
                rooms = structure.get('name', '') if isinstance(structure, dict) else str(structure)

                ppm2 = calc_ppm2(price, area)

                slug = ad.get('slug', '') or ad.get('url', '')
                full_url = (
                    f"https://4zida.rs/{slug}" if slug and not slug.startswith('http')
                    else slug or f"https://4zida.rs/stan-na-prodaju/{ad_id}"
                )

                title = ad.get('title') or f"Stan {area}m² – {nb_name or 'Beograd'}"

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
        logger.error(f"[4zida.rs] API greška: {e}")

    return results

            for ad in ads:
                try:
                    ad_id = str(ad.get('id', ''))
                    listing_id = f"4zida_{ad_id}"

                    price = ad.get('price') or ad.get('totalPrice')
                    area = ad.get('m2') or ad.get('size')

                    addr = ad.get('address', {}) or {}
                    city_part = addr.get('cityPart', {}) or {}
                    street = addr.get('street', {}) or {}
                    nb_name = city_part.get('name', location_name) if isinstance(city_part, dict) else location_name
                    st_name = street.get('name', '') if isinstance(street, dict) else ''
                    location_str = f"{nb_name}, {st_name}".strip(', ')

                    structure = ad.get('structure', {}) or {}
                    rooms = structure.get('name', '') if isinstance(structure, dict) else str(structure)

                    ppm2 = calc_ppm2(price, area)

                    slug = ad.get('slug', '') or ad.get('url', '')
                    full_url = (
                        f"https://4zida.rs/{slug}" if slug and not slug.startswith('http')
                        else slug or f"https://4zida.rs/stan-na-prodaju/{ad_id}"
                    )

                    title = ad.get('title') or f"Stan {area}m² – {nb_name}"

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
            logger.error(f"[4zida.rs] API greška za placeId {place_id}: {e}")

        time.sleep(2)

    return results

# ============================================================
# SCRAPER: CITY EXPERT (API)
# ============================================================

def scrape_cityexpert(config):
    """
    Dohvati stanove na prodaju u Beogradu bez filtera po opštini.
    Izbegavamo municipalities[] koji vraća 500 grešku.
    """
    results = []
    api_url = (
        "https://cityexpert.rs/api/Search/"
        "?ptId=1&cityId=1&rentOrSale=s&currentPage=1&resultsPerPage=60&sort=datedesc"
    )
    logger.info(f"[City Expert] {api_url}")
    try:
        data = fetch_json(api_url, extra_headers={'Referer': 'https://cityexpert.rs/'})
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
    max_ppm2 = int(config.get('max_price_per_m2', 1500))
    targets = config.get('target_locations', ['Novi Beograd', 'Zemun', 'Ledine', 'Bezanija'])

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

        if loc_ok and price_ok:
            ppm2 = listing.get('price_per_m2', 0)
            logger.info(
                f"🎯 MATCH: [{listing['source']}] {listing['title']} | "
                f"{ppm2:.0f}€/m² | {listing['url']}"
            )
            if telegram_token and telegram_chat_id:
                msg = format_message(listing)
                ok = send_telegram(telegram_token, telegram_chat_id, msg)
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
            print(f"  Oglasi u traženim lokacijama: {len(loc_listings)}")
            prices = [l['price_per_m2'] for l in loc_listings if l.get('price_per_m2')]
            if prices:
                print(f"  Min: {min(prices):.0f}€/m² | Max: {max(prices):.0f}€/m² | Prosek: {sum(prices)/len(prices):.0f}€/m²")
        sys.exit(0)

    main()
