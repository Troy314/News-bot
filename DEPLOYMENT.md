# Deployment (Docker + Dockge + Cloudflare Tunnel)

**Keep this file up to date as the migration progresses** — it's the
running record of what's live, what's pending, and the exact paths/values
needed to pick this back up in a future session.

## Target architecture

Server `steins` (Ubuntu 26.04, reached via SSH alias `myserver`,
Tailscale IP `100.87.94.39`) runs everything as Docker Compose stacks
managed through Dockge. No inbound ports are opened on the server or
router — public access is entirely via an outbound-only Cloudflare
Tunnel, fronted by Cloudflare Access (login-gated, since the News_bot web
UI has no auth of its own).

```
Internet --> Cloudflare Access (login gate) --> Cloudflare Tunnel
          --> cloudflared container --(docker network)--> news_bot container
```

This server will likely host more than one app over time; the
`cloudflared` tunnel and the `cloudflared_net` Docker network are both
shared/reusable — a future app just needs its own stack on
`cloudflared_net` plus one more Public Hostname entry in the Cloudflare
Tunnel dashboard, no new tunnel, no new nameserver work.

## Status: Done

- [x] Docker Engine + Compose plugin installed on `steins` (apt repo
      method, official Docker apt source, `docker-ce` + `docker-compose-plugin`).
      `troy314` added to the `docker` group (no `sudo` needed for `docker`
      commands in new sessions).
- [x] Scoped passwordless sudo granted for setup
      (`/etc/sudoers.d/news-bot-setup` on the server, limited to
      `apt-get`/`systemctl`/`usermod`/`install`/`tee`/`chmod`/`gpg`/`mkdir`/`ufw`
      — not full root). Safe to `sudo rm /etc/sudoers.d/news-bot-setup`
      once everything below is done, if you'd rather not keep it.
- [x] Dockge installed and running: `~/docker/dockge/compose.yaml`,
      bound to `127.0.0.1:5001` only (not publicly exposed — reach it via
      an SSH tunnel: `ssh -L 5001:localhost:5001 myserver`, then
      `http://localhost:5001`). First visit creates the admin account.
- [x] Shared Docker network `cloudflared_net` created (`docker network
      create cloudflared_net`) — every stack that needs to be reachable
      through the tunnel joins this network.
