---
name: news-source-add
description: Add, fix or tune a News_bot source (RSS/Atom feed, CSS-selector listing page, job/thesis board, email alert, INSPIRE-HEP) — discover the feed, dry-run the scrape without touching the DB, pick scoped selectors, add it via the API, and check its health; also extend topic tagging (CONTENT_TAG_RULES). Use when the user wants to follow a new lab/experiment/news site/subreddit/Mastodon account/job board, when a source shows "empty"/"error" status, or asks to add a physics topic tag.
---

# Add / fix a News_bot source

The full design notes are in CLAUDE.md ("Adding a source", "Source health
tracking", "Social media sources", "Job/thesis offer sources",
"Content tagging") — read the relevant section before changing scraper code.
This skill is the operational shortcut.

## 1. Discover → dry-run (no DB writes)

```bash
cd ~/Code/News_bot
T=.claude/skills/news-source-add/scripts/test_source.py
bin/python $T --detect https://lab.example.org/news          # scraper.detect_feed()
bin/python $T --rss  https://lab.example.org/feed/ --limit 5
bin/python $T --css  https://lab.example.org/news --article "div.views-row" \
    --title "h3 a" --link "h3 a" --base https://lab.example.org --date "time" [--image img] \
    [--filter "thèse|phd"] [--ua browser]
```
It calls the project's own `scrape_rss`/`scrape_css`, so output = real behavior.

Decision rules:
- A discovered feed must be **topic-scoped**. Site-wide `/rss.xml` feeds
  (IN2P3, IPPOG, CNRS) mix in unrelated content → use CSS on the scoped listing page.
- Feed rejects the default UA but works with a browser UA (Interactions) → set
  `user_agent`. A Cloudflare challenge/consent wall → don't bypass; find another
  page (see `external-source-import` skill registry).
- Mastodon `/@user.rss`, Reddit `/r/<sub>/.rss` work; YouTube needs the
  `feeds/videos.xml?channel_id=` URL found by hand; X/Instagram/Threads aren't supported.
- CSS selectors: the article container is the smallest element repeating once
  per story (`div.post-card`), class-based — never a devtools "Copy selector"
  path with `:nth-child`. Blank title/link selector = the container itself is
  the `<a>`. No `href`, only `id` anchors → handled (`url#id`).
- Dates: `date_selector` supports English and French month names; a listing
  that only shows the year in a heading above → `date_year_selector`.
- Job boards: `category: "job"`, optional `title_filter` regex,
  `deadline_selector`, `location_selector`, `forced_job_tag`, `paginate`
  (JetEngine "load more" pages).

## 2. Add it

With the app running (`bin/python main.py`, web UI on :8000 — the only correct
entrypoint; it runs FastAPI + the Discord bot together):

```bash
curl -s -X POST localhost:8000/api/sources -H 'Content-Type: application/json' -d '{
  "name": "ALICE", "kind": "css", "category": "website", "url": "https://alice.cern/index.php/news",
  "article_selector": "div.views-row", "title_selector": "h3 a", "link_selector": "h3 a",
  "base_url": "https://alice.cern", "date_selector": "h5 time", "image_selector": "img",
  "group_tag": "Experiments", "enabled": true }'
```
`kind` ∈ rss/css/email/inspirehep; `category` ∈ website/newsletter/social/job.
Names are unique (409 on duplicate). Edit later with `PUT /api/sources/{id}`
(sparse — omitted fields unchanged). The UI's Sources tab does the same
(Auto-detect / "already a feed" / Configure manually, plus the point-and-click
selector picker). Only add to `seed_sources.py` if it should exist on a fresh DB.

## 3. Verify health

Trigger a scrape (UI "Run now" / `POST /api/run-now`) and check the Sources tab:
`ok` (articles found), `empty` (fetched, 0 extracted → selectors drifted),
`error` (fetch/parse failed). Any new code path that scrapes one source must
use `scrape_source_safe()` so health stays current. The first scrape is
capped by `FIRST_SCRAPE_LIMIT` so a new source doesn't flood the digest.

## Topic tags

Tags are by physics topic, first-match-wins regexes covering **EN + FR**.
Extend the term table in CLAUDE.md ("Content tagging") first, then mirror it
into `CONTENT_TAG_RULES` in `scraper.py`, ordering specific before general.
Don't reintroduce article-*type* tags. Re-tag existing articles if needed.

## Data safety

`news.db` is real accumulated data. Back up before schema changes
(`sqlite3 news.db ".backup news.db.backup-$(date +%Y%m%d-%H%M%S)"`, the
existing naming). Table rebuilds: `PRAGMA legacy_alter_table=ON` + 
`PRAGMA foreign_key_check` (the `sources_old` incident) — see `safe-data-migration`.
