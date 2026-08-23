"""
Renders a list of scraper.Article objects into the same Inoreader-style,
month-grouped, source-tagged, filterable HTML digest this project has used
since the original static generate_digest.py -- now fed from the DB instead
of a fresh scrape, and reused for three different outputs: the `!digest`
Discord command, the daily @everyone Discord post, and the newsletter email.
"""

import html
import mimetypes
from collections import OrderedDict
from datetime import datetime, timezone

from scraper import Article


def time_ago(pub_date: datetime | None, now: datetime) -> str:
    if pub_date is None:
        return ""
    seconds = (now - pub_date).total_seconds()
    if seconds < 0:
        return "just now"
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    if seconds < 7 * 86400:
        return f"{int(seconds // 86400)}d ago"
    if seconds < 31 * 86400:
        return f"{int(seconds // (7 * 86400))}w ago"
    if seconds < 365 * 86400:
        return f"{int(seconds // (31 * 86400))}mo ago"
    return f"{int(seconds // (365 * 86400))}y ago"


def full_date(pub_date: datetime | None) -> str:
    if pub_date is None:
        return "Unknown date"
    return pub_date.astimezone(timezone.utc).strftime("%b %d, %Y, %I:%M %p UTC")


def source_hues(names: list[str]) -> dict[str, int]:
    """Evenly-rotated hue per source, assigned by alphabetical order so a
    given source keeps the same color across renders regardless of article
    counts or which sources are present in this particular list."""
    ordered = sorted(set(names))
    n = len(ordered) or 1
    return {name: round(i * 360 / n) for i, name in enumerate(ordered)}


CARD_TEMPLATE = """
      <a class="card" href="{link}" target="_blank" rel="noopener noreferrer" data-source="{site_attr}">
        {image_block}
        <div class="card-body">
          <h3>{title}</h3>
          <div class="card-meta">
            <span class="tag" style="--hue: {hue}">{site}</span>
            <span class="date" title="{relative}">{full_date}</span>
          </div>
        </div>
      </a>"""

IMAGE_BLOCK = '<div class="card-image"><img src="{image_url}" alt="" loading="lazy" onerror="this.parentElement.remove()"></div>'


def render_card(article: Article, now: datetime, hues: dict[str, int]) -> str:
    image_block = IMAGE_BLOCK.format(image_url=html.escape(article.image_url)) if article.image_url else ""
    return CARD_TEMPLATE.format(
        link=html.escape(article.link),
        image_block=image_block,
        title=html.escape(article.title),
        site=html.escape(article.source_name),
        site_attr=html.escape(article.source_name),
        hue=hues.get(article.source_name, 0),
        full_date=full_date(article.pub_date),
        relative=html.escape(time_ago(article.pub_date, now)),
    )


def group_by_month(articles: list[Article]) -> "OrderedDict[tuple[int, int] | None, list[Article]]":
    """Group already-newest-first-sorted articles by (year, month)."""
    groups: "OrderedDict[tuple[int, int] | None, list[Article]]" = OrderedDict()
    for article in articles:
        key = (article.pub_date.year, article.pub_date.month) if article.pub_date else None
        groups.setdefault(key, []).append(article)
    return groups


def month_label(key: tuple[int, int] | None) -> str:
    if key is None:
        return "Unknown date"
    year, month = key
    return datetime(year, month, 1).strftime("%B %Y")


MONTH_SECTION_TEMPLATE = """
    <details class="month-section"{open_attr}>
      <summary>
        <span class="chevron">&#9656;</span>
        <span class="month-label">{label}</span>
        <span class="month-count">{count} articles</span>
      </summary>
      <div class="grid">{cards}
      </div>
    </details>"""


def render_month_section(key, articles: list[Article], now: datetime, hues: dict[str, int], is_first: bool) -> str:
    cards = "".join(render_card(a, now, hues) for a in articles)
    return MONTH_SECTION_TEMPLATE.format(
        open_attr=" open" if is_first else "",
        label=html.escape(month_label(key)),
        count=len(articles),
        cards=cards,
    )


CHIP_TEMPLATE = '<button class="chip" data-source="{site_attr}" style="--hue: {hue}">{site} <span class="chip-count">{count}</span></button>'