- [x] News_bot containerized and **cut over from the old native
      deployment**:
  - Stack at `~/docker/stacks/news-bot/` (`Dockerfile`, `compose.yaml`,
    app source in `app/`, SQLite DB in `data/news.db`, secrets in `.env`).
  - `DB_PATH=/data/news.db` (overridden via env, keeps the DB out of the
    image so rebuilds don't touch data).
  - No public port published; only `127.0.0.1:8001 -> 8000` for
    loopback debugging (reach via SSH tunnel, same as Dockge above).
  - The **old native deployment** (`/home/troy314/App/News-bot`, run via
    systemd unit `news-bot.service`) was stopped and disabled
    (`systemctl disable`), **not deleted** — kept as a fallback. Its
    `news.db`/`.env` were copied (not moved) into the new Docker stack, so
    the old copy is now stale/frozen from the moment of cutover onward —
    don't treat it as a live source of truth if you ever look at it again.
  - Verified working: web UI responds (`curl 127.0.0.1:8001` → 200,
    real data), Discord bot logs in and connects to the Gateway.
  - `cloudflared` stack scaffolded at `~/docker/stacks/cloudflared/compose.yaml`
    (image `cloudflare/cloudflared:latest`, `tunnel run`, joins
    `cloudflared_net`) — **not started yet**, waiting on the tunnel token
    (see below).
- [x] Cloudflare account created.
- [x] **Decision: full-domain migration**, not subdomain-only delegation.
      Cloudflare's free self-serve onboarding doesn't support adding a bare
      subdomain as its own zone (that needs Enterprise/support) — the
      original "delegate just `srv.alpsciences.com`, touch nothing else"
      plan isn't available through the dashboard. Went with migrating all
      of `alpsciences.com` to Cloudflare's nameservers instead.
- [x] `alpsciences.com` added as a site in Cloudflare (Free plan).
      Cloudflare's automatic DNS scan found **4 A + 1 CNAME + 3 MX + 2
      TXT**, verified by hand against OVH's live zone before proceeding
      — exact match, nothing missing:
      - A `alpsciences.com` → `185.199.108.153`/`.109.153`/`.110.153`/`.111.153` (GitHub Pages, proxied)
      - CNAME `www` → `troy314.github.io` (proxied)
      - MX `alpsciences.com` → `1 mx1.mail.ovh.net`, `5 mx2.mail.ovh.net`, `100 mx3.mail.ovh.net` (DNS only)
      - TXT `alpsciences.com` → `v=spf1 include:mx.ovh.com -all` (DNS only)
      - TXT `alpsciences.com` → `1|www.alpsciences.com` (DNS only, an OVH-internal marker, preserved as-is)
- [x] **OVH nameservers switched to Cloudflare's** (`austin.ns.cloudflare.com` /
      `mallory.ns.cloudflare.com`). Confirmed propagated: the `.com`
      registry and both 1.1.1.1/8.8.8.8 already resolve to Cloudflare's NS.
      Verified directly against Cloudflare's authoritative servers: apex
      site loads (`HTTP 200`), `www` still redirects to apex exactly as
      before (GitHub Pages' own behavior, unaffected), MX/SPF/OVH-marker
      TXT all serve unchanged. **No breakage** — email and the GitHub
      Pages site are confirmed unaffected by the migration.
- [x] SSL/TLS mode confirmed set to **"Full"** already (no change needed).
- [x] Cloudflare Tunnel created (Zero Trust Free plan — confirmed
      sufficient: Tunnels + Access are both included, the 50-seat cap
      only matters for team accounts). Tunnel token saved directly on the
      server (never passed through chat) at
      `~/docker/stacks/cloudflared/.env`. `cloudflared` container started
      and verified connected — 4 redundant QUIC connections registered to
      Cloudflare's edge (Marseille PoPs: mrs01/mrs04/mrs07).

## Cloudflare Tunnel + Access setup

- [x] Public Hostname added: `news.alpsciences.com` → `HTTP` →
      `news_bot:8000`. Verified end-to-end (bypassing local DNS cache
      propagation delay with `curl --resolve`): real data returned from
      the container through the tunnel. **Currently wide open, no Access
      gate yet — anyone who finds the URL can use it** until the next
      item is done.
- [x] Cloudflare Access application added, gating `news.alpsciences.com`
      (Zero Trust → Access → Applications). Considered Discord OAuth first
      — not viable, Discord isn't a native Cloudflare IdP and its OAuth2
      isn't standards-compliant OIDC (no discovery document), so "Generic
      OIDC" wouldn't reliably work. Also considered GitHub OAuth (fully
      supported) but settled on the simpler built-in **One-Time PIN**
      (email code, no IdP registration needed) — policy "Owner only":
      Allow → Include → Emails → `hectorpillot@gmail.com`.
      Verified end-to-end: unauthenticated requests now get an actual
      `302` redirect to `shiny-base-79de.cloudflareaccess.com`'s login
      page instead of the app's content — confirmed via `curl`, not just
      assumed. (First attempt silently didn't gate anything — the
      Access wizard's final "Add application" confirm step had been
      missed; re-saving fixed it. Worth remembering if this is ever
      reconfigured: always verify with a real unauthenticated request
      after saving, don't trust the dashboard alone.)
- [x] **Second gotcha, also fixed**: the app's login page initially only
      offered "Sign in with Cloudflare" (i.e. log in with your actual
      Cloudflare account) instead of the intended One-Time PIN email code
      — no code was ever sent because that button was never clicked.
      Cause: One-Time PIN wasn't enabled as a login method for this
      specific Access application (per-app login-method selection is
      separate from the account-wide list). Fixed via Zero Trust →
      Authentication (moved around in Cloudflare's UI — used the
      dashboard's own search to find it, menu path isn't stable enough to
      rely on) → enabled One-Time PIN → confirmed it now appears as a
      login option → **confirmed working with a real login (code
      received by email, logged in, reached the real app)**.

## Status: Deployment complete

The core migration (Docker + Dockge + Cloudflare Tunnel + Access) is
fully done and verified working end-to-end with a real browser login
(one-time PIN emailed, entered, landed on the real app). Everything below
is optional cleanup, not urgent:

- [ ] Once the Docker deployment has proven solid (a few days of normal
      use, a daily digest run observed to fire correctly): consider
      deleting the old `/home/troy314/App/News-bot` native copy, and
      `sudo rm /etc/sudoers.d/news-bot-setup` if you don't want the
      scoped passwordless sudo left in place.
- [ ] **Security review (2026-09-27) — deferred, not urgent**: overall
      posture is solid (no inbound ports open, Cloudflare Access gates
      every request — verified with `curl`, HTTPS enforced with a `301`
      redirect from plain HTTP, secrets never in git, Access session
      cookie is `Secure; HttpOnly`). One optional hardening noted but
      deferred at the user's request: no `Strict-Transport-Security`
      (HSTS) header currently — Cloudflare dashboard → SSL/TLS → Edge
      Certificates → enable "Always Use HTTPS" + HSTS. Not a real
      vulnerability today (HTTP already redirects to HTTPS), just belt-
      and-suspenders. News_bot's own application code (FastAPI routes,
      SQL construction, scraped-content rendering) hasn't been audited
      for typical web-app bugs — low practical risk since Access blocks
      all unauthenticated traffic before it ever reaches the app.
