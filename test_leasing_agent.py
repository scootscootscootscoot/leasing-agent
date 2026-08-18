#!/usr/bin/env python3
"""Offline tests — no network. Run: python3 test_leasing_agent.py

Covers the parts that quietly go wrong: the scoring maths, the dedupe key
(which already had one real bug), the Craigslist and rent.com decoders (whose
fixtures are verbatim items from live responses), the square-footage parser,
walkability credit, and the feedback learner's refusal to draw conclusions
from too little evidence.
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import learn
import score
import sqft
import walk
from crawler import dedupe_key
from geo import bbox, dist_mi, haversine_mi, proximity_credit
from sources import rent
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

    def test_condition_keywords(self):
        """Learned from feedback: 'old'/'vintage' dislikes were invisible."""
        self.assertEqual(score.condition_credit(
            {"title": "Gorgeous renovated house"}), 1.0)
        self.assertEqual(score.condition_credit(
            {"title": "1950's Vintage Austin Home"}), 0.0)
        self.assertEqual(score.condition_credit({"title": "2bd apartment"}), 0.5)
        # pos/neg cancel: "renovated 1950s" is not double-counted
        self.assertEqual(score.condition_credit(
            {"title": "Renovated 1950s bungalow"}), 0.5)

    def test_amenity_keywords(self):
        self.assertEqual(score.amenity_credit(
            {"description": "steps from the pool"}), 1.0)
        self.assertEqual(score.amenity_credit(
            {"description": "nothing special"}), 0.5)

    def test_renovated_outranks_vintage_all_else_equal(self):
        base = {"price": 2000, "beds": 2, "baths": 1, "sqft": 1000,
                "prop_type": "house", "lat": 30.2988, "lon": -97.7048}
        good, _, _ = score.score_listing(
            {**base, "title": "Renovated home"}, CFG)
        bad, _, _ = score.score_listing(
            {**base, "title": "Vintage 1950s home"}, CFG)
        self.assertGreater(good, bad)

    def test_duplex_multiplier(self):
        """Feedback: 'dont like duplexes they should be downgraded'."""
        base = {"price": 2000, "beds": 2, "baths": 1, "sqft": 1000,
                "lat": 30.2988, "lon": -97.7048}
        dup, _, _ = score.score_listing({**base, "prop_type": "duplex"}, CFG)
        house, _, _ = score.score_listing({**base, "prop_type": "house"}, CFG)
        self.assertAlmostEqual(dup / house, 0.85, places=2)

    def test_availability_gate(self):
        """Feedback: a 'not available' unit surfaced as a card."""
        ok = {"price": 2000, "beds": 2, "baths": 2}
        self.assertTrue(score.passes_filters({**ok, "available": "TODAY"}, CFG))
        self.assertTrue(score.passes_filters({**ok, "available": None}, CFG))
        far = (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()
        self.assertFalse(score.passes_filters({**ok, "available": far}, CFG))
        soon = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
        self.assertTrue(score.passes_filters({**ok, "available": soon}, CFG))
        # Redfin sends naive ISO stamps (no tz) — must not crash the gate.
        naive_far = (datetime.now() + timedelta(days=90)).isoformat()
        self.assertFalse(score.passes_filters({**ok, "available": naive_far}, CFG))


class TestDedupe(unittest.TestCase):
    def test_reposts_with_same_pin_and_rent_collapse(self):
        """The real bug: one Hyde Park complex reposted five times."""
        a = {"source": "craigslist", "source_id": "1", "lat": 30.3065,
             "lon": -97.7298, "price": 1524, "beds": 2}
        b = {**a, "source_id": "2"}     # different posting, same unit
        self.assertEqual(dedupe_key(a), dedupe_key(b))

    def test_distinct_floorplans_in_one_complex_survive(self):
        """Real floorplans differ in their dimensions, and that is the key."""
        a = {"source": "redfin", "source_id": "x:1", "address": "1900 Simond Ave",
             "price": 2100, "beds": 2, "baths": 1, "sqft": 880, "unit": "A2"}
        b = {**a, "source_id": "x:2", "baths": 2, "sqft": 1040, "unit": "B2"}
        self.assertNotEqual(dedupe_key(a), dedupe_key(b))

    def test_identical_units_behind_different_labels_collapse(self):
        """Deliberate: same address, same rent, same beds/baths/sqft.

        The `unit` label used to be part of the identity, which kept these
        apart. It also split the same house across two sources, because
        rent.com writes a derived label where Redfin writes none — so the
        label is no longer trusted. Two units identical in every field we
        track are one entry on a shortlist.
        """
        a = {"source": "redfin", "source_id": "x:1", "address": "1900 Simond Ave",
             "price": 2100, "beds": 2, "baths": 1, "sqft": 880, "unit": "A2"}
        b = {**a, "source_id": "x:2", "unit": "B2"}
        self.assertEqual(dedupe_key(a), dedupe_key(b))

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


class TestSqft(unittest.TestCase):
    def test_parses_common_spellings(self):
        for text, want in (
                ("2/1 bungalow, 1,150 sqft, fenced yard", 1150),
                ("Lovely home — 980 sq. ft. — pets ok", 980),
                ("1250 square feet of space", 1250),
                ("SQFT: 1,400", 1400),
                ("cozy 875ft2 unit", 875)):
            self.assertEqual(sqft.parse_sqft(text), want, text)

    def test_rejects_implausible_numbers(self):
        """Lot sizes are the common false positive; they sit above the band."""
        self.assertIsNone(sqft.parse_sqft("on a 9,000 sqft lot"))
        self.assertIsNone(sqft.parse_sqft("43,560 sq ft parcel"))
        self.assertIsNone(sqft.parse_sqft("tiny 90 sq ft closet"))
        self.assertIsNone(sqft.parse_sqft("no size given here"))
        self.assertIsNone(sqft.parse_sqft("$1,850/mo, 2 bed"))

    def test_comma_grouped_thousands_are_read_whole(self):
        """'1,150 sqft' must not be read as 150 sqft."""
        self.assertEqual(sqft.parse_sqft("1,150 sqft"), 1150)
        self.assertEqual(sqft.parse_sqft("sqft: 1,450"), 1450)

    def test_labelled_form_wins(self):
        # The labelled number is the unit; the bare one is the building.
        self.assertEqual(
            sqft.parse_sqft("in a 1900 sqft house — sqft: 850"), 850)

    def test_enrich_marks_basis(self):
        rec = {"sqft": 1100, "title": "x"}
        self.assertEqual(sqft.enrich(rec)["sqft_basis"], "reported")

        rec = {"sqft": None, "title": "2/1 with 1,050 sq ft"}
        got = sqft.enrich(rec)
        self.assertEqual((got["sqft"], got["sqft_basis"]), (1050, "parsed"))

        rec = {"sqft": None, "title": "no numbers"}
        self.assertEqual(sqft.enrich(rec)["sqft_basis"], "missing")

    def test_out_of_band_reported_value_is_dropped(self):
        """A source reporting a lot size must not poison the estimator."""
        got = sqft.enrich({"sqft": 43560, "title": "big lot"})
        self.assertIsNone(got["sqft"])
        self.assertEqual(got["sqft_basis"], "missing")

    def test_impossible_size_for_the_bed_count_is_dropped(self):
        """A live crawl really did return '2 bed, 360 sq ft'."""
        got = sqft.enrich({"sqft": 360, "beds": 2, "title": "unit B"})
        self.assertIsNone(got["sqft"])
        self.assertEqual(got["sqft_basis"], "missing")

    def test_genuinely_small_but_real_units_survive(self):
        """A 528 sq ft two-bed is cramped but real; it must not be dropped."""
        got = sqft.enrich({"sqft": 528, "beds": 2, "title": "1630 E 6th St"})
        self.assertEqual((got["sqft"], got["sqft_basis"]), (528, "reported"))

    def test_implausible_sizes_do_not_reach_the_model(self):
        rows = ([{"sqft": 1000, "beds": 2, "prop_type": "house",
                  "sqft_basis": "reported"}] * 6
                + [{"sqft": 300, "beds": 2, "prop_type": "house",
                    "sqft_basis": "reported"}] * 6)
        self.assertEqual(sqft.SqftModel(rows).estimate("house", 2), 1000)

    def test_model_learns_from_corpus(self):
        rows = [{"sqft": 1000 + i, "beds": 2, "prop_type": "house",
                 "sqft_basis": "reported"} for i in range(8)]
        model = sqft.SqftModel(rows)
        self.assertAlmostEqual(model.estimate("house", 2), 1003, delta=2)

    def test_model_falls_back_when_corpus_is_thin(self):
        """Two comparables is not a median; the static table must take over."""
        model = sqft.SqftModel([{"sqft": 5000, "beds": 2, "prop_type": "house",
                                 "sqft_basis": "reported"}] * 2)
        got = model.estimate("house", 2)
        self.assertLess(got, 2000, "a 2-sample outlier must not become truth")

    def test_estimated_space_credit_is_discounted(self):
        """An estimated size may rank, but must not beat a measured one."""
        measured = sqft.space_credit(1800, 700, 1800, "reported")
        estimated = sqft.space_credit(1800, 700, 1800, "estimated")
        self.assertEqual(measured, 1.0)
        self.assertLess(estimated, measured)
        self.assertGreater(estimated, 0.5)

    def test_price_per_sqft(self):
        self.assertEqual(sqft.price_per_sqft({"price": 2000, "sqft": 1000}), 2.0)
        self.assertIsNone(sqft.price_per_sqft({"price": 2000, "sqft": None}))


class TestWalk(unittest.TestCase):
    """Amenity scoring, with a synthetic amenity set — no network."""

    HERE = (30.2988, -97.7048)

    def test_nothing_nearby_scores_zero(self):
        credit, counts = walk.amenity_score(*self.HERE, [])
        self.assertEqual((credit, counts), (0.0, {}))

    def test_closer_beats_further(self):
        near = [["grocery", 30.2990, -97.7050]]
        far = [["grocery", 30.3100, -97.7200]]
        self.assertGreater(walk.amenity_score(*self.HERE, near)[0],
                           walk.amenity_score(*self.HERE, far)[0])

    def test_diminishing_returns_per_category(self):
        """Twenty cafes must not outscore a cafe plus a shop plus a park."""
        many = [["food", 30.2989, -97.7049]] * 20
        mixed = [["food", 30.2989, -97.7049],
                 ["grocery", 30.2989, -97.7049],
                 ["park", 30.2989, -97.7049]]
        self.assertGreater(walk.amenity_score(*self.HERE, mixed)[0],
                           walk.amenity_score(*self.HERE, many)[0])

    def test_credit_is_bounded(self):
        crowd = [[c, 30.2988, -97.7048] for c in walk.CATEGORIES] * 40
        credit, _ = walk.amenity_score(*self.HERE, crowd)
        self.assertLessEqual(credit, 1.0)
        self.assertGreater(credit, 0.0)

    def test_unlocated_listing_scores_zero(self):
        self.assertEqual(walk.amenity_score(None, None, [["food", 30.3, -97.7]]),
                         (0.0, {}))

    def test_classify_maps_osm_tags(self):
        self.assertEqual(walk._classify({"shop": "supermarket"}), "grocery")
        self.assertEqual(walk._classify({"highway": "bus_stop"}), "transit")
        self.assertIsNone(walk._classify({"amenity": "fountain"}))

    def test_bbox_query_covers_ways_not_just_nodes(self):
        """Parks are usually ways; a node-only query silently loses them."""
        q = walk._bbox_query(bbox(30.2988, -97.7048, 3.0))
        self.assertIn("nwr", q)
        self.assertIn("out center", q)

    def test_table_cell_handles_unreachable(self):
        self.assertIsNone(walk._cell([[None]], 0, 0))
        self.assertIsNone(walk._cell([], 0, 0))
        self.assertEqual(walk._cell([[1609.344]], 0, 0), 1609.344)


class TestRoutedScoring(unittest.TestCase):
    """A routed walk must override crow-flies wherever we have one."""

    def _rec(self, **kw):
        base = {"price": 2000, "beds": 2, "baths": 1, "sqft": 1000,
                "prop_type": "house", "lat": 30.2900, "lon": -97.7100}
        base.update(kw)
        return base

    def test_routed_distance_is_preferred(self):
        straight = score.score_listing(self._rec(), CFG)[2]
        routed = score.score_listing(
            self._rec(walk_routes={"mlk_station": {"mi": 2.4, "min": 48}}),
            CFG)[2]
        self.assertEqual(straight["mlk_station"]["basis"], "straight")
        self.assertEqual(routed["mlk_station"]["basis"], "routed")
        self.assertEqual(routed["mlk_station"]["mi"], 2.4)
        self.assertEqual(routed["mlk_station"]["walk_min"], 48)

    def test_long_walk_scores_below_short_walk(self):
        near = score.score_listing(
            self._rec(walk_routes={"mlk_station": {"mi": 0.4, "min": 8}}),
            CFG)[0]
        far = score.score_listing(
            self._rec(walk_routes={"mlk_station": {"mi": 2.4, "min": 48}}),
            CFG)[0]
        self.assertGreater(near, far)

    def test_straight_line_is_kept_for_comparison(self):
        d = score.score_listing(
            self._rec(walk_routes={"mueller": {"mi": 3.0, "min": 60}}),
            CFG)[2]
        self.assertIsNotNone(d["mueller"]["straight_mi"])
        self.assertNotEqual(d["mueller"]["straight_mi"], 3.0)

    def test_walk_credit_raises_the_score(self):
        low = score.score_listing(self._rec(walk_credit=0.0), CFG)[0]
        high = score.score_listing(self._rec(walk_credit=1.0), CFG)[0]
        self.assertGreater(high, low)

    def test_reader_handles_both_stored_shapes(self):
        """Rows written before walk routing existed must still render."""
        self.assertEqual(dist_mi({"mueller": 1.2}, "mueller"), 1.2)
        self.assertEqual(dist_mi({"mueller": {"mi": 1.2}}, "mueller"), 1.2)
        self.assertIsNone(dist_mi({}, "mueller"))


class TestRentParser(unittest.TestCase):
    """Verbatim shapes from a live rent.com __NEXT_DATA__ payload."""

    ITEM = {
        "id": "lc5897531",
        "name": "Centro Studio Homes",
        "address": "824 Camino La Costa",
        "addressFull": "824 Camino La Costa, Austin, TX 78752",
        "propertyType": "TOWNHOME",
        "zipCode": "78752",
        "urlPathname": "/apartment/centro-studio-homes-austin-tx-lc5897531",
        "location": {"lat": 30.329345, "lng": -97.70343, "city": "Austin",
                     "stateAbbr": "TX", "zip": "78752"},
        "priceRange": {"min": 1699, "max": 2114},
        "bedRange": {"min": 2, "max": 2},
        "availabilityStatus": "TODAY",
        "floorPlans": [
            {"bedCount": 2, "bathCount": 1, "availableCount": 4,
             "priceRange": {"max": 2114, "min": 1874},
             "sqFtRange": {"min": 1100, "max": 1150}},
            {"bedCount": 2, "bathCount": 2, "availableCount": 1,
             "priceRange": {"max": 2400, "min": 2200},
             "sqFtRange": {"min": 1250, "max": 1250}},
        ],
    }

    def test_one_record_per_floor_plan(self):
        recs = rent._parse(self.ITEM, "townhouses")
        self.assertEqual(len(recs), 2)
        self.assertEqual([r["price"] for r in recs], [1874, 2200])
        self.assertEqual([r["sqft"] for r in recs], [1100, 1250])
        self.assertEqual([r["baths"] for r in recs], [1, 2])

    def test_floor_plans_get_distinct_ids(self):
        ids = {r["source_id"] for r in rent._parse(self.ITEM, "townhouses")}
        self.assertEqual(len(ids), 2)

    def test_geo_and_type_carried_through(self):
        r = rent._parse(self.ITEM, "townhouses")[0]
        self.assertEqual((r["lat"], r["lon"]), (30.329345, -97.70343))
        self.assertEqual(r["prop_type"], "townhouse")
        self.assertTrue(r["url"].startswith("https://www.rent.com/apartment/"))

    def test_property_without_floor_plans_still_yields_a_record(self):
        item = dict(self.ITEM, floorPlans=[])
        recs = rent._parse(item, "houses")
        self.assertEqual(len(recs), 1)
        self.assertTrue(recs[0]["is_complex"])
        self.assertEqual(recs[0]["price"], 1699)

    def test_category_supplies_type_when_the_field_is_missing(self):
        item = dict(self.ITEM)
        item.pop("propertyType")
        self.assertEqual(rent._parse(item, "houses")[0]["prop_type"], "house")

    def test_neighbourhood_slug_extraction(self):
        self.assertEqual(
            rent._slug("/texas/austin-apartments/hyde-park-neighborhood"),
            "hyde-park")
        self.assertIsNone(rent._slug("/texas/austin-apartments"))

    def test_filters_use_the_config_band(self):
        self.assertEqual(rent._filters(CFG),
                         "2-bedroom_min-price-1500_max-price-3000")

    def test_duplicate_properties_collapse(self):
        """The same property appears under several categories and hoods."""
        recs = rent._parse(self.ITEM, "townhouses") * 3
        self.assertEqual(len(rent._dedupe(recs)), 2)


class TestCrossSourceDedupe(unittest.TestCase):
    """The same house from two sources must collapse to one card.

    Both fixtures are real pairs that appeared twice on the dashboard.
    """

    def test_synthetic_floorplan_label_does_not_split(self):
        redfin = {"source": "redfin", "source_id": "1",
                  "address": "1004 E 38th 1/2 St", "unit": None,
                  "price": 1900, "beds": 2.0, "baths": 1.0, "sqft": 1116,
                  "lat": 30.2958854, "lon": -97.7212906}
        rent = {"source": "rent", "source_id": "2",
                "address": "1004 E 38th 1/2 St", "unit": "2bd/1ba",
                "price": 1900, "beds": 2.0, "baths": 1.0, "sqft": 1116,
                "lat": 30.2958784, "lon": -97.7212942}
        self.assertEqual(dedupe_key(redfin), dedupe_key(rent))

    def test_address_case_does_not_split(self):
        a = {"source": "rent", "source_id": "1",
             "address": "2928 E 13th St unit A", "unit": "2bd/1ba",
             "price": 2040, "beds": 2.0, "baths": 1.0, "sqft": 750,
             "lat": 30.2773619, "lon": -97.7063124}
        b = {"source": "redfin", "source_id": "2",
             "address": "2928 E 13th St Unit A", "unit": None,
             "price": 2040, "beds": 2.0, "baths": 1.0, "sqft": 750,
             "lat": 30.2772452, "lon": -97.7063542}
        self.assertEqual(dedupe_key(a), dedupe_key(b))

    def test_street_abbreviation_does_not_split(self):
        """'Danbury Square' vs 'Danbury Sq' shipped as two cards."""
        rent = {"source": "rent", "source_id": "1",
                "address": "1304 Danbury Square unit A",
                "price": 1795, "beds": 2, "baths": 2, "sqft": 2324,
                "lat": 30.2801, "lon": -97.7222}
        redfin = {"source": "redfin", "source_id": "2",
                  "address": "1304 Danbury Sq Unit A",
                  "price": 1795, "beds": 2, "baths": 2, "sqft": 2324,
                  "lat": 30.2801, "lon": -97.7222}
        self.assertEqual(dedupe_key(rent), dedupe_key(redfin))

        # …and also when neither record carries coordinates.
        for r in (rent, redfin):
            r.pop("lat"), r.pop("lon")
        self.assertEqual(dedupe_key(rent), dedupe_key(redfin))

    def test_int_and_float_beds_agree(self):
        a = {"source": "a", "source_id": "1", "address": "1 Main",
             "beds": 2, "baths": 1, "sqft": 900, "price": 1800}
        b = dict(a, source="b", source_id="2", beds=2.0, baths=1.0)
        self.assertEqual(dedupe_key(a), dedupe_key(b))

    def test_different_floorplans_still_separate(self):
        """Same complex, same price, different product — must not collapse."""
        small = {"source": "rent", "source_id": "1", "address": None,
                 "lat": 30.2988, "lon": -97.7048,
                 "beds": 2, "baths": 1, "sqft": 850, "price": 1900}
        large = dict(small, source_id="2", baths=2, sqft=1000)
        self.assertNotEqual(dedupe_key(small), dedupe_key(large))


class TestFeedback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = Store(self.tmp.name)
        self.store.upsert({
            "source": "redfin", "source_id": "1", "price": 2000, "beds": 2,
            "sqft": 1000, "prop_type": "house", "score": 80.0,
            "parts": {"price": 0.5, "walk": 0.9},
            "lat": 30.30, "lon": -97.70})
        self.lid = self.store.top(1)[0]["id"]

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_records_and_reads_back(self):
        self.store.add_feedback(self.lid, "like", "great light", "dashboard")
        rows = self.store.feedback_for(self.lid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], "like")
        self.assertEqual(rows[0]["reason"], "great light")

    def test_rejects_a_bad_verdict(self):
        with self.assertRaises(ValueError):
            self.store.add_feedback(self.lid, "meh", "")

    def test_latest_wins_but_history_is_kept(self):
        """Changing your mind is signal, so both verdicts stay on file."""
        self.store.add_feedback(self.lid, "like", "looks good")
        self.store.add_feedback(self.lid, "dislike", "saw it, dark")
        self.assertEqual(len(self.store.feedback_for(self.lid)), 2)
        self.assertEqual(
            self.store.latest_feedback()[self.lid]["verdict"], "dislike")
        self.assertEqual(self.store.feedback_counts(),
                         {"like": 0, "dislike": 1})

    def test_snapshot_captures_the_score_as_judged(self):
        self.store.add_feedback(self.lid, "like", "")
        snap = json.loads(self.store.feedback_for(self.lid)[0]["snapshot"])
        self.assertEqual(snap["score"], 80.0)
        self.assertEqual(json.loads(snap["parts"])["walk"], 0.9)


class TestLearn(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = Store(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _add(self, i, verdict, walk_credit, reason=""):
        self.store.upsert({
            "source": "redfin", "source_id": str(i), "price": 2000, "beds": 2,
            "sqft": 1000, "prop_type": "house", "score": 70.0,
            "parts": {"walk": walk_credit, "price": 0.5},
            "lat": 30.30, "lon": -97.70})
        lid = [r for r in self.store.top(99) if r["source_id"] == str(i)][0]["id"]
        self.store.add_feedback(lid, verdict, reason)

    def test_thin_evidence_yields_no_suggestions(self):
        self._add(1, "like", 0.9)
        self._add(2, "dislike", 0.1)
        out = learn.analyse(self.store, CFG)
        self.assertFalse(out["enough"])
        self.assertEqual(out["suggestions"], [])

    def test_clear_signal_is_detected_and_bounded(self):
        for i in range(6):
            self._add(i, "like", 0.85 + i * 0.01)
        for i in range(6, 12):
            self._add(i, "dislike", 0.10 + i * 0.01)

        out = learn.analyse(self.store, CFG)
        self.assertTrue(out["enough"])
        walk_row = next(c for c in out["components"] if c["component"] == "walk")
        self.assertGreater(walk_row["effect"], 1.0)

        bump = next(s for s in out["suggestions"] if s["component"] == "walk")
        self.assertGreater(bump["to"], bump["from"])
        # A single round of feedback must not be able to run away with a weight.
        self.assertLessEqual(bump["to"], bump["from"] * (1 + learn.MAX_STEP))

    def test_noise_produces_no_suggestion(self):
        """Both groups looking alike must not be mistaken for a preference."""
        for i in range(6):
            self._add(i, "like", 0.5 + (i % 2) * 0.02)
        for i in range(6, 12):
            self._add(i, "dislike", 0.5 + (i % 2) * 0.02)
        out = learn.analyse(self.store, CFG)
        self.assertTrue(out["enough"])
        self.assertEqual(
            [s for s in out["suggestions"] if s["component"] == "walk"], [])

    def test_themes_ignore_one_offs_and_stopwords(self):
        for i in range(6):
            self._add(i, "like", 0.9, "the yard is great and the yard is big")
        for i in range(6, 12):
            self._add(i, "dislike", 0.1, "noisy street")
        out = learn.analyse(self.store, CFG)
        words = {t["word"] for t in out["themes"]["like"]}
        self.assertIn("yard", words)
        self.assertNotIn("the", words)

    def test_report_never_raises_on_an_empty_corpus(self):
        self.assertIn("feedback", learn.report(learn.analyse(self.store, CFG)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
