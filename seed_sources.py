"""
One-time (but safely re-runnable) seed of the `sources` table with the 20
sources already reverse-engineered for this project -- 15 with a real
RSS/Atom feed, 5 (ALICE, LIGO, IPPOG, CNRSNews) that needed hand-built CSS
selectors because no usable feed exists for them. Run directly, or imported
by main.py on first launch when the table is empty.
"""

import db

SEED_SOURCES = [
    {"name": "CERN", "kind": "rss", "url": "https://home.cern/fr/feed/"},
    {"name": "ATLAS", "kind": "rss", "url": "https://atlas.cern/updates/feed.xml"},
    {"name": "CMS", "kind": "rss", "url": "https://cms.cern/taxonomy/term/1090/feed/all"},
    {"name": "LHCb", "kind": "rss", "url": "https://lhcb-outreach.web.cern.ch/feed/"},
    {"name": "CERNCourier", "kind": "rss", "url": "https://cerncourier.com/feed/"},
    {"name": "IN2P3", "kind": "rss", "url": "https://www.in2p3.cnrs.fr/rss.xml"},
    {"name": "Virgo", "kind": "rss", "url": "https://www.virgo-gw.eu/feed/"},
    {"name": "MPG", "kind": "rss", "url": "https://www.mpg.de/12642524/particle-physics.rss"},
    {"name": "ScienceNews", "kind": "rss", "url": "https://www.sciencenews.org/topic/particle-physics/feed"},
    {"name": "Guardian", "kind": "rss", "url": "https://www.theguardian.com/science/particlephysics/rss"},
    {"name": "PhysicsToday", "kind": "rss", "url": "https://physicstoday.aip.org/particle-physics.rss"},
    {"name": "TheConversation", "kind": "rss", "url": "https://theconversation.com/topics/particle-physics-1449/articles.atom"},
    {"name": "Nature", "kind": "rss", "url": "https://www.nature.com/subjects/particle-physics/nature.rss"},
    {"name": "PhysOrg", "kind": "rss", "url": "https://phys.org/rss-feed/physics-news/particle-physics/"},
    {"name": "Fermilab", "kind": "rss", "url": "https://news.fnal.gov/feed/"},
    {
        "name": "Interactions",
        "kind": "rss",
        "url": "https://www.interactions.org/index.rss",
        # Blocks the default User-Agent with a 403 but allows a browser-like one.
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ),
    },
    # No usable feed exists for these four (ALICE: Drupal site with the
    # aggregator module disabled; LIGO: /news.rss returns a broken 406;
    # IPPOG: /rss.xml is the generic site feed and mixes in member-profile
    # pages; CNRSNews: /rss.xml is site-wide across all topics, not scoped
    # to particle physics) -- CSS-scrape their listing pages instead.
    {
        "name": "ALICE",
        "kind": "css",
        "url": "https://alice.cern/index.php/news",
        "article_selector": "div.views-row",
        "title_selector": "h3 a",
        "link_selector": "h3 a",
        "base_url": "https://alice.cern",
        "date_selector": "h5 time",
        "image_selector": "img",
    },
    {
        "name": "LIGO",
        "kind": "css",
        "url": "https://www.ligo.caltech.edu/news",
        "article_selector": "div.panel:has(.thumb)",
        "title_selector": "h3",
        "link_selector": "a",
        "base_url": "https://www.ligo.caltech.edu",
        "date_selector": "p strong",
        "image_selector": "img",
    },
    {
        "name": "IPPOG",
        "kind": "css",
        "url": "https://ippog.org/news",
        "article_selector": "div.views-row",
        "title_selector": "h3.resourceTitle a",
        "link_selector": "h3.resourceTitle a",
        "base_url": "https://ippog.org",
        "date_selector": "time",
        "image_selector": "img",
    },
    {
        "name": "CNRSNews",
        "kind": "css",
        "url": "https://news.cnrs.fr/particle-physics",
        "article_selector": "div.views-row",
        "title_selector": ".views-field-title a",
        "link_selector": ".views-field-title a",
        "base_url": "https://news.cnrs.fr",
        "date_selector": ".date-display-single",
        "image_selector": ".views-field-field-visuel-principal img",
    },
]

COLUMNS = [
    "name", "kind", "url", "article_selector", "title_selector", "link_selector",
    "base_url", "date_selector", "image_selector", "user_agent",
]


def seed() -> int:
    """Insert any seed source not already present (matched by name).
    Returns how many were newly inserted."""
    conn = db.get_conn()
    inserted = 0
    try:
        for src in SEED_SOURCES:
            row = [src.get(col) for col in COLUMNS]
            cur = conn.execute(
                f"INSERT OR IGNORE INTO sources ({', '.join(COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in COLUMNS)})",
                row,
            )
            inserted += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return inserted


if __name__ == "__main__":
    db.init_db()
    n = seed()
    print(f"Inserted {n} new source(s) (already-present ones were skipped).")
