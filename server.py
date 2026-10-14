"""Serve the transit dashboard as an HTML page, as raw BGRA frames for the screensaver, and as raw
RGB565 framebuffer frames for the Pi. Headless Chromium renders each frame. A cron job on the Pi
copies one to `/dev/fb0` each minute
"""

import collections
import concurrent.futures
import csv
import datetime
import functools
import gzip
import http.server
import io
import json
import pathlib
import string
import time
import urllib.parse
import urllib.request
import zipfile

import numpy
import yaml
import zoneinfo
from google.transit import gtfs_realtime_pb2
from PIL import Image
from playwright.sync_api import sync_playwright

CONFIG_PATH = pathlib.Path(__file__).with_name("config.yaml")

MAX_MINUTES = 59
"""Hide trains that arrive later than this"""

FADE_AFTER_MINUTES = 5
"""Show trains at full opacity up to this many minutes after a station's
`minutesToPlatform`"""

FADE_UNTIL_MINUTES = 20
"""Fade trains linearly to `MIN_OPACITY` at this many minutes after a station's
`minutesToPlatform`"""

MIN_OPACITY = 0.3
"""Opacity of trains at `FADE_UNTIL_MINUTES` and later"""

NYC = zoneinfo.ZoneInfo("America/New_York")
"""Time zone for the operating hours and the PATH schedule"""

DIRECTIONS = {
    "DOWNTOWN": ("↓", "S"),
    "EAST": ("→", "0"),
    "NJ": ("←", "ToNJ"),
    "NY": ("→", "ToNY"),
    "UPTOWN": ("↑", "N"),
    "WEST": ("←", "1"),
}
"""Map each uppercase config direction to its arrow and its suffix, i.e. a subway stop ID suffix, an
LIRR direction ID, or a PATH API direction label"""

FEED_URL = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs"

FEED_SUFFIXES = ["", "-ace", "-bdfm", "-g", "-jz", "-l", "-nqrw", "-si"]
"""Suffixes of all subway feeds. We fetch all of them because service changes can send any line to
a stop"""

LIRR_URL = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/lirr%2Fgtfs-lirr"
"""Live LIRR trips"""

LIRR_SCHEDULE_URL = "https://rrgtfsfeeds.s3.amazonaws.com/gtfslirr.zip"
"""MTA's published LIRR GTFS schedule, for station names and branch colors"""

STATIONS_URL = "https://data.ny.gov/api/views/39hk-dx4f/rows.csv?accessType=DOWNLOAD"
"""MTA subway platforms with their names, daytime lines, and GTFS stop IDs"""

PATH_URL = "https://www.panynj.gov/bin/portauthority/ridepath.json"
"""Live PATH departures for all stations. The official RidePATH app uses this unofficial API"""

PATH_SCHEDULE_URL = (
    "https://rapid.nationalrtap.org/GTFSFileManagement/UserUploadFiles/14843/PATHGTFS.zip"
)
"""PATH's published GTFS schedule. Transitland lists it as the current source at
https://www.transit.land/feeds/f-dr5r-path~nj~us and shows that PATH uploads each new version (which
covers a few months) to this same URL. If it expires, the PATH rows show only live trains"""