def render_chips(articles: list[Article], hues: dict[str, int]) -> str:
    counts: dict[str, int] = {}
    for a in articles:
        counts[a.source_name] = counts.get(a.source_name, 0) + 1

    chips = ['<button class="chip chip-all active" data-source="__all__">All</button>']
    for name in sorted(counts):
        chips.append(
            CHIP_TEMPLATE.format(
                site_attr=html.escape(name),
                hue=hues.get(name, 0),
                site=html.escape(name),
                count=counts[name],
            )
        )
    return "\n      ".join(chips)


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{page_title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: #16171a;
    color: #e8e8ea;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }}
  header {{
    position: sticky;
    top: 0;
    z-index: 10;
    background: #1c1d21;
    border-bottom: 1px solid #2a2b30;
  }}
  .header-top {{ display: flex; align-items: baseline; gap: 12px; padding: 18px 28px 12px; flex-wrap: wrap; }}
  header h1 {{ font-size: 18px; margin: 0; font-weight: 600; }}
  header .count {{ color: #8a8d94; font-size: 13px; }}
  header .generated {{ margin-left: auto; color: #6b6e75; font-size: 12px; }}
  .chips {{ display: flex; flex-wrap: wrap; gap: 8px; padding: 0 28px 16px; }}
  .chip {{
    appearance: none; border: 1px solid #2f3037; background: #232429; color: #b7bac1;
    font-size: 12.5px; font-weight: 500; padding: 5px 12px; border-radius: 999px; cursor: pointer;
    transition: border-color 0.12s ease, color 0.12s ease, background 0.12s ease;
  }}
  .chip:hover {{ border-color: #4c4f56; color: #e8e8ea; }}
  .chip[data-source]:not(.chip-all) {{ border-color: hsla(var(--hue), 70%, 60%, 0.35); }}
  .chip[data-source]:not(.chip-all).active {{
    background: hsla(var(--hue), 70%, 55%, 0.22); border-color: hsl(var(--hue), 70%, 60%); color: hsl(var(--hue), 85%, 82%);
  }}
  .chip-all.active {{ background: #33343b; border-color: #5a5d66; color: #f2f2f4; }}
  .chip-count {{ color: inherit; opacity: 0.65; font-weight: 400; }}
  .sections {{ padding-bottom: 32px; }}
  .month-section {{ border-bottom: 1px solid #232429; }}
  .month-section summary {{
    list-style: none; cursor: pointer; display: flex; align-items: center; gap: 10px;
    padding: 16px 28px; user-select: none;
  }}
  .month-section summary::-webkit-details-marker {{ display: none; }}
  .month-section .chevron {{ display: inline-block; font-size: 11px; color: #6b6e75; transition: transform 0.12s ease; }}
  .month-section[open] .chevron {{ transform: rotate(90deg); }}
  .month-section .month-label {{ font-size: 14px; font-weight: 600; color: #e8e8ea; }}
  .month-section .month-count {{ font-size: 12.5px; color: #6b6e75; }}
  .month-section summary:hover .month-label {{ color: #9aa5ff; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 18px; padding: 4px 28px 32px; }}
  .card {{
    display: flex; flex-direction: column; background: #1e1f24; border: 1px solid #2a2b30; border-radius: 10px;
    overflow: hidden; text-decoration: none; color: inherit; transition: transform 0.12s ease, border-color 0.12s ease;
  }}
  .card:hover {{ transform: translateY(-2px); border-color: #3d4046; }}
  .card.hidden {{ display: none; }}
  .card-image {{ width: 100%; aspect-ratio: 16 / 9; background: #26272c; overflow: hidden; }}
  .card-image img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
  .card-body {{ padding: 14px 16px 16px; display: flex; flex-direction: column; gap: 10px; flex: 1; }}
  .card-body h3 {{ margin: 0; font-size: 15px; line-height: 1.4; font-weight: 600; color: #f2f2f4; }}
  .card-meta {{ margin-top: auto; display: flex; align-items: center; flex-wrap: wrap; gap: 8px; font-size: 12px; color: #8a8d94; }}
  .card-meta .tag {{
    background: hsla(var(--hue), 70%, 55%, 0.18); color: hsl(var(--hue), 85%, 78%); font-weight: 600;
    font-size: 11px; padding: 2px 9px; border-radius: 999px; white-space: nowrap;
  }}
  .card-meta .date {{ color: #8a8d94; }}
  .empty-state {{ display: none; padding: 60px 28px; text-align: center; color: #6b6e75; font-size: 14px; }}
  @media (prefers-color-scheme: light) {{
    body {{ background: #f4f4f6; color: #1a1a1e; }}
    header {{ background: #ffffff; border-bottom-color: #e4e4e8; }}
    header .count, header .generated {{ color: #6b6e75; }}
    .chip {{ background: #eef0f3; border-color: #dfe1e6; color: #4a4d55; }}
    .chip-all.active {{ background: #e4e4e8; border-color: #c7c9d0; color: #1a1a1e; }}
    .card {{ background: #ffffff; border-color: #e4e4e8; }}
    .card-image {{ background: #eceef1; }}
    .card-body h3 {{ color: #1a1a1e; }}
    .card-meta .date {{ color: #6b6e75; }}
    .month-section {{ border-bottom-color: #e4e4e8; }}
    .month-section .month-label {{ color: #1a1a1e; }}
  }}
</style>
</head>
<body>
  <header>
    <div class="header-top">
      <h1>{page_title}</h1>
      <span class="count">{count} articles &middot; {source_count} sources</span>
      <span class="generated">Generated {generated}</span>
    </div>
    <div class="chips">
      {chips}
    </div>
  </header>
  <div class="sections">{sections}
  </div>
  <div class="empty-state">No articles match the selected filters.</div>
  <script>
    (function () {{
      var chips = Array.prototype.slice.call(document.querySelectorAll('.chip'));
      var cards = Array.prototype.slice.call(document.querySelectorAll('.card'));
      var sections = Array.prototype.slice.call(document.querySelectorAll('.month-section'));
      var allChip = document.querySelector('.chip-all');
      var emptyState = document.querySelector('.empty-state');
      var active = new Set();

      function applyFilter() {{
        var visible = 0;
        cards.forEach(function (card) {{
          var show = active.size === 0 || active.has(card.dataset.source);
          card.classList.toggle('hidden', !show);
          if (show) visible++;
        }});
        sections.forEach(function (section) {{
          var hasVisible = section.querySelector('.card:not(.hidden)') !== null;
          section.style.display = hasVisible ? '' : 'none';
        }});
        emptyState.style.display = visible === 0 ? 'block' : 'none';
        allChip.classList.toggle('active', active.size === 0);
      }}

      chips.forEach(function (chip) {{
        chip.addEventListener('click', function () {{
          var src = chip.dataset.source;
          if (src === '__all__') {{
            active.clear();
            chips.forEach(function (c) {{ c.classList.remove('active'); }});
          }} else {{
            if (active.has(src)) {{ active.delete(src); chip.classList.remove('active'); }}
            else {{ active.add(src); chip.classList.add('active'); }}
          }}
          applyFilter();
        }});
      }});

      applyFilter();
    }})();
  </script>
</body>
</html>
"""


def render_digest_html(articles: list[Article], page_title: str = "Particle Physics News") -> str:
    now = datetime.now(timezone.utc)
    articles = sorted(articles, key=lambda a: a.pub_date or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    hues = source_hues([a.source_name for a in articles])

    month_groups = group_by_month(articles)
    sections = "".join(
        render_month_section(key, group, now, hues, is_first=(i == 0))
        for i, (key, group) in enumerate(month_groups.items())
    )

    return PAGE_TEMPLATE.format(
        page_title=html.escape(page_title),
        count=len(articles),
        source_count=len(set(a.source_name for a in articles)),
        generated=now.strftime("%Y-%m-%d %H:%M UTC"),
        chips=render_chips(articles, hues),
        sections=sections,
    )


RSS_ITEM_CAP = 100


def _rss_image_mime(image_url: str) -> str:
    guessed, _ = mimetypes.guess_type(image_url)
    return guessed if guessed and guessed.startswith("image/") else "image/jpeg"


def render_rss_feed(articles: list[Article], title: str, link: str, description: str) -> str:
    """A single aggregated RSS 2.0 feed combining every source this app
    scrapes -- "the cumulative feed of all the feeds": one subscription in
    an ordinary RSS reader instead of following each source individually.
    Plain string templating (matching render_digest_html's own convention
    above), not xml.etree -- a static RSS document has no real need for a
    DOM-building API, same minimal-dependency preference as the rest of
    this app.

    Capped at RSS_ITEM_CAP (100) most-recent articles, not literally every
    row ever scraped -- an RSS reader only cares about what's new/recent,
    and an unbounded feed would just make every refresh slower for no
    benefit. `articles` is expected pre-sorted newest-first (get_articles()
    already returns it that way) and pre-filtered to whatever the caller
    wants included (this function doesn't itself care about category,
    archived status, etc.).

    Each item carries its image two different ways, since RSS readers
    don't agree on which one they honor: `<enclosure>` (the RSS 2.0 spec's
    own mechanism) and `<media:thumbnail>` (the Media RSS extension --
    this app already *reads* `media:thumbnail` from other feeds in
    scraper.extract_rss_image, so writing the same tag out is the most
    broadly-supported choice) both point at the same URL, plus the image
    is embedded a *third* time as a literal `<img>` inside `<description>`
    for readers that only ever render what's in the description body.
    `length="0"` on `<enclosure>` (the byte-size attribute RSS technically
    wants) is a deliberate placeholder, not a bug -- getting the real size
    would mean an extra fetch per image per feed request, and every reader
    this was checked against ignores the value anyway.

    **Security note on `<description>`'s embedded HTML**: no `<![CDATA[]]>`
    is used anywhere in this feed, specifically to avoid CDATA-injection --
    if any scraped title/excerpt ever contained the literal sequence
    `]]>`, a CDATA section would end early and everything after it would
    be interpreted as raw, unescaped XML by the reader, a real injection
    vector into other readers' rendering of this feed. Instead this builds
    the inner HTML fragment with `html.escape()` on every embedded value
    (image URL, excerpt text) *first*, then `html.escape()`s that entire
    resulting HTML string *again* before placing it as the `<description>`
    element's own XML text content -- correct, not a double-escaping bug:
    an RSS reader XML-unescapes an element's text content exactly once,
    which must yield back well-formed (already HTML-escaped) HTML, which
    is what the *first* escape pass produced. Skipping either pass would
    let scraped content break out of its intended context -- the first
    protects the inner HTML's own structure, the second protects the
    surrounding XML document."""
    now = datetime.now(timezone.utc)
    items = []
    for a in articles[:RSS_ITEM_CAP]:
        pub_date = a.pub_date or now

        enclosure = ""
        media_thumbnail = ""
        img_html = ""
        if a.image_url:
            safe_image_url = html.escape(a.image_url)
            enclosure = f'\n      <enclosure url="{safe_image_url}" type="{_rss_image_mime(a.image_url)}" length="0" />'
            media_thumbnail = f'\n      <media:thumbnail url="{safe_image_url}" />'
            img_html = f'<img src="{safe_image_url}" alt="" />'

        excerpt_html = f"<p>{html.escape(a.excerpt)}</p>" if a.excerpt else ""
        description_html = img_html + excerpt_html
        description_field = (
            f"\n      <description>{html.escape(description_html)}</description>" if description_html else ""
        )

        items.append(f"""
    <item>
      <title>{html.escape(a.title)}</title>
      <link>{html.escape(a.link)}</link>
      <guid isPermaLink="true">{html.escape(a.link)}</guid>
      <pubDate>{pub_date.astimezone(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")}</pubDate>
      <source>{html.escape(a.source_name)}</source>
      <category>{html.escape(a.content_tag or "")}</category>{description_field}{enclosure}{media_thumbnail}
    </item>""")

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
  <channel>
    <title>{html.escape(title)}</title>
    <link>{html.escape(link)}</link>
    <description>{html.escape(description)}</description>
    <language>en</language>
    <lastBuildDate>{now.strftime("%a, %d %b %Y %H:%M:%S +0000")}</lastBuildDate>{"".join(items)}
  </channel>
</rss>
"""
