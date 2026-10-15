import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from broadcastify_tak_bridge.config import ConfigError, load_config


CONFIG = """
tak_server:
  host: ${TEST_TAK_HOST}
  cert: /tmp/client.pem
  key: /tmp/client.key
  ca: /tmp/ca.pem
broadcastify:
  stream_url: ${TEST_STREAM_URL}
  password: ${TEST_PASSWORD}
  channels:
    - source: mono
      label: DISPATCH
"""


class ConfigTests(unittest.TestCase):
    def _write_config(self, value=CONFIG):
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "config.yml"
        path.write_text(value, encoding="utf-8")
        return directory, path

    def test_environment_values_are_expanded(self):
        directory, path = self._write_config()
        self.addCleanup(directory.cleanup)
        with patch.dict(
            os.environ,
            {
                "TEST_TAK_HOST": "tak.example.invalid",
                "TEST_STREAM_URL": "https://example.invalid/audio",
                "TEST_PASSWORD": "value:with#yaml-characters",
            },
            clear=False,
        ):
            config = load_config(path)

        self.assertEqual(config["tak_server"]["host"], "tak.example.invalid")
        self.assertEqual(
            config["broadcastify"]["password"],
            "value:with#yaml-characters",
        )
        self.assertEqual(config["broadcastify"]["channels"][0]["source"], "mono")
        self.assertFalse(config["service"]["log_transcripts"])

    def test_missing_environment_value_is_rejected(self):
        directory, path = self._write_config()
        self.addCleanup(directory.cleanup)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ConfigError, "TEST_STREAM_URL"):
                load_config(path)

    def test_invalid_channel_is_rejected(self):
        value = CONFIG.replace("source: mono", "source: center")
        directory, path = self._write_config(value)
        self.addCleanup(directory.cleanup)
        with patch.dict(
            os.environ,
            {
                "TEST_TAK_HOST": "tak.example.invalid",
                "TEST_STREAM_URL": "https://example.invalid/audio",
                "TEST_PASSWORD": "test-password",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(ConfigError, "mono, left, or right"):
                load_config(path)

    def test_example_configuration_loads_with_documented_environment(self):
        example = Path(__file__).resolve().parents[1] / "config.example.yml"
        with patch.dict(
            os.environ,
            {
                "TAK_HOST": "tak.example.invalid",
                "BROADCASTIFY_STREAM_URL": "https://example.invalid/audio",
                "BROADCASTIFY_USERNAME": "example-user",
                "BROADCASTIFY_PASSWORD": "example-password",
                "WHISPER_MODEL": "medium.en",
                "TAK_CHAT_ROOM": "DISPATCH",
                "TAK_SENDER_CALLSIGN": "DISPATCH",
            },
            clear=False,
        ):
            config = load_config(example)

        self.assertEqual(config["broadcastify"]["channels"][0]["source"], "mono")
        self.assertEqual(config["whisper"]["model"], "medium.en")
        self.assertEqual(config["tak_output"]["chat_room"], "DISPATCH")


if __name__ == "__main__":
    unittest.main()
