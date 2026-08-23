# News Bot

A local, always-on app that scrapes particle-physics / gravitational-wave
news sources, stores every article in SQLite, and notifies you of what's
*new* once a day — through a local web UI, a Discord bot, and (optionally)
email. It replaced an earlier stateless `news_bot.py` script (still visible
in git history) that only ever posted individual embeds or a freshly
re-scraped digest file with no persistence, dedup, or way to manage sources
except editing Python.

## Architecture

**One process, one asyncio event loop** (`main.py`): FastAPI (via uvicorn,
`web.py`) and the discord.py bot (`discord_bot.py`) run concurrently via
`asyncio.gather`. Both need the same event loop — this is why they're one
process, not two separately-launched services. `main.py` is the only
correct way to run this app; running `web.py` or `discord_bot.py` directly
would only start half of it.

- `db.py` — SQLite schema + tiny connection/settings helpers. One file,
  `news.db` (gitignored — real, growing user data, same convention as
  `.env`).
- `scraper.py` — the scraping engine: RSS/Atom + CSS-selector extraction,
  feed autodiscovery (`detect_feed`), and DB read/write helpers
  (`persist_articles`, `get_articles`, `mark_notified`).
- `digest.py` — renders a list of `scraper.Article` into the dark,
  month-grouped, source-tagged, filterable HTML digest. Reused for three
  different outputs: `!digest`/`!forcedigest` (Discord), the daily
  `@everyone` post, and the newsletter email body — one rendering
  implementation, never duplicated per output.
- `mailer.py` — SMTP sending (stdlib `smtplib`, zero extra dependency) for
  the newsletter-email subscription feature. A no-op-with-a-log-warning,
  not a crash, when SMTP isn't configured — email is an optional channel
  alongside Discord, not a required one.
- `discord_bot.py` — bot commands (`!news`, `!sites`, `!digest`,
  `!forcedigest`) plus `notify_discord()`, called by the scheduler.
- `scheduler.py` — `run_daily_job()` (scrape → persist → notify Discord +
  email → mark notified) and the `tasks.loop(time=...)` wrapper around it.
- `web.py` — the REST API + static file serving for the frontend.
- `static/` — vanilla HTML/JS/CSS frontend, no build step, no framework.
  Talks to `web.py` purely over `fetch()`.
- `seed_sources.py` — one-time-but-safely-rerunnable seed of the 20 sources
  already reverse-engineered for this project before the DB-backed rewrite
  (15 real feeds + 5 CSS-scraped fallbacks — see "Adding a source" below).
  `main.py` calls `seed_sources.seed()` on every startup; it's a no-op once
  those 20 already exist (matched by name, `INSERT OR IGNORE`).

## Data model (`db.py`)

- `sources` — one row per website/newsletter/job board. `kind` (`rss`/`css`)
  picks the extraction method; `category` (`website`/`newsletter`/
  `social`/`job`) is purely a UI grouping (which Sources-tab section, Feed
  vs Jobs tab), all four are scraped identically. A `css` source needs
  `article_selector`/`base_url` always; `title_selector`/`link_selector`
  are usually required too but may be left blank to mean "the article
  container itself is the title/link element" (see "The scraping engine"
  below); `date_selector`/`date_year_selector`/`deadline_selector`
  (`job`-only, see "Job deadlines" below)/`forced_job_tag` (`job`-only,
  see "Job tags" below)/`image_selector`/`user_agent` are optional on
  either kind.
- `articles` — every article ever seen, deduped by `link` (`UNIQUE`
  constraint; `persist_articles()` uses `INSERT OR IGNORE` and reports
  which ids were newly inserted). `notified` (0/1) is how the app tracks
  "have I already told someone about this" — **this is the whole
  dedup/new-detection mechanism**, replacing the old script's
  `seen_links.json`. `deadline` (job application deadline) and `job_tag`
  (Fellowship/PhD/.../`NULL`) are both `job`-category-only in practice,
  `NULL` everywhere else — see "Job deadlines" and "Job tags" below.
  `archived` (0/1, default 0) hides an article from ordinary browsing
  without deleting it — see "Archiving a job offer" below; currently only
  ever set via the Jobs tab's own archive button, but the column itself
  isn't category-restricted. `excerpt` (short plain-text blurb, `NULL` for
  every `kind="css"` source and every article scraped before this column
  existed) feeds the aggregate RSS feed's `<description>` — see
  "Aggregated RSS feed" below.
- `job_tags` — user-editable `name` + `keywords` (JSON array) rows driving
  `job_tag` classification, seeded once with 5 starter tags
  (`DEFAULT_JOB_TAGS`), then entirely user data — see "Job tags" below.
- `folders` / `favorites` — a favorite is a row in `favorites` keyed by
  `article_id`, optionally pointing at a `folder_id`. No folder = "just
  favorited, unsorted" (`folder_id IS NULL`), not a separate concept.
- `newsletter_subscribers` — just an email + timestamp; anyone can
  self-register via the web UI, no confirmation email loop (this is a
  single-user personal tool, not a public product).
- `settings` — key/value, seeded once from `.env` on first run
  (`DEFAULT_SETTINGS` in `db.py`) and editable afterward via the web UI's
  Settings tab (`PUT /api/settings`) *without* touching `.env` again. Only
  non-secret, occasionally-changed values live here (digest time/timezone,
  Discord channel id) — `DISCORD_TOKEN` and `SMTP_*` stay in `.env` only,
  never exposed through the API, even though this is a local-only app. Also
  holds `last_digest_run_date` (see "Missed-run catch-up" below) — an
  internal bookkeeping value, not something the Settings tab exposes.
