#!/usr/bin/env python3
"""Dry-run a News_bot source config WITHOUT touching the sources/articles tables.

Run from the News_bot root with its venv:
  bin/python .claude/skills/news-source-add/scripts/test_source.py --detect https://example.org/news
  bin/python .claude/skills/news-source-add/scripts/test_source.py --rss https://example.org/feed/
  bin/python .claude/skills/news-source-add/scripts/test_source.py --css https://example.org/news \
      --article "div.post-card" --title "h3 a" --link "h3 a" --base https://example.org \
      [--date "time"] [--image img] [--filter "regex"] [--ua browser] [--limit 10]

Prints what the scraper would extract (title, link, date, tag). Uses the
project's own scraper.py functions, so the result matches the real scrape.
(Job sources read job tags from news.db — read-only.)
"""
import argparse
import os
import sys

sys.path.insert(0, os.getcwd())
try:
    import scraper  # noqa: E402
except ImportError:
    sys.exit("Run this from the News_bot project root (bin/python …).")

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")


class Src(dict):
    """sqlite3.Row stand-in: any column the scraper reads but we didn't set is None."""
    def __missing__(self, key):
        return None


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--detect", metavar="URL")
    g.add_argument("--rss", metavar="URL")
    g.add_argument("--css", metavar="URL")
    ap.add_argument("--article"); ap.add_argument("--title", default="")
    ap.add_argument("--link", default=""); ap.add_argument("--base")
    ap.add_argument("--date"); ap.add_argument("--image"); ap.add_argument("--filter")
    ap.add_argument("--ua", help="'browser' or a full UA string")
    ap.add_argument("--limit", type=int, default=10)
    a = ap.parse_args()

    if a.detect:
        feed = scraper.detect_feed(a.detect)
        print(f"detect_feed → {feed or 'nothing found (configure CSS selectors manually)'}")
        if feed:
            print("Check it's topic-scoped, not a site-wide feed (IN2P3/IPPOG/CNRS lesson).")
        return

    ua = BROWSER_UA if a.ua == "browser" else a.ua
    src = Src(id=0, name="(dry-run)", url=a.rss or a.css, kind="rss" if a.rss else "css",
              category="website", user_agent=ua, article_selector=a.article,
              title_selector=a.title, link_selector=a.link, base_url=a.base,
              date_selector=a.date, image_selector=a.image, title_filter=a.filter, paginate=0)
    if src["kind"] == "css" and not (a.article and a.base):
        sys.exit("--css needs at least --article and --base")
    try:
        arts = scraper.scrape_rss(src, limit=a.limit) if a.rss else scraper.scrape_css(src, limit=a.limit)
    except scraper.ScrapeError as e:
        sys.exit(f"ERROR (would be status 'error'): {e}")
    if not arts:
        print("0 articles (would be status 'empty') — selectors probably don't match.")
        return
    for art in arts:
        date = art.pub_date.date().isoformat() if art.pub_date else "no date"
        print(f"- [{date}] {art.title[:90]}\n    {art.link}  ({art.content_tag})")
    print(f"\n{len(arts)} article(s). Dates missing on {sum(1 for x in arts if not x.pub_date)}.")


if __name__ == "__main__":
    main()
