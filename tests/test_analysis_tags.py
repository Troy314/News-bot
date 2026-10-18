"""Analysis routes: the `tags` filter (PhD / Outreach / both). Lancer : bin/python -m unittest discover -s tests"""
import os
import tempfile
import unittest
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "test.db")

from fastapi.testclient import TestClient  # noqa: E402


class AnalysisTagFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import db
        db.init_db()
        conn = db.get_conn()
        conn.execute("INSERT INTO sources (name, url, kind, category) VALUES ('Jobs A', 'https://a.example', 'rss', 'job')")
        sid = conn.execute("SELECT id FROM sources WHERE name = 'Jobs A'").fetchone()[0]
        rows = [("p1", "PhD", "30000 € brut annuel"), ("p2", "PhD", None), ("o1", "Outreach", "35 000 €"), ("x1", "Post-Doc", None)]
        for link, tag, pay in rows:
            conn.execute(
                "INSERT INTO articles (source_id, title, link, pub_date, job_tag, remuneration) VALUES (?, ?, ?, '2026-10-01T00:00:00+00:00', ?, ?)",
                (sid, link, link, tag, pay),
            )
        conn.commit()
        conn.close()
        from web import app
        cls.client = TestClient(app)

    def count(self, tags):
        data = self.client.get("/api/jobs/analysis", params={"tags": tags}).json()
        return sum(s["count"] for s in data["by_source"]), sum(m["total"] for m in data["over_time"])

    def test_no_filter_counts_everything(self):
        self.assertEqual(self.count(""), (4, 4))

    def test_phd_only(self):
        self.assertEqual(self.count("PhD"), (2, 2))

    def test_outreach_only(self):
        self.assertEqual(self.count("Outreach"), (1, 1))

    def test_both(self):
        self.assertEqual(self.count("PhD,Outreach"), (3, 3))

    def test_stats_only_return_requested_tags(self):
        tags = {s["tag"] for s in self.client.get("/api/jobs/stats", params={"tags": "PhD,Outreach"}).json()}
        self.assertEqual(tags, {"PhD", "Outreach"})


if __name__ == "__main__":
    unittest.main()
