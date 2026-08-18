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

`scraper.classify_article(title)` — ordered, first-match-wins keyword
regexes (`CONTENT_TAG_RULES`) sorted into `Obituary`/`Award`/`Personnel`/
`Discovery`/`Event`/`Jobs`/`Funding`/`Outreach`, defaulting to `News`.
Deliberately **title-only, English-only, no ML/API call** — title is the
only text reliably available across every source (CSS-scraped sources have
no body text at all, and RSS descriptions vary too much in quality/length
to rely on), and several primary sources (CERN's `/fr/feed/`, IN2P3, CNRS
News) publish in French, which these regexes don't attempt to cover — see
"Suggested next steps" if that's worth fixing later.

`Article.__init__` computes this automatically (`content_tag or
classify_article(title)`) whenever `content_tag` isn't passed in, so any
freshly-scraped article always has one. Rows that predate this feature
have `content_tag IS NULL` in the DB; `scraper.backfill_content_tags()`
(called once at startup by `main.py`) fills those in permanently. **Read
paths that bypass `Article`/`get_articles()` won't get the constructor's
fallback** — `web.py`'s `/api/articles` learned this the hard way (it runs
its own SQL for the favorites/folder `LEFT JOIN`, so every article showed
`content_tag: null` until a matching defensive fallback was added there
too). If you add another raw-SQL read path over `articles`, add the same
`classify_article()` fallback, or route it through `get_articles()`
instead.

## Adding a source

Two-tier, matching the web UI's "Add website / newsletter" form:
1. **Auto-detect** (`scraper.detect_feed(url)`): checks the page's own
   `<link rel="alternate" type="application/rss+xml|atom+xml">` tags
   first, then a fixed list of common paths (`COMMON_FEED_PATHS`:
   `/feed/`, `/rss.xml`, `/feed.xml`, `/rss/`, `/atom.xml`,
   `/index.rss`) against the URL's origin. Each candidate is verified by
   actually parsing it (`_looks_like_feed`), not just checking the HTTP
   status — a 200 with an HTML error page is a real failure mode this
   guards against. Probing 404s here are *expected*, not errors — they're
   logged at `debug`, not `error` (`quiet=True` on the fetch helpers), so
   a normal "no feed, fall back to manual" outcome doesn't spam the log
   like a real failure would.
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
   selectors directly.
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

- **Feed**: every article, source-filter chips (colored by an
  alphabetically-stable hue rotation — same `source_hues()` logic as
  `digest.py`, kept in sync manually since this is plain JS with no shared
  module system with the Python renderer), a ☆/★ favorite toggle per
  card, a Grid/Timeline view toggle (see below), and "Check for news now"
  (`POST /api/run-now` — this literally calls `scheduler.run_daily_job()`,
  same code path as the daily schedule and `!forcedigest`, so testing from
  the UI behaves identically to the real automatic run, individual Discord
  embeds included).
  - **Timeline view** (`renderTimeline()`/`renderTimelineRow()`) groups
    articles by calendar day ("Today"/"Yesterday"/full date — `dayLabel()`)
    as a vertical list with a connecting line and a per-source-colored dot,
    a small thumbnail, and the same tag/content-tag/date meta row as a grid
    card. Grid and Timeline share the *same* underlying article list, hue
    map, and source-filter chips (`applyFeedFilter()` toggles `.card` and
    `.timeline-row` elements by the same `data-source` attribute) — a
    filter or favorite action doesn't need to know which view is currently
    visible.
- **Favorites**: folder sidebar + grid, a per-article folder `<select>` to
  move it between folders without unfavoriting first.
- **Sources**: enable/disable + delete per source, a health badge per
  source (🟢/🟡/🔴/⚪, hover for the actual `last_error` — see "Source
  health tracking" above), and the add-source flow described above.
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
