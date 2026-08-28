import unittest
from unittest.mock import patch

import scraper


class NekretninePriceTests(unittest.TestCase):
    def test_primary_nested_price(self):
        price = scraper.extract_nekretnine_price(
            {}, {'price': {'value': 125000}}, {}, 50
        )
        self.assertEqual(price, 125000)

    def test_fallback_total_price_string(self):
        price = scraper.extract_nekretnine_price(
            {'totalPrice': '149.500 EUR'}, {}, {}, 70
        )
        self.assertEqual(price, 149500)

    def test_fallback_total_price_us_format(self):
        price = scraper.extract_nekretnine_price(
            {'totalPrice': '$149,500.00'}, {}, {}, 70
        )
        self.assertEqual(price, 149500)

    def test_fallback_price_per_m2_times_area(self):
        price = scraper.extract_nekretnine_price(
            {}, {}, {'pricePerSquareMeter': {'value': '1.850 EUR'}}, 64
        )
        self.assertEqual(price, 118400)

    def test_does_not_guess_from_unrelated_text(self):
        price = scraper.extract_nekretnine_price(
            {'title': 'Stan 250000 Beograd'}, {}, {}, 60
        )
        self.assertIsNone(price)


class FourZidaCacheTests(unittest.TestCase):
    def setUp(self):
        scraper._four_zida_working_url = scraper.FOUR_ZIDA_URL_CANDIDATES[1]

    @patch('scraper.time.sleep', return_value=None)
    @patch('scraper.fetch_json')
    def test_cached_url_is_tried_first(self, fetch_json, _sleep):
        fetch_json.return_value = {'ads': []}

        scraper.scrape_4zida({})

        self.assertEqual(fetch_json.call_args_list[0].args[0], scraper.FOUR_ZIDA_URL_CANDIDATES[1])
        self.assertEqual(fetch_json.call_count, 1)

    @patch('scraper.time.sleep', return_value=None)
    @patch('scraper.fetch_json')
    def test_broken_cached_url_falls_back(self, fetch_json, _sleep):
        cached = scraper.FOUR_ZIDA_URL_CANDIDATES[1]
        fallback = scraper.FOUR_ZIDA_URL_CANDIDATES[0]

        def response(url, extra_headers=None):
            if url == cached:
                raise RuntimeError('endpoint down')
            return {'ads': []}

        fetch_json.side_effect = response
        scraper.scrape_4zida({})

        self.assertEqual(fetch_json.call_args_list[0].args[0], cached)
        self.assertEqual(fetch_json.call_args_list[1].args[0], fallback)
        self.assertEqual(scraper._four_zida_working_url, fallback)


if __name__ == '__main__':
    unittest.main()
