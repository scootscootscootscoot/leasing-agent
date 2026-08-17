#!/usr/bin/env python3
"""Offline tests — no network. Run: python3 test_leasing_agent.py

Covers the parts that quietly go wrong: the scoring maths, the dedupe key
(which already had one real bug), and the Craigslist positional decoder, whose
fixture is a verbatim item from a live response.
"""
import json
import os
import tempfile
import unittest

import score
from crawler import dedupe_key
from geo import bbox, haversine_mi, proximity_credit
from sources.craigslist import _parse, _prop_type
from store import Store

CFG = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "config.json")))

# Verbatim from sapi.craigslist.org, 2026-08-16.
CL_ITEM = [
    3299913, 3912492, 1, 1524, "1:1~30.3065~-97.7298", "0ak07K", -2,
    [13, "sRx1sLwUu6FLK2NuyFNzYm"],
    [4, "3:01010_eeoDxAJq00n_0ak07K"],
    [6, "austin-beautiful-hyde-park-living-pool"],
    [10, "$1,524"],
    "Beautiful Hyde Park Living! Pool, Gym, and SIX WEEKS FREE!",
    [5, 2, 995],
]
CL_MIN_ID = 7944709097


class TestGeo(unittest.TestCase):
    def test_known_distance(self):
        # Mueller centre to MLK Jr Station, ~1.4 mi apart.
        d = haversine_mi(30.2988, -97.7048, 30.2799, -97.7135)
        self.assertAlmostEqual(d, 1.4, delta=0.25)

    def test_zero_distance(self):
        self.assertEqual(haversine_mi(30.3, -97.7, 30.3, -97.7), 0.0)

    def test_bbox_encloses_radius(self):
        b = bbox(30.2988, -97.7048, 3.0)
        # North edge must be at least 3mi away, and not wildly more.
        d = haversine_mi(30.2988, -97.7048, b["north"], -97.7048)
        self.assertGreaterEqual(d, 2.95)
        self.assertLess(d, 3.2)

    def test_proximity_credit_bounds(self):
        self.assertEqual(proximity_credit(0.1, 0.75, 3.5), 1.0)
        self.assertEqual(proximity_credit(5.0, 0.75, 3.5), 0.0)
        mid = proximity_credit(2.125, 0.75, 3.5)
        self.assertAlmostEqual(mid, 0.5, delta=0.02)

    def test_proximity_credit_handles_missing(self):
        self.assertEqual(proximity_credit(None, 0.75, 3.5), 0.0)


class TestScore(unittest.TestCase):
    def test_price_credit_band(self):
        self.assertEqual(score.price_credit(1500, 1500, 3000), 1.0)
        self.assertEqual(score.price_credit(3000, 1500, 3000), 0.0)
        self.assertAlmostEqual(score.price_credit(2250, 1500, 3000), 0.5)

    def test_price_credit_missing(self):
        self.assertEqual(score.price_credit(None, 1500, 3000), 0.0)

    def test_space_credit_missing_is_not_zero(self):
        """Craigslist house posts routinely omit sqft; zeroing buries them."""
        self.assertEqual(score.space_credit(None, 700, 1800), 0.4)

    def test_space_credit_clamps(self):
        self.assertEqual(score.space_credit(600, 700, 1800), 0.0)
        self.assertEqual(score.space_credit(2400, 700, 1800), 1.0)

    def test_house_outranks_apartment_all_else_equal(self):
        base = {"price": 2000, "beds": 2, "baths": 2, "sqft": 1000,
                "lat": 30.2988, "lon": -97.7048}
        house, _, _ = score.score_listing({**base, "prop_type": "house"}, CFG)
        apt, _, _ = score.score_listing({**base, "prop_type": "apartment"}, CFG)
        self.assertGreater(house, apt)

    def test_closer_to_station_scores_higher(self):
        base = {"price": 2000, "beds": 2, "sqft": 1000, "prop_type": "house"}
        near, _, _ = score.score_listing(
            {**base, "lat": 30.2799, "lon": -97.7135}, CFG)   # at the station
        far, _, _ = score.score_listing(
            {**base, "lat": 30.3200, "lon": -97.6800}, CFG)
        self.assertGreater(near, far)

    def test_filters_reject_out_of_band(self):
        ok = {"price": 2000, "beds": 2, "baths": 2}
        self.assertTrue(score.passes_filters(ok, CFG))
        self.assertFalse(score.passes_filters({**ok, "price": 900}, CFG))
        self.assertFalse(score.passes_filters({**ok, "price": 4200}, CFG))
        self.assertFalse(score.passes_filters({**ok, "beds": 1}, CFG))
        self.assertFalse(score.passes_filters({**ok, "beds": None}, CFG))

    def test_baseline_matching(self):
        self.assertTrue(score.is_baseline({"property_name": "The Platform"}, CFG))
        self.assertTrue(score.is_baseline({"title": "Starlight Village"}, CFG))
        self.assertFalse(score.is_baseline({"title": "Casa Del Rio"}, CFG))