- `sources.last_checked_at` / `last_status` / `last_error` and
  `articles.content_tag` were added after the initial DB-backed rewrite —
  see `COLUMN_MIGRATIONS` in `db.py` for the `ALTER TABLE` migration
  pattern (`CREATE TABLE IF NOT EXISTS` is a no-op on an already-existing
  table, so a genuinely new column needs this every time, checked against
  `PRAGMA table_info` first so it's a harmless no-op once applied). Follow
  this same pattern for the *next* new column — don't just edit `SCHEMA`
  and assume existing installs pick it up. `sources.date_year_selector`/
  `deadline_selector`/`forced_job_tag`, `articles.deadline`/`job_tag`/
  `archived`/`excerpt`, and `job_tags.sort_order` are the most recent ones
  added this way — note
  `job_tags.sort_order` needed an *extra* backfill step beyond the plain
  `ALTER TABLE ADD COLUMN` (`UPDATE job_tags SET sort_order = id WHERE
  sort_order IS NULL`, run every startup, harmless once every row has a
  real value): a bare new nullable column would otherwise leave existing
  tags with `sort_order IS NULL`, and SQLite sorts `NULL` *first* in
  ascending order — silently reordering already-configured tags'
  classification priority the moment `ORDER BY sort_order` shipped, not
  just leaving them unordered. `sources.category`'s `job` value is a
  different kind of migration (a `CHECK` constraint value, not a new
  column) — see "Job/thesis offer sources" below for why that needs a
  real table recreate, not an `ALTER`.

## The scraping engine (`scraper.py`)

Two extraction modes, same as the original script:
- **`rss`** — parsed via stdlib `xml.etree.ElementTree`, no extra
  dependency. Handles both RSS 2.0 (`channel/item`) and Atom
  (`{atom}entry`). A few real-world gotchas already fixed here, don't
  regress them:
  - **Bare `&` in item text is invalid XML** and makes `ET.fromstring`
    reject the *whole* feed (hit on cerncourier.com) — `BARE_AMPERSAND_RE`
    patches literal `&` (not already part of a real entity like `&amp;`)
    to `&amp;` before parsing. This is a general fix, not
    source-specific — any future feed with the same issue is already
    covered.
  - **Dates**: RFC 822 (`pubDate`) via `email.utils.parsedate_to_datetime`;
    Atom's `published`/`updated` via `datetime.fromisoformat`. Both are
    forced timezone-aware (defaulting to UTC if the source omitted an
    offset) — naive datetimes crash `discord.Embed(timestamp=...)` and
    complicate sorting.
  - **Images**: cascades `media:thumbnail` → `media:content` →
    `enclosure[type=image/*]` → first `<img>` found inside
    `content:encoded` or `description` HTML. Protocol-relative URLs
    (`//host/...`, seen from ATLAS) are upgraded to `https:` —
    `discord.Embed`/`<img>` need a fully-qualified URL.
  - **Transient malformed responses**: one automatic retry on
    `ET.ParseError` (observed intermittently on interactions.org) before
    giving up on that source for the run.
  - **User-Agent blocking**: some sources (interactions.org) 403 the
    default UA but allow a browser-like one — `sources.user_agent`
    overrides per-source, applies to both `rss` and `css` kinds.
- **`css`** — CSS-selector scraping via BeautifulSoup, for sites with no
  usable feed. Date parsing is a fixed fallback chain, tried in order until
  one succeeds — extend *this chain*, don't fork a new one, if a future
  source needs yet another format:
  1. A `<time datetime="...">` attribute (`datetime.fromisoformat`) —
     tried first since it's unambiguous and locale-independent by
     construction.
  2. `TEXT_DATE_PATTERNS` — full-English-month "Month D, YYYY"
     (`%B %d, %Y`), `MM.DD.YYYY`, and `YYYY.MM.DD` (added for JUNO's
     `"(2026.06.12)"` — distinguished from `MM.DD.YYYY` by the first group
     being 4 digits, so the two patterns can't ambiguously match the same
     text).
  3. `parse_english_date()` — **abbreviated** English month names
     ("Oct 9, 2025", SuperNEMO; "02 April 2026", Auger's day-first order).
     `%b`/strptime would parse these but is exactly the locale-dependent
     behavior this file otherwise avoids (same reasoning as
     `FRENCH_MONTHS`'s comment) — hand-rolled instead via `EN_MONTHS` +
     `_english_month_number()`, the identical `name.startswith(abbrev)`
     shape as `_french_month_number`. Two regexes, `EN_DATE_DMY_RE`
     (digits first) and `EN_DATE_MDY_RE` (letters first) — mutually
     exclusive by construction (a string can't start with both a digit and
     a letter), so trying both in either order is safe. **Before this
     existed**, a handful of abbreviated English months (Oct/Nov/Dec/May)
     happened to parse "correctly" anyway, purely by coincidence — their
     3-letter form is also a valid prefix-match against `FRENCH_MONTHS`
     (`"oct"`.startswith(`"oct"`), etc.), so they silently fell through to
     `parse_french_date()` and got the right answer for the wrong reason,
     while Jan/Feb/Jun/Jul/Sep silently returned `None`. Caught testing
     SuperNEMO's dates by hand across all twelve months, not by symptom —
     nothing was visibly broken until checked systematically.
  4. `parse_french_date()` — day-month-year ("24 juil. 2026") and, since
     GANIL's news list uses the reverse order ("juin 15, 2026", no comma),
     also a month-day-year fallback (`FRENCH_DATE_MDY_RE`) tried after the
     primary day-first pattern.
  - **`sources.date_year_selector` (optional): for pages that group
    listings under a separate year heading instead of repeating the year on
    every item.** Subatech's `actualites` page is the real example this was
    built for: each item's own `date_selector` only ever yields day+month
    ("22 juil.", no year at all — CSS can't express "nearest preceding
    sibling of an ancestor" to reach the `<h3>2026</h3>` that precedes a
    whole block of items). When `parse_css_date()` on the item's own date
    text comes back `None` *and* that text has no 4-digit year in it,
    `scraper.find_preceding_year()` walks backwards from the article
    container in real document order (`find_all_previous`, nearest first)
    testing each element against `date_year_selector` with
    `soupsieve.match()` (already a bs4 dependency, used for `:has()`
    elsewhere) — the first match's text becomes the year, combined with the
    day/month text and re-parsed via `parse_french_date()`. Leave this
    blank for every ordinary source (the vast majority, where the year is
    already part of the item's own date text) — it's a no-op unless set.
  - **Both `title_selector` and `link_selector` may be left blank,
    meaning "the article container itself is the title/link element."**
    Some listing pages have no wrapping element around a bare `<a>` at all
    for `container.select_one(...)` to find as a *descendant* — Subatech's
    `theses` page is the example: `<a href="...">Title text</a>` sits
    directly under `<ul>`, no `<li>`, nothing to select_one() into. When
    either selector is blank, `scrape_css()` uses the container itself as
    that element (`title_el.get_text()`/`link_el.get("href")` both work
    fine on any Tag, container included). Every source that predates this
    has a real, non-blank selector, so this is a no-op for them — don't
    leave both blank on a source where a real per-item wrapper element
    exists; the descendant-selector path is strictly more precise when
    it's available.

`MAX_ARTICLES_PER_FETCH` (10) caps how many articles are pulled per source
per scrape — this bounds both scrape time and DB growth per run, not a
"only 10 exist" assumption.

## Source health tracking

`scrape_source_safe()` (not `scrape_source()` — see below) never raises,
and always records `last_checked_at`/`last_status`/`last_error` on the
source row as a side effect, regardless of outcome. Three statuses, not
two, because "fetched fine but found nothing" and "fetch/parse genuinely
failed" are different problems with different fixes:
- `"ok"` — fetched, found articles.
- `"empty"` — fetched successfully, zero articles extracted. For a `css`
  source this usually means the site changed its HTML and the selectors
  need updating; for `rss` it's unusual (feeds almost always have at least
  one item) but not inherently broken.
- `"error"` — the fetch or parse itself failed (network error, non-200,
  totally malformed response). Raised internally as `scraper.ScrapeError`
  from `scrape_rss`/`scrape_css`, caught here so one broken source can't
  take down `scrape_all_sources()`'s loop over the rest.

`scrape_source()` (the plain dispatcher) *does* raise `ScrapeError` on
fetch failure — it's the low-level building block. Every real call site
(`scrape_all_sources()`, `discord_bot.py`'s `!news` command) goes through
`scrape_source_safe()` instead, so both the daily job *and* a manual
Discord check keep the Sources tab's health indicator current. If you add
a third call site that scrapes a single source, use `scrape_source_safe()`
too, not the raising version — otherwise that source's health silently
stops updating from that path.

## Content tagging

`scraper.classify_article(title)` tags articles by **physics topic** —
what the article is actually about (Neutrino, Muon, Higgs, Gravitational
Waves, Dark Matter, ...) — not by article type. An earlier version tagged
by type (Obituary/Award/Personnel/Discovery/Event/Jobs/Funding/Outreach);
that was explicitly replaced, not layered on top, so don't reintroduce it
as a second tagging dimension without being asked.

Ordered, first-match-wins keyword regexes (`CONTENT_TAG_RULES`), each
covering **both English and French** — several primary sources publish in
French (CERN's `/fr/feed/`, IN2P3, CNRS News) and would otherwise fall
into the `General Physics` default just for being in the "wrong"
language. The term-equivalence table below is the source of truth this
list was built from; extend *this table* first when adding a topic, then
mirror it into `CONTENT_TAG_RULES` — don't add French terms to the code
without a corresponding table row, or the two will drift apart silently.

| Topic | English | French |
|---|---|---|
| Gravitational Waves | gravitational wave(s), LIGO, Virgo, KAGRA, GWTC | onde(s) gravitationnelle(s) |
| Neutrino | neutrino(s) | neutrino(s) *(same word)* |
| Muon | muon(s) | muon(s) *(same word)* |
| Higgs | Higgs | Higgs *(same word)* |
| Dark Matter | dark matter | matière noire |
| Dark Energy | dark energy | énergie noire |
| Antimatter | antimatter, antiproton | antimatière |
| Black Holes | black hole(s) | trou(s) noir(s) |
| Superconductivity | superconduct* | supraconduct* |
| Quantum Computing | quantum comput*, qubit(s) | ordinateur/informatique quantique |
| Standard Model | standard model | modèle standard |
| Accelerator & Collider | LHC, large hadron collider, accelerator(s), collider(s), beamline(s) | accélérateur(s), collisionneur(s) |
| Cosmic Rays | cosmic ray(s), cosmic particle(s) | rayon(s) cosmique(s) |
| Detector Technology | detector(s), sensor(s) | détecteur(s), capteur(s) |
| Exotic Particles | exotic particle(s), tetraquark(s), pentaquark(s), baryon(s), hadron(s) | particule(s) exotique(s) |
| *(default)* | `General Physics` — no topic matched | *(same fallback for both languages)* |

Deliberately **title-only, no ML/API call, no body-text dependency** —
title is the only text reliably available across every source (CSS-scraped
sources have no body/description at all). This means genuinely
topic-generic articles (funding news, personnel changes, obituaries,
event announcements) correctly land in `General Physics` rather than being
forced into a topic they're not really about — roughly 55-60% of real
articles do, by design, not as a classifier weakness to chase down.

`Article.__init__` computes this automatically (`content_tag or
classify_article(title)`) whenever `content_tag` isn't passed in, so any
freshly-scraped article always has one. Two backfill paths, for two
different situations — **use the right one**:
- `scraper.backfill_content_tags()` — fills only `content_tag IS NULL`
  rows (predates the feature entirely). Safe to leave running on every
  startup (`main.py` does), since it's a no-op once nothing is NULL.
- `scraper.retag_all_articles()` — recomputes **every** row, used once
  when `CONTENT_TAG_RULES` itself changed (the type→topic redesign was the
  first time this ran). Not wired into startup — run it by hand after a
  rules change: `python -c "import scraper; scraper.retag_all_articles()"`.

**Read paths that bypass `Article`/`get_articles()` won't get the
constructor's fallback** — `web.py`'s `/api/articles` learned this the
hard way (it runs its own SQL for the favorites/folder `LEFT JOIN`, so
every article showed `content_tag: null` until a matching defensive
fallback was added there too). If you add another raw-SQL read path over
`articles`, add the same `classify_article()` fallback, or route it
through `get_articles()` instead.

## Adding a source

The web UI's "Add source" form (`static/app.js`'s `sourceFormHtml()`) has
**three buttons side by side, not an auto-detect-then-fallback flow** —
auto-detection is the default path, but a manual override is always
available, not just shown after detection fails. `category` is a separate,
orthogonal choice — `website`/`newsletter`/`social`, purely a UI grouping,
all scraped identically once you have a feed URL or selectors.

1. **"Auto-detect feed"** runs `scraper.detect_feed(url)`, checked in this
   order:
   1. **The given URL might already *be* a feed** — e.g. a YouTube
      `feeds/videos.xml?channel_id=...` URL found by hand and pasted
      directly. Checked first (one fetch, its result reused for the next
      step too if it's *not* already a feed) since it's the cheapest and
      most certain check.
   2. The page's own `<link rel="alternate" type="application/rss+xml|
      atom+xml">` tags.
   3. Feed suffixes (`PATH_SUFFIXES`: `/.rss`, `.rss`, `/.atom`, `/rss`,
      `/rss.xml`, `/feed`) appended to the **exact given URL's own path**
      — covers per-account feeds a site-root guess would never find, like
      Reddit's `/r/name/.rss` or Mastodon's `/@user.rss`.
   4. A fixed list of common paths (`COMMON_FEED_PATHS`: `/feed/`,
      `/rss.xml`, `/feed.xml`, `/rss/`, `/atom.xml`, `/index.rss`) against
      the URL's **root origin** — for site-wide blog feeds.

   Every candidate is verified by actually parsing it (`_looks_like_feed`),
   not just checking the HTTP status — a 200 with an HTML error page is a
   real failure mode this guards against. **A real bug this had: "parses
   without an `ET.ParseError`" isn't the same as "is a feed."**
   `_looks_like_feed` originally returned `True` the moment `ET.fromstring`
   didn't raise, without checking the parsed result actually contained any
   `<item>`/`<entry>`. Well-formed HTML (all tags properly closed — common
   from modern frameworks, e.g. a Next.js-rendered site) parses as valid
   XML with zero matches, so a bare homepage with no feed at all was
   silently accepted as "a feed" — caught for real: `detect_feed()` on
   Subatech's plain homepage returned the homepage URL *as its own feed*.
   This also meant a genuinely-empty-but-syntactically-valid RSS document
   (IJCLab's site-wide `/en/feed/`, which parses fine but has zero
   `<item>`s) was accepted too, offering a technically-real but useless
   feed instead of correctly reporting "nothing found." Fixed by checking
   `len(list(parse_feed_items(content))) > 0`, not just that parsing
   didn't throw. Probing 404s here are *expected*,
   not errors — logged at `debug`, not `error` (`quiet=True` on the fetch
   helpers), so a normal "no feed, fall back to manual" outcome doesn't
   spam the log like a real failure would. **Known limitation, not chased
   further**: if step 3's more-specific candidate transiently fails (a
   rate limit, a network blip — observed on Reddit during rapid testing)
   but step 4's less-specific one happens to succeed, the function returns
   a technically-valid but too-broad feed with no retry. The web UI shows
   the discovered feed URL before you commit to adding it specifically so
   this is visible/reviewable, not just silently wrong.
2. **"This URL is already a feed"** skips detection entirely: the typed
   URL is submitted verbatim as `kind: "rss"`, with **no server
   round-trip and no verification that it actually parses**. For a user
   who already knows their exact feed URL (found by hand, e.g. a YouTube
   channel feed a consent wall prevented auto-discovering — see "Social
   media sources" below), waiting on `detect_feed()`'s multi-step probing
   is pure latency with no chance of a better answer; this exists
   specifically to skip that wait. The status line says "not verified" for
   exactly this reason — a typo here won't be caught until the first real
   scrape reports `"error"` status in the Sources tab, not at add-time.
3. **"Configure manually"** jumps straight to the CSS-selector fields
   without needing an auto-detect attempt to fail first — useful when you
   already know a feed doesn't exist for this page, or already worked out
   the right selectors elsewhere. The same shape `seed_sources.py`'s 5 CSS
   sources use: article/title/link/base-url selectors, plus optional
   date/image ones.

   This is a real, hands-on step — inspect the page's HTML (browser dev
   tools, "Inspect Element") to find the right selectors, same as every
   CSS source in `seed_sources.py` was built. There is no heuristic
   fallback that guesses selectors from a bare URL; that was considered
   and explicitly not built (see the original chat's "Add source" design
   question) — it would be unreliable enough that it'd need manual
   correction almost every time anyway, no better than just asking for
   selectors directly. **Practical steps for finding selectors**: open the
   page, right-click one article/story card → Inspect. Find the smallest
   element that wraps one whole story and repeats once per story (its tag
   + class, e.g. `div.post-card`) — that's the article container. Inside
   it, find the title text's element (`title_selector`) and the `<a>` tag
   for the link (`link_selector`, often the same element as the title, or
   its direct parent). `base_url` is just the site's origin, used to
   resolve relative `href`s. Avoid a browser's "Copy selector" devtools
   command for this — it produces an over-specific path (often with
   `:nth-child(3)`-style indices) matching only *that one* element, not
   every story on the page; a plain class-based selector generalizes,
   an indexed path doesn't.
3. **A generic auto-discovery *website-wide* feed existing doesn't mean
   it's the right one** — several real sources hit this: IN2P3's
   `/rss.xml`, IPPOG's `/rss.xml`, and CNRS News' `/rss.xml` all exist and
   parse fine, but are the *site's entire content feed* (events, member
   profiles, every topic), not scoped to particle physics. These three
   are seeded as `css` sources for exactly this reason — a feed
   "existing" isn't sufficient, it has to actually match what you asked
   for. `detect_feed()` can't detect this automatically; if a newly-added
   auto-detected feed turns out to be too broad, delete it and re-add
   manually with CSS selectors scoped to the right listing page.

**Editing an already-added source**: `PUT /api/sources/{id}`
(`SourceUpdate` in `web.py`) accepts every field a source has, including
`kind` and `category` — both were originally missing from that model (only
addable at creation), fixed once "modify an existing source" became a real
need. `static/app.js`'s Sources tab has an "Edit" button per row
(`openEditSourceForm()`) that inserts the form as a real DOM sibling
**right after that specific `.source-row`** (`rowEl.insertAdjacentElement
("afterend", wrapper)`), not into the shared `#add-source-form` container
at the top of the tab — that was the first version, and editing a source
near the bottom of a long list meant the form popped up scrolled away from
the row you were looking at. Only one inline edit form can be open at a
time (`openEditSourceForm` removes any existing `.inline-edit-form` before
inserting a new one); `loadSources()`'s full `innerHTML` replacement of the
row-list container also implicitly clears it on any refresh, so a stale
form can't survive a reload. Pre-filled with the row's current values;
unlike the add-flow it has no auto-detect step, since you're editing
something that (presumably) already resolved to a working config once —
it's a plain form covering every field at once, submitted as a single PUT.

**Group is editable without opening this form at all** — each source row
has its own `<input class="group-input">` (a plain text field with a
shared `<datalist id="existing-groups-inline">` for autocomplete,
rebuilt from `state.sources` on every `loadSources()`), PUTing just
`{group_tag: ...}` on `change`. This is the one field with a faster path
than the full Edit form, added specifically because re-grouping sources is
a much more frequent action than reconfiguring their scrape settings — and
unlike the Edit form's fields, this input's `change` handler sends an
explicit empty string (not `null`) when cleared, so — unlike every other
field in this app's sparse-PUT convention — **this is the one place a
group actually can be cleared back to unset**, not just replaced.

Same sparse-PUT
contract as the rest of this API: an omitted field is left alone, not
cleared, so you can't currently blank out an optional field (`user_agent`,
`date_selector`, etc.) back to empty through this form — only replace it
with a new value. **The edit form's own `kind` field switching to `css`
does not client-side-validate that the selector fields got filled in** —
if you save with them empty, the source just reports `"error"` status on
its next scrape (`BeautifulSoup.select(None)` raises, caught by
`scrape_source_safe()`'s broad exception handler), which is a real,
visible-in-the-Sources-tab failure mode, not a crash. Acceptable given the
existing safety net; not worth extra form validation for.

**CSS-selector sources without a real per-article `href`**: some pages
(CPPM's news listing is the concrete case this was built for) render each
article's "link" as an `<a id="...">` used purely for in-page anchor
navigation, with no `href` at all. `scrape_css()` falls back to that `id`
when `href` is empty, constructing `<source url>#<id>` as the article's
link — a genuine, distinct, working URL (it really does scroll to that
section), even though every article on the page technically shares the
same base URL. CPPM's actual config is a good worked example of this plus
several other patterns at once: `article_selector: ".box"`,
`title_selector: "h1.title"`, `link_selector: "a.anchor-section:last-of-type"`
(the page emits three anchor `<a id>`s per article — a generic
`"section_N_"` one, the raw untouched title, and a slug+hash one; `:last-of-type`
reliably lands on the slug one without hardcoding a position count),
`date_selector: "font i"`, `image_selector: ".content img"`.

**French-language dates**: `parse_french_date()` handles "24 juil. 2026,
09:09:38"-style text (CPPM's `date_selector` above relies on this) via a
hand-built month-name lookup (`FRENCH_MONTHS`), not `strptime`'s `%B` --
that's locale-dependent, and trusting the server to have a French locale
installed would be fragile in exactly the same way the RFC822/ISO date
handling elsewhere in this file already avoids trusting locale-sensitive
parsing. This is a real, recurring need, not a one-off: CERN's `/fr/feed/`,
IN2P3, and CNRS News are also French-language sources — if a new source's
`date_selector` text isn't parsing, check whether it's a French date before
assuming the selector itself is wrong.

`_french_month_number()` matches **both** abbreviated ("juil.") and full
("juillet") month names against the same `FRENCH_MONTHS` lookup table, via
a `startswith()` check rather than two separate tables — every French full
month name happens to start with its own standard abbreviation, so this
covers both forms for free. Caught for real: LPC's dates
(`date_selector: "p div"`) are "3 juillet 2026" (full name, no time),
unlike CPPM's abbreviated-with-time format — the initial abbreviation-only
lookup silently failed on LPC's dates (`_french_month_number` returned
`None` for `"juillet"`) until this was generalized. If a future French
source's dates still don't parse, check for a genuinely different format
(different punctuation, day-of-week prefix, etc) before assuming the month
lookup needs yet another entry — the lookup itself is already
form-agnostic.

**LPC** (`clrwww.in2p3.fr`) is a simpler worked example than CPPM — real
per-article `href`s (no anchor-id fallback needed), `article_selector:
"li.item.actualites"`, `title_selector`/`link_selector` both just `"a"`
(the whole card is one anchor), `date_selector: "p div"` (a `<div>` mixing
a Font Awesome calendar icon with the date text — `get_text()` only picks
up the text, the icon has none of its own).

## Social media sources

`category = 'social'` was added after `website`/`newsletter` already
existed — since SQLite can't `ALTER` a `CHECK` constraint in place,
`db._ensure_social_category()` does a real recreate-and-copy migration
(rename `sources` → `sources_old`, recreate under the new constraint from
`SCHEMA`, copy every column back via `PRAGMA table_info` so it doesn't need
updating if more columns get added later, drop the old table). Runs once
at startup via `init_db()`, after `_ensure_columns()` so there's a
complete column set to copy, and is a no-op forever after via a check
against the table's own stored `CREATE TABLE` SQL text.

**This migration corrupted production data once, silently, for hours —
see the next section.** Don't copy its original `PRAGMA foreign_keys =
OFF` / `ON` pattern alone for a future rename-based migration; read
`_ensure_social_category()`'s docstring in `db.py` first.

## Incident: a rename-based migration silently broke `articles`' foreign key

**What happened**: `_ensure_social_category()`'s `ALTER TABLE sources
RENAME TO sources_old` didn't just rename the table — SQLite's default
`legacy_alter_table=OFF` behavior *also* rewrites the stored schema text of
every other table's foreign keys that reference it. `articles.source_id
REFERENCES sources(id)` silently became `REFERENCES "sources_old"(id)` the
instant the rename ran, and stayed that way through the rest of the
migration (recreating `sources` under its new name did nothing to fix
`articles`' already-rewritten schema). `PRAGMA foreign_keys = OFF` — which
the migration already had — only suppresses *runtime constraint
enforcement*; it has nothing to do with whether a rename rewrites other
tables' schema text. Those are two unrelated settings, and only one of
them was handled.

**Why it stayed invisible**: `INSERT OR IGNORE` (used everywhere articles
are persisted) resolves a `UNIQUE` conflict *before* ever evaluating the
`FOREIGN KEY` constraint — so as long as every scraped article that run
happened to already exist (the normal steady state for a site with no
genuinely new content), every insert short-circuited on the duplicate
`link` and the broken FK was simply never reached. The bug shipped, the
app kept running, `!forcedigest`/the daily catch-up both "succeeded" (with
"no new articles"), for however long it took a real new article to show
up. The first one to appear crashed `POST /api/run-now` with
`sqlite3.OperationalError: no such table: main.sources_old`, a 500 the
user hit directly.

**Why the existing safety check didn't catch it either**: this project
already had an orphan-row check (`source_id NOT IN (SELECT id FROM
sources)`), used to verify the migration right after writing it. That
check only compares *id values* — it stayed green the entire time,
because every already-existing article's `source_id` genuinely did match
a real row in the new `sources` table. The corruption was in the
*schema text* of a constraint definition, not in any row's data, which a
value-level check structurally cannot see.

**The fix, in two parts**:
1. `PRAGMA legacy_alter_table = ON` for the duration of any table rename
   in a migration — this makes `RENAME` a pure rename, with no side effect
   on other tables' stored schema. Added to `_ensure_social_category()`;
   use it for any future rename-based migration too, not just this one.
2. `db._check_foreign_key_integrity()`, run once at the end of every
   `init_db()` call via `PRAGMA foreign_key_check` — this inspects actual
   constraint *definitions*, not row values, so it's the check that would
   have caught this immediately (verified by running it against a restored
   pre-fix backup: it flagged all 197 articles at once). Logs loudly, does
   not raise — a corrupt FK doesn't stop the app from serving whatever
   already works, and raising here would turn a data problem into an
   availability problem.

**Recovery** (what was actually run against the live DB, in case a similar
fix is ever needed again): back up `news.db` first, then a recreate-and-copy
of `articles` itself — rename to `articles_broken`, recreate `articles`
fresh from `SCHEMA` (correct FK), copy every column back via `PRAGMA
table_info`, drop the old table — with the same `legacy_alter_table = ON`
guard this time. Verified after: `articles`' own stored schema once again
says `REFERENCES sources(id)`; `favorites.article_id REFERENCES
articles(id)` (a *third* table, depending on the one just renamed) was
checked too, and confirmed *not* corrupted, since `legacy_alter_table=ON`
was active for that rename from the start.

**Which platforms actually work, verified against real accounts, not
assumed:**
- **Mastodon** — works well. Every account has a public `/@user.rss` feed,
  auto-discovered via the profile page's own `<link rel=alternate>` tag
  with zero extra code. Verified against `@Mastodon@mastodon.social` (20
  real posts parsed correctly).
- **Reddit** — works. `/r/<subreddit>/.rss` is public and un-authenticated;
  found via the new path-suffix detection tier above (not autodiscovery —
  Reddit's subreddit pages don't carry a scoped `<link>` tag). Subject to
  Reddit's own rate limiting on rapid repeated requests (see the
  known-limitation note above).
- **YouTube** — partially works, with a real constraint: the human-facing
  channel page (`youtube.com/@handle`) is behind a cookie-consent wall that
  a plain `requests` fetch can't get past (confirmed: got a consent-wall
  page, not the real channel page), so the channel ID can't be
  auto-discovered from a pasted profile URL. **Bypassing a consent wall
  wasn't attempted or built — that's a legal/regulatory gate, not a
  technical obstacle to route around.** What does work: the feed endpoint
  itself, `youtube.com/feeds/videos.xml?channel_id=...` (or the older
  `?user=<legacy-username>` form, for channels that still have one — e.g.
  `?user=fermilab` was verified working), is plain public XML with no
  wall. If you already have or can find your channel's feed URL by hand,
  paste *that* directly into the URL field — `detect_feed()`'s first check
  is "is the given URL already a feed," so it's accepted immediately with
  no further discovery needed.
- **Not supported, and not planned**: Twitter/X, Instagram, Threads,
  Bluesky. None expose a public feed for arbitrary accounts, and reaching
  their content would mean building around anti-bot/auth measures — the
  same principle already established elsewhere in this project (see the
  SuperSoluce/PSNProfiles-icon history in earlier design notes): don't
  build a bypass for a real access control, use a different source instead.

**Real bug this surfaced and fixed, not social-specific despite being
found there**: Mastodon toots have **no `<title>` element at all** in
their RSS `<item>` — a toot isn't titled content the way a blog post is,
so Mastodon's feed generator omits the tag entirely rather than emitting
an empty one. `parse_feed_items()` required both `title` and `link` to
yield an item, so every toot was silently dropped (0 articles found, no
error — status `"empty"`, correctly not `"error"`, since the fetch and
parse both genuinely succeeded). Fixed generally, not as a Mastodon
special-case: `derive_title()` falls back to the item's own `description`
text (stripped of HTML, truncated at a word boundary) whenever `<title>`
is missing — benefits *any* titleless RSS item, not just Mastodon's.

## Notification flow ("send *new* news")

`scheduler.run_daily_job()`:
1. `scraper.scrape_and_persist_all()` — scrape every enabled source,
   `INSERT OR IGNORE` into `articles` (deduped by `link`), returns the ids
   that were *actually new* this run.
2. If nothing new: log and stop. No Discord post, no email — "new news"
   means don't notify when there isn't any, matching the original bot's
   steady-state behavior.
3. If there's something new: `discord_bot.notify_discord()` posts one
   `@everyone` heads-up message (`AllowedMentions(everyone=True)` — **also
   needs the bot to actually have the "Mention Everyone" server
   permission**, or the ping silently doesn't notify anyone even though the
   message sends), then one individual embed per new article (oldest
   first, so the channel reads top-to-bottom chronologically) — **not** a
   digest file; that was the first design and was explicitly changed
   (individual messages read better in a chat channel, and `!digest`
   already covers "I want the downloadable file" separately). Same new
   articles also become the body of one `digest.render_digest_html()` email
   to every `newsletter_subscribers` row — the file-shaped digest is still
   the right shape for email, just not for Discord.
4. `scraper.mark_notified(new_ids)` — only *after* both notify attempts,
   so a crash before this point means the next run will just include
   those articles again rather than silently losing them.
5. `scheduler._mark_ran_today()` — records today's date (in the digest
   timezone) in `settings.last_digest_run_date`, regardless of whether
   anything was new. This is what "Missed-run catch-up" below checks, and
   is deliberately updated even on a nothing-new run — "checked today,
   found nothing" and "haven't checked today yet" need to read differently
   to that logic, or a genuinely quiet news day would look identical to a
   missed run and trigger a same-day re-check for no reason.

## Missed-run catch-up

If the app wasn't running at the scheduled time (PC was off, crashed,
etc), `tasks.loop(time=...)` just silently waits for tomorrow's
occurrence — it does **not** fire immediately on a late start. That's the
literal, if slightly counterintuitive, behavior of a time-of-day loop, and
it means "missed" news could otherwise go unsent for a full extra day.
`scheduler.catch_up_if_missed()` (called once from `discord_bot.py`'s
`on_ready`, right after `scheduler.start()`) covers this: if today's
scheduled time has already passed and `last_digest_run_date` isn't today,
it runs `run_daily_job()` immediately instead of waiting. Naturally
idempotent if `on_ready` ever fires more than once in a session (discord.py
reconnects don't normally re-trigger it, but nothing here assumes that) —
the date check alone prevents a second catch-up the same day.

The web UI's Feed tab is a different, wider view — `GET /api/articles`
with no filter returns *everything* in the DB, not just unnotified
articles. "New since last check" is a Discord/email-specific framing;
browsing the full history is what the web app is for.

## Scheduling gotcha

`tasks.loop(time=...)`'s schedule is fixed **at decoration time** — see
`scheduler.py`'s `_digest_schedule_time()` called once when the module
loads. Changing `digest_hour`/`digest_minute`/`digest_timezone` via the
Settings tab updates the DB immediately, but **the running process keeps
using whatever time it started with** until restarted. This is a
deliberate simplicity tradeoff (live-rescheduling would mean tearing down
and recreating the `Loop` object mid-run) for a value that changes rarely
— the Settings tab's hint text says this explicitly, don't remove it
without also fixing the underlying limitation.

## Web UI (`static/`)

Vanilla JS, no build step, no framework — matches this project's existing
minimal-dependency convention (same reasoning as choosing stdlib
`xml.etree`/`smtplib` over extra packages). Five tabs, client-side
show/hide (`static/app.js`'s `.tab-btn`/`.view` toggle), each tab lazily
loads its own data on first activation rather than everything loading
upfront.

- **Feed**: a single grid (`renderCard()`), grouped into collapsible
  per-day sections (`groupByDay()`/`renderDayGroup()`, `<details>`/
  `<summary>`, same established pattern as `digest.py`'s month-sections)
  with a vertical accent line (`.day-group { border-left: ... }`)
  separating one day's cluster from the next. **Days within the last
  month (`OPEN_BY_DEFAULT_MS`, 30 days) open by default; older days
  collapsed** — checked per-group against that group's own articles'
  `pub_date`, not just "is this the first/most-recent group in the list".
  Widened twice on request: first from "only the single most recent day"
  to a rolling week (day-by-day expand-clicking was tedious), then from a
  week to a month. If this needs widening again, it's just the one
  constant.
  - **This replaced an earlier separate Grid/Timeline toggle** — a
    distinct vertical-list-with-dots view, switched via two buttons. That
    was explicitly reverted in favor of one grid with day-dividers; if
    you're tempted to reintroduce a second view mode for the Feed tab,
    check first — it's been tried and undone once already.
  - **Both chip rows live inside a `<details class="filter-menu">`,
    closed by default** — same collapsible mechanism as `.day-group`
    (native `<details>`/`<summary>`, no JS needed for open/close state),
    just wrapping the filters instead of a day's cards. Requested
    explicitly so the chip rows (which can get long once a lot of sources/
    groups exist) don't take up vertical space above the fold on every
    Feed visit; click "▶ Filters" to expand. The Jobs tab's own
    `#job-filters` uses the identical `.filter-menu` CSS.
  - **Two independent chip filters**, combined with AND: `renderSourceChips()`
    (colored by an alphabetically-stable hue rotation — same `source_hues()`
    logic as `digest.py`, kept in sync manually since this is plain JS with
    no shared module system with the Python renderer) and `renderTagChips()`
    (content topic, uncolored). Both share one `wireChips()` click handler
    (parameterized by which `Set` and which `data-*` key each chip group
    uses, plus an `onChange` callback so the same `wireChips()`/
    `renderSourceChips()` pair is reusable by the Jobs tab below — Jobs
    only needs the source chips, not the topic ones) and one combined
    `applyFeedFilter()` — a thin wrapper around the shared
    `applyDayFilter(containerId, activeSourcesSet, activeTagsSet,
    noMatchElId)`, which hides individual `.card`s by `data-source`/
    `data-tag` *and* hides a whole `.day-group` if every card inside it got
    filtered out (mirrors `digest.py`'s empty-month-section hiding) —
    `#feed-no-match` shows only when a filter combination matches nothing,
    kept separate from `#feed-empty` (genuinely zero articles in the DB),
    since those need different messages. `applyDayFilter` is scoped to one
    `containerId` (`#feed-days` vs `#job-days`) rather than querying
    `.day-group` globally, so filtering one tab's cards never touches the
    other tab's (both exist in the DOM at once, only one is visually
    `.view.active`).
    - **There was briefly a third chip row for source `group_tag`, filtering
      the same way as source/tag.** Explicitly removed in favor of
      *visually clustering* the existing source chips by group instead —
      `renderSourceChips()` groups source names by `source_group` (falling
      back to an "Ungrouped" bucket sorted last) and inserts a
      `.chip-group-label` (`flex-basis: 100%`, forcing a line break in the
      `.chips` flex-wrap container) before each cluster, rather than adding
      a parallel filter dimension. Falls back to one flat, unclustered chip
      list when no source has a group set at all, so the common
      "haven't used groups yet" case doesn't show a pointless "Ungrouped"
      label. If you're tempted to bring back a separate group-filter row,
      check first — same "tried and reverted" caution as the Grid/Timeline
      toggle above; this was a deliberate simplification, not an oversight.
      (The Sources tab's *own* group filter — a single-select chip row
      alongside the health filter, `applySourceFilters()` — is unrelated
      and unaffected; only the Feed tab's group filter was removed.)
  - A Small/Medium/Large card-size toggle (`applyFeedSize()`, reusing the
    `.view-toggle` segmented-button pattern), purely a `data-size` attribute
    on `#feed-days` that CSS attribute-selectors key off
    (`#feed-days[data-size="small"] .grid { ... }` etc) — Medium is the
    unmodified default, so only Small/Large need actual overrides. Setting
    the attribute once on the (always-present, never-replaced) `#feed-days`
    element means it survives `loadFeed()` re-rendering its `innerHTML`
    without needing to be reapplied after every fetch. Persisted to
    `localStorage` (`newsbot-feed-size`), applied before the first
    `loadFeed()` call so there's no flash of the wrong size on load.
  - A ☆/★ favorite toggle per card, and "Check for news now"
    (`POST /api/run-now` — this literally calls `scheduler.run_daily_job()`,
    same code path as the daily schedule and `!forcedigest`, so testing
    from the UI behaves identically to the real automatic run, individual
    Discord embeds included).
- **Jobs**: a job/thesis-offer-listing tab, structurally a smaller sibling
  of Feed — same day-grouped grid (`groupByDay()`/`renderDayGroup()`
  reused as-is), same collapsible `.filter-menu`, same "Check for jobs
  now" → `POST /api/run-now` action, but scoped to `sources.category =
  "job"` and with no topic-chip row (physics-topic classification is
  meaningless for a job posting's title — `renderCard()`'s
  `showContentTag` option suppresses the "General Physics" badge every
  job card would otherwise show). See "Job/thesis offer sources (category
  `job`)" below for the underlying data model and which real sites this
  scrapes.
  - **Undated job listings default their day-group open, unlike Feed's
    30-day cutoff.** Most job-board pages have no reliable per-listing
    "posted" date at all (a search-results page just lists whatever's
    currently open) — `groupByDay()`'s existing "unknown" bucket (already
    used by Feed for unparseable dates) is what these land in, and
    `loadJobs()` forces that one bucket open by default
    (`isRecent = pubDate ? ... : true`) rather than leaving it collapsed
    the way an old *dated* Feed entry would be.
- **Favorites**: folder sidebar + grid, a per-article folder `<select>` to
  move it between folders without unfavoriting first.
- **Sources**: enable/disable + delete per source, a health badge per
  source (🟢/🟡/🔴/⚪, hover for the actual `last_error` — see "Source
  health tracking" above), a group badge (see "Source groups" below), two
  independent single-select filter-chip rows — health (All / 🟢 Working /
  🟡 Empty / 🔴 Broken) and group — combined via one `applySourceFilters()`
  (deliberately single-select per row, unlike the Feed tab's opt-in chips,
  since "show me the broken ones" / "show me just this group" are each a
  mutually-exclusive question within their own row, not a combinable one),
  and the add-source flow described above. Sources are split into four
  listed sections by `category` — Websites, Newsletters, Social Media,
  Job/Thesis Offers.
  - Each row also shows its **latest scraped article** as a second line
    (`renderLatestArticle()`, "No articles scraped yet." for a source that
    genuinely hasn't produced one — freshly-added sources before their
    first successful scrape are the common case). `web.py`'s
    `list_sources()` gets this in the same query as the source list itself,
    via a `LEFT JOIN` on a `GROUP BY source_id` subquery using SQLite's
    documented (if non-standard-SQL) bare-column-with-MAX() behavior: the
    other selected columns (`title`, `link`) come from the *same row* that
    produced the `MAX(pub_date)`, not an arbitrary one — verified against
    real data before relying on it. One query per Sources-tab load, not
    one per source.
- **Settings**: digest schedule (see the restart caveat above), Discord
  channel id, and newsletter subscriber management.

## Source groups

`sources.group_tag` is a free-text, user-defined label — deliberately
**orthogonal to `category`** (`website`/`newsletter`/`social`/`job`), not a
replacement or a sub-value of it. `category` is a fixed, structural
classification tied to how a source is scraped/presented (the four
Sources-tab sections); `group_tag` is whatever organizational scheme the
user wants (e.g. "CNRS Labs", "Gravitational Wave", a country) and is
entirely optional — a source with no group still works identically
everywhere, and group-based UI (the Sources-tab group filter, the Feed
tab's chip clustering) simply falls back to an ungrouped/flat rendering
if no source has one set.

Single value per source, not a multi-tag/join-table design — kept simple
since nothing so far asked for a source belonging to more than one group.
The add-source form's Group field has a `<datalist>` populated from every
distinct `group_tag` already in use (`state.sources`, recomputed each time
the form opens), so picking an existing group autocompletes rather than
needing to retype it — but it's still a plain text input underneath, so a
typo creates a new group rather than erroring.

Exposed on articles too (`sources.group_tag AS source_group` in `web.py`'s
`list_articles` query) — the Feed tab uses this to *visually cluster* its
source chips by group (see the Feed tab bullet above), not to filter; group
membership is a property of the *source*, but the clustering naturally
happens where the *articles*' chip list is built.

## Job/thesis offer sources (category `job`)

`job` is a fourth `sources.category` value, added the same way `social`
was (see the FK-corruption incident below — SQLite can't `ALTER` a `CHECK`
constraint in place, so this needed the identical recreate-and-copy
migration; `db.py`'s `_recreate_sources_table()` was factored out of what
used to be `_ensure_social_category`'s body so `_ensure_job_category` could
reuse the exact same safety-critical PRAGMA dance rather than re-deriving
it). **A `job` source is scraped by the exact same engine as everything
else** — `scrape_all_sources()` has no category branch at all, so job
sources get picked up by the daily run, "Check for news now"/"Check for
jobs now" (same `POST /api/run-now` either button calls), and Discord
notifications identically to news sources. The only place category matters
is the web UI: `GET /api/articles` gained `category`/`exclude_category`
query params (`web.py`) so Feed can ask for `exclude_category=job` and Jobs
can ask for `category=job` — two separate tabs over one shared table, not
a separate `job_postings` table.

**Sources actually configured**, verified against real scraped output
(none of these were guessed) — all tagged `group_tag: "Job Offers"` so
they cluster together in the Sources tab regardless of which lab:

| Source | Site | Notes |
|---|---|---|
| LPC Caen Theses | clrwww.in2p3.fr | SPIP CMS, `li.item.rub` — same site/engine as the "LPC" news source, different content-type class. A second request to add this exact page again (capital-P `-Propositions-de-theses-.html`) was recognized as the same URL — this server's routing is case-insensitive — and skipped as a duplicate rather than creating a second source pointed at identical content. |
| APC Theses | apc.u-paris.fr | Drupal Views table |
| ALICE Job Announcements | alice-collaboration.web.cern.ch | Drupal Views table, real `<time datetime>` per row |
| CERN Careers | careers.cern | Server-rendered (not the JS-only SPA it might look like at a glance — checked the raw HTML before assuming otherwise), 98 postings across every field of work, only the first `MAX_ARTICLES_PER_FETCH` (10) scraped per run |
| CERN Careers: Communication | careers.cern | Same site/selectors as CERN Careers, just the `/explore-careers/communication-stakeholder-engagement/` filtered view — added as its **own** source specifically because /alljobs's own 10-item cap never reaches this far down the page (Physics/Engineering sort ahead of Communication in document order), so this is the only way new Communication-track postings actually get noticed |
| Subatech Theses | www-subatech.in2p3.fr | `title_selector`/`link_selector` both blank (self-link container — see "The scraping engine" above) |
| CPPM Theses | cppm.in2p3.fr | Vue-hydrated widget, but full content is present in the raw server HTML; uses the pre-existing anchor-id link fallback (`<a id="...">`, no `href`) already built for CPPM's news source |
| IP2I Offers | ip2i.in2p3.fr | A plain HTML table (`nth-of-type` selectors, since cells share one class) — mostly M2 internship subjects rather than theses, since that's what's currently posted, but the page covers stages/thèses/emplois together |
| LUPM Offers | lupm.in2p3.fr | One flat `<ul><li><a>` list mixing job/thesis/internship postings — `div.col-md-8:not(.header-fabify)` distinguishes the real content column from an identically-classed header element elsewhere on the page |
| IJCLab Job Offers | ijclab.in2p3.fr | Custom `pods_widget_view` post type (`div.element`/`a.titre`); each item's visible date range text ("2024-02-01 - 2027-07-26") is ambiguous — start date? end date? — so `date_selector` is deliberately left unset rather than picking one and guessing |
| IPHC Job Offers | iphc.cnrs.fr | The page's `emplois-et-stages` URL covers job offers, PhD subjects, *and* Master internships as three separate `<ul>` sections sharing one class (`ul.wp-block-list`) — see IPHC Theses below for the sibling source pulling the second section from the *same* URL |
| IPHC Theses | iphc.cnrs.fr | Same URL as IPHC Job Offers, different section: `ul.wp-block-list:nth-of-type(2)` (verified `nth-of-type` counts correctly here since every sibling `<ul>` in the container already shares the `wp-block-list` class, confirmed against the real page before relying on it — the internship-only third section, `:nth-of-type(3)`, was deliberately left out as lower-priority) |
| L2IT Jobs | l2it.in2p3.fr | Currently just one posting, in a single prose `<li>` — no repeated-card structure at all; `title_selector: strong` picks up the bold job-title lead-in text only, not the full sentence that follows |
| LPSC Offers | lpsc.in2p3.fr | An "Ultimate Addons for Beaver Builder" tabs widget — all four tab panels' HTML is present in the static page regardless of which one is visually active, just CSS-hidden, so no JS/AJAX was needed. Only the "Offres d'emplois" panel's *first* `<ul>` (the real CDD postings) is scraped, via `h3:first-of-type + ul` — the panel also has several more `<ul>`s below it (permanent-position/competition-page boilerplate, the same on every visit) that would just be noise. The "Thèses" panel (`section-simple-2`) is currently `"Informations à venir"` (empty) — nothing to derive selectors from yet. |
| ADUM: particule | adum.fr | ADUM's `propositionFR.pl` form posts via `method="post"`, which this app's plain-GET scraping engine can't submit — **but the same CGI script also accepts its search fields as a GET query string** (`?find=1&sujetChoix=particule`, verified against real results before relying on it: unfiltered baseline returns the whole unrelated form, the query string returns 5 real "particule"-matching thesis proposals). This is ADUM-specific behavior (an old-style Perl CGI script, not a rule that generalizes to every POST-only form) — don't assume every such site has the same GET fallback without checking. Uses the blank title/link_selector self-link pattern (`a[href*="voirproposition"]`), same as Subatech Theses. |

**Explicitly not added, and why** (checked, not guessed-and-skipped):
- **LLR** (`llr.in2p3.fr`, both `-offres-d-emploi-` and the earlier
  `les-offres-m2-theses-2026` page) — the site's HTTPS certificate is
  broken (missing intermediate cert, `SSL: CERTIFICATE_VERIFY_FAILED`,
  confirmed independently with a plain `curl` outside the app too, not
  just this scraper's own request library) — a genuine site-side
  misconfiguration, not something to route around by disabling
  certificate verification. HTTP redirects to the broken HTTPS, so there's
  no unencrypted fallback either. Matches the page-monitor listing's own
  "Failed to connect to server" note for the same domain. Revisit once
  LLR's own certificate is fixed.
- **CNRS emploi, unfiltered** (`emploi.cnrs.fr/Offres.aspx`) — the route no
  longer exists; the portal returns its own soft-404 "Page introuvable"
  content (HTTP 200, not a real 404) at that exact URL. The platform's
  since been redesigned since this URL was recorded.
- **CNRS emploi, keyword search** (`Recherche.aspx?...` for "particule" /
  "médiation" / "communication") — the *route itself* works (466
  server-rendered offers on the unfiltered page, unlike `Offres.aspx`
  above), but the keyword search is genuinely POST/JS-driven: every
  plausible query-string parameter name (`text`, `q`, `keyword`, `search`,
  the field's own ASP.NET WebForms name, ...) was tried and every one
  returned the identical unfiltered 466-offer page, byte-for-byte — unlike
  ADUM, there's no GET fallback here to discover. The filtering happens in
  `recherche.min.js`, a minified client-side bundle; replicating it would
  need real browser automation or reverse-engineering that bundle, neither
  of which fits this project's plain-`requests`-and-BeautifulSoup engine.
  Scraping the unfiltered 466-offer firehose instead wasn't done either —
  that's not the physics-specific job list this was asked for.
- **LPNHE** (`spip.php?rubrique512`) — the page loads (stale "Propositions
  de thèses **2025**" heading) but its body has no actual proposal content
  right now, just the site nav. Nothing to derive real selectors from
  until the lab publishes something there again.
- **LAPP** (`emplois-et-stages/emplois`) — the page's own WordPress loop
  renders `"No Posts Found!"` (verified the raw HTML, not just the visible
  page); the parent hub page it links from has no other content either.
  Nothing to derive a verified selector from until a real post exists to
  check the markup against — unlike IPHC/LPSC above, there was no existing
  post whose structure could be trusted.
- **LP2I Bordeaux** (`emplois-stages/`) — a landing page whose "Thèses" /
  "CDD" / "CONCOURS CNRS" items are static Divi "blurb" labels with no
  `href` and no associated content block at all (confirmed by walking the
  DOM, not assumed from appearance) — genuinely nothing live here beyond a
  mailto link for internship applications.
- **IN2P3 "Fellowships des 2 infinis"** — a single static *announcement*
  article (already-awarded fellowships for a given year), not a live/
  rolling job board. Doesn't fit "notify me of new offers" — there's
  nothing that changes here to notify about.
- **IJCLab thesis-proposals PDF** — a PDF document, not an HTML page. Out
  of scope for this scraping engine (BeautifulSoup/CSS selectors), which
  has no PDF-parsing path at all; would need a fundamentally different
  mechanism (text extraction + its own change-detection) if ever added.
- **Nitter search for "PhD Offer particle physics" tweets** — Nitter is a
  third-party mirror that exists specifically to route around Twitter/X's
  own access restrictions. This app has a standing, explicit policy
  (documented under "Social media sources" below) against scraping
  Twitter/X or building around its anti-bot measures; a Nitter instance is
  exactly that kind of workaround wearing a different hostname, so it's
  out of scope for the same reason a direct Twitter/X scraper would be.

## Job deadlines and the deadline calendar

`sources.deadline_selector` is an optional `job`-only CSS selector, parsed
through the exact same `parse_css_date()` chain as `date_selector` (see
"The scraping engine" above) but stored in a separate `articles.deadline`
column — deliberately not reusing `pub_date`, since a deadline (when an
offer *closes*) and a posted date (when it was *listed*) are different
facts that can both exist on the same listing (ALICE Job Announcements has
both, e.g.). Most job sources don't expose one at all — `deadline` stays
`NULL`, which is a real, common state (Jobs' calendar view and the card
badge both just skip an article with no deadline, not a "should have one
but didn't parse" failure). Two sources currently have one wired up:
- **IPHC Job Offers** (`deadline_selector: "strong"`) — a clean, isolated
  `<strong>jusqu'au 12/05/2026 |</strong>` element with exactly one date in
  it, parsed via a new `DD/MM/YYYY` `TEXT_DATE_PATTERNS` entry (day-first,
  the same default assumption `FRENCH_DATE_RE` already makes for this
  app's overwhelmingly French/European sources).
- **ALICE Job Announcements** — checked, *not* wired up: the deadline text
  ("expired on - 17-08-2026") sits as a bare trailing text node in the
  *same* `<td>` as the posted date's own `<time>` element, with no element
  of its own to select — `parse_css_date()`'s single-regex-search over the
  whole `<td>`'s text would find the *posted* date first (it appears
  earlier in the text) and misreport it as the deadline. Left unwired
  rather than building content-string-anchored regex matching (`"expired
  on" ... `) into the otherwise format-based, source-agnostic date-parsing
  chain -- that would be a one-off hack, not a generalizable fix.

**Backfilling an existing row**: `persist_articles()`'s `INSERT OR IGNORE`
dedup means adding a `deadline_selector` to an already-scraped source does
**not** retroactively fill in `deadline` for articles it already has a row
for (re-running the scrape sees the same `link`, skips it entirely, same
as any other field). IPHC's two pre-existing rows were backfilled by hand
via direct SQL after verifying the extraction logic worked on a fresh
`scraper.scrape_source()` call — this is a one-time fix for existing data,
not something the app does automatically; a source that gains a
`deadline_selector` after being added will only show deadlines on
*newly*-scraped listings going forward, unless backfilled the same way.

**The calendar view** (Jobs tab, "📅 Deadlines" toggle, `static/app.js`) is
a real Mon–Sun month grid — same `buildMonthGrid()` shape (42 cells,
Monday-start, padded with adjacent months' days) and the same
`Today`/`‹`/`›` nav pattern this app would use for any other month
calendar, but built from scratch here since Jobs' day-grouped list view
(`groupByDay()`) is a *scrolling list by day*, not a *grid by day* — the
two views need genuinely different layouts, not a shared component. **It's
a pure client-side regroup of `state.jobs`** (the exact same array
`loadJobs()` already fetched for the list view) **by each article's
`deadline` field — no new endpoint.** Days with more than 3 deadlines show
a "+N more" overflow, and clicking either a pill or the day cell itself
opens a modal (`openJobDayModal()`, this app's first use of the new
`.modal`/`.modal__box` CSS — see below) listing the full `renderCard()`-
style cards for that day. The list and calendar views are two `<div>`s
toggled by `display:none`/`block` (a `.view-toggle` pair, same segmented-
button pattern as Feed's S/M/L card-size toggle) rather than separate
tab-bar tabs, since they're two presentations of the *same* Jobs data, not
two different datasets.

**Missing dates default to when scraping actually found the article, not
`NULL`.** A source with no `date_selector` at all, or whose date text
failed to parse, used to leave `pub_date` `NULL` — landing the article
permanently in Feed/Jobs' "Unknown date" bucket. `persist_articles()` now
falls back to `datetime.now(timezone.utc)` (the scrape moment) whenever
`a.pub_date` is `None`, right before the `INSERT` — since `INSERT OR
IGNORE` only ever touches a genuinely *new* row, this fallback fires once,
at first-seen time, and is never recomputed on a later re-scrape (an
already-stored row is never touched again by this path). **This is
deliberately in `persist_articles()`, not `Article.__init__`** — the same
`Article` class is also used to reconstruct rows already read back from
the DB (`_row_to_article()`), and defaulting there would re-stamp "now"
onto an old row's genuinely-`NULL` date *every time it's displayed*,
which would make an old article's date silently drift forward on every
page load instead of staying fixed. The 113 pre-existing `NULL`-`pub_date`
rows (from before this fallback existed) were backfilled once by hand via
direct SQL, using each row's own `first_seen_at` — already exactly "the
date scraping found it" for every row ever inserted, including old ones,
so no guessing was needed. **Caught and fixed a real format-mismatch bug
while doing this backfill**: `first_seen_at` is stored as SQLite's bare
`datetime('now')` shape (`"YYYY-MM-DD HH:MM:SS"`, space-separated, no
timezone marker) — appending a literal `Z` to that (`"...:00Z"`, no `T`)
is *not* valid ISO 8601, and while Python's `datetime.fromisoformat()`
happens to tolerate it, `new Date(...)` in a real JS engine does **not**
— it silently produces an Invalid Date. Caught by actually testing the
JS parse (not assumed from the Python side working), same "verify both
runtimes independently" lesson as the `created_at`/`parseUtcTimestamp()`
pattern documented in this app's design lineage. Fixed by converting to
match `.isoformat()`'s own shape exactly (`REPLACE(first_seen_at, ' ',
'T') || '+00:00'` — a real `T` separator and explicit offset, not `Z`)
before writing it back, so every `pub_date` in the column -- freshly
scraped, backfilled, or original -- is the same parseable shape.

**Manual deadline override** (`PUT /api/articles/{id}/deadline`,
`{ deadline: "YYYY-MM-DD" | null }`) exists because most job sources don't
expose a deadline to scrape at all (see above) — every job card gets a
small "✎" (has one) / "+ Deadline" (doesn't) button
(`renderCard()`'s `allowDeadlineEdit` option, only passed `true` from the
Jobs tab's own card-rendering call sites, never from Feed) opening a
small modal with a plain `<input type="date">`, Save/Clear/Cancel. Not
restricted to `category="job"` articles server-side — the route trusts
the UI to only ever surface the control there, same "server doesn't
re-enforce a UI-only restriction" shape as other simple field-edit routes
in this app (e.g. `/pin`-style toggles in sibling projects). Saving closes
the day-detail modal if one's open (`openJobDayModal()`'s cards were
rendered once from already-fetched data and won't reflect the edit on
their own) and reloads Jobs so both the list and calendar view pick up
the change immediately.

## Job tags (Fellowship/PhD/Internship/Professorship/Outreach/...)

A second, **user-editable** classification layer for `job`-category
articles only, deliberately separate from the physics `content_tag`
system (Neutrino/Higgs/...) documented under "Content tagging" above —
different axis entirely (what *kind* of position this is, not what
physics topic it's about), and job postings suppress `content_tag` display
anyway (every posting would just show "General Physics", meaningless
noise — see the Jobs tab bullet under "Web UI").
- **`job_tags` table** (`id`, `name`, `keywords` — a JSON array of
  strings, `sort_order`) — seeded once on first run with 7 starter tags
  (`db.DEFAULT_JOB_TAGS`: Post-Doc, Fellowship, PhD, Internship,
  Professorship, Outreach, Physicist, each with a handful of EN/FR
  keywords), then it's entirely the user's data — never re-seeded,
  editable/deletable via a new "Job tags" card on the **Settings tab**
  (`GET/POST/PUT/DELETE /api/job-tags`). Unlike `content_tag`'s fixed
  Python `CONTENT_TAG_RULES` list, there's no code change needed to add/
  edit/remove a tag — that's the point of this being a table, not a
  hardcoded list, and is what "make interface to add tag, use multiple
  keywords for each tag" was actually asking for.
- **`scraper.classify_job(title, job_tags)`** — same "ordered, first
  keyword match wins" shape as `classify_article()`, ordering by
  `job_tags.sort_order` (falling back to `id` for ties/un-set rows).
  Unlike `content_tag`, there's **no default/fallback tag** — most job
  titles won't match any seeded tag, and that's a real, legitimate
  "unclassified" state (shown as an "Untagged" bucket in the Jobs tab's
  filter chips), not something forced into a catch-all the way
  `content_tag` always resolves to at least "General Physics".
  - **`sort_order` is reorderable from the Settings UI** (▲/▼ buttons per
    tag row, `POST /api/job-tags/{id}/move?direction=up|down` — swaps
    `sort_order` with the immediate neighbor in current order, so the
    frontend never needs to know or compute a raw `sort_order` value).
    **This is a real classification fix, not just a UI nicety**:
    Post-Doc is seeded with `sort_order` *before* every other tag
    specifically because real posting titles ("Post-doctorant en
    cosmologie", LPSC; "Postdoctoral Researcher...", ALICE) would
    otherwise get mis-tagged PhD or Fellowship, since a hyphen counts as
    a word boundary — `\bdoctorant\b` still matches inside
    "Post-doctorant" even with the word-boundary fix below, purely
    because there's a hyphen right before it. Checking Post-Doc's own
    keywords *first* means those titles never reach PhD's keywords at
    all, fixing the collision at the ordering level instead of trying to
    solve it in the regex. If a *newly* seeded install's Post-Doc ever
    needs to be re-prioritized above some other tag added later, the
    Settings UI's move buttons are how — no DB surgery needed.
  - **Matches on a word boundary (`\bkeyword\b`), not a plain substring —
    caught by testing, not assumed.** A plain-substring first version
    mis-tagged "Synthèse de nouveaux éléments superlourds..." (a
    materials-science posting) as **PhD**, purely because "thèse" is a
    substring of "Synthèse". Word-boundary matching fixes that case
    cleanly (no boundary between "syn" and "thèse" in one contiguous
    word) while still matching a real standalone "thèse". As noted above,
    it does *not* fix every compound-word collision on its own (a hyphen
    is still a boundary) — that's what Post-Doc's ordering priority is
    actually for.
  - **`sources.forced_job_tag`** bypasses `classify_job()`'s keyword
    matching entirely for a source whose *whole listing* is reliably one
    type of offer, regardless of how any individual title happens to be
    worded — e.g. a lab's own dedicated "Theses" page has no reason to
    ever contain the word "thèse"/"PhD" in each title, since the page
    itself already establishes that. Set per-source via a new "Always tag
    as" dropdown in the Add/Edit source forms (populated from the real
    `job_tags` list, cached in `state.jobTags` when the Sources tab loads
    so the form can build synchronously without its own round-trip),
    threaded through the same way `category` already was:
    `Article.forced_job_tag` ← `source["forced_job_tag"]` (set in both
    `scrape_rss()`/`scrape_css()`) → checked *before* `classify_job()` in
    `persist_articles()`. Currently set on LPC Caen Theses, APC Theses,
    CPPM Theses, Subatech Theses, IPHC Theses, and the ADUM particule
    search (all → PhD, since each is unambiguously a thesis-proposal
    listing) and CERN Careers: Communication (→ Outreach, since every
    posting under that filtered career-page view is, definitionally, a
    communication/outreach role). `retag_jobs()` also honors this
    override (via a JOIN to `sources`) so pointing an *already-scraped*
    source at a forced tag retags its existing rows immediately, not just
    future scrapes.
  - `persist_articles()` only fetches `job_tags` from the DB when at
    least one article being persisted is `category == "job"` (a plain
    news-only scrape run never touches that table) — this needed
    `Article` to carry the source's `category` through from
    `scrape_rss()`/`scrape_css()`, which it didn't before (title/link/
    dates/etc were already threaded through per-article, but nothing
    needed to know *which dex* — sorry, *which category* — an article
    came from until job-tag classification did).
- **`scraper.retag_jobs()` / `POST /api/job-tags/retag`** — recomputes
  `job_tag` for every existing `job`-category article using the *current*
  `job_tags` table. Unlike `retag_all_articles()` (content_tag's
  equivalent, a run-by-hand one-liner for a scheme redesign that happens
  essentially never), this is a **button in the Settings UI** ("Reapply
  tags to existing listings") — tag keywords are expected to change
  routinely as the user tunes them, not just once, so the retag path needs
  to be as accessible as the edit itself, not a developer-only escape
  hatch.
- **Display**: `renderCard()` shows a `.job-tag` badge (indigo, matching
  this app's accent color) next to the source tag whenever `article.job_tag`
  is set — automatically absent on every non-job article, since `job_tag`
  is only ever computed for `category == "job"` rows. The Jobs tab's
  filter panel gained a second chip row (`renderTagChips()`, generalized
  from a `content_tag`-only function to accept a `field`/`activeSet`/
  `emptyLabel` config so Feed's topic chips and Jobs' tag chips share one
  implementation) alongside the existing source-chip row.
  - **Jobs' tag chips are opt-*out*, the opposite polarity of every other
    chip row in this app (Feed's source/topic chips, Jobs' own source
    chips are all opt-*in*: empty = show all, non-empty = show only the
    active ones).** Requested explicitly ("by default, hide fellowship,
    internship, physicist, post-doc, professorship") — `applyDayFilter()`
    gained a `tagMode` parameter (`"include"`, the default every other
    caller still uses unchanged, vs `"exclude"` for Jobs' tag row alone):
    with `"exclude"`, a tag *in* the active set is **hidden**, not shown-
    only, while an empty set still means "show everything" in both modes
    (the one point the two polarities agree on). Same deliberate
    asymmetry this app's design lineage already documents for category
    vs difficulty filters elsewhere — don't "fix" this to match Feed's
    polarity.
  - **The hidden-tags set persists across reloads** (`localStorage`,
    `newsbot-hidden-job-tags`), seeded on first-ever use with
    `JOB_TAG_HIDE_DEFAULT` (Fellowship/Internship/Physicist/Post-Doc/
    Professorship) and updated every time a chip is clicked
    (`saveHiddenJobTags()`, called from `applyJobFilter()` itself so
    there's one choke point rather than wiring persistence into every
    click handler separately). `loadJobs()` re-seeds `activeJobTags` from
    the persisted set (not an empty `Set`) on every load, and explicitly
    calls `applyJobFilter()` once after rendering — rendering the chips
    alone doesn't hide anything, only *clicking* one used to trigger the
    filter, which was fine when the set always started empty but would
    otherwise leave the initial hidden-by-default cards visible until the
    first click.
  - **`renderTagChips()` now reflects `activeSet`'s real membership when
    it renders**, not an always-starts-empty assumption — needed
    specifically because Jobs pre-populates `activeJobTags` *before*
    calling it (unlike every prior caller, which always started from an
    empty `Set`). Without this fix the chip UI and the actual filtered
    card set would visibly disagree (chips shown as un-toggled while
    cards were already hidden underneath).

## Selector picker (point-and-click CSS selectors)

The Add-source/Edit-source forms' manual CSS-selector fields (Article
container/Title/Link/Date/Year/Deadline/Image) each gained a "🎯 Pick"
button that opens a live, clickable preview of the target page instead of
requiring the selector to be typed by hand while reading page source in
dev tools — the workflow explicitly modeled on webscraper.io's browser
extension (point at an element, get a selector back), adapted to work as
a plain web page with no extension/native-app privileges of its own.
- **`GET /api/sources/preview-html?url=...`** (`web.py`) fetches the
  target page server-side (`scraper.fetch_html`, the same function every
  scheduled scrape already uses — no new SSRF surface, since this app
  already fetches whatever URL a source is configured with on every run;
  it's a single-user local tool that fetches user-typed URLs by design)
  and rewrites it for safe embedding:
  1. Every `<script>` tag is stripped, so the target site's own JS can't
     fight the picker's hover/click handling or navigate the preview away.
  2. A `<base href>` is injected as the *first* child of `<head>` (first
     `<base>` in a document wins, per spec — this overrides one the
     original page might already have had) so the page's own relative
     image/CSS URLs still resolve and it renders recognizably rather than
     as an unstyled wall of text.
  3. `static/picker-inject.js` is appended as an **absolute-URL**
     `<script src="{request.base_url}static/picker-inject.js">` — not a
     relative path, since the just-injected `<base href>` now points at
     the *target* site's origin, and a relative src would resolve against
     that instead of coming back to this app's own server. `request:
     Request`'s own `base_url` is used rather than a hardcoded host/port,
     so this keeps working regardless of what `HOST`/`PORT` the app is
     actually bound to.
- **The iframe is sandboxed (`sandbox="allow-scripts"`, deliberately *no*
  `allow-same-origin`)** — same security shape as Achievement Vault's
  `GithubDocViewer.jsx` (a sibling project, same author's other app,
  documented in that project's own CLAUDE.md): scripts run for real
  (needed for hover-highlight and click handling to work at all), but the
  sandboxed document gets an **opaque origin**, so `picker-inject.js` can
  never read this app's real cookies, localStorage, or parent-window DOM
  even though it's genuinely executing — `postMessage` is the *only*
  channel back to the parent, which is exactly why the picker script talks
  to `app.js` exclusively through it in both directions (parent → iframe:
  which field/mode to pick for; iframe → parent: the computed selector).
  Loading the picker script itself from an absolute cross-origin URL
  doesn't need CORS or break the sandbox — script *loading* (unlike
  `fetch`/XHR reading a response body) was never CORS-gated, the same way
  a CDN-hosted `<script src>` works on any page regardless of origin.
- **Two different selector-generation modes**, matching how
  `scrape_css()` itself actually uses these fields — picking the wrong
  kind of selector for a field would produce something that *looks*
  plausible in the preview but doesn't mean what `scrape_css()` needs it
  to mean:
  - **"Article container" (mode `"container"`)**: the clicked element
    usually *is* one instance of a repeating card/row. `pickContainerSelector()`
    tries the clicked element's own tag+full-class-list first, then
    progressively drops trailing classes, checking `document.querySelectorAll(...).length
    > 1` after each attempt — the first variant that matches more than one
    element wins. Walks up to the parent (up to 5 levels) only if nothing
    at the clicked element's own level ever generalizes, e.g. when the
    real repeating element is a wrapping `<li>` around the `<a>` that was
    actually clicked.
  - **Every other field (mode `"child"`)**: needs a selector *relative to
    one container instance*, matching exactly what `container.select_one(field_selector)`
    does at scrape time. `pickChildSelector()` finds which already-matched
    instance of the current `article_selector` value contains the clicked
    element (`containers.find(c => c.contains(el))`), then builds a
    descendant path upward from the clicked element, stopping as soon as
    the accumulated path is unique **within that one container** (not the
    whole document) — deliberately the mirror image of the container
    picker's "generalize until it repeats" logic. If `article_selector`
    hasn't been picked/typed yet, falls back to scoping against
    `document.body` (an absolute-from-page selector), a reasonable
    default for the "container not set yet" case rather than an error.
  - Both report a match count back to the status line (`matches N
    elements`) so the picked selector's plausibility is visible
    immediately, without needing to separately test-scrape the source
    after saving it.
- **Switching which field you're picking for reuses the already-loaded
  iframe** rather than re-fetching the page — `openPicker()` only rebuilds
  the modal/iframe when the *URL* changes, and just sends a new
  `{type: "picker-mode", ...}` postMessage otherwise. This matters in
  practice: a real add-source session typically picks all 4-7 fields on
  the same page in sequence, and re-fetching on every click would be both
  slow and (for a rate-sensitive site) needlessly repetitive traffic.
- **This is this app's first modal** (`.modal`/`.modal__box`/`.modal__head`
  CSS, `static/style.css`) — also reused by the deadline calendar's
  day-detail popup above. Kept intentionally minimal/single-purpose rather
  than building a general dialog/modal component, since (as of this
  writing) exactly two call sites exist.

## Experiment/lab news sources (batch add)

A batch of 11 international experiment/observatory/lab news pages, added
the normal `website`-category way (no new mechanism, unlike the job
sources above) — listed here mainly for the real markup patterns and
scraping-engine gaps they surfaced, several of which are now general fixes
(see "The scraping engine" above for the date-parsing chain these drove).

| Source | Site | Notes |
|---|---|---|
| GANIL-SPIRAL2 News | ganil-spiral2.eu | The given URL was the bare homepage, not a news page — the real listing (`div.news-container-list`) lives at `/fr/le-ganil/actualites/`, found via the homepage's own "Plus d'actualités" link rather than guessed. Dates ("juin 15, 2026") are what drove the month-day-year French fallback. |
| Belle II Monthly | belle2.org | 403s the default UA (`sources.user_agent` set to a browser UA, same fix as interactions.org). Only 3 items exist (monthly digest PDFs, not per-article news) — titles are bare month names ("August"); that's genuinely all the page gives, not a selector picking the wrong text. |
| T2K News | t2k-experiment.org | Standard WordPress `div.post` loop, nothing unusual. |
| SuperNEMO News | supernemo.org | `div.row:has(a.post-link)` — 48 real distinct items on one archive page, only the first 10 scraped as usual. Dates ("Oct 9, 2025") are what drove the abbreviated-English-month fix. |
| Pierre Auger Observatory News | auger.org | Real `<time datetime>` per item; dates also happen to render as "02 April 2026" (day-first, no comma) in the visible text, which is what drove `EN_DATE_DMY_RE` — moot for this source specifically since the `datetime` attribute is tried first, but the same day-first shape recurs on sites without one. |
| HESS News | hess-experiment.eu | Elementor "loop item" custom post type (`div.hess_news`); `<time>` with no `datetime` attribute, plain "July 23, 2026" text. |
| Euclid Consortium Blog | euclid-ec.org | WordPress block-theme query loop (`li.wp-block-post`) — the title (`h2`) has no link of its own; the real `href` is on a sibling `<figure>`'s image link instead, so `title_selector` and `link_selector` point at two different descendants rather than the same one. |
| KM3NeT News | km3net.org | No dedicated date element at all — the date ("8 July 2026") is inline plain text at the start of the article body's first `<p>`, mixed with bold lead-in text; `date_selector` points at that whole `<p>`, and `parse_css_date`'s regex search just finds the date substring within it, same "search the whole text" tolerance every date selector already has. |
| JUNO News | juno.ihep.cas.cn | **A real duplicate-matching bug, caught before it shipped**: `tr:has(a.hh14)` matched 18 elements for 16 real items, because this is an old nested-table layout (tables inside tables) — an *outer* `<tr>` that merely contains a deeply-nested inner `<tr>` wrapping the real link also legitimately "has" that link as a descendant, so `:has()` matched both the wrapper and the real row for several items, producing duplicate scrapes of the same article within one run. Fixed with `tr:has(> td.hui12dt)` — the `>` direct-child combinator *inside* `:has()` restricts the match to a `<tr>` whose date cell is an immediate child, not just any descendant, which the outer wrapping rows aren't. **If you use `:has()` as an article_selector on a table-based (especially old/nested-table) layout, check the match count against the real distinct-item count before trusting it** — `INSERT OR IGNORE`'s dedup means this kind of bug doesn't corrupt data, it just silently wastes scrape slots on duplicates of the same article instead of surfacing new ones, the same class of "invisible until you count" issue as the coincidental month-parsing bug above. Dates ("2026.06.12") are what drove the new `YYYY.MM.DD` `TEXT_DATE_PATTERNS` entry. |
| FAIR/GSI News | gsi.de | The given URL (fair-center.eu) is FAIR's own homepage with no news listing at all; its real "Press" link points to a *different* domain entirely (gsi.de, the partner lab operating the facility) which does have one. Found via the homepage's own nav link, not guessed. Self-link container (`a.news-listing-headline`, blank title/link_selector) since the 4 matched items span two different wrapper structures (one `div.item.news_latest` card, three bare list entries) that only share the anchor's own class. |
| NASA News | nasa.gov | The given URL is a general hub with a `"Loading the News..."` JS-driven `<article>` (not scrapable) *plus* a separate, real, server-rendered "latest news" widget (`a.latest-news-item`, 4 items) elsewhere on the same page — both exist on one page, only one is real. No date element exists for this widget (the date is only in the URL path); `date_selector` is deliberately left unset rather than parsing dates out of a URL, which no other source in this app does either. |

Several other candidate URLs from the same batch were checked and
deliberately **not** added, each for a different reason — useful to know
before re-attempting any of these:
- **AEGIS** (`aegis.web.cern.ch/news.php`) — every news item is a plain
  `<p>`/`<h3>` pair with **no per-item link or anchor id at all** (the only
  `<a>` on each entry is a photo-gallery lightbox trigger, not a real
  article page). Without a genuine distinct URL per item, there's nothing
  safe to dedupe on — the nearest usable "link" would be the page URL plus
  the item's *year* heading id, which every item published in the same
  year would share, so `INSERT OR IGNORE` would silently keep only the
  first one scraped per year and drop the rest forever. Skipped rather
  than fabricating a fragile per-position link that would also break the
  moment a new item shifts everything else's position.
- **SVOM**, **CTAO**, **Rubin Observatory**, **LBNF/DUNE** — all four
  render essentially empty static HTML (SVOM: ~400 characters of real
  text behind a WordPress "post grid" shortcode; CTAO and Rubin: literal
  `"Loading the News..."` placeholders; DUNE: zero matches against every
  plausible article/card selector tried) — confirmed by inspecting the raw
  fetched HTML directly, not assumed from how the rendered page looks.
  All four are genuine client-side-rendered news lists (fetched via JS
  after page load), which this project's plain `requests` + BeautifulSoup
  engine has no way to execute. Would need real browser automation
  (Selenium/Playwright) to scrape, which isn't a dependency this project
  carries — a significant, unbudgeted architecture change, not a
  selector-tuning fix.

## Archiving a job offer

`articles.archived` (0/1, default 0) hides one article from ordinary
browsing without deleting the row — "I've applied, or this isn't relevant
anymore" for a job/thesis offer specifically (the only place the UI
exposes a button for it, `renderCard()`'s `allowArchive` option, only
passed `true` from Jobs' own rendering call sites — Feed cards never get
one). `PUT /api/articles/{id}/archive` sets it, `DELETE .../archive`
clears it (unarchiving exists in the API for completeness/reversibility,
even though there's no "show archived"/unarchive button in the UI yet).

- **Confirmed with a native `confirm()` before the request fires** —
  requested explicitly ("ask for confirmation"), and matches this app's
  own existing precedent (Sources tab's delete button already uses plain
  `confirm()`, not a custom modal) rather than introducing a second
  confirmation pattern. Deliberately **not** the instant-delete-with-undo-
  toast flow a Collection-style app might use for this — a confirmed
  action doesn't need a second safety net on top, and DELETE .../archive
  already makes it reversible if the confirmation itself doesn't save.
- **Excluded by default everywhere articles are read back out** —
  `web.py`'s `list_articles()` and `scraper.get_articles()` (used by the
  `/api/digest.html` download and the new `/api/feed.xml` RSS feed, see
  below) both default to `archived = 0`, so an archived offer disappears
  from the Jobs list, the calendar, the digest download, and the RSS feed
  all at once from one flag — not four separate places to keep in sync.
  **Deliberately not applied to a favorites/folder-scoped query** though
  (`list_articles`'s `include_archived`/`favorites_only`/`folder_id`
  interaction) — archiving declutters the main list, it isn't meant to
  also make something you'd explicitly favorited unreachable from the
  Favorites tab; the two are independent axes, not one subsuming the
  other.

## Aggregated RSS feed (`GET /api/feed.xml`)

"The cumulative feed of all the feeds" — every source this app scrapes
(every category, not just `job`), combined into one ordinary RSS 2.0
document any RSS reader can subscribe to, instead of following each
configured source individually. Linked as "📡 RSS feed" next to the
existing "Download full digest" button on both the Feed and Jobs
toolbars (same `target="_blank"` pattern).

- **`digest.render_rss_feed()`** — plain string templating (`html.escape`
  per field), not `xml.etree` — a static RSS document has no real need
  for a DOM-building API, same minimal-dependency preference the rest of
  this app already has (stdlib `xml.etree.ElementTree` is used for
  *parsing* incoming feeds in `scraper.py`, but building one out doesn't
  need the same machinery). Lives in `digest.py` alongside
  `render_digest_html()` — both take the same `list[Article]` input and
  differ only in output format, matching that file's own "one input,
  several rendered outputs" framing (`!digest`, the daily Discord post,
  the newsletter email, and now this).
- **Capped at the 100 most-recent articles** (`RSS_ITEM_CAP`), not every
  row this app has ever scraped — an RSS reader only cares about what's
  new, and an unbounded feed would just make every client's refresh
  slower for no benefit. `scraper.get_articles()` already returns
  newest-first, so the cap is a plain slice, no separate sort needed.
- The feed's `<link>` uses FastAPI's `request.base_url`, not a hardcoded
  host/port — keeps working regardless of what `HOST`/`PORT` the app is
  actually bound to, same reasoning as the selector picker's injected
  `<script src>` above.
- **Each item carries an excerpt and image, requested explicitly after the
  first version shipped title/link-only.**
  - **`articles.excerpt`** — a short plain-text blurb, populated only for
    `kind="rss"` sources (`scraper.extract_excerpt()`, pulling from an RSS
    item's `<description>` or an Atom entry's `<summary>`/`<content>`,
    stripped of HTML and truncated at a word boundary via a new shared
    `html_to_text()` helper that `derive_title()` now also uses).
    `kind="css"` sources have no body text on the page at all to draw one
    from (same limitation `classify_article()`'s title-only classification
    already has, documented under "Content tagging") — their articles
    simply have no excerpt, not a fabricated one. **Only populated going
    forward, not backfilled** — unlike `pub_date`'s backfill (which had
    `first_seen_at` as a ready substitute for every existing row),
    reconstructing an excerpt for an already-scraped article would mean
    re-fetching every RSS source's *current* feed and matching by link,
    which isn't guaranteed to still contain an older, already-rolled-off
    item — deemed not worth the risk/complexity for a purely cosmetic
    backfill, so older articles just show image-only or bare items in the
    feed until naturally superseded by newer scrapes.
  - **The image is embedded three different ways**, since real RSS
    readers don't agree on which one they honor: `<enclosure>` (the RSS
    2.0 spec's own mechanism), `<media:thumbnail>` (the Media RSS
    extension — this app already *reads* that same tag from other feeds
    in `extract_rss_image()`, so writing it back out is the most broadly-
    compatible choice, not a new convention), and a literal `<img>` inside
    `<description>` for readers that only render what's in the
    description body. `<enclosure>`'s `length="0"` (the spec's byte-size
    attribute) is a deliberate placeholder — getting the real size would
    mean an extra fetch per image per feed request, and every reader
    checked against ignores the value anyway.
  - **Security: no `<![CDATA[]]>` anywhere in this feed, on purpose.**
    Wrapping `<description>`'s embedded HTML in CDATA is the more common
    pattern in hand-rolled RSS generators, but it has a real injection
    hole: if any scraped title/excerpt ever contains the literal sequence
    `]]>`, a CDATA section ends early and everything after it is parsed as
    raw, unescaped XML by the reader — a real vector for injecting
    arbitrary markup into how other people's RSS readers render this
    feed, entirely plausible for content pulled from arbitrary third-party
    sites this app doesn't control. Verified this can't happen here with
    a deliberately hostile test string (an excerpt containing `]]>`
    itself) parsed back through `xml.etree.ElementTree` and unescaped the
    same way a real reader would: the sequence survives as inert literal
    text at every layer, never able to end anything early. The mechanism
    is two escape passes, not a bug: `html.escape()` on each embedded
    value (image URL, excerpt) builds a well-formed inner HTML *string*
    first, then `html.escape()` is applied *again* to that whole string
    before it becomes `<description>`'s own XML text content. A reader
    XML-unescapes an element's text exactly once, which must yield back
    already-well-formed HTML — that's what the first pass produced, so
    the second pass is what protects the *surrounding XML document*, not
    a redundant no-op. Skipping either pass would let scraped content
    break out of its intended context.
- **Security: `GET /api/sources/preview-html` (the selector picker's
  preview endpoint, "Selector picker" above) gained an SSRF guard**,
  requested as a general "take security measures" pass alongside this
  feature. Unlike every other URL this app fetches (always one a source
  is already configured with), that endpoint is reachable with an
  arbitrary URL the caller never has to save first — it's called straight
  from the Add-source form, before anything's persisted. `web.py`'s new
  `_is_safe_external_url()` rejects anything that isn't plain `http(s)`,
  and anything whose hostname resolves (`socket.getaddrinfo`) to a
  private/loopback/link-local/reserved/multicast address (`ipaddress`
  stdlib) — the standard defense against this server being turned into a
  proxy into its own internal network (a real class of vulnerability
  regardless of this app's "personal, single-user, no auth layer" design,
  since defense in depth doesn't get to assume the deployment stays
  exactly as intended forever). Explicitly documented as best-effort, not
  airtight (a DNS answer could change between this check and the actual
  fetch moments later — a fully airtight fix would need the HTTP client
  itself to validate the IP it connects to, which `requests` doesn't
  expose a hook for cheaply) — but it stops the overwhelming majority of
  real SSRF attempts (a literal internal IP, or a hostname like
  `localhost`) for negligible cost. Verified against a real test matrix
  (loopback, RFC 1918 private ranges, the AWS/cloud metadata address
  `169.254.169.254`, and non-http(s) schemes including `file://`) before
  relying on it, not assumed correct from reading the `ipaddress` docs
  alone.

## Discord bot (`discord_bot.py`)

Kept as a fully usable secondary interface, not just a notification
sink — explicit choice over simplifying to notify-only once the web UI
existed, since Discord access (e.g. from a phone) is sometimes more
convenient than the local web app.

- `!news <site>` — live-scrapes one named source via `scrape_source_safe()`
  (bypassing the DB's cached articles, but still updating that source's
  health) and posts each as an embed; also persists results, so the DB
  stays in sync with any manual check.
- `!sites` — lists configured sources.
- `!digest` — posts the full current digest (everything in the DB) as a
  downloadable **file**, on demand — the one place in Discord that still
  uses the file-shaped digest; see "Notification flow" above for why the
  automatic daily post doesn't.
- `!forcedigest` — runs the *exact* daily job (`scheduler.run_daily_job()`)
  immediately: individual embeds if there's anything new, same as the real
  8am run (or a missed-run catch-up). Useful for testing without waiting
  for the schedule or faking the clock.

`discord_bot.py` and `scheduler.py` import each other; the cycle is broken
with a **lazy import inside functions** (`on_ready`, `force_digest_command`)
rather than at module level — both modules fully load before either one's
function body actually needs the other. If you add another cross-reference
between them, use the same pattern, not a top-level import.

## Running it

`python main.py` — the only correct entrypoint (see Architecture above).
Reads `.env` for `DISCORD_TOKEN`, `NEWS_CHANNEL_ID` (first-run seed only,
see Data model), `HOST`/`PORT` (default `127.0.0.1:8000`), and optional
`SMTP_*` for the email channel. `DISCORD_TOKEN` unset is *not* fatal — the
web app still runs, only Discord features (bot commands, the daily
`@everyone` post) are disabled, logged as a warning once at startup.

**Autostart**: `~/.config/autostart/news-bot-server.desktop` (XDG
autostart, GNOME/Wayland on this machine — same convention this machine's
Thunderbird entry already used) launches `main.py` at login and leaves it
running continuously, since the daily job only fires if the process is
actually up at that time — this is a genuinely different requirement from
the old script's `--digest`-then-exit autostart entry (retired along with
`news_bot.py`). If this repo is ever moved/re-cloned to a different path,
that file's `Exec=` line needs updating by hand — it's outside the repo,
not tracked by git.