PAGE = string.Template("""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="30">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
/* Scale the layout to fit the screen. In portrait, 1 column of stations fills the height and 3
   trains per row fill the width */
html { font-size: min(1vh, 1.78vw); }
/* Split the free space equally above and below the stations */
body { background: #000; color: #fff; display: flex; flex-direction: column; font-family: Helvetica, Arial, "Liberation Sans", sans-serif; height: 100vh; justify-content: space-evenly; padding: 0 1.5rem; }
main { display: flex; flex-direction: column; gap: 1.8rem; }
h2 { color: #777; font-size: 2rem; font-weight: 400; }
/* Fit as many trains as the width allows (13.5rem fits a bullet and 2 digits). Hide the rest in
   0-height extra rows, and clip each train so that hidden ones don't spill out of their rows.
   `auto-fill` needs a fixed-width arrow column */
ol { column-gap: 2rem; display: grid; font-size: 6rem; font-variant-numeric: tabular-nums; font-weight: 700; grid-auto-rows: 0; grid-template-columns: 5.2rem repeat(auto-fill, minmax(13.5rem, 1fr)); grid-template-rows: auto; line-height: 1.05; list-style: none; overflow: hidden; }
ol::before { color: #777; content: attr(data-arrow); font-weight: 400; text-align: center; width: 5.2rem; }
li { align-items: center; display: flex; gap: 1.2rem; overflow: hidden; }
/* Official MTA line colors */
.bullet { align-items: center; background: #808183; border-radius: 50%; display: inline-flex; font-size: 3.2rem; height: 4.8rem; justify-content: center; width: 4.8rem; }
.r1, .r2, .r3 { background: #ee352e; }
.r4, .r5, .r6 { background: #00933c; }
.r7 { background: #b933ad; }
.rA, .rC, .rE { background: #0039a6; }
.rB, .rD, .rF, .rM { background: #ff6319; }
.rG { background: #6cbe45; }
.rJ, .rZ { background: #996633; }
.rL { background: #a7a9ac; }
.rN, .rQ, .rR, .rW { background: #fccc0a; color: #000; }
/* In landscape, flow the stations into 2 balanced columns. 3 trains per row then fill half the
   width, and the taller column of stations fills about 90% of the height. Skip the last station's
   margin (like `gap` in portrait) so that it doesn't push the stations above center */
@media (orientation: landscape) {
  html { font-size: min(0.83vw, 1.6vh); }
  main { column-gap: 6rem; columns: 2; display: block; }
  section { break-inside: avoid; }
  section:not(:last-child) { margin-bottom: 1.8rem; }
}
</style>
</head>
<body>
<main>$sections</main>
</body>
</html>
""")
"""Dashboard layout. Sizes use `rem` so the page scales to any screen"""


@functools.cache
def mta_platforms() -> list[dict]:
    """Download the MTA platform list once"""
    print("Downloading MTA stations...")
    return list(
        csv.DictReader(
            io.TextIOWrapper(urllib.request.urlopen(STATIONS_URL, timeout=30), "utf-8-sig")
        )
    )


def read_gtfs(url: str, names: list[str]) -> list[list[dict]]:
    """Download a GTFS zip file and read the given tables, e.g. "stops" for `stops.txt`"""
    with zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(url, timeout=30).read())) as z:
        return [
            list(csv.DictReader(io.TextIOWrapper(z.open(f"{n}.txt"), "utf-8-sig"))) for n in names
        ]


@functools.cache
def lirr_stations() -> tuple[dict, dict]:
    """Download the LIRR schedule once. Return a map of stop IDs to station names and a map of route
    IDs to branch colors
    """
    print("Downloading LIRR stations...")
    stops, routes = read_gtfs(LIRR_SCHEDULE_URL, ["stops", "routes"])
    return {s["stop_id"]: s["stop_name"] for s in stops}, {
        r["route_id"]: r["route_color"] for r in routes
    }