class TestDedupe(unittest.TestCase):
    def test_reposts_with_same_pin_and_rent_collapse(self):
        """The real bug: one Hyde Park complex reposted five times."""
        a = {"source": "craigslist", "source_id": "1", "lat": 30.3065,
             "lon": -97.7298, "price": 1524, "beds": 2}
        b = {**a, "source_id": "2"}     # different posting, same unit
        self.assertEqual(dedupe_key(a), dedupe_key(b))

    def test_distinct_floorplans_in_one_complex_survive(self):
        a = {"source": "redfin", "source_id": "x:1", "address": "1900 Simond Ave",
             "price": 2100, "beds": 2, "unit": "A2"}
        b = {**a, "source_id": "x:2", "unit": "B2"}
        self.assertNotEqual(dedupe_key(a), dedupe_key(b))

    def test_different_rent_is_different_listing(self):
        a = {"source": "craigslist", "source_id": "1", "lat": 30.3, "lon": -97.7,
             "price": 1800, "beds": 2}
        b = {**a, "source_id": "2", "price": 2200}
        self.assertNotEqual(dedupe_key(a), dedupe_key(b))

    def test_no_address_no_geo_never_dedupes(self):
        a = {"source": "craigslist", "source_id": "1"}
        b = {"source": "craigslist", "source_id": "2"}
        self.assertNotEqual(dedupe_key(a), dedupe_key(b))


class TestCraigslistParser(unittest.TestCase):
    def setUp(self):
        self.rec = _parse(CL_ITEM, CL_MIN_ID, "austin")

    def test_decodes_core_fields(self):
        r = self.rec
        self.assertEqual(r["source_id"], str(CL_MIN_ID + 3299913))
        self.assertEqual(r["price"], 1524)
        self.assertEqual(r["beds"], 2.0)
        self.assertEqual(r["sqft"], 995)
        self.assertAlmostEqual(r["lat"], 30.3065)
        self.assertAlmostEqual(r["lon"], -97.7298)

    def test_builds_a_real_url(self):
        self.assertEqual(
            self.rec["url"],
            "https://austin.craigslist.org/apa/d/"
            "austin-beautiful-hyde-park-living-pool/7948009010.html")

    def test_address_left_empty_for_dedupe(self):
        """Nothing vaguer than a street may land in `address` — the deduper
        treats it as a unique identity."""
        self.assertIsNone(self.rec["address"])

    def test_malformed_items_are_skipped_not_raised(self):
        self.assertIsNone(_parse([1, 2], 0, "austin"))
        self.assertIsNone(_parse("nonsense", 0, "austin"))
        self.assertIsNone(_parse([1, 2, 3, 4, 5, 6, 7], 0, "austin"))

    def test_prop_type_never_guesses_apartment(self):
        self.assertEqual(_prop_type("Charming Bungalow in Cherrywood"), "house")
        self.assertEqual(_prop_type("Updated Duplex near UT"), "duplex")
        self.assertEqual(_prop_type("Spacious 2BR with pool"), "unknown")
        self.assertEqual(_prop_type("Luxury apartment living"), "apartment")


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = Store(self.tmp.name)
        self.rec = {"source": "redfin", "source_id": "a1", "price": 2000,
                    "beds": 2, "prop_type": "house", "title": "Test House",
                    "lat": 30.29, "lon": -97.70, "score": 80.0}

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_insert_then_seen(self):
        self.assertEqual(self.store.upsert(self.rec), "new")
        self.assertEqual(self.store.upsert(self.rec), "seen")

    def test_price_cut_reported_increase_is_not(self):
        self.store.upsert(self.rec)
        self.assertEqual(self.store.upsert({**self.rec, "price": 1800}), "drop")
        self.assertEqual(self.store.upsert({**self.rec, "price": 2400}), "seen")

    def test_price_history_accumulates(self):
        self.store.upsert(self.rec)
        self.store.upsert({**self.rec, "price": 1800})
        rows = self.store.top(1)
        self.assertEqual(len(self.store.history(rows[0]["id"])), 2)

    def test_alert_gate(self):
        self.store.upsert(self.rec)
        self.assertEqual(len(self.store.unalerted(70, 10)), 1)
        self.store.mark_alerted([self.store.top(1)[0]["id"]])
        self.assertEqual(len(self.store.unalerted(70, 10)), 0)

    def test_find_by_short_prefix(self):
        self.store.upsert(self.rec)
        lid = self.store.top(1)[0]["id"]
        self.assertEqual(self.store.find(lid[:8])["id"], lid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
