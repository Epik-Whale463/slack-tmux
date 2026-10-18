import unittest
from unittest.mock import patch

from telly import discovery


class DiscoverAgentsTest(unittest.TestCase):
    def test_dedupes_alias_resolving_to_same_binary(self):
        paths = {"command-code": "/opt/bin/command-code", "cmd": "/opt/bin/cmd"}

        with patch("telly.discovery.shutil.which", side_effect=paths.get), patch(
            "telly.discovery.os.path.realpath", return_value="/opt/bin/index.mjs"
        ), patch("telly.discovery._probe_version", return_value="1.84.0"):
            found = discovery.discover_agents(("command-code", "cmd"))

        self.assertEqual([a.name for a in found], ["command-code"])
        self.assertEqual(found[0].version, "1.84.0")

    def test_skips_missing_binaries(self):
        with patch("telly.discovery.shutil.which", return_value=None):
            found = discovery.discover_agents()

        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
