import json
import urllib.request
from datetime import datetime
from pathlib import Path

LAT = 31.605964561819555
LON = -94.64340711642238

CACHE_FILE = Path.home() / "meshcore-bbs" / "weather_cache.json"

USER_AGENT = "NAC-BBS/1.0 (andy@andydelgado.com)"


def get_json(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/geo+json",
        },
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def c_to_f(celsius):
    if celsius is None:
        return None

    return round((celsius * 9 / 5) + 32)


def main():
    print("Contacting National Weather Service...")

    point_url = f"https://api.weather.gov/points/{LAT},{LON}"
    point_data = get_json(point_url)

    props = point_data["properties"]

    forecast_url = props["forecast"]
    stations_url = props["observationStations"]

    forecast_data = get_json(forecast_url)

    periods = forecast_data["properties"]["periods"]

    station_data = get_json(stations_url)
    stations = station_data["features"]

    observation = None
    station_id = None

    for station in stations[:8]:
        candidate_id = station["properties"].get("stationIdentifier")

        if not candidate_id:
            continue

        obs_url = (
            f"https://api.weather.gov/stations/"
            f"{candidate_id}/observations/latest"
        )

        try:
            candidate_obs = get_json(obs_url)

            candidate_props = candidate_obs.get("properties", {})
            temp_value = candidate_props.get(
                "temperature", {}
            ).get("value")

            description = candidate_props.get("textDescription")

            if temp_value is not None or description:
                observation = candidate_obs
                station_id = candidate_id
                print(
                    f"Using observation station: {station_id}"
                )
                break

        except Exception as exc:
            print(
                f"Skipping station {candidate_id}: {exc}"
            )


    current = {}

    if observation:
        obs_props = observation["properties"]

        current = {
            "station": station_id,
            "timestamp": obs_props.get("timestamp"),
            "description": obs_props.get("textDescription"),
            "temperature_f": c_to_f(
                obs_props.get("temperature", {}).get("value")
            ),
            "wind_speed_kph": obs_props.get(
                "windSpeed", {}
            ).get("value"),
            "wind_direction_degrees": obs_props.get(
                "windDirection", {}
            ).get("value"),
        }

    forecast_periods = []

    for period in periods[:4]:
        forecast_periods.append(
            {
                "name": period.get("name"),
                "temperature": period.get("temperature"),
                "temperature_unit": period.get(
                    "temperatureUnit"
                ),
                "wind_speed": period.get("windSpeed"),
                "wind_direction": period.get(
                    "windDirection"
                ),
                "short_forecast": period.get(
                    "shortForecast"
                ),
                "detailed_forecast": period.get(
                    "detailedForecast"
                ),
            }
        )

    cache = {
        "location": {
            "lat": LAT,
            "lon": LON,
        },
        "updated_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "forecast_generated_at": forecast_data[
            "properties"
        ].get("generatedAt"),
        "forecast_office": props.get("cwa"),
        "current": current,
        "forecast": forecast_periods,
    }

    CACHE_FILE.write_text(
        json.dumps(cache, indent=2)
    )

    print(f"Weather cache updated: {CACHE_FILE}")

    if current:
        print(
            f"Current: "
            f"{current.get('temperature_f')} F, "
            f"{current.get('description')}"
        )

    for period in forecast_periods[:2]:
        print(
            f"{period['name']}: "
            f"{period['temperature']} "
            f"{period['temperature_unit']}, "
            f"{period['short_forecast']}"
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Weather update FAILED: {exc}")
        raise

