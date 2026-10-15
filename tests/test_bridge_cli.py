import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "bridge"


class BridgeCommandTests(unittest.TestCase):
    def run_bridge(self, *arguments):
        return subprocess.run(
            [str(BRIDGE), *arguments],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_help_lists_guided_commands(self):
        result = self.run_bridge("help")

        self.assertEqual(result.returncode, 0, result.stderr)
        for command in ("setup", "doctor", "start", "status", "logs", "stop"):
            self.assertIn(command, result.stdout)

    def test_no_argument_shows_help(self):
        result = self.run_bridge()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Usage: ./bridge COMMAND", result.stdout)

    def test_unknown_command_fails_with_usage(self):
        result = self.run_bridge("not-a-command")

        self.assertEqual(result.returncode, 2)
        self.assertIn("Unknown command", result.stderr)
        self.assertIn("Usage: ./bridge COMMAND", result.stderr)


if __name__ == "__main__":
    unittest.main()
