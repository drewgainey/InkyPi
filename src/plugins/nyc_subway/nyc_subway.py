from plugins.base_plugin.base_plugin import BasePlugin
from utils.http_client import get_http_session
from google.transit import gtfs_realtime_pb2
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import logging
import os
import pytz

logger = logging.getLogger(__name__)

FEED_BASE_URL = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs"
FEED_SUFFIXES = {
    "1": "", "2": "", "3": "", "4": "", "5": "", "6": "", "7": "",
    "A": "-ace", "C": "-ace", "E": "-ace",
    "B": "-bdfm", "D": "-bdfm", "F": "-bdfm", "M": "-bdfm",
    "G": "-g",
    "J": "-jz", "Z": "-jz",
    "L": "-l",
    "N": "-nqrw", "Q": "-nqrw", "R": "-nqrw", "W": "-nqrw",
    "SIR": "-si",
}

# Realtime route ids that differ from the bullet shown to riders
ROUTE_DISPLAY = {"GS": "S", "FS": "S", "H": "S", "SI": "SIR", "SS": "SIR"}

# Official MTA line colors, with text color for legibility
ROUTE_COLORS = {
    "1": "#EE352E", "2": "#EE352E", "3": "#EE352E",
    "4": "#00933C", "5": "#00933C", "6": "#00933C",
    "7": "#B933AD",
    "A": "#0039A6", "C": "#0039A6", "E": "#0039A6",
    "B": "#FF6319", "D": "#FF6319", "F": "#FF6319", "M": "#FF6319",
    "G": "#6CBE45",
    "J": "#996633", "Z": "#996633",
    "L": "#A7A9AC",
    "N": "#FCCC0A", "Q": "#FCCC0A", "R": "#FCCC0A", "W": "#FCCC0A",
    "S": "#808183", "SIR": "#0039A6",
}
DARK_TEXT_ROUTES = {"N", "Q", "R", "W"}

DIRECTIONS = {"N": "Northbound", "S": "Southbound"}
DEFAULT_MAX_ARRIVALS = 5
REQUEST_TIMEOUT = 15

_STATION_DATA = None


def load_station_data():
    global _STATION_DATA
    if _STATION_DATA is None:
        with open(os.path.join(os.path.dirname(__file__), "stations.json")) as f:
            _STATION_DATA = json.load(f)
    return _STATION_DATA


def feed_suffix_for(stop_id, route):
    """Returns the realtime feed suffix carrying a route at the given stop."""
    if route == "S":
        # 42 St shuttle is in the numbered-line feed; Franklin and Rockaway shuttles are in ACE
        return "" if stop_id.startswith("9") else "-ace"
    return FEED_SUFFIXES.get(route)


def display_route(route_id):
    """Maps a realtime route id to (bullet label, is_express)."""
    if route_id in ROUTE_DISPLAY:
        return ROUTE_DISPLAY[route_id], False
    if len(route_id) == 2 and route_id.endswith("X"):
        return route_id[0], True
    return route_id, False


