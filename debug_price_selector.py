#!/usr/bin/env python3
"""Jednokratna dijagnostika: nadji tacnu CSS klasu za cenu na halooglasi.com
(stari selektor '.price-box-main, [class*=price-main]' vise ne pogadja nista).
Pokreni: python debug_price_selector.py
Pa posalji ceo ispis nazad."""
import requests

URL = ("https://www.halooglasi.com/nekretnine/prodaja-zemljista"
       "?grad_id_l-lokacija_id_l-mikrolokacija_id_l=525206,525208,525211,55297,538989"
       "&sort=ValidFromMoment_desc")

session = requests.Session()
session.trust_env = False
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept-Language': 'sr-RS,sr;q=0.9,en;q=0.8',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Encoding': 'gzip, deflate',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
})

r = session.get(URL, timeout=20, verify=False)
print(f"HTTP status: {r.status_code}, duzina HTML-a: {len(r.text)}\n")

from bs4 import BeautifulSoup
soup = BeautifulSoup(r.text, 'html.parser')

items = soup.select('.product-item')
print(f"Nadjeno .product-item elemenata: {len(items)}\n")

if not items:
    print("PROBLEM: .product-item selektor uopste ne pogadja nista!")
    print("Prvih 500 karaktera HTML-a (da vidimo da li je stranica uopste ista):")
    print(r.text[:500])
else:
    # Za prva 2 oglasa, ispisi SVAKI element ciji tekst sadrzi '€' zajedno sa njegovom klasom
    for idx, item in enumerate(items[:2]):
        print(f"{'='*70}\nOGLAS #{idx+1}\n{'='*70}")
        found_price_el = False
        for el in item.find_all(True):
            text = el.get_text(strip=True)
            if '€' in text and len(text) < 60:
                classes = el.get('class', [])
                print(f"  TAG: <{el.name}> class={classes!r}")
                print(f"  TEXT: {text!r}\n")
                found_price_el = True
        if not found_price_el:
            print("  Nijedan element sa '€' nije nadjen unutar ovog oglasa.")
            print("  Ceo tekst oglasa (da vidimo da li cena uopste postoji u HTML-u):")
            print(f"  {item.get_text(' | ', strip=True)[:400]}")

print("\nGOTOVO — posalji ceo ovaj ispis nazad.")
