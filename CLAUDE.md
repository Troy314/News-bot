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

- `sources` — one row per website/newsletter. `kind` (`rss`/`css`) picks
  the extraction method; `category` (`website`/`newsletter`) is purely a
  UI grouping, both are scraped identically. A `css` source additionally
  needs `article_selector`/`title_selector`/`link_selector`/`base_url`
  (see below); `date_selector`/`image_selector`/`user_agent` are optional
  on either kind.
- `articles` — every article ever seen, deduped by `link` (`UNIQUE`
  constraint; `persist_articles()` uses `INSERT OR IGNORE` and reports
  which ids were newly inserted). `notified` (0/1) is how the app tracks
  "have I already told someone about this" — **this is the whole
  dedup/new-detection mechanism**, replacing the old script's
  `seen_links.json`.
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
  and assume existing installs pick it up.

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
  usable feed. Date parsing tries a `<time datetime="...">` attribute
  first, then regex-matches "Month D, YYYY" or "MM.DD.YYYY" in the
  element's text (`TEXT_DATE_PATTERNS`) — extend this list, don't fork a
  new date-parsing path, if a future source needs another format.

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

Two-tier detection, matching the web UI's "Add source" form (`category` is
a separate, orthogonal choice — `website`/`newsletter`/`social`, purely a
UI grouping, all scraped identically once you have a feed URL or selectors):
1. **Auto-detect** (`scraper.detect_feed(url)`), checked in this order:
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
   real failure mode this guards against. Probing 404s here are *expected*,
   not errors — logged at `debug`, not `error` (`quiet=True` on the fetch
   helpers), so a normal "no feed, fall back to manual" outcome doesn't
   spam the log like a real failure would. **Known limitation, not chased
   further**: if step 3's more-specific candidate transiently fails (a
   rate limit, a network blip — observed on Reddit during rapid testing)
   but step 4's less-specific one happens to succeed, the function returns
   a technically-valid but too-broad feed with no retry. The web UI shows
   the discovered feed URL before you commit to adding it specifically so
   this is visible/reviewable, not just silently wrong.
2. **Manual CSS selectors** (the same shape `seed_sources.py`'s 5 CSS
   sources use): when no feed is found, the form asks for
   article/title/link/base-url selectors plus optional date/image ones.
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

## Social media sources

`category = 'social'` was added after `website`/`newsletter` already
existed — since SQLite can't `ALTER` a `CHECK` constraint in place,
`db._ensure_social_category()` does a real recreate-and-copy migration
(rename `sources` → `sources_old`, recreate under the new constraint from
`SCHEMA`, copy every column back via `PRAGMA table_info` so it doesn't need
updating if more columns get added later, drop the old table) — verified
against the real production DB (23 sources, 197 articles at the time)
before trusting it, with `PRAGMA foreign_keys` toggled off/on around the
swap so `articles.source_id` references survive the rename. Runs once at
startup via `init_db()`, after `_ensure_columns()` so there's a complete
column set to copy, and is a no-op forever after via a check against the
table's own stored `CREATE TABLE` SQL text.

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
`xml.etree`/`smtplib` over extra packages). Four tabs, client-side
show/hide (`static/app.js`'s `.tab-btn`/`.view` toggle), each tab lazily
loads its own data on first activation rather than everything loading
upfront.

- **Feed**: a single grid (`renderCard()`), grouped into collapsible
  per-day sections (`groupByDay()`/`renderDayGroup()`, `<details>`/
  `<summary>`, most recent day open, older days collapsed — same
  established pattern as `digest.py`'s month-sections) with a vertical
  accent line (`.day-group { border-left: ... }`) separating one day's
  cluster from the next.
  - **This replaced an earlier separate Grid/Timeline toggle** — a
    distinct vertical-list-with-dots view, switched via two buttons. That
    was explicitly reverted in favor of one grid with day-dividers; if
    you're tempted to reintroduce a second view mode for the Feed tab,
    check first — it's been tried and undone once already.
  - **Two independent chip filters**, combined with AND:
    `renderSourceChips()` (colored by an alphabetically-stable hue
    rotation — same `source_hues()` logic as `digest.py`, kept in sync
    manually since this is plain JS with no shared module system with the
    Python renderer) and `renderTagChips()` (content topic, uncolored).
    Both share one `wireChips()` click handler and one combined
    `applyFeedFilter()`, which hides individual `.card`s by
    `data-source`/`data-tag` *and* hides a whole `.day-group` if every
    card inside it got filtered out (mirrors `digest.py`'s empty-month-
    section hiding) — `#feed-no-match` shows only when a filter combination
    matches nothing, kept separate from `#feed-empty` (genuinely zero
    articles in the DB), since those need different messages.
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
- **Favorites**: folder sidebar + grid, a per-article folder `<select>` to
  move it between folders without unfavoriting first.
- **Sources**: enable/disable + delete per source, a health badge per
  source (🟢/🟡/🔴/⚪, hover for the actual `last_error` — see "Source
  health tracking" above), a quick single-select filter chip row (All /
  🟢 Working / 🟡 Empty / 🔴 Broken, `applyHealthFilter()` — deliberately
  single-select unlike the Feed tab's opt-in chips, since "show me the
  broken ones" is a mutually-exclusive question, not a combinable one),
  and the add-source flow described above.
- **Settings**: digest schedule (see the restart caveat above), Discord
  channel id, and newsletter subscriber management.

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
