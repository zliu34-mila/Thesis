import csv
import io
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


HOST = "127.0.0.1"
PORT = 8000
SERVER_VERSION = "3"
FIRMS_SOURCES = (
    "VIIRS_SNPP_NRT",
    "VIIRS_NOAA20_NRT",
    "VIIRS_NOAA21_NRT",
    "MODIS_NRT",
)
CALIFORNIA_BOUNDS = "-125,32,-114,42"
FIRMS_MAP_KEY = os.environ.get("FIRMS_MAP_KEY", "").strip()


class FireAtlasHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        page_path = {
            "/": "index.html",
            "/index.html": "index.html",
            "/transform": "transform.html",
            "/transform.html": "transform.html",
            "/generate": "generate.html",
            "/generate.html": "generate.html",
        }.get(self.path)
        if page_path is None:
            self.send_error(404)
            return

        try:
            with open(page_path, "rb") as page:
                content = page.read()
        except OSError:
            self.send_error(500, f"Could not read {page_path}.")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Fire-Atlas-Version", SERVER_VERSION)
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        if self.path == "/api/weather":
            self._handle_weather_request()
            return
        if self.path != "/api/fires":
            self.send_error(404)
            return

        if not FIRMS_MAP_KEY:
            self._send_response(
                503,
                b"Set the FIRMS_MAP_KEY environment variable before starting the server.",
                "text/plain; charset=utf-8",
            )
            return

        headers = []
        records = []

        for source in FIRMS_SOURCES:
            url = (
                "https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
                f"{quote(FIRMS_MAP_KEY, safe='')}/{source}/{CALIFORNIA_BOUNDS}/2"
            )
            request = Request(url, headers={"Accept": "text/csv"})

            try:
                with urlopen(request, timeout=30) as response:
                    csv_text = response.read().decode("utf-8-sig")
            except HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace").strip()
                message = f"NASA FIRMS request failed for {source} (HTTP {error.code})."
                if detail:
                    message = f"{message} {detail}"
                self._send_response(502, message.encode(), "text/plain; charset=utf-8")
                return
            except (TimeoutError, URLError) as error:
                reason = error.reason if isinstance(error, URLError) else "request timed out"
                message = f"Could not retrieve {source} from NASA FIRMS: {reason}"
                self._send_response(502, message.encode(), "text/plain; charset=utf-8")
                return

            rows = csv.reader(io.StringIO(csv_text))
            try:
                source_headers = [header.strip().lower() for header in next(rows)]
            except StopIteration:
                self._send_response(
                    502,
                    f"NASA FIRMS returned an empty response for {source}.".encode(),
                    "text/plain; charset=utf-8",
                )
                return

            if "latitude" not in source_headers or "longitude" not in source_headers:
                self._send_response(
                    502,
                    f"NASA FIRMS returned an invalid CSV response for {source}.".encode(),
                    "text/plain; charset=utf-8",
                )
                return

            for header in source_headers:
                if header not in headers:
                    headers.append(header)

            for row in rows:
                if not row or all(not value.strip() for value in row):
                    continue
                if len(row) != len(source_headers):
                    self._send_response(
                        502,
                        f"NASA FIRMS returned a malformed CSV row for {source}.".encode(),
                        "text/plain; charset=utf-8",
                    )
                    return
                records.append(dict(zip(source_headers, row)))

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=headers, lineterminator="\n")
        writer.writeheader()
        writer.writerows(records)
        self._send_response(200, output.getvalue().encode(), "text/csv; charset=utf-8")

    def _handle_weather_request(self):
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_response(400, b"Invalid request content length.", "text/plain; charset=utf-8")
            return
        if content_length <= 0 or content_length > 4096:
            self._send_response(400, b"Invalid weather request size.", "text/plain; charset=utf-8")
            return

        try:
            payload = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_response(400, b"Weather request must contain valid JSON.", "text/plain; charset=utf-8")
            return

        if not isinstance(payload, dict):
            self._send_response(400, b"Weather request must be a JSON object.", "text/plain; charset=utf-8")
            return

        latitude = payload.get("latitude")
        longitude = payload.get("longitude")
        if (
            isinstance(latitude, bool)
            or not isinstance(latitude, (int, float))
            or not -90 <= latitude <= 90
            or isinstance(longitude, bool)
            or not isinstance(longitude, (int, float))
            or not -180 <= longitude <= 180
        ):
            self._send_response(400, b"Weather request coordinates are invalid.", "text/plain; charset=utf-8")
            return

        query = urlencode({
            "latitude": f"{latitude:.6f}",
            "longitude": f"{longitude:.6f}",
            "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m",
            "timezone": "UTC",
            "wind_speed_unit": "kmh",
        })
        request = Request(
            f"https://api.open-meteo.com/v1/forecast?{query}",
            headers={"Accept": "application/json"},
        )

        try:
            with urlopen(request, timeout=20) as response:
                content = response.read()
            data = json.loads(content)
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace").strip()
            message = f"Open-Meteo request failed (HTTP {error.code})."
            if detail:
                message = f"{message} {detail}"
            self._send_response(502, message.encode(), "text/plain; charset=utf-8")
            return
        except (TimeoutError, URLError) as error:
            reason = error.reason if isinstance(error, URLError) else "request timed out"
            self._send_response(
                502,
                f"Could not reach Open-Meteo: {reason}".encode(),
                "text/plain; charset=utf-8",
            )
            return
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_response(502, b"Open-Meteo returned invalid JSON.", "text/plain; charset=utf-8")
            return

        current = data.get("current") if isinstance(data, dict) else None
        required_fields = (
            "time",
            "temperature_2m",
            "relative_humidity_2m",
            "wind_speed_10m",
            "wind_direction_10m",
        )
        if not isinstance(current, dict) or any(field not in current for field in required_fields):
            self._send_response(502, b"Open-Meteo response is missing current weather fields.", "text/plain; charset=utf-8")
            return

        self._send_response(200, json.dumps(data).encode(), "application/json; charset=utf-8")

    def _send_response(self, status, content, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Fire-Atlas-Version", SERVER_VERSION)
        self.end_headers()
        self.wfile.write(content)


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), FireAtlasHandler)
    print(f"California Fire Atlas available at http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping California Fire Atlas.")
    finally:
        server.server_close()