- [x] **Cloudflare Access bypass for `/api/feed.xml` (2026-09-27)** — a
      second, path-scoped Access Application (policy action **Bypass**,
      Include: Everyone) was added so RSS readers (Obsidian was the one
      that surfaced this) can fetch the feed without an interactive login,
      which they have no way to complete. Deliberately public — "subscribe
      in any RSS reader" was always this endpoint's own intent.
- [ ] **The same bypass still needs adding for `/api/jobs-feed.xml`**
      (the new jobs-only feed split off from the combined one, see
      `CLAUDE.md`) — same steps as above, just a different path.
- [ ] **Indeed job-alert email import (2026-09-27) — code done, needs your
      inbox set up.** Requested: job sources for "chargé de médiation
      scientifique"/"chargé de communication scientifique"/"ingénieur
      pédagogique"/"médiateur scientifique" from Grenoble/Lyon/Chambéry
      universities + Indeed. What actually shipped:
      - **Université Savoie Mont Blanc (Chambéry) and Université Lyon 1**:
        added as real `css` sources (ids 513/514), verified against live
        HTML and via the app's own scraper — both real institutional
        recruitment platforms (same underlying JetEngine/WordPress
        template family, confirmed live: SMB already has a real posting
        titled "Ingénieur pédagogique spécialisé...").
      - **Université Grenoble Alpes**: dropped. Their own jobs site
        renders entirely client-side (empty static HTML); the national
        aggregator (choisirleservicepublic.gouv.fr) was investigated
        further at the user's request but its filtering is also
        JS/AJAX-driven with no discoverable static URL scheme. Neither
        is reachable by this app's `requests`+BeautifulSoup engine
        without a real browser to observe network calls.
      - **Indeed**: direct scraping is Cloudflare-blocked (confirmed with
        the user's own exact search URLs — real `403` challenge page, not
        a generic guess), and their RSS feed is dead (`404`). Declined to
        build a bot-detection bypass when asked — not a security-research
        context, and it's the same "don't defeat an active block" line
        this app has already drawn elsewhere (PSNProfiles'/Untappd's
        image CDNs in the sibling project). Went with a legitimate
        alternative instead: **Indeed's own job-alert emails**, read over
        IMAP from a dedicated inbox — a first-party feature, not
        scraping.
      - Added a new `email` source kind (`db.py`'s `sources.kind` CHECK
        constraint + a `_ensure_email_kind()` migration following the
        exact same recreate-table pattern as the `social`/`job` category
        migrations — verified on the live DB: all 63 pre-existing sources
        and 0 foreign-key violations survived the migration). New
        `email_import.py` (stdlib `imaplib`/`email` only, zero new
        dependency, same convention as `mailer.py`'s SMTP) reads UNSEEN
        messages from Indeed's alert sender, marks them `\Seen` once
        processed. Wired into `scraper.py` as `scrape_email()`, dispatched
        from `scrape_source()` exactly like `rss`/`css`. A placeholder
        source row (id 515, "Indeed Job Alerts (email)") is already
        created and confirmed to **no-op gracefully** (`status: empty`,
        no error) with IMAP unconfigured.
      - Added "médiateur"/"mediateur" to the existing `Outreach` job_tag
        (id 5) so all 4 requested phrases now classify correctly —
        verified directly: "médiation"/"communication" titles already
        matched via existing keywords, "ingénieur pédagogique" already
        matched via the existing `Ingé` tag, only "médiateur scientifique"
        needed the new keyword (word-stem mismatch against "médiation").
      - **Still needed from the user**: create the dedicated Gmail
        address, enable 2-Step Verification, generate an app password,
        and set up Indeed job alerts (the two combined queries already
        given) pointed at that inbox. Then add directly on the server
        (never through chat) to `~/docker/stacks/news-bot/.env`:
        `IMAP_HOST=imap.gmail.com`, `IMAP_EMAIL=...`,
        `IMAP_APP_PASSWORD=...` (the 16-character app password, not the
        real account password), then `docker compose up -d` to pick it up.
      - **Known limitation, flagged in `email_import.py`'s own docstring**:
        the HTML-parsing regex for extracting job links/titles from
        Indeed's alert emails was written without a real sample to test
        against (no alerts existed yet at implementation time). Treat the
        first real run as a validation pass — check `docker compose logs`
        for "yielded no job links" warnings, which mean the parsing
        pattern needs adjusting against the actual email markup.
- [ ] A stray file `troy314@100.87.94.39` (an accidental local SQLite
      backup, likely from a mistyped `scp`) exists in both the local dev
      copy and was `git rm`'d already in the live server copy's working
      tree (shown as `D` in `git status`) — harmless, just noise; delete
      or commit its removal whenever convenient.

## Deploying an update (over Tailscale)

The server copy is **not a git checkout** — sync the working tree, then rebuild:

```bash
cd ~/Code/News_bot
rsync -av --exclude='.git/' --exclude='*.db*' --exclude='bin/' --exclude='lib/' \
  --exclude='lib64' --exclude='include/' --exclude='pyvenv.cfg' --exclude='__pycache__/' \
  --exclude='.claude/' --exclude='.env' --exclude='desktop/' --exclude='troy314@*' \
  --exclude='audit/shots/' --exclude='audit/baseline/' \
  ./ myserver:~/docker/stacks/news-bot/app/
ssh myserver 'cd ~/docker/stacks/news-bot && docker compose up -d --build && docker compose logs --tail 15 news_bot'
```

The rsync also carries `CHANGELOG.md` and `changelog.py` (they feed `GET /api/version`
and Admin → Bêta/Correctifs) — **bump the version and add the `CHANGELOG.md` entry in the same
commit as any live change** (`tests/test_changelog.py` fails if they disagree). The UI-audit
screenshots (`audit/shots`, `audit/baseline`) contain real data: never deploy or commit them.

Add `-n` to the rsync first for a dry run. `data/` (the DB) is outside `app/`
and never touched. Verify by SSH-tunnelling `127.0.0.1:8001`, or compare
`md5sum` of local vs `docker exec news_bot md5sum /app/...`. The public site
sits behind Cloudflare Access, so it can't be curl-tested unauthenticated.

## Key facts / reference

| What | Value |
|---|---|
| Server SSH alias | `myserver` (→ `100.87.94.39`, user `troy314`) |
| Server hostname | `steins` |
| Dockge UI | `127.0.0.1:5001` on the server (SSH-tunnel to reach it) |
| News_bot stack | `~/docker/stacks/news-bot/` |
| News_bot debug port | `127.0.0.1:8001` on the server (SSH-tunnel to reach it) |
| News_bot data | `~/docker/stacks/news-bot/data/news.db` |
| cloudflared stack | `~/docker/stacks/cloudflared/` |
| Shared Docker network | `cloudflared_net` |
| Old native deployment (fallback, frozen) | `/home/troy314/App/News-bot`, systemd unit `news-bot.service` (disabled) |
| Domain | `alpsciences.com`, registrar OVH, DNS now on Cloudflare (`austin`/`mallory`.ns.cloudflare.com) |
| Public hostname | `news.alpsciences.com` — live, gated by Cloudflare Access |
| Cloudflare Zero Trust team domain | `shiny-base-79de.cloudflareaccess.com` |
| Access-allowed email | `hectorpillot@gmail.com` (One-Time PIN login) |
