"""Adds the underground-laboratory (and FCC PED) news sources via the running app's API.
Safe to re-run (409 = already exists). Usage: bin/python add_underground_labs.py [base_url]"""
import sys

import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
GROUP = "Underground labs"

SOURCES = [
    {"name": "Kamioka (ICRR)", "kind": "rss", "url": "https://www.icrr.u-tokyo.ac.jp/en/news/feed/"},
    {"name": "LSC Canfranc", "kind": "rss", "url": "https://lsc-canfranc.es/en/feed/"},
    {"name": "SUPL", "kind": "rss", "url": "https://www.supl.org.au/feed/"},
    {"name": "SNOLAB", "kind": "rss", "url": "https://www.snolab.ca/news/feed/"},
    {
        "name": "CJPL", "kind": "css", "url": "https://cjpl.tsinghua.edu.cn/",
        "article_selector": "div.newsK", "title_selector": "div.newstxt a",
        "link_selector": "div.newstxt a", "base_url": "https://cjpl.tsinghua.edu.cn",
        "image_selector": "img",
    },
    {
        "name": "FCC PED", "kind": "css", "url": "https://fcc-ped.web.cern.ch/ped-news-feed/",
        "group_tag": "Experiments", "article_selector": "li.wp-block-post",
        "title_selector": "h2.wp-block-post-title a", "link_selector": "h2.wp-block-post-title a",
        "base_url": "https://fcc-ped.web.cern.ch", "date_selector": "time", "image_selector": "img",
    },
    {
        "name": "LNGS", "kind": "css", "url": "https://www.lngs.infn.it/en/news",
        "article_selector": "div.item", "title_selector": "h4 a", "link_selector": "h4 a",
        "base_url": "https://www.lngs.infn.it", "image_selector": "img",
    },
    {
        "name": "SURF", "kind": "css", "url": "https://sanfordlab.org/news",
        "article_selector": "article.m-teaser", "title_selector": "h3.m-teaser__title a",
        "link_selector": "h3.m-teaser__title a", "base_url": "https://sanfordlab.org",
        "image_selector": "img",
    },
]

for s in SOURCES:
    body = {"category": "website", "group_tag": GROUP, "enabled": True, **s}
    r = requests.post(f"{BASE}/api/sources", json=body, timeout=30)
    print(s["name"], "->", "exists" if r.status_code == 409 else r.status_code)