@functools.lru_cache(maxsize=2)
def path_schedule(date: datetime.date) -> tuple[dict, dict]:
    """Download the PATH schedule once per date. Return a map of station names to codes such as
    "WTC", and a map of (station code, destination code) pairs such as ("WTC", "HOB") to sorted
    (timestamp, color) pairs of scheduled departures on the date
    """
    print("Downloading PATH schedule for", date, "...")
    calendar, calendar_dates, routes, stops, trips, stop_times = read_gtfs(
        PATH_SCHEDULE_URL, ["calendar", "calendar_dates", "routes", "stops", "trips", "stop_times"]
    )

    # Find the services that run on the date
    day = date.strftime("%Y%m%d")
    services = {
        c["service_id"]
        for c in calendar
        if c[date.strftime("%A").lower()] == "1" and c["start_date"] <= day <= c["end_date"]
    }
    for d in calendar_dates:
        if d["date"] == day:
            (services.add if d["exception_type"] == "1" else services.discard)(d["service_id"])

    # Map stop IDs to station codes. Each station has a "place_<code>" stop with its platforms' name
    codes = {
        s["stop_name"]: s["stop_id"].removeprefix("place_")
        for s in stops
        if s["stop_id"].startswith("place_")
    }
    station_codes = {s["stop_id"]: codes[s["stop_name"]] for s in stops}

    # Map the IDs of trips that run on the date to their route colors
    colors = {r["route_id"]: r["route_color"] for r in routes}
    trip_colors = {
        t["trip_id"]: colors[t["route_id"]] for t in trips if t["service_id"] in services
    }

    # Group the stop times of those trips by trip
    trip_stops = collections.defaultdict(list)
    for s in stop_times:
        if s["trip_id"] in trip_colors:
            trip_stops[s["trip_id"]].append(s)

    # Add each departure except at the last stop and at stops without pickup (`pickup_type` 1), e.g.
    # 23rd Street for some late-night trains. GTFS times count from "noon minus 12h" (i.e.
    # midnight except on DST change days) and can exceed 24:00:00
    start = datetime.datetime.combine(date, datetime.time(12), NYC).timestamp() - 12 * 3600
    departures = collections.defaultdict(list)
    for trip_id, ss in trip_stops.items():
        ss.sort(key=lambda s: int(s["stop_sequence"]))
        for s in ss[:-1]:
            if s.get("pickup_type") == "1":
                continue
            h, m, sec = map(int, s["departure_time"].split(":"))
            departures[(station_codes[s["stop_id"]], station_codes[ss[-1]["stop_id"]])].append(
                (start + h * 3600 + m * 60 + sec, trip_colors[trip_id])
            )
    return codes, {k: sorted(v) for k, v in departures.items()}


