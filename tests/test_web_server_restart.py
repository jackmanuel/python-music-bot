import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src" / "music_bot"
sys.path.insert(0, str(SRC_DIR))

from web_server import build_restart_command


class WebServerRestartTests(unittest.TestCase):
    def test_restart_command_preserves_startup_flags(self):
        with patch.object(sys, "executable", "pythonw.exe"), patch.object(
            sys,
            "argv",
            ["src/music_bot/music_bot.pyw", "--no-cache"],
        ):
            command = build_restart_command()

        self.assertEqual(
            command,
            [
                "pythonw.exe",
                str((PROJECT_ROOT / "src" / "music_bot" / "music_bot.pyw").resolve()),
                "--no-cache",
            ],
        )

    def test_restart_command_preserves_multiple_startup_arguments(self):
        with patch.object(sys, "executable", "pythonw.exe"), patch.object(
            sys,
            "argv",
            ["src/music_bot/music_bot.pyw", "--stream-only", "--example"],
        ):
            command = build_restart_command()

        self.assertEqual(command[2:], ["--stream-only", "--example"])


if __name__ == "__main__":
    unittest.main()