**Desktop launcher** (`desktop/launch-app.sh` / `desktop/stop-app.sh`): a
double-clickable app entry, `~/.local/share/applications/news-bot.desktop`
(outside the repo, like the autostart entry above — needs its `Exec=`/
`Icon=` paths updated by hand if this repo moves), same on-demand pattern
Achievement Vault (`bin/launch-app.sh`) and Compta (`webapp/run.sh`) both
already use: check if the server's up (`GET /api/settings`), start it if
not, then open the web UI in a browser "app" window (Firefox first, since
it's this machine's preferred browser; Chrome/Chromium `--app=` as a
frameless-window fallback; `xdg-open` last resort). **Deliberately not
`bin/`** — unlike the other two projects, `bin/` here *is* the project's
Python venv (`bin/python`, `bin/uvicorn`, ...), not a spare scripts
folder, so the launcher pair lives in its own `desktop/` directory instead
to avoid dropping a shell script into venv-managed territory.
- **This is a thinner script than the other two apps' launchers, because
  News Bot already has the autostart entry above** — the common case is
  "server's already running, just open a window onto it," not "start it
  fresh." It still starts the server itself (the exact same `bin/python
  main.py` command the autostart entry runs) if that hasn't happened yet
  for any reason (autostart disabled, hasn't run since login, or the
  process died) — same fallback-start behavior as the other two, just a
  less common path here.
- `stop-app.sh` stops whichever process is actually running main.py right
  now — whether *it* was started by `launch-app.sh` or by the autostart
  entry, since both end up as the identical `bin/python main.py` process,
  matched the same way (`pgrep`, this repo's own absolute paths, same
  "don't trust nohup's `$!`" reasoning as Achievement Vault's script). It
  does **not** disable autostart — the server comes back on next login
  regardless, same as it always has; this only ever stops the current
  instance.
- `desktop/icon.svg` is a generated app icon (an RSS-feed glyph — this app
  scrapes RSS feeds and CSS-selector listings, so it fits — in the web
  UI's own dark background/accent-blue colors, `#16171a`/`#9aa5ff`), same
  "generated in the app's own palette, not a designed asset" convention as
  Achievement Vault's `bin/icon.png` and Compta's `webapp/icon.svg`.

**Railway**: retired. `railpack.json` is deleted from this repo — the app
is local-first by design (SQLite + PC autostart), and a second cloud-hosted
copy would need its own answer for SQLite persisting across redeploys
(ephemeral filesystem without an attached volume) for no real benefit.
**The actual Railway project still needs deleting/disconnecting by hand
via Railway's dashboard** — there's no Railway CLI in this environment, so
removing the repo-side config is as far as this could go automatically.

## Retired

`news_bot.py` and `generate_digest.py` (the original hardcoded-`SOURCES`-
dict script and its static HTML generator) were fully replaced by this
app — visible in git history, not kept alongside as dead code. If you're
looking for how a particular site's scraping used to be configured before
the DB-backed rewrite, that history (and `seed_sources.py`'s `SEED_SOURCES`
list, which carried every source's config forward) is the place to look,
not a lingering duplicate file.
