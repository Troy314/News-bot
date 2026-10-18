"""Version et journal des versions : /api/version, et un journal sans version vide (une version sans ligne ne passe pas).
Lancer : bin/python -m unittest discover -s tests"""
import os
import tempfile
import unittest
from pathlib import Path

# Never touch the real database: web.py → db.py reads DB_PATH at import time.
os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from changelog import parse_changelog  # noqa: E402

CHANGELOG = Path(__file__).resolve().parents[1] / "CHANGELOG.md"
CURRENT = "1.3"  # update with each release (CHANGELOG.md's top version)


class ChangelogTests(unittest.TestCase):
    def test_every_release_has_entries_and_the_top_one_is_the_current_version(self):
        releases = parse_changelog(CHANGELOG.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(releases), 2)
        self.assertTrue(all(r["items"] for r in releases))
        self.assertEqual(releases[0]["version"], CURRENT)

    def test_version_endpoint_returns_the_journal(self):
        import db
        db.init_db()
        from web import app

        body = TestClient(app).get("/api/version").json()
        self.assertEqual(body["version"], CURRENT)
        self.assertTrue(body["releases"][0]["items"])


if __name__ == "__main__":
    unittest.main()