def render_html(config: dict) -> str:
    """Build the dashboard page from live subway, LIRR, and PATH data"""

    def load(f, default, *args):
        """Treat a failed download or parse as empty so the other lines still show. The caches don't
        keep errors, so the next request retries
        """
        try:
            return f(*args)
        except Exception as e:
            print("Failed to load", f.__name__, *args, e)
            return default

    def fetch(url: str):
        """Download a live feed and parse its entities, or the PATH API's trains"""
        # The PATH API blocks the default user agent with "HTTP Error 403: Forbidden"
        data = urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": "nyc-subway-dashboard"}),
            timeout=10,
        ).read()
        if url != PATH_URL:
            return gtfs_realtime_pb2.FeedMessage.FromString(data).entity

        # Flatten the PATH API's trains into (station code, direction, destination code, timestamp,
        # color) here, so that `load()` also catches errors from a changed response format. Each
        # countdown starts at its update time. Use the first color for combined lines such as
        # "4D92FB,FF9900"
        return [
            (
                r["consideredStation"],
                d["label"],
                m["target"],
                datetime.datetime.fromisoformat(m["lastUpdated"]).timestamp()
                + int(m["secondsToArrival"]),
                m["lineColor"].split(",")[0],
            )
            for r in json.loads(data)["results"]
            for d in r["destinations"]
            for m in d["messages"]
        ]

    # Get the subway platforms, LIRR stations, today's and tomorrow's PATH schedules, and all live
    # feeds in parallel. PATH's schedule lists trains after midnight under the next day
    today = datetime.datetime.now(NYC).date()
    with concurrent.futures.ThreadPoolExecutor() as executor:
        (
            platforms,
            (lirr_names, lirr_colors),
            (path_codes, schedule),
            (_, next_schedule),
            *mta_feeds,
            lirr_feed,
            path_feed,
        ) = executor.map(
            lambda a: load(*a),
            [
                (mta_platforms, []),
                (lirr_stations, ({}, {})),
                (path_schedule, ({}, {}), today),
                (path_schedule, ({}, {}), today + datetime.timedelta(days=1)),
            ]
            + [
                (fetch, [], u) for u in [FEED_URL + s for s in FEED_SUFFIXES] + [LIRR_URL, PATH_URL]
            ],
        )

    # Read the clock after the downloads (up to 30s each on a cold cache) so countdowns aren't stale
    now = time.time()

    # Index (timestamp, bullet label, bullet HTML attributes) by stop ID. Skip each trip's last stop
    # because the user can't board there. Use the departure time if there is no arrival (e.g. at
    # the first stop). Strip the "X" from express routes such as "6X". Show the "FS", "GS", and "H"
    # shuttles as a gray "S" bullet instead of e.g. a green "GS" bullet
    trains = collections.defaultdict(list)
    for feed in mta_feeds:
        for entity in feed:
            route = (
                "S"
                if (r := entity.trip_update.trip.route_id) in ("FS", "GS", "H")
                else r.rstrip("X")
            )
            for update in entity.trip_update.stop_time_update[:-1]:
                trains[update.stop_id].append(
                    (
                        update.arrival.time or update.departure.time,
                        route,
                        f'class="bullet r{route[:1]}"',
                    )
                )

    # Index LIRR trains by "LIRR:<stop ID>:<direction ID>", where 0 is east and 1 is west. Like PATH
    # trains, label each with its destination's first letter in its branch color
    for entity in lirr_feed:
        trip, updates = entity.trip_update.trip, entity.trip_update.stop_time_update
        for update in updates[:-1]:
            trains[f"LIRR:{update.stop_id}:{trip.direction_id}"].append(
                (
                    update.arrival.time or update.departure.time,
                    lirr_names.get(updates[-1].stop_id, "?")[:1],
                    f'class="bullet" style="background: #{lirr_colors.get(trip.route_id, "808183")}"',
                )
            )

    # Group live PATH trains by station, direction, and destination code
    live = collections.defaultdict(list)
    for code, label, target, t, color in path_feed:
        live[(code, label, target)].append((t, color))

    # The API lists only the next 2 trains per destination. Add the scheduled trains after the one
    # closest to the last live train. Index all by "PATH:<station>:<direction>". Label each with its
    # destination's first letter (e.g. "N" for "NWK") to fit a bullet
    for (code, label, target), departures in live.items():
        scheduled = schedule.get((code, target), []) + next_schedule.get((code, target), [])
        trains[f"PATH:{code}:{label}"] += [
            (t, target[:1], f'class="bullet" style="background: #{color}"')
            for t, color in departures
            + scheduled[
                min(
                    range(len(scheduled)),
                    key=lambda i: abs(scheduled[i][0] - max(departures)[0]),
                    default=-1,
                )
                + 1 :
            ]
        ]

    # Build the rows. Hide trains that leave before the user can get to the station or after
    # `MAX_MINUTES`. Fade trains linearly from full opacity at `FADE_AFTER_MINUTES` to `MIN_OPACITY`
    # at `FADE_UNTIL_MINUTES`, both counted from the station's `minutesToPlatform`
    sections = ""
    for station in config["stations"]:
        rows = ""
        for platform in station["platforms"]:
            # Parse a platform such as "R/W", "downtown 2/3", or "NJ PATH" into an optional
            # direction and slash-delimited lines. Uppercase it so that e.g. "Uptown a/c" works too.
            # Without a direction, show uptown then downtown (or west then east for the LIRR, or NJ
            # then NY for the PATH)
            given, _, names = platform.upper().rpartition(" ")
            lines = set(names.split("/"))
            for direction in (
                [given]
                if given
                else {"LIRR": ["WEST", "EAST"], "PATH": ["NJ", "NY"]}.get(
                    names, ["UPTOWN", "DOWNTOWN"]
                )
            ):
                arrow, suffix = DIRECTIONS[direction]

                # Find the row's trains by PATH station code, by LIRR stop IDs, or by the subway
                # platforms at the station that serve any of the lines. The lines only pick the
                # platform. The row shows every train that stops there
                keys = (
                    [f"PATH:{path_codes.get(station['name'])}:{suffix}"]
                    if lines == {"PATH"}
                    else [
                        f"LIRR:{i}:{suffix}" for i, n in lirr_names.items() if n == station["name"]
                    ]
                    if lines == {"LIRR"}
                    else [
                        p["GTFS Stop ID"] + suffix
                        for p in platforms
                        if p["Stop Name"] == station["name"]
                        and lines & set(p["Daytime Routes"].split())
                    ]
                )
                # Skip rows without trains. The arrows still show each row's direction
                if items := "".join(
                    f'<li style="opacity: {1 - (1 - MIN_OPACITY) * min(1, max(0, (m := int((t - now) // 60)) - station["minutesToPlatform"] - FADE_AFTER_MINUTES) / (FADE_UNTIL_MINUTES - FADE_AFTER_MINUTES)):.2f}"><span {a}>{label}</span>{m}</li>'
                    for t, label, a in sorted(
                        t
                        for k in keys
                        for t in trains[k]
                        if station["minutesToPlatform"] * 60 <= t[0] - now < (MAX_MINUTES + 1) * 60
                    )
                ):
                    rows += f'<ol data-arrow="{arrow}">{items}</ol>'

        # Hide stations without rows
        if rows:
            sections += f"<section><h2>{station['name']}</h2>{rows}</section>"

    return PAGE.substitute(sections=sections)