def parse_arrivals(feed, stop_ids, stop_names, now_ts, min_minutes=0, routes_filter=None):
    """Extracts upcoming arrivals at the given parent stop ids from a GTFS-realtime FeedMessage.

    Returns a list of dicts with route, express flag, direction (N/S), destination, minutes and stop id.
    Trains that terminate at the stop are excluded since they can't be boarded.
    """
    arrivals = []
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        trip_update = entity.trip_update
        route, express = display_route(trip_update.trip.route_id)
        if routes_filter and route not in routes_filter:
            continue

        updates = trip_update.stop_time_update
        if not updates:
            continue
        terminal_id = updates[-1].stop_id

        for update in updates:
            platform_id = update.stop_id
            parent_id, direction = platform_id[:-1], platform_id[-1:]
            if parent_id not in stop_ids or direction not in DIRECTIONS:
                continue
            if platform_id == terminal_id:
                break

            arrival_ts = update.arrival.time or update.departure.time
            if not arrival_ts or arrival_ts < now_ts:
                break
            minutes = int((arrival_ts - now_ts) // 60)
            if minutes < min_minutes:
                break

            arrivals.append({
                "route": route,
                "express": express,
                "direction": direction,
                "destination": stop_names.get(terminal_id[:-1], terminal_id[:-1]),
                "minutes": minutes,
                "stop_id": parent_id,
                "timestamp": arrival_ts,
            })
            break
    return arrivals


def direction_label(stops, direction):
    """Uses the station's own label (e.g. "Uptown") when all its platforms agree, else a generic one."""
    key = "north" if direction == "N" else "south"
    labels = {stop[key] for stop in stops if stop[key] and stop[key] != "Last Stop"}
    if len(labels) == 1:
        return labels.pop()
    return DIRECTIONS[direction]


class NycSubway(BasePlugin):
    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params['stations'] = [
            {"id": s["id"], "name": s["name"], "borough": s["borough"], "routes": " ".join(s["routes"])}
            for s in load_station_data()["stations"]
        ]
        template_params['style_settings'] = True
        return template_params

    def generate_image(self, settings, device_config):
        station_id = settings.get("station")
        if not station_id:
            raise RuntimeError("Station is required.")

        data = load_station_data()
        station = next((s for s in data["stations"] if s["id"] == station_id), None)
        if not station:
            raise RuntimeError(f"Unknown station: {station_id}")

        routes_filter = {r.strip().upper() for r in settings.get("routes", "").replace(",", " ").split() if r.strip()}
        stops = [
            stop for stop in station["stops"]
            if not routes_filter or routes_filter.intersection(stop["routes"])
        ]
        if not stops:
            raise RuntimeError(f"None of the lines {', '.join(sorted(routes_filter))} stop at {station['name']}.")

        direction_setting = settings.get("direction", "both")
        directions = ["N", "S"] if direction_setting not in DIRECTIONS else [direction_setting]
        max_arrivals = self._int_setting(settings, "maxArrivals", DEFAULT_MAX_ARRIVALS, minimum=1)
        min_minutes = self._int_setting(settings, "minMinutes", 0, minimum=0)

        stop_ids = {stop["id"] for stop in stops}
        suffixes = {
            feed_suffix_for(stop["id"], route)
            for stop in stops for route in stop["routes"]
        } - {None}

        now_ts = int(datetime.now().timestamp())
        arrivals = self._fetch_arrivals(suffixes, stop_ids, data["stop_names"], now_ts, min_minutes, routes_filter)

        columns = []
        for direction in directions:
            # Skip a direction no platform serves, e.g. the outbound side of a terminal
            if all(stop["north" if direction == "N" else "south"] == "Last Stop" for stop in stops):
                continue
            direction_arrivals = sorted(
                (a for a in arrivals if a["direction"] == direction), key=lambda a: a["timestamp"]
            )[:max_arrivals]
            columns.append({
                "label": direction_label(stops, direction),
                "arrivals": [self._with_colors(a) for a in direction_arrivals],
            })

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        tz = pytz.timezone(device_config.get_config("timezone", default="America/New_York"))
        updated = datetime.now(tz).strftime("%I:%M %p").lstrip("0")

        template_params = {
            # Complexes join several platform names; show the one for the selected lines instead
            "station_name": settings.get("title") or data["stop_names"].get(stops[0]["id"], station["name"]),
            "routes": [self._with_colors({"route": r, "express": False}) for r in station["routes"]
                       if not routes_filter or r in routes_filter],
            "columns": columns,
            "updated": updated,
            "plugin_settings": settings,
        }
        return self.render_image(dimensions, "nyc_subway.html", "nyc_subway.css", template_params)

    def _fetch_arrivals(self, suffixes, stop_ids, stop_names, now_ts, min_minutes, routes_filter):
        session = get_http_session()

        def fetch(suffix):
            response = session.get(FEED_BASE_URL + suffix, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(response.content)
            return feed

        arrivals = []
        failures = 0
        with ThreadPoolExecutor(max_workers=len(suffixes)) as executor:
            futures = {suffix: executor.submit(fetch, suffix) for suffix in suffixes}
            for suffix, future in futures.items():
                try:
                    feed = future.result()
                except Exception as e:
                    failures += 1
                    logger.error(f"Failed to fetch MTA feed 'gtfs{suffix}': {e}")
                    continue
                arrivals.extend(parse_arrivals(feed, stop_ids, stop_names, now_ts, min_minutes, routes_filter))

        if failures == len(suffixes):
            raise RuntimeError("Failed to fetch MTA realtime data, please try again later.")
        return arrivals

    @staticmethod
    def _with_colors(arrival):
        route = arrival["route"]
        return {
            **arrival,
            "color": ROUTE_COLORS.get(route, "#808183"),
            "text_color": "#000000" if route in DARK_TEXT_ROUTES else "#FFFFFF",
        }

    @staticmethod
    def _int_setting(settings, key, default, minimum):
        try:
            return max(minimum, int(settings.get(key) or default))
        except (TypeError, ValueError):
            return default
