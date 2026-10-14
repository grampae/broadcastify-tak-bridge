"""Optional transcript location resolution."""

from __future__ import annotations

import logging
import math
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import requests
import yaml


log = logging.getLogger(__name__)

ADDRESS_PATTERN = re.compile(
    r"\b\d{1,6}\s+"
    r"(?:north|south|east|west|n\.?|s\.?|e\.?|w\.?)?\s*"
    r"(?:[a-zA-Z0-9'-]+\s+){0,4}"
    r"(?:street|avenue|road|drive|way|boulevard|lane|court|place|highway|"
    r"route|st|ave|rd|dr|blvd|ln|ct|pl)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ResolvedLocation:
    latitude: float
    longitude: float
    name: str
    precision: str

    def __post_init__(self) -> None:
        if not math.isfinite(self.latitude) or not -90 <= self.latitude <= 90:
            raise ValueError("Latitude must be between -90 and 90")
        if not math.isfinite(self.longitude) or not -180 <= self.longitude <= 180:
            raise ValueError("Longitude must be between -180 and 180")


def load_location_file(path: str | Path | None) -> tuple[list[dict], dict[str, str]]:
    if not path:
        return [], {}
    location_path = Path(path)
    if not location_path.is_file():
        log.warning("Location file does not exist: %s", location_path)
        return [], {}

    try:
        data = yaml.safe_load(location_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Cannot load location file {location_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("The location file root must be a mapping")

    locations = data.get("locations", [])
    if not isinstance(locations, list):
        raise ValueError("locations must be a list")
    normalized: list[dict] = []
    for index, item in enumerate(locations):
        if not isinstance(item, dict):
            raise ValueError(f"locations[{index}] must be a mapping")
        try:
            name = str(item["name"]).strip()
            latitude = float(item["latitude"])
            longitude = float(item["longitude"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"locations[{index}] has invalid coordinates or name") from exc
        if not name or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise ValueError(f"locations[{index}] has invalid coordinates or name")
        aliases = [str(value).strip() for value in item.get("aliases", []) if str(value).strip()]
        normalized.append(
            {
                "name": name,
                "latitude": latitude,
                "longitude": longitude,
                "aliases": aliases,
            }
        )

    raw_unit_map = data.get("unit_map", {})
    if not isinstance(raw_unit_map, dict):
        raise ValueError("unit_map must be a mapping")
    unit_map = {str(key): str(value) for key, value in raw_unit_map.items()}
    return normalized, unit_map


class LocationResolver:
    def __init__(self, config: dict):
        self.locations, self.unit_map = load_location_file(config.get("file"))
        self.address_database = str(config.get("address_database", "")).strip()
        self.geocoding = config.get("geocoding", {})

        self._lookup: dict[str, ResolvedLocation] = {}
        for item in self.locations:
            resolved = ResolvedLocation(
                item["latitude"], item["longitude"], item["name"], "approximate"
            )
            for value in [item["name"], *item["aliases"]]:
                self._lookup[value.lower()] = resolved

    def apply_unit_map(self, transcript: str) -> str:
        result = transcript
        for spoken, canonical in self.unit_map.items():
            result = re.sub(
                r"\b" + re.escape(spoken) + r"\b",
                canonical,
                result,
                flags=re.IGNORECASE,
            )
        return result

    def resolve(self, transcript: str) -> ResolvedLocation | None:
        address_match = ADDRESS_PATTERN.search(transcript)
        if address_match:
            address = address_match.group(0)
            local = self._lookup_address(address)
            if local:
                return local
            remote = self._remote_geocode(address)
            if remote:
                return remote

        lowered = transcript.lower()
        matches = [key for key in self._lookup if key in lowered]
        if matches:
            return self._lookup[max(matches, key=len)]
        return None

    def _lookup_address(self, address: str) -> ResolvedLocation | None:
        if not self.address_database:
            return None
        path = Path(self.address_database)
        if not path.is_file():
            return None

        normalized = " ".join(address.upper().split())
        try:
            with sqlite3.connect(path) as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    "SELECT lat, lon, address FROM addresses "
                    "WHERE UPPER(address) = ? LIMIT 1",
                    (normalized,),
                ).fetchone()
        except sqlite3.Error as exc:
            log.warning("Address database lookup failed: %s", exc)
            return None
        if not row:
            return None
        return ResolvedLocation(
            float(row["lat"]),
            float(row["lon"]),
            str(row["address"]),
            "exact",
        )

    def _remote_geocode(self, address: str) -> ResolvedLocation | None:
        if not self.geocoding.get("enabled", False):
            return None

        query = address
        suffix = str(self.geocoding.get("region_suffix", "")).strip()
        if suffix:
            query = f"{address}, {suffix}"
        params = {"q": query, "format": "json", "limit": 1}
        country_code = str(self.geocoding.get("country_code", "")).strip()
        if country_code:
            params["countrycodes"] = country_code

        try:
            response = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params=params,
                headers={
                    "User-Agent": self.geocoding.get(
                        "user_agent", "broadcastify-tak-bridge/0.1"
                    )
                },
                timeout=5,
            )
            response.raise_for_status()
            values = response.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("External geocoding failed: %s", exc)
            return None
        if not values:
            return None
        return ResolvedLocation(
            float(values[0]["lat"]),
            float(values[0]["lon"]),
            address,
            "exact",
        )
