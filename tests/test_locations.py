import tempfile
import unittest
from pathlib import Path

from broadcastify_tak_bridge.locations import LocationResolver, ResolvedLocation


class LocationResolverTests(unittest.TestCase):
    def test_invalid_coordinates_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Latitude"):
            ResolvedLocation(100.0, 0.0, "Invalid", "exact")

    def test_longest_alias_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "locations.yml"
            path.write_text(
                """
locations:
  - name: Example Station
    latitude: 40.0
    longitude: -100.0
    aliases: [station]
  - name: Example Central Station
    latitude: 41.0
    longitude: -101.0
    aliases: [central station]
unit_map:
  engine one: Engine 1
""",
                encoding="utf-8",
            )
            resolver = LocationResolver(
                {
                    "file": str(path),
                    "address_database": "",
                    "geocoding": {"enabled": False},
                }
            )
            result = resolver.resolve("Respond to central station for an alarm")

        self.assertIsNotNone(result)
        self.assertEqual(result.name, "Example Central Station")
        self.assertEqual(resolver.apply_unit_map("engine one responding"), "Engine 1 responding")


if __name__ == "__main__":
    unittest.main()
