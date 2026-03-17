from __future__ import annotations

import logging

import httpx

from .config import Settings

log = logging.getLogger(__name__)

# WMO weather code descriptions
WEATHER_CODES = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "foggy",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    61: "slight rain",
    63: "moderate rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "heavy freezing rain",
    71: "slight snow",
    73: "moderate snow",
    75: "heavy snow",
    77: "snow grains",
    80: "slight rain showers",
    81: "moderate rain showers",
    82: "violent rain showers",
    85: "slight snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}

POLLEN_LEVELS = {0: "None", 1: "Low", 2: "Moderate", 3: "High", 4: "Very High"}


async def get_weather(settings: Settings, client: httpx.AsyncClient) -> str | None:
    """Fetch weather from Open-Meteo."""
    try:
        resp = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": settings.latitude,
                "longitude": settings.longitude,
                "current": "temperature_2m,apparent_temperature,weather_code",
                "daily": "temperature_2m_max,temperature_2m_min,weather_code",
                "timezone": settings.timezone,
                "forecast_days": 2,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        current = data.get("current", {})
        daily = data.get("daily", {})

        temp = current.get("temperature_2m")
        feels = current.get("apparent_temperature")
        code = current.get("weather_code", 0)
        condition = WEATHER_CODES.get(code, "")

        hi = daily.get("temperature_2m_max", [None])[0]
        lo = daily.get("temperature_2m_min", [None])[0]
        daily_code = daily.get("weather_code", [0])[0]
        daily_condition = WEATHER_CODES.get(daily_code, "")

        location = settings.location_name

        # Today
        today_parts = []
        if temp is not None:
            s = f"{temp:.0f}°C"
            if feels is not None and abs(feels - temp) >= 2:
                s += f" feels like {feels:.0f}°C"
            today_parts.append(s)
        if condition:
            today_parts.append(condition)
        if daily_condition and daily_condition != condition:
            today_parts.append(f"{daily_condition} later")
        if hi is not None and lo is not None:
            today_parts.append(f"high {hi:.0f}°C low {lo:.0f}°C")

        result_parts = []
        if today_parts:
            result_parts.append(f"{location} today: " + ", ".join(today_parts) + ".")

        # Tomorrow (if available)
        tomorrow_hi = daily.get("temperature_2m_max", [None, None])
        tomorrow_lo = daily.get("temperature_2m_min", [None, None])
        tomorrow_code = daily.get("weather_code", [0, 0])
        if len(tomorrow_hi) > 1 and tomorrow_hi[1] is not None:
            tmrw_parts = []
            tmrw_condition = WEATHER_CODES.get(tomorrow_code[1], "")
            if tmrw_condition:
                tmrw_parts.append(tmrw_condition)
            if tomorrow_hi[1] is not None and tomorrow_lo[1] is not None:
                tmrw_parts.append(f"high {tomorrow_hi[1]:.0f}°C low {tomorrow_lo[1]:.0f}°C")
            if tmrw_parts:
                result_parts.append("Tomorrow: " + ", ".join(tmrw_parts) + ".")

        return "\n".join(result_parts) if result_parts else None

    except Exception as e:
        log.error("Weather fetch failed: %s", e)
        return None


async def get_pollen(settings: Settings, client: httpx.AsyncClient) -> str | None:
    """Fetch pollen data from pollen.mydoglog.ca."""
    try:
        resp = await client.get(
            "https://pollen.mydoglog.ca/api/nearest",
            params={
                "lat": settings.latitude,
                "lng": settings.longitude,
                "provider": "aerobiology",
            },
        )
        resp.raise_for_status()
        data = resp.json()

        # Data is nested under readings[0]
        readings = data.get("readings", [])
        if not readings:
            return None
        reading = readings[0]

        if reading.get("out_of_season"):
            return None

        level = reading.get("pollen_level", 0)
        level_name = POLLEN_LEVELS.get(level, "Unknown")
        if level == 0:
            return None

        parts = [f"Pollen: {level_name}"]

        # Add dominant categories
        categories = []
        if reading.get("total_trees", 0) > 0:
            categories.append("trees")
        if reading.get("total_grasses", 0) > 0:
            categories.append("grasses")
        if reading.get("total_weeds", 0) > 0:
            categories.append("weeds")
        if categories:
            parts[0] += f" ({', '.join(categories)})"

        # Add elevated species (level >= 3)
        species_list = reading.get("species", [])
        elevated = [
            s["name"].split(",")[0]  # Take first common name
            for s in species_list
            if s.get("type") == "pollen" and s.get("level", 0) >= 3
        ]
        if elevated:
            parts.append(f"{' and '.join(elevated[:3])} elevated")

        return ". ".join(parts) + "."

    except Exception as e:
        log.error("Pollen fetch failed: %s", e)
        return None