class Handler(http.server.BaseHTTPRequestHandler):
    """Serve `/` for browser previews, `/frame` for the Pi, and `/frame.bgra` for the screensaver"""

    timeout = 10
    """Close idle connections after this many seconds. Browsers pre-open sockets that otherwise block
    this single-threaded server (https://bugs.python.org/issue31639)"""

    def do_GET(self):
        # Read the config on each request so edits apply without a restart
        config = yaml.safe_load(CONFIG_PATH.read_text())

        # Serve the page for previews in a normal browser
        if (url := urllib.parse.urlsplit(self.path)).path == "/":
            self.reply(render_html(config).encode(), "text/html; charset=utf-8")
            return

        # Reject other paths. `/frame` is for the Pi and `/frame.bgra` is for the screensaver
        if url.path not in ["/frame", "/frame.bgra"]:
            self.send_error(404)
            return

        # Tell the Pi or the screensaver to turn off the screen outside the operating hours, if any.
        # Each [start, end] window can span midnight. Parse the times so that e.g. "9:00" works too
        clock = datetime.datetime.now(NYC).time()
        if "operatingHours" in config and not any(
            start <= clock < end if start < end else clock >= start or clock < end
            for start, end in (
                [datetime.datetime.strptime(t, "%H:%M").time() for t in w]
                for w in config["operatingHours"]
            )
        ):
            self.send_response(204)
            self.end_headers()
            return

        # Screenshot the page at the requested screen size, e.g. "?size=1920,1080". For the Pi, swap
        # the page's width and height for a portrait monitor, i.e. a rotation of 90 or 270 degrees
        width, height = map(int, urllib.parse.parse_qs(url.query)["size"][0].split(","))
        rotation = config.get("displayRotateDegrees", 0) if url.path == "/frame" else 0
        page.set_viewport_size(
            {"height": width, "width": height}
            if rotation % 180
            else {"height": height, "width": width}
        )
        page.set_content(render_html(config))
        image = Image.open(io.BytesIO(page.screenshot()))

        # Send raw BGRA pixels to the screensaver, which is X11's 32-bit pixel layout on
        # little-endian machines, so that it can draw them without decoding
        if url.path == "/frame.bgra":
            self.reply(image.convert("RGBA").tobytes("raw", "BGRA"), "application/octet-stream")
            return

        # Rotate the screenshot counterclockwise to fit the Pi's framebuffer
        rgb = numpy.asarray(image.convert("RGB").rotate(rotation, expand=True), dtype=numpy.uint16)

        # Pack the pixels into the Pi's 16-bit RGB565 framebuffer format
        self.reply(
            ((rgb[..., 0] >> 3) << 11 | (rgb[..., 1] >> 2) << 5 | rgb[..., 2] >> 3)
            .astype("<u2")
            .tobytes(),
            "application/octet-stream",
        )

    def reply(self, body: bytes, content_type: str):
        """Send a 200 response with the given body"""
        # Send the status line
        self.send_response(200)

        # Compress the body if the client accepts gzip. Frames are mostly black, so this shrinks
        # them by about 98% (e.g. 4.1MB to 74KB). Level 6 takes about 17ms vs 68ms for the default
        # level 9 and gives a similar size
        if "gzip" in self.headers.get("Accept-Encoding", ""):
            body = gzip.compress(body, 6)
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    # Use one browser page for all requests. A single-threaded server keeps Playwright on one thread
    with sync_playwright() as playwright:
        page = playwright.chromium.launch().new_page()
        print("Starting server...")
        http.server.HTTPServer(
            ("", yaml.safe_load(CONFIG_PATH.read_text())["port"]), Handler
        ).serve_forever()
