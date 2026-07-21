import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from blackboard_gui.branding_cache import BrandingCache
from blackboard_gui.client import SchoolBranding


class BrandingCacheTest(unittest.TestCase):
    def test_round_trip_school_name_and_rendered_logo(self):
        with TemporaryDirectory() as directory:
            with patch.object(BrandingCache, "_root", return_value=Path(directory)):
                cache = BrandingCache()
                expected = SchoolBranding("Example University", b"png-data")
                cache.save("https://learn.example.edu", expected)
                actual = cache.load("https://learn.example.edu")
                self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
