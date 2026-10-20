from google.transit import gtfs_realtime_pb2

from src.plugins.nyc_subway.nyc_subway import (
    direction_label,
    display_route,
    feed_suffix_for,
    parse_arrivals,
)

NOW = 1_700_000_000
STOP_NAMES = {"A27": "42 St-Port Authority Bus Terminal", "A02": "Inwood-207 St", "A55": "Euclid Av"}


def make_feed(trips):
    """trips: list of (route_id, [(platform_stop_id, arrival_ts), ...])"""
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "1.0"
    for i, (route_id, stops) in enumerate(trips):
        entity = feed.entity.add()
        entity.id = str(i)
        entity.trip_update.trip.route_id = route_id
        for stop_id, ts in stops:
            update = entity.trip_update.stop_time_update.add()
            update.stop_id = stop_id
            update.arrival.time = ts
    return feed


class TestParseArrivals:

    def test_extracts_arrival_with_destination_and_minutes(self):
        feed = make_feed([("A", [("A27N", NOW + 185), ("A02N", NOW + 1800)])])
        arrivals = parse_arrivals(feed, {"A27"}, STOP_NAMES, NOW)
        assert len(arrivals) == 1
        assert arrivals[0]["route"] == "A"
        assert arrivals[0]["direction"] == "N"
        assert arrivals[0]["destination"] == "Inwood-207 St"
        assert arrivals[0]["minutes"] == 3

    def test_skips_trains_terminating_at_stop(self):
        feed = make_feed([("A", [("A28N", NOW + 60), ("A27N", NOW + 185)])])
        assert parse_arrivals(feed, {"A27"}, STOP_NAMES, NOW) == []

    def test_skips_departed_trains_and_trains_inside_walk_time(self):
        feed = make_feed([
            ("A", [("A27S", NOW - 30), ("A55S", NOW + 1800)]),
            ("C", [("A27S", NOW + 120), ("A55S", NOW + 1800)]),
            ("E", [("A27S", NOW + 600), ("A55S", NOW + 1800)]),
        ])
        arrivals = parse_arrivals(feed, {"A27"}, STOP_NAMES, NOW, min_minutes=5)
        assert [a["route"] for a in arrivals] == ["E"]

    def test_route_filter_and_express_mapping(self):
        feed = make_feed([
            ("6X", [("631S", NOW + 300), ("640S", NOW + 900)]),
            ("7", [("723S", NOW + 300), ("726S", NOW + 900)]),
        ])
        arrivals = parse_arrivals(feed, {"631", "723"}, {}, NOW, routes_filter={"6"})
        assert len(arrivals) == 1
        assert arrivals[0]["route"] == "6"
        assert arrivals[0]["express"] is True


class TestHelpers:

    def test_display_route(self):
        assert display_route("GS") == ("S", False)
        assert display_route("H") == ("S", False)
        assert display_route("SI") == ("SIR", False)
        assert display_route("7X") == ("7", True)
        assert display_route("A") == ("A", False)

    def test_feed_suffix_for_shuttles(self):
        assert feed_suffix_for("902", "S") == ""
        assert feed_suffix_for("S01", "S") == "-ace"
        assert feed_suffix_for("H12", "S") == "-ace"
        assert feed_suffix_for("R16", "W") == "-nqrw"
        assert feed_suffix_for("S09", "SIR") == "-si"

    def test_direction_label(self):
        same = [{"north": "Uptown", "south": "Downtown"}, {"north": "Uptown", "south": "Downtown"}]
        mixed = same + [{"north": "Queens", "south": "Last Stop"}]
        assert direction_label(same, "N") == "Uptown"
        assert direction_label(mixed, "N") == "Northbound"
        assert direction_label(mixed, "S") == "Downtown"
