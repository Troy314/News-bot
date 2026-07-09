"""
Generate a static, Inoreader-style HTML digest of every article currently
returned by the configured news_bot SOURCES -- a dark card grid you can open
directly in a browser, no Discord required. Includes per-source colored tags
and client-side click-to-filter chips (all filtering runs in the browser;
this stays a single static file, no server needed).

Run:  python generate_digest.py [output_file.html]
"""

import sys
import html
from collections import OrderedDict
from datetime import datetime, timezone

from news_bot import scrape_all_sites, Article

OUTPUT_FILE = sys.argv[1] if len(sys.argv) > 1 else "digest.html"


def time_ago(pub_date: datetime | None, now: datetime) -> str:
    if pub_date is None:
        return ""
    delta = now - pub_date
    seconds = delta.total_seconds()
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
    given source keeps the same color across regenerations regardless of
    how many articles it has or where it falls in SOURCES."""
    ordered = sorted(names)
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
        site=html.escape(article.site),
        site_attr=html.escape(article.site),
        hue=hues.get(article.site, 0),
        full_date=full_date(article.pub_date),
        relative=html.escape(time_ago(article.pub_date, now)),
    )


def group_by_month(articles: list[Article]) -> "OrderedDict[tuple[int, int] | None, list[Article]]":
    """Group already-newest-first-sorted articles by (year, month). Relies on
    the input already being sorted descending, so groups come out in
    chronological order with any undated articles trailing at the end."""
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


def render_month_section(key: tuple[int, int] | None, articles: list[Article], now: datetime, hues: dict[str, int], is_first: bool) -> str:
    cards = "".join(render_card(a, now, hues) for a in articles)
    return MONTH_SECTION_TEMPLATE.format(
        open_attr=" open" if is_first else "",
        label=html.escape(month_label(key)),
        count=len(articles),
        cards=cards,
    )


CHIP_TEMPLATE = '<button class="chip" data-source="{site_attr}" style="--hue: {hue}">{site} <span class="chip-count">{count}</span></button>'


def render_chips(results: dict[str, list[Article]], hues: dict[str, int]) -> str:
    chips = ['<button class="chip chip-all active" data-source="__all__">All</button>']
    for name in sorted(results):
        count = len(results[name])
        if count == 0:
            continue
        chips.append(
            CHIP_TEMPLATE.format(
                site_attr=html.escape(name),
                hue=hues.get(name, 0),
                site=html.escape(name),
                count=count,
            )
        )
    return "\n      ".join(chips)


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Particle Physics News</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {{
    color-scheme: dark;
  }}
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
  .header-top {{
    display: flex;
    align-items: baseline;
    gap: 12px;
    padding: 18px 28px 12px;
  }}
  header h1 {{
    font-size: 18px;
    margin: 0;
    font-weight: 600;
  }}
  header .count {{
    color: #8a8d94;
    font-size: 13px;
  }}
  header .generated {{
    margin-left: auto;
    color: #6b6e75;
    font-size: 12px;
  }}
  .chips {{
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    padding: 0 28px 16px;
  }}
  .chip {{
    appearance: none;
    border: 1px solid #2f3037;
    background: #232429;
    color: #b7bac1;
    font-size: 12.5px;
    font-weight: 500;
    padding: 5px 12px;
    border-radius: 999px;
    cursor: pointer;
    transition: border-color 0.12s ease, color 0.12s ease, background 0.12s ease;
  }}
  .chip:hover {{
    border-color: #4c4f56;
    color: #e8e8ea;
  }}
  .chip[data-source]:not(.chip-all) {{
    border-color: hsla(var(--hue), 70%, 60%, 0.35);
  }}
  .chip[data-source]:not(.chip-all).active {{
    background: hsla(var(--hue), 70%, 55%, 0.22);
    border-color: hsl(var(--hue), 70%, 60%);
    color: hsl(var(--hue), 85%, 82%);
  }}
  .chip-all.active {{
    background: #33343b;
    border-color: #5a5d66;
    color: #f2f2f4;
  }}
  .chip-count {{
    color: inherit;
    opacity: 0.65;
    font-weight: 400;
  }}
  .sections {{
    padding-bottom: 32px;
  }}
  .month-section {{
    border-bottom: 1px solid #232429;
  }}
  .month-section summary {{
    list-style: none;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 16px 28px;
    user-select: none;
  }}
  .month-section summary::-webkit-details-marker {{
    display: none;
  }}
  .month-section .chevron {{
    display: inline-block;
    font-size: 11px;
    color: #6b6e75;
    transition: transform 0.12s ease;
  }}
  .month-section[open] .chevron {{
    transform: rotate(90deg);
  }}
  .month-section .month-label {{
    font-size: 14px;
    font-weight: 600;
    color: #e8e8ea;
  }}
  .month-section .month-count {{
    font-size: 12.5px;
    color: #6b6e75;
  }}
  .month-section summary:hover .month-label {{
    color: #9aa5ff;
  }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    gap: 18px;
    padding: 4px 28px 32px;
  }}
  .card {{
    display: flex;
    flex-direction: column;
    background: #1e1f24;
    border: 1px solid #2a2b30;
    border-radius: 10px;
    overflow: hidden;
    text-decoration: none;
    color: inherit;
    transition: transform 0.12s ease, border-color 0.12s ease;
  }}
  .card:hover {{
    transform: translateY(-2px);
    border-color: #3d4046;
  }}
  .card.hidden {{
    display: none;
  }}
  .card-image {{
    width: 100%;
    aspect-ratio: 16 / 9;
    background: #26272c;
    overflow: hidden;
  }}
  .card-image img {{
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }}
  .card-body {{
    padding: 14px 16px 16px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    flex: 1;
  }}
  .card-body h3 {{
    margin: 0;
    font-size: 15px;
    line-height: 1.4;
    font-weight: 600;
    color: #f2f2f4;
  }}
  .card-meta {{
    margin-top: auto;
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
    font-size: 12px;
    color: #8a8d94;
  }}
  .card-meta .tag {{
    background: hsla(var(--hue), 70%, 55%, 0.18);
    color: hsl(var(--hue), 85%, 78%);
    font-weight: 600;
    font-size: 11px;
    padding: 2px 9px;
    border-radius: 999px;
    white-space: nowrap;
  }}
  .card-meta .date {{
    color: #8a8d94;
  }}
  .empty-state {{
    display: none;
    padding: 60px 28px;
    text-align: center;
    color: #6b6e75;
    font-size: 14px;
  }}
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
      <h1>Particle Physics News</h1>
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
            if (active.has(src)) {{
              active.delete(src);
              chip.classList.remove('active');
            }} else {{
              active.add(src);
              chip.classList.add('active');
            }}
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


def generate() -> str:
    results = scrape_all_sites()
    now = datetime.now(timezone.utc)

    all_articles = [a for articles in results.values() for a in articles]
    all_articles.sort(key=lambda a: a.pub_date or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    hues = source_hues(list(results.keys()))

    month_groups = group_by_month(all_articles)
    sections = "".join(
        render_month_section(key, articles, now, hues, is_first=(i == 0))
        for i, (key, articles) in enumerate(month_groups.items())
    )

    page = PAGE_TEMPLATE.format(
        count=len(all_articles),
        source_count=len(results),
        generated=now.strftime("%Y-%m-%d %H:%M UTC"),
        chips=render_chips(results, hues),
        sections=sections,
    )
    return page


if __name__ == "__main__":
    html_page = generate()
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html_page)
    print(f"Wrote digest to {OUTPUT_FILE}")
