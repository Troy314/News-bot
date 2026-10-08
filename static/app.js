// News Bot frontend -- vanilla JS, no build step. Talks to the FastAPI
// backend in web.py over fetch().

const state = {
  articles: [],
  sources: [],
  folders: [],
  activeFolderId: null,
  jobs: [],
  jobTags: [],
};

// Escapes quotes too, so this is safe inside attribute values as well as text.
// The old innerHTML-based version left " and ' alone, which broke any
// data-* attribute holding JSON or a title with quotes -- the card's
// data-job-tags and filter logic silently failed for those cards.
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// A CSS-selector text input paired with a "🎯 Pick" button that opens the
// live-preview picker (see the "Selector picker" section below) -- shared
// by both the Add-source and Edit-source forms, whose fields are identical
// except for the "sel-"/"edit-sel-" id prefix.
function selectorFieldHtml(formType, key, label, value = "") {
  const id = (formType === "edit" ? "edit-sel-" : "sel-") + key;
  return `
    <div class="field">
      <label>${label}</label>
      <div class="selector-field-row">
        <input type="text" id="${id}" value="${escapeHtml(value)}" style="width:100%">
        <button type="button" class="pick-btn" data-form="${formType}" data-field="${key}" title="Pick this element on a live preview of the page">🎯 Pick</button>
      </div>
    </div>`;
}

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed (${resp.status})`);
  }
  if (resp.status === 204) return null;
  return resp.json();
}

function sourceHues(names) {
  const ordered = [...new Set(names)].sort();
  const n = ordered.length || 1;
  const hues = {};
  ordered.forEach((name, i) => { hues[name] = Math.round((i * 360) / n); });
  return hues;
}

function timeAgo(pubDate) {
  if (!pubDate) return "";
  const seconds = (Date.now() - new Date(pubDate).getTime()) / 1000;
  if (seconds < 0) return "just now";
  if (seconds < 3600) return `${Math.max(1, Math.floor(seconds / 60))}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  if (seconds < 7 * 86400) return `${Math.floor(seconds / 86400)}d ago`;
  if (seconds < 31 * 86400) return `${Math.floor(seconds / (7 * 86400))}w ago`;
  return `${Math.floor(seconds / (31 * 86400))}mo ago`;
}

function fullDate(pubDate) {
  if (!pubDate) return "Unknown date";
  return new Date(pubDate).toLocaleString(undefined, {
    year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

// -------------------------------------------------------------------------
// Tabs
// -------------------------------------------------------------------------

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(`view-${btn.dataset.view}`).classList.add("active");
    if (btn.dataset.view === "favorites") loadFavorites();
    if (btn.dataset.view === "jobs") loadJobs();
    if (btn.dataset.view === "analysis") loadAnalysis();
    if (btn.dataset.view === "sources") loadSources();
    if (btn.dataset.view === "settings") loadSettings();
    if (btn.dataset.view === "debug") loadDebug();
  });
});

// -------------------------------------------------------------------------
// Feed
// -------------------------------------------------------------------------

// Per-article "viewed"/"remind me later" state -- localStorage only (no
// server column for this, same "per-viewer convenience, not shared state"
// spirit as everything else this app keeps client-side only), keyed by
// article id. A viewed article's card is grayed out; a snoozed one stays
// grayed until its 24h remindAt passes, at which point it's treated as
// unviewed again (both flags cleared) so the card "lights back up" and
// reads as fresh/prominent again rather than needing a distinct third
// visual state that would just look identical to "never viewed" anyway.
const FEED_STATE_KEY = "newsbot-feed-article-state";
const REMIND_LATER_MS = 24 * 60 * 60 * 1000;

function loadFeedState() {
  try {
    return JSON.parse(localStorage.getItem(FEED_STATE_KEY) || "{}");
  } catch (e) {
    return {};
  }
}

function saveFeedState(state) {
  try {
    localStorage.setItem(FEED_STATE_KEY, JSON.stringify(state));
  } catch (e) {
    // Private-browsing / storage-disabled -- per-viewer convenience only,
    // fine to silently no-op (see artifact-capabilities-style guidance:
    // never let a storage failure break the page).
  }
}

// "due" (remindAt has passed) takes priority over "viewed" -- that's the
// whole point of the feature, a snoozed article should stop reading as
// grayed-out dead weight once its time is up. Lazily clears an expired
// remindAt right here (rather than a separate sweep) so the very next
// state read/write already reflects it.
function feedArticleStatus(id) {
  const state = loadFeedState();
  const entry = state[id];
  if (!entry) return "normal";
  if (entry.remindAt) {
    if (Date.now() >= entry.remindAt) {
      delete state[id];
      saveFeedState(state);
      return "normal";
    }
    return "viewed";
  }
  return entry.viewed ? "viewed" : "normal";
}

function markArticleViewed(id) {
  const state = loadFeedState();
  state[id] = { ...state[id], viewed: true };
  saveFeedState(state);
}

function markArticleRemindLater(id) {
  const state = loadFeedState();
  state[id] = { viewed: true, remindAt: Date.now() + REMIND_LATER_MS };
  saveFeedState(state);
}

// Swaps a broken/unreachable <img> (article image or source logo, either
// one can 404 or just not load) for the same letter-avatar placeholder a
// card with no image at all already gets -- called from the <img>'s own
// onerror rather than inlining the replacement HTML there, since escaping
// a full HTML string inside an HTML attribute inside a template literal
// got unreadable fast. Global (not module-scoped) since onerror="..." is
// evaluated as a plain global-scope expression.
function cardImageFallback(img) {
  const wrap = img.parentElement;
  wrap.classList.add("card-image--placeholder");
  wrap.style.setProperty("--hue", img.dataset.hue);
  wrap.textContent = img.dataset.fallbackInitial;
}

function renderCard(article, hues, {
  showFavoriteToggle = true, showContentTag = true, allowDeadlineEdit = false, allowArchive = false,
  selectMode = false, selectedSet = null, openEmbedded = false,
} = {}) {
  const hue = hues[article.source_name] ?? 0;
  // "Always show a picture" is Feed-only (openEmbedded) -- the article's
  // own image, then the source's configured logo_url (sources.logo_url --
  // blank until one is actually set), then a generic letter-avatar
  // placeholder (source's first initial on a hue-tinted background, same
  // hue the source's tag chip already uses). Jobs postings almost never
  // have an image_selector configured, so this would otherwise put a big
  // placeholder tile on nearly every job card -- explicitly asked *not*
  // to have any picture there, so Jobs keeps the original "only if a real
  // image exists" behavior, no fallback chain at all.
  const initial = escapeHtml((article.source_name || "?").charAt(0).toUpperCase());
  let image;
  if (openEmbedded) {
    const cardImageSrc = article.image_url || article.source_logo || null;
    image = cardImageSrc
      ? `<div class="card-image"><img src="${escapeHtml(cardImageSrc)}" alt="" loading="lazy" data-fallback-initial="${initial}" data-hue="${hue}" onerror="cardImageFallback(this)"></div>`
      : `<div class="card-image card-image--placeholder" style="--hue:${hue}">${initial}</div>`;
  } else {
    image = article.image_url
      ? `<div class="card-image"><img src="${escapeHtml(article.image_url)}" alt="" loading="lazy" onerror="this.parentElement.remove()"></div>`
      : "";
  }
  // "New scraped" vs "older" -- articles.notified (already a real,
  // existing column: 0 until the next daily digest run marks everything
  // notified in one batch) rather than a new field, and deliberately kept
  // separate from the personal per-viewer "viewed"/card--viewed state
  // above: notified reflects the digest pipeline (has this gone out in a
  // Discord/email digest yet), not whether *you* individually opened it
  // in the web UI. Sort/day-grouping is untouched either way -- this is a
  // badge, not a different ordering. Feed-only, requested directly --
  // Jobs cards never show it (allowDeadlineEdit doubles as "this is a
  // Jobs card", same flag deadline/remuneration/city/archive already key
  // off), since "new" read as noise there: most job sources' digests
  // aren't the way the user actually notices a posting, unlike Feed.
  const newBadge = !article.notified && !allowDeadlineEdit ? `<span class="new-badge">NEW</span>` : "";
  // Moved into the article-viewer modal itself for openEmbedded cards (see
  // openArticleViewer) -- once a click anywhere on the card opens that
  // modal instead of the raw link, keeping a second independent action
  // (favorite) on the card underneath just adds a competing click target
  // for no benefit; every action for an embedded-viewed article now lives
  // in one place. Jobs cards (openEmbedded is never true there) are
  // unaffected -- still favoritable straight from the card as before.
  const favBtn = showFavoriteToggle && !openEmbedded
    ? `<button class="fav-btn" data-id="${article.id}" title="Favorite"${selectMode ? " disabled" : ""}>${article.is_favorite ? "★" : "☆"}</button>`
    : "";
  // content_tag is a physics-topic classification (neutrino, muon, ...) --
  // meaningless for a job/thesis-offer listing, so the Jobs tab suppresses
  // it via showContentTag:false rather than showing "General Physics" on
  // every card.
  const contentTag = article.content_tag && showContentTag
    ? `<span class="content-tag">${escapeHtml(article.content_tag)}</span>`
    : "";
  // An offer can carry several topic tags (Fellowship/PhD/Outreach/...).
  // On Jobs cards (allowDeadlineEdit doubles as "this is a Jobs card") they
  // are removable chips plus a picker of the tags the offer doesn't have
  // yet; elsewhere, plain read-only chips. Every edit sends the full
  // resulting list (see setJobCardTags), so a stale card can't half-apply.
  const jobTagsList = article.job_tag_list || [];
  // Subject topics: free-form, multi-valued, never auto-filled. Jobs cards only
  // (the editor needs the Jobs-only allowDeadlineEdit flag); elsewhere nothing.
  const topicsList = article.topic_list || [];
  const topics = allowDeadlineEdit
    ? `<span class="topics">
        ${topicsList.map((t) => `<span class="topic-tag">${escapeHtml(t)}<button class="topic-remove" data-id="${article.id}" data-topic="${escapeHtml(t)}" title="Remove this topic" aria-label="Remove ${escapeHtml(t)}"${selectMode ? " disabled" : ""}>✕</button></span>`).join("")}
        <input type="text" class="topic-add" data-id="${article.id}" list="topic-suggestions" placeholder="+ topic" title="Add a subject topic (Enter)"${selectMode ? " disabled" : ""}>
      </span>`
    : topicsList.map((t) => `<span class="topic-tag">${escapeHtml(t)}</span>`).join("");
  const jobTag = allowDeadlineEdit
    ? `<span class="job-tags">
        ${jobTagsList.map((t) => `<span class="job-tag">${escapeHtml(t)}<button class="job-tag-remove" data-id="${article.id}" data-tag="${escapeHtml(t)}" title="Remove this tag" aria-label="Remove ${escapeHtml(t)}"${selectMode ? " disabled" : ""}>✕</button></span>`).join("")}
        ${jobTagsList.length === 0 ? `<span class="job-tag job-tag--none">Untagged</span>` : ""}
        <select class="job-tag-add" data-id="${article.id}" title="Add a topic tag"${selectMode ? " disabled" : ""}>
          <option value="">+ tag</option>
          ${(state.jobTags || []).filter((t) => !jobTagsList.includes(t.name))
            .map((t) => `<option value="${escapeHtml(t.name)}">${escapeHtml(t.name)}</option>`).join("")}
        </select>
      </span>`
    : jobTagsList.map((t) => `<span class="job-tag">${escapeHtml(t)}</span>`).join("");
  // Only ever set for kind="rss" sources (see scraper.extract_excerpt) --
  // null for every css-scraped source (job listings included), so this
  // renders nothing extra there, same "absent means no markup" shape as
  // jobTag/deadline below.
  // On Jobs cards a saved page snapshot is a far better description than the
  // scraped one-liner, so its opening text takes over (see savedTextDescription).
  const description = allowDeadlineEdit && article.saved_text
    ? savedTextDescription(article)
    : article.excerpt;
  const excerpt = description
    ? `<p class="card-excerpt">${escapeHtml(description)}</p>`
    : "";
  const deadline = article.deadline
    ? `<span class="deadline-badge" data-passed="${new Date(article.deadline).getTime() < Date.now()}">Deadline: ${escapeHtml(fullDate(article.deadline))}</span>`
    : "";
  // Most job sources don't expose a deadline to scrape at all (see
  // CLAUDE.md's "Job deadlines") -- this lets the user set/correct/clear
  // one by hand instead of it just staying permanently blank. Only shown
  // where the caller opts in (Jobs tab), not on ordinary Feed cards.
  const deadlineEditBtn = allowDeadlineEdit
    ? `<button class="deadline-edit-btn" data-id="${article.id}" data-deadline="${escapeHtml(article.deadline ? isoDay(new Date(article.deadline)) : "")}" title="${article.deadline ? "Edit deadline" : "Set application deadline"}"${selectMode ? " disabled" : ""}>${article.deadline ? "✎" : "+ Deadline"}</button>`
    : "";
  // scraper.extract_remuneration() is best-effort (title/excerpt only --
  // see its docstring), so this badge is absent as often as present;
  // the edit button (Jobs only, same allowDeadlineEdit gating) is the
  // reliable way to actually fill it in.
  const remuneration = article.remuneration
    ? `<span class="remuneration-badge">💰 ${escapeHtml(article.remuneration)}</span>`
    : "";
  const remunerationEditBtn = allowDeadlineEdit
    ? `<button class="remuneration-edit-btn" data-id="${article.id}" data-remuneration="${escapeHtml(article.remuneration || "")}" title="${article.remuneration ? "Edit pay" : "Set pay"}"${selectMode ? " disabled" : ""}>${article.remuneration ? "✎" : "+ Pay"}</button>`
    : "";
  // scraper's location_selector is best-effort (most CSS job sources don't
  // expose a location element at all, and RSS/email-imported offers never
  // will), so this badge is absent as often as present; the edit button
  // (Jobs only, same allowDeadlineEdit gating) is the reliable way to fill
  // it in by hand, same pair-of-controls shape as remuneration above.
  const city = article.city
    ? `<span class="city-badge">📍 ${escapeHtml(article.city)}</span>`
    : "";
  const cityEditBtn = allowDeadlineEdit
    ? `<button class="city-edit-btn" data-id="${article.id}" data-city="${escapeHtml(article.city || "")}" title="${article.city ? "Edit location" : "Set location"}"${selectMode ? " disabled" : ""}>${article.city ? "✎" : "+ City"}</button>`
    : "";
  // Manual URL correction -- most useful for the "Manually added" source
  // (see CLAUDE.md), whose rows only ever got a Google-search fallback
  // link, not a real one, at import time. Jobs-only (same gating as
  // every other manual-edit control here), always "✎" (never "+") since
  // link is NOT NULL and every article already has one.
  const linkEditBtn = allowDeadlineEdit
    ? `<button class="link-edit-btn" data-id="${article.id}" data-link="${escapeHtml(article.link)}" title="Edit this article's URL"${selectMode ? " disabled" : ""}>✎ URL</button>`
    : "";
  // Hides the offer from the Jobs tab (list + calendar) without deleting
  // it -- confirmed with a native confirm() before the API call actually
  // fires, same lightweight pattern the Sources tab's delete button
  // already uses in this app (not the instant-delete-with-undo toast flow
  // Collection-style apps use elsewhere, since there's no "undo" here --
  // just a reversible unarchive via the API if ever needed). Now a real,
  // reachable toggle both ways -- "↩ Unarchive" when already archived,
  // only ever shown here when the Jobs tab's "Show archived" toggle is
  // on, since an archived article isn't fetched at all otherwise (same
  // shape as hideBtn's Hide/Unhide pair below).
  const archiveBtn = allowArchive
    ? article.archived
      ? `<button class="unarchive-btn" data-id="${article.id}" title="Unarchive this offer"${selectMode ? " disabled" : ""}>↩ Unarchive</button>`
      : `<button class="archive-btn" data-id="${article.id}" title="Archive this offer"${selectMode ? " disabled" : ""}>🗄 Archive</button>`
    : "";
  // A second, gentler declutter action, distinct from Archive -- hides an
  // offer from the main list the same way, but never clears pay or saved
  // text (Archive does, see archive_article's docstring). Only ever
  // reachable as "Unhide" here when the Jobs tab's "Show hidden" toggle is
  // on, since a hidden article isn't fetched at all otherwise.
  const hideBtn = allowArchive
    ? article.hidden
      ? `<button class="unhide-btn" data-id="${article.id}" title="Unhide this offer"${selectMode ? " disabled" : ""}>👁 Unhide</button>`
      : `<button class="hide-btn" data-id="${article.id}" title="Hide this offer (keeps pay/saved text)"${selectMode ? " disabled" : ""}>🙈 Hide</button>`
    : "";
  // Saves a local plain-text snapshot of the offer's own page (see
  // openSaveTextModal/save_article_text) -- Jobs-only (allowArchive
  // doubles as "this is a Jobs card", same flag deadline/remuneration/
  // archive already key off), since a job listing routinely disappears or
  // gets edited once its deadline passes, unlike a news article. Pasted by
  // hand (View Page Source or copy the rendered page), not fetched
  // server-side -- job pages are disproportionately likely to sit behind
  // Cloudflare/a login wall, same class of block this app already hits
  // for whole sources elsewhere. Archiving the offer clears the snapshot
  // again (server-side, see archive_article) -- this bridges the gap
  // before a decision is made, it isn't a permanent second archive.
  const saveTextBtn = allowArchive
    ? article.saved_text
      ? `<button class="view-text-btn" data-id="${article.id}" title="View the saved page text"${selectMode ? " disabled" : ""}>📄 Text</button>`
      : `<button class="save-text-btn" data-id="${article.id}" title="Paste this page's text (survives if the listing is taken down)"${selectMode ? " disabled" : ""}>💾 Save text</button>`
    : "";
  // Jobs' bulk-archive select mode. The checkbox is still the visual
  // indicator of selection, but the whole card -- image included, via
  // data-select-id on the outer .card rather than just .card-body -- is
  // what's actually clickable (see the delegated click handler below);
  // easier to hit than a small checkbox, and fav/deadline/archive are all
  // disabled while this is on (see above) so there's nothing else on the
  // card competing for the click except the checkbox itself, excluded
  // there so the two don't double-toggle each other. Reflects
  // selectedSet's *current* membership at render time (same reasoning as
  // renderTagChips' activeSet check elsewhere in this file) so re-
  // rendering after a selection change doesn't visually reset it.
  const selectCheckbox = selectMode
    ? `<input type="checkbox" class="job-select" data-id="${article.id}"${selectedSet?.has(article.id) ? " checked" : ""}>`
    : "";
  // The <a> itself is always a plain, real link (href + target="_blank")
  // regardless of openEmbedded -- hover preview, right-click "copy link"/
  // "open in new tab", and a real new tab on ctrl/cmd/middle-click all keep
  // working natively. Feed's "open embedded" viewer (a same-origin iframe
  // modal instead of a new tab -- see view_article_html in web.py) is
  // layered on top via the delegated click handler below, keyed off
  // data-embed-url on the outer .card (image included, not just the text
  // body -- clicking the thumbnail opens the article same as the title),
  // which preventDefault()s a plain click before the link's own default
  // navigation fires.
  const titleLink = `<a href="${escapeHtml(article.link)}" target="_blank" rel="noopener noreferrer">${escapeHtml(article.title)}</a>`;
  // "viewed"/"remind me later" graying -- see feedArticleStatus above.
  // Feed-only (openEmbedded): Jobs has no such concept, and a job posting
  // being "already looked at" isn't the same signal as a news article
  // being read.
  const viewedClass = openEmbedded && feedArticleStatus(article.id) === "viewed" ? " card--viewed" : "";
  const hiddenClass = article.hidden ? " card--hidden" : "";
  const archivedClass = article.archived ? " card--archived" : "";
  return `
    <div class="card${viewedClass}${hiddenClass}${archivedClass}" data-source="${escapeHtml(article.source_name)}" data-tag="${escapeHtml(article.content_tag || "")}" data-job-tags="${escapeHtml(JSON.stringify(jobTagsList.length ? jobTagsList : ["Untagged"]))}" data-deadline="${escapeHtml(article.deadline || "")}"${selectMode ? ` data-select-id="${article.id}"` : ""}${openEmbedded ? ` data-embed-url="${escapeHtml(article.link)}" data-embed-title="${escapeHtml(article.title)}" data-embed-id="${article.id}" data-embed-fav="${article.is_favorite ? "1" : "0"}"` : ""}>
      ${selectCheckbox}
      ${favBtn}
      ${image}
      <div class="card-body">
        <h3>${titleLink}</h3>
        ${excerpt}
        <div class="card-meta">
          <span class="tag" style="--hue:${hue}">${escapeHtml(article.source_name)}</span>
          ${newBadge}
          ${contentTag}
          ${jobTag}
          ${topics}
          <span title="${escapeHtml(timeAgo(article.pub_date))}">${escapeHtml(fullDate(article.pub_date))}</span>
          ${deadline}
          ${deadlineEditBtn}
          ${remuneration}
          ${remunerationEditBtn}
          ${city}
          ${cityEditBtn}
          ${linkEditBtn}
          ${saveTextBtn}
          ${hideBtn}
          ${archiveBtn}
        </div>
      </div>
    </div>`;
}

// Two independent filters (source, content topic) combined with AND --
// each chip group is wired the same way via wireChips(), and either one
// changing re-applies the combined filter over both.
const activeSources = new Set();
const activeTags = new Set();

function wireChips(container, activeSet, dataKey, onChange) {
  container.querySelectorAll(".chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const val = chip.dataset[dataKey];
      if (val === "__all__") {
        activeSet.clear();
        container.querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
      } else {
        chip.classList.toggle("active");
        activeSet.has(val) ? activeSet.delete(val) : activeSet.add(val);
      }
      container.querySelector(".chip-all").classList.toggle("active", activeSet.size === 0);
      onChange();
    });
  });
}

// Source chips are visually clustered by each source's group_tag (see
// "Source groups" in CLAUDE.md) rather than having a separate group filter
// row -- one flat chip list, sections just break onto their own line via
// .chip-group-label's flex-basis:100%. Falls back to a plain unclustered
// list when no source has a group set, so the common "haven't used groups
// yet" case doesn't show an empty/pointless "Ungrouped" label.
function renderSourceChips(container, articles, activeSet, onChange) {
  const counts = {};
  const groupOf = {};
  articles.forEach((a) => {
    counts[a.source_name] = (counts[a.source_name] || 0) + 1;
    groupOf[a.source_name] = a.source_group || null;
  });
  const hues = sourceHues(Object.keys(counts));
  const names = Object.keys(counts).sort();
  const chipHtml = (name) =>
    `<button class="chip" data-hue="1" style="--hue:${hues[name]}" data-source="${escapeHtml(name)}">${escapeHtml(name)} <span style="opacity:.65">${counts[name]}</span></button>`;

  let html = `<button class="chip chip-all active" data-source="__all__">All sources</button>`;
  const hasAnyGroup = names.some((name) => groupOf[name]);

  if (!hasAnyGroup) {
    html += names.map(chipHtml).join("");
  } else {
    const byGroup = new Map();
    names.forEach((name) => {
      const g = groupOf[name] || "Ungrouped";
      if (!byGroup.has(g)) byGroup.set(g, []);
      byGroup.get(g).push(name);
    });
    const groupNames = [...byGroup.keys()].sort((a, b) => {
      if (a === "Ungrouped") return 1;
      if (b === "Ungrouped") return -1;
      return a.localeCompare(b);
    });
    groupNames.forEach((g) => {
      html += `<span class="chip-group-label">${escapeHtml(g)}</span>` + byGroup.get(g).map(chipHtml).join("");
    });
  }

  container.innerHTML = html;
  wireChips(container, activeSet, "source", onChange);
  return hues;
}

// Generalized so Jobs' job_tag chip row (Fellowship/PhD/Internship/...)
// reuses this instead of a near-duplicate function -- field picks which
// article property to count/group by, emptyLabel buckets articles with no
// value there (job_tag is genuinely absent for most postings, unlike
// content_tag which always has at least "General Physics").
function renderTagChips(container, articles, {
  field = "content_tag", activeSet = activeTags, onChange = applyFeedFilter,
  allLabel = "All topics", emptyLabel = null,
} = {}) {
  const counts = {};
  articles.forEach((a) => {
    const raw = a[field];
    // A list field (a multi-tagged offer) counts once under each of its tags.
    const vals = Array.isArray(raw) ? (raw.length ? raw : [emptyLabel]) : [raw || emptyLabel];
    vals.filter(Boolean).forEach((val) => { counts[val] = (counts[val] || 0) + 1; });
  });
  const names = Object.keys(counts).sort();
  // Reflects activeSet's *current* membership at render time, not just an
  // always-starts-empty assumption -- needed now that Jobs pre-populates
  // activeJobTags from a persisted hidden-tags set before this ever runs
  // (see loadHiddenJobTags), so those chips need to render already-toggled.
  container.innerHTML = `<button class="chip chip-all${activeSet.size === 0 ? " active" : ""}" data-tag="__all__">${allLabel}</button>` +
    names.map((name) => `<button class="chip${activeSet.has(name) ? " active" : ""}" data-tag="${escapeHtml(name)}">${escapeHtml(name)} <span style="opacity:.65">${counts[name]}</span></button>`).join("");
  wireChips(container, activeSet, "tag", onChange);
}

function dayLabel(pubDate) {
  if (!pubDate) return "Unknown date";
  const d = new Date(pubDate);
  const now = new Date();
  const startOf = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diffDays = Math.round((startOf(now) - startOf(d)) / 86400000);
  if (diffDays === 0) return "Today";
  if (diffDays === 1) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "long", year: "numeric", month: "long", day: "numeric" });
}

function groupByDay(articles) {
  const groups = new Map();
  articles.forEach((a) => {
    const key = a.pub_date ? new Date(a.pub_date).toDateString() : "unknown";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(a);
  });
  return groups;
}

const OPEN_BY_DEFAULT_MS = 30 * 24 * 60 * 60 * 1000; // keep the last month's days expanded

function renderDayGroup(articles, hues, isOpen, cardOptions = {}) {
  const cards = articles.map((a) => renderCard(a, hues, cardOptions)).join("");
  return `
    <details class="day-group"${isOpen ? " open" : ""}>
      <summary>
        <span class="chevron">&#9656;</span>
        <span class="day-group-label">${escapeHtml(dayLabel(articles[0].pub_date))}</span>
        <span class="day-group-count">${articles.length} article${articles.length === 1 ? "" : "s"}</span>
      </summary>
      <div class="grid">${cards}</div>
    </details>`;
}

// Shared by Feed (source + content-topic chips, tagKey "tag") and Jobs
// (source + job-tag chips, tagKey "jobTag" -- the dataset-API camelCase
// form of the card's data-job-tag attribute). activeTagsSet may be null
// (tagOk unconditionally true) for a view with no tag filter at all.
// tagMode "include" (Feed's content-topic chips): empty set = show all,
// non-empty = show *only* the active tags. tagMode "exclude" (Jobs' tag
// chips, which default to hiding a few noisy tags -- see loadHiddenJobTags
// below): empty set = show all too (the two modes agree on "nothing
// selected"), but a non-empty set *hides* those tags instead of narrowing
// to them -- the opposite polarity, same deliberate asymmetry this app's
// design lineage already uses for category vs difficulty filters elsewhere.
// searchTerm (already lowercased/trimmed by the caller) matches against
// the card's own title text -- cheap and sufficient for "find the one
// article I remember the headline of" without needing a server round-trip,
// same "filter what's already loaded client-side" approach every other
// filter here already uses. unreadOnly reuses card--viewed (see
// feedArticleStatus) rather than a separate concept -- Feed-only, Jobs
// always passes false since it has no such state.
// A card's topic-tag list, from its data-job-tags JSON. Defensive on purpose:
// one malformed attribute must not abort the whole filter pass (which left
// every later card unfiltered).
function cardTagList(card) {
  try {
    const list = JSON.parse(card.dataset.jobTags || "[]");
    return list.length ? list : ["Untagged"];
  } catch {
    return ["Untagged"];
  }
}

function applyDayFilter(
  containerId, activeSourcesSet, activeTagsSet, noMatchElId, tagKey = "tag", tagMode = "include",
  searchTerm = "", unreadOnly = false, hideExpired = false,
) {
  let anyVisible = false;
  document.querySelectorAll(`#${containerId} .day-group`).forEach((dayGroup) => {
    let dayGroupVisible = false;
    dayGroup.querySelectorAll(".card").forEach((card) => {
      const sourceOk = activeSourcesSet.size === 0 || activeSourcesSet.has(card.dataset.source);
      let tagOk = true;
      if (activeTagsSet) {
        const cardTags = tagKey === "jobTags"
          ? cardTagList(card)
          : [card.dataset[tagKey]];
        const hit = cardTags.some((t) => activeTagsSet.has(t));
        tagOk = tagMode === "exclude" ? !hit : (activeTagsSet.size === 0 || hit);
      }
      const searchOk = !searchTerm || card.querySelector("h3").textContent.toLowerCase().includes(searchTerm);
      const unreadOk = !unreadOnly || !card.classList.contains("card--viewed");
      // Jobs-only (hideExpired is never true for Feed's own call site) --
      // a blank data-deadline (most postings never get one, see "Job
      // deadlines" in CLAUDE.md) always passes, since there's nothing to
      // judge as expired; only a real, parseable, past date is filtered.
      const deadlineOk = !hideExpired || !card.dataset.deadline
        || new Date(card.dataset.deadline).getTime() >= Date.now();
      const show = sourceOk && tagOk && searchOk && unreadOk && deadlineOk;
      card.classList.toggle("hidden", !show);
      if (show) dayGroupVisible = true;
    });
    dayGroup.style.display = dayGroupVisible ? "" : "none";
    if (dayGroupVisible) anyVisible = true;
  });
  document.getElementById(noMatchElId).style.display = anyVisible ? "none" : "block";
}

let feedSearchTerm = "";
let feedUnreadOnly = false;

function applyFeedFilter() {
  applyDayFilter("feed-days", activeSources, activeTags, "feed-no-match", "tag", "include", feedSearchTerm, feedUnreadOnly);
}

async function loadFeed() {
  // Job/thesis-offer sources (category "job") have their own tab (Jobs) --
  // excluded here so they don't also show up mixed into the physics-news
  // feed, same category-scoping Jobs itself uses in reverse (category=job).
  const articlesRaw = await api("/api/articles?exclude_category=job");
  // folder_id can legitimately be null for a favorite with no folder, so
  // favorite status needs its own dedicated fetch, not a folder_id check.
  const favRows = await api("/api/articles?exclude_category=job&favorites_only=true");
  const favSet = new Set(favRows.map((a) => a.id));
  articlesRaw.forEach((a) => { a.is_favorite = favSet.has(a.id); });

  state.articles = articlesRaw;
  const daysEl = document.getElementById("feed-days");
  const empty = document.getElementById("feed-empty");
  const noMatch = document.getElementById("feed-no-match");
  const chipsEl = document.getElementById("feed-chips");
  const tagChipsEl = document.getElementById("feed-tag-chips");

  activeSources.clear();
  activeTags.clear();
  noMatch.style.display = "none";

  if (articlesRaw.length === 0) {
    daysEl.innerHTML = "";
    empty.style.display = "block";
    chipsEl.innerHTML = "";
    tagChipsEl.innerHTML = "";
    return;
  }
  empty.style.display = "none";

  const hues = renderSourceChips(chipsEl, articlesRaw, activeSources, applyFeedFilter);
  renderTagChips(tagChipsEl, articlesRaw);

  const groups = groupByDay(articlesRaw);
  const openSince = Date.now() - OPEN_BY_DEFAULT_MS;
  let html = "";
  for (const [, group] of groups) {
    const pubDate = group[0].pub_date;
    const isRecent = pubDate ? new Date(pubDate).getTime() >= openSince : false;
    html += renderDayGroup(group, hues, isRecent, { openEmbedded: true });
  }
  daysEl.innerHTML = html;
  // No per-card fav-btn to bind here anymore -- Feed's favorite toggle now
  // lives inside the article-viewer modal (openArticleViewer binds its own
  // button directly when it's created).
  // feedSearchTerm/feedUnreadOnly persist across reloads (unlike
  // activeSources/activeTags above, deliberately not cleared -- clearing
  // an in-progress search every time the list refreshes, e.g. after
  // "Check for news now", would be more disruptive than helpful), so a
  // freshly re-rendered set of cards needs the filter re-applied
  // explicitly here rather than relying on it having already run once.
  applyFeedFilter();
}

async function toggleFavorite(btn) {
  const id = Number(btn.dataset.id);
  const isFav = btn.textContent.trim() === "★";
  if (isFav) {
    await api(`/api/articles/${id}/favorite`, { method: "DELETE" });
    btn.textContent = "☆";
  } else {
    await api(`/api/articles/${id}/favorite`, { method: "PUT", body: JSON.stringify({ folder_id: null }) });
    btn.textContent = "★";
  }
}

// Feed's "open embedded" article viewer -- a same-origin iframe modal
// (backed by GET /api/articles/view-html, see web.py) instead of a new
// tab, reusing the same .modal/.modal__box shell + iframe-in-a-modal
// pattern as the Add-source selector picker (openPicker) elsewhere in
// this file. No sandbox="allow-scripts" here (unlike the picker's
// iframe) -- view-html already strips every <script> tag server-side, and
// nothing here needs to run any.
//
// The favorite toggle lives here now, not on the card (see renderCard's
// favBtn comment) -- id/isFavorite come in via the card's own
// data-embed-id/data-embed-fav rather than a second API call, since the
// card was already rendered from a fully-loaded article. "Remind me
// later" and opening the viewer at all both mark the article viewed
// (feedArticleStatus) -- immediately reflected on the underlying card's
// class too, not just persisted for next load, so the grayed-out state is
// visible the moment you close the modal instead of only after a refresh.
function openArticleViewer(url, title, id, isFavorite) {
  markArticleViewed(id);
  document.querySelector(`.card[data-embed-id="${id}"]`)?.classList.add("card--viewed");

  document.getElementById("article-viewer-modal")?.remove();
  const wrapper = document.createElement("div");
  wrapper.id = "article-viewer-modal";
  wrapper.className = "modal";
  wrapper.innerHTML = `
    <div class="modal__box article-viewer-box">
      <div class="modal__head">
        <h3>${escapeHtml(title)}</h3>
        <div class="article-viewer-head-actions">
          <button class="fav-btn article-viewer-fav" data-id="${id}" title="Favorite">${isFavorite ? "★" : "☆"}</button>
          <button class="modal__close">&times;</button>
        </div>
      </div>
      <div class="article-viewer-actions">
        <button class="article-remind-later-btn" data-id="${id}">⏰ Remind me later (24h)</button>
        <a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" class="hint">Open in a new tab instead ↗</a>
      </div>
      <div class="article-viewer-frame-wrap">
        <iframe src="/api/articles/view-html?url=${encodeURIComponent(url)}"></iframe>
      </div>
    </div>`;
  document.body.appendChild(wrapper);
  const close = () => wrapper.remove();
  wrapper.addEventListener("click", (e) => { if (e.target === wrapper) close(); });
  wrapper.querySelector(".modal__close").addEventListener("click", close);
  wrapper.querySelector(".article-viewer-fav").addEventListener("click", (e) => toggleFavorite(e.target));
  wrapper.querySelector(".article-remind-later-btn").addEventListener("click", () => {
    markArticleRemindLater(id);
    document.querySelector(`.card[data-embed-id="${id}"]`)?.classList.add("card--viewed");
    close();
  });
}

// Delegated (cards are re-rendered on every filter change) -- the whole
// card (image included) is the click target (data-embed-url only exists
// there when openEmbedded is set, see renderCard), not just the title
// link, same "easier to hit than one small element" reasoning as Jobs'
// select handler. Excludes the favorite star, which has its own separate
// handler (toggleFavorite) and would otherwise also bubble up into this
// one and wrongly open the viewer on a favorite click. Only a plain
// left-click is intercepted; ctrl/cmd/middle-click and "open in new tab"
// still work normally on the title's own <a> since preventDefault() is
// skipped for those, same courtesy a real link should always extend.
document.addEventListener("click", (e) => {
  if (e.target.closest(".fav-btn")) return;
  const card = e.target.closest(".card[data-embed-url]");
  if (!card) return;
  if (e.ctrlKey || e.metaKey || e.shiftKey || e.button === 1) return;
  e.preventDefault();
  openArticleViewer(card.dataset.embedUrl, card.dataset.embedTitle, Number(card.dataset.embedId), card.dataset.embedFav === "1");
});

// Right-click quick actions (favorite / remind later) without opening the
// viewer first -- a native <menu type="context"> was removed from web
// standards years ago, so the only way to *add* items to a right-click
// menu is to suppress the browser's own entirely and draw a replacement.
// Feed-only (data-embed-url), same scoping as everything else in this
// section; Jobs cards don't get this.
function closeCardContextMenu() {
  document.getElementById("card-context-menu")?.remove();
}

document.addEventListener("contextmenu", (e) => {
  const card = e.target.closest(".card[data-embed-url]");
  if (!card) return;
  e.preventDefault();
  closeCardContextMenu();

  const id = Number(card.dataset.embedId);
  const isFav = card.dataset.embedFav === "1";

  const menu = document.createElement("div");
  menu.id = "card-context-menu";
  menu.className = "card-context-menu";
  // clientX/Y are viewport-relative, matching position:fixed -- correct
  // regardless of how far the page is scrolled.
  menu.style.left = `${e.clientX}px`;
  menu.style.top = `${e.clientY}px`;
  menu.innerHTML = `
    <button data-action="fav">${isFav ? "★ Remove favorite" : "☆ Favorite"}</button>
    <button data-action="remind">⏰ Remind me later (24h)</button>
  `;
  document.body.appendChild(menu);

  menu.querySelector('[data-action="fav"]').addEventListener("click", async () => {
    if (isFav) {
      await api(`/api/articles/${id}/favorite`, { method: "DELETE" });
    } else {
      await api(`/api/articles/${id}/favorite`, { method: "PUT", body: JSON.stringify({ folder_id: null }) });
    }
    card.dataset.embedFav = isFav ? "0" : "1";
    closeCardContextMenu();
  });
  menu.querySelector('[data-action="remind"]').addEventListener("click", () => {
    markArticleRemindLater(id);
    card.classList.add("card--viewed");
    closeCardContextMenu();
  });
});

// Dismiss on any other click or Escape, without interfering with what
// that click/key otherwise does (no preventDefault/stopPropagation here).
document.addEventListener("click", (e) => {
  if (!e.target.closest("#card-context-menu")) closeCardContextMenu();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeCardContextMenu();
});

document.getElementById("run-now-btn").addEventListener("click", async () => {
  const status = document.getElementById("run-now-status");
  status.textContent = "Checking every source... this can take a minute.";
  try {
    const result = await api("/api/run-now", { method: "POST" });
    status.textContent = result.sent ? "New articles found and sent!" : "No new articles since the last check.";
    await loadFeed();
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
});

document.getElementById("feed-search").addEventListener("input", (e) => {
  feedSearchTerm = e.target.value.trim().toLowerCase();
  applyFeedFilter();
});

document.getElementById("feed-unread-toggle").addEventListener("click", (e) => {
  feedUnreadOnly = !feedUnreadOnly;
  e.target.classList.toggle("active", feedUnreadOnly);
  applyFeedFilter();
});

// -------------------------------------------------------------------------
// Jobs (job/thesis-offer listing sources -- category "job", scraped by the
// exact same scraper.py engine as news sources, just surfaced on their own
// tab instead of mixed into Feed. "Check for jobs now" hits the same
// /api/run-now as Feed -- it scrapes every enabled source regardless of
// category in one pass, there's no separate job-only scrape endpoint.)
// -------------------------------------------------------------------------

const activeJobSources = new Set();
// activeJobTags is the *hidden*-tags set here (see applyDayFilter's
// tagMode "exclude"), not an inclusion filter -- persisted to localStorage
// so a tag hidden once stays hidden across reloads, with a starting
// default (Fellowship/Internship/Physicist/Post-Doc/Professorship hidden,
// PhD/Outreach/Untagged/anything else shown) requested explicitly rather
// than defaulting to "show everything."
const activeJobTags = new Set();
let jobHues = {};

// Bulk-archive select mode -- a plain checkbox per card (renderCard's
// selectMode option), not "click the card to select" the way some apps do
// it, since a job card's title link still needs to be independently
// clickable while this is on. selectedJobIds persists only for the
// current session/load (not localStorage -- unlike hidden job tags, a
// half-finished bulk-archive selection isn't something worth restoring
// after a reload).
let jobSelectMode = false;
const selectedJobIds = new Set();

const JOB_TAG_HIDE_KEY = "newsbot-hidden-job-tags";
const JOB_TAG_HIDE_DEFAULT = ["Fellowship", "Internship", "Physicist", "Post-Doc", "Professorship"];

function loadHiddenJobTags() {
  const stored = localStorage.getItem(JOB_TAG_HIDE_KEY);
  if (stored === null) return new Set(JOB_TAG_HIDE_DEFAULT);
  try {
    return new Set(JSON.parse(stored));
  } catch (e) {
    return new Set(JOB_TAG_HIDE_DEFAULT);
  }
}

// Separated out from loadJobs() so toggling select mode can re-render the
// day groups (to show/hide checkboxes) without an API round-trip -- it
// just regroups state.jobs, already fetched.
function renderJobDays() {
  const daysEl = document.getElementById("job-days");
  const groups = groupByDay(state.jobs);
  const openSince = Date.now() - OPEN_BY_DEFAULT_MS;
  let html = "";
  for (const [, group] of groups) {
    // Most job-board pages have no reliable per-listing date at all (a
    // search-results page just lists whatever's currently open) -- the
    // undated "unknown" bucket defaults open so those aren't hidden behind
    // an extra click, unlike Feed where "old" undated news would be rare.
    const pubDate = group[0].pub_date;
    const isRecent = pubDate ? new Date(pubDate).getTime() >= openSince : true;
    html += renderDayGroup(group, jobHues, isRecent, {
      showContentTag: false, allowDeadlineEdit: true, allowArchive: true,
      selectMode: jobSelectMode, selectedSet: selectedJobIds,
    });
  }
  daysEl.innerHTML = html;

  document.querySelectorAll("#job-days .fav-btn").forEach((btn) => {
    btn.addEventListener("click", () => toggleFavorite(btn));
  });
}

function saveHiddenJobTags() {
  localStorage.setItem(JOB_TAG_HIDE_KEY, JSON.stringify([...activeJobTags]));
}

// One click back to JOB_TAG_HIDE_DEFAULT -- added after a saved per-browser
// preference silently diverged from the current default (a tag click at
// any point persists forever, with no visible indicator that it happened).
document.getElementById("job-tag-reset-btn")?.addEventListener("click", () => {
  localStorage.removeItem(JOB_TAG_HIDE_KEY);
  loadJobs();
});

let jobSearchTerm = "";

// Hides an offer whose application deadline has already passed -- it's no
// longer actionable (you can't apply once it's closed), so it's noise in
// the normal browsing list the same way an archived offer is, but this is
// a *view* filter, not an archive: nothing is hidden server-side, and a
// wrong/corrected deadline just needs a page reload to reappear, not an
// unarchive call. Defaults on (hide expired) per explicit request, but
// stays a real, persisted toggle rather than a one-way action, since a
// deadline is sometimes stale/wrong (most sources don't expose one at
// all -- see "Job deadlines" in CLAUDE.md -- so this only ever affects
// offers where one was actually set, scraped or by hand).
const JOB_HIDE_EXPIRED_KEY = "newsbot-job-hide-expired";

function loadJobHideExpired() {
  const stored = localStorage.getItem(JOB_HIDE_EXPIRED_KEY);
  return stored === null ? true : stored === "1";
}

let jobHideExpired = loadJobHideExpired();

function applyJobFilter() {
  saveHiddenJobTags();
  localStorage.setItem(JOB_HIDE_EXPIRED_KEY, jobHideExpired ? "1" : "0");
  applyDayFilter("job-days", activeJobSources, activeJobTags, "job-no-match", "jobTags", "exclude", jobSearchTerm, false, jobHideExpired);
  renderJobCalendar();
}

// "Show hidden" (jobShowHidden) -- unlike hideExpired above, this changes
// what's actually *fetched*: a hidden article is excluded server-side by
// default (see list_articles' `hidden` filtering), not just client-side
// re-filtered, so toggling this re-runs loadJobs() with include_hidden=1
// rather than re-filtering already-fetched data. Session-local, not
// persisted -- reviewing hidden offers is an occasional deliberate check,
// not a lasting preference the way hideExpired's default is.
let jobShowHidden = false;
// Same shape as jobShowHidden, for archived offers -- added after a real
// gap: unhiding an offer that was *also* archived left it invisible
// everywhere (not hidden anymore, so "Show hidden" wouldn't surface it;
// still archived, so the normal list wouldn't either), with no UI path
// back at all. This closes that gap and doubles as the first real
// "view archived offers" surface this app has had.
let jobShowArchived = false;

// Suggestions for the topic inputs: every topic already used on any job.
function refreshTopicSuggestions(jobs) {
  let list = document.getElementById("topic-suggestions");
  if (!list) {
    list = document.createElement("datalist");
    list.id = "topic-suggestions";
    document.body.appendChild(list);
  }
  const all = [...new Set(jobs.flatMap((a) => a.topic_list || []))].sort();
  list.innerHTML = all.map((t) => `<option value="${escapeHtml(t)}">`).join("");
}

async function loadJobs() {
  const jobsRaw = await api(`/api/articles?category=job${jobShowHidden ? "&include_hidden=1" : ""}${jobShowArchived ? "&include_archived=1" : ""}`);
  const favRows = await api("/api/articles?category=job&favorites_only=true");
  const favSet = new Set(favRows.map((a) => a.id));
  jobsRaw.forEach((a) => { a.is_favorite = favSet.has(a.id); });
  state.jobs = jobsRaw;
  // Needed for renderCard's job-tag-select options -- may already be
  // populated from Sources/Settings, but Jobs can load first (or those
  // tabs never get visited this session), so fetch it fresh here too.
  state.jobTags = await api("/api/job-tags");
  refreshTopicSuggestions(jobsRaw);
  refreshPendingDuplicatesCount();

  const daysEl = document.getElementById("job-days");
  const empty = document.getElementById("job-empty");
  const noMatch = document.getElementById("job-no-match");
  const chipsEl = document.getElementById("job-chips");
  const tagChipsEl = document.getElementById("job-tag-chips");

  activeJobSources.clear();
  activeJobTags.clear();
  loadHiddenJobTags().forEach((t) => activeJobTags.add(t));
  noMatch.style.display = "none";
  selectedJobIds.clear();
  updateJobSelectedCount();
  document.getElementById("job-hide-expired-toggle").classList.toggle("active", jobHideExpired);
  document.getElementById("job-show-hidden-toggle").classList.toggle("active", jobShowHidden);
  document.getElementById("job-show-archived-toggle").classList.toggle("active", jobShowArchived);

  if (jobsRaw.length === 0) {
    daysEl.innerHTML = "";
    empty.style.display = "block";
    chipsEl.innerHTML = "";
    tagChipsEl.innerHTML = "";
    renderJobCalendar();
    return;
  }
  empty.style.display = "none";

  jobHues = renderSourceChips(chipsEl, jobsRaw, activeJobSources, applyJobFilter);
  renderTagChips(tagChipsEl, jobsRaw, {
    field: "job_tag_list", activeSet: activeJobTags, onChange: applyJobFilter,
    allLabel: "All tags", emptyLabel: "Untagged",
  });

  renderJobDays();

  // Chips render pre-toggled from the persisted hidden-tags set above, but
  // rendering the chips doesn't itself hide anything -- apply the filter
  // once so the cards actually match what the chips show.
  applyJobFilter();

  renderJobCalendar();
}

document.getElementById("job-run-now-btn").addEventListener("click", async () => {
  const status = document.getElementById("job-run-now-status");
  status.textContent = "Checking every source... this can take a minute.";
  try {
    const result = await api("/api/run-now", { method: "POST" });
    status.textContent = result.sent ? "New articles found and sent!" : "No new articles since the last check.";
    await loadJobs();
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
});

document.getElementById("job-search").addEventListener("input", (e) => {
  jobSearchTerm = e.target.value.trim().toLowerCase();
  applyJobFilter();
});

document.getElementById("job-hide-expired-toggle").addEventListener("click", (e) => {
  jobHideExpired = !jobHideExpired;
  e.target.classList.toggle("active", jobHideExpired);
  applyJobFilter();
});

// Changes what's fetched (include_hidden=1), not just client-side
// filtering -- re-runs loadJobs() rather than applyJobFilter().
document.getElementById("job-show-hidden-toggle").addEventListener("click", async (e) => {
  jobShowHidden = !jobShowHidden;
  e.target.classList.toggle("active", jobShowHidden);
  await loadJobs();
});

document.getElementById("job-show-archived-toggle").addEventListener("click", async (e) => {
  jobShowArchived = !jobShowArchived;
  e.target.classList.toggle("active", jobShowArchived);
  await loadJobs();
});

// -------------------------------------------------------------------------
// Jobs: deadline calendar view -- a real Mon-Sun month grid (same shape as
// a typical calendar app), separate from the day-grouped list above.
// Purely a client-side regroup of state.jobs by each listing's `deadline`
// field -- no new endpoint, mirrors how the list view itself is just a
// different grouping of the same fetched data.
// -------------------------------------------------------------------------

document.querySelectorAll("#job-view-toggle .view-toggle-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#job-view-toggle .view-toggle-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const isCalendar = btn.dataset.view === "calendar";
    document.getElementById("job-list-view").style.display = isCalendar ? "none" : "block";
    document.getElementById("job-calendar-view").style.display = isCalendar ? "block" : "none";
  });
});

// Quick-filter by source group (PhD / ComSci) -- a shortcut over the
// existing per-source chip filter (activeJobSources), not a separate
// filtering concept: picking a group just sets activeJobSources to every
// source name in that group, same mechanism a manual multi-select of
// individual source chips would produce. "All" clears the set, matching
// the existing "empty set = show everything" convention every other chip
// row in this app already uses. Reuses source_group off each article
// (already present in /api/articles' response) rather than a second
// lookup against /api/sources, since state.jobs already has it per-row.
document.querySelectorAll("#job-group-toggle .view-toggle-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#job-group-toggle .view-toggle-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const group = btn.dataset.group;
    activeJobSources.clear();
    if (group) {
      state.jobs
        .filter((a) => a.source_group === group)
        .forEach((a) => activeJobSources.add(a.source_name));
    }
    applyJobFilter();
  });
});

let jobCalMonth = new Date(new Date().getFullYear(), new Date().getMonth(), 1);

function buildMonthGrid(monthStart) {
  // 42 cells (6 weeks), Monday-start, padded with the adjacent months' days
  // -- same layout every month-grid calendar uses, so a 5-week month still
  // renders a full rectangular grid instead of a ragged last row.
  const firstOfMonth = new Date(monthStart);
  const jsDay = firstOfMonth.getDay(); // 0=Sun..6=Sat
  const mondayOffset = (jsDay + 6) % 7; // days to step back to reach Monday
  const gridStart = new Date(firstOfMonth);
  gridStart.setDate(gridStart.getDate() - mondayOffset);

  const cells = [];
  for (let i = 0; i < 42; i++) {
    const d = new Date(gridStart);
    d.setDate(d.getDate() + i);
    cells.push(d);
  }
  return cells;
}

function isoDay(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function renderJobCalendar() {
  const grid = document.getElementById("job-cal-grid");
  const label = document.getElementById("job-cal-label");
  const emptyEl = document.getElementById("job-cal-empty");
  if (!grid) return;

  label.textContent = jobCalMonth.toLocaleDateString(undefined, { month: "long", year: "numeric" });

  // Same Hide-expired rule as the list view (applyDayFilter): a past deadline
  // is dropped from the calendar while the toggle is on.
  const withDeadline = state.jobs.filter((a) =>
    a.deadline && (!jobHideExpired || new Date(a.deadline).getTime() >= Date.now()));
  emptyEl.style.display = withDeadline.length === 0 ? "block" : "none";

  const byDay = new Map();
  withDeadline.forEach((a) => {
    const key = isoDay(new Date(a.deadline));
    if (!byDay.has(key)) byDay.set(key, []);
    byDay.get(key).push(a);
  });

  const today = isoDay(new Date());
  const cells = buildMonthGrid(jobCalMonth);
  const html = cells.map((d) => {
    const key = isoDay(d);
    const inMonth = d.getMonth() === jobCalMonth.getMonth();
    const items = byDay.get(key) || [];
    const pills = items.slice(0, 3).map((a) => {
      const hue = jobHues[a.source_name] ?? 0;
      return `<button class="cal__pill" data-hue="1" style="--hue:${hue}" data-id="${a.id}" title="${escapeHtml(a.title)}">${escapeHtml(a.title)}</button>`;
    }).join("");
    const overflow = items.length > 3 ? `<div class="cal__more" data-day="${key}">+${items.length - 3} more</div>` : "";
    return `
      <div class="cal__cell${inMonth ? "" : " cal__cell--out"}${key === today ? " cal__cell--today" : ""}" data-day="${key}">
        <div class="cal__daynum">${d.getDate()}</div>
        <div class="cal__pills">${pills}${overflow}</div>
      </div>`;
  }).join("");

  grid.innerHTML = `
    <div class="cal__weekday">Mon</div><div class="cal__weekday">Tue</div><div class="cal__weekday">Wed</div>
    <div class="cal__weekday">Thu</div><div class="cal__weekday">Fri</div><div class="cal__weekday">Sat</div>
    <div class="cal__weekday">Sun</div>
    ${html}`;

  grid.querySelectorAll(".cal__pill, .cal__more, .cal__cell").forEach((el) => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      const day = el.dataset.day || el.closest(".cal__cell")?.dataset.day;
      if (day) openJobDayModal(day, byDay.get(day) || []);
    });
  });
}

function openJobDayModal(day, items) {
  const existing = document.getElementById("job-day-modal");
  if (existing) existing.remove();

  const hues = jobHues;
  const cards = items.map((a) => renderCard(a, hues, { showContentTag: false, allowDeadlineEdit: true, allowArchive: true })).join("");
  const wrapper = document.createElement("div");
  wrapper.id = "job-day-modal";
  wrapper.className = "modal";
  wrapper.innerHTML = `
    <div class="modal__box">
      <div class="modal__head">
        <h3>Deadlines on ${escapeHtml(new Date(day).toLocaleDateString(undefined, { weekday: "long", year: "numeric", month: "long", day: "numeric" }))}</h3>
        <button class="modal__close">&times;</button>
      </div>
      <div class="grid">${cards || '<p class="hint">Nothing here.</p>'}</div>
    </div>`;
  document.body.appendChild(wrapper);
  wrapper.addEventListener("click", (e) => { if (e.target === wrapper) wrapper.remove(); });
  wrapper.querySelector(".modal__close").addEventListener("click", () => wrapper.remove());
  wrapper.querySelectorAll(".fav-btn").forEach((btn) => {
    btn.addEventListener("click", () => toggleFavorite(btn));
  });
}

// Manual deadline editor -- most job sources don't expose a deadline to
// scrape at all (see CLAUDE.md), so every job card's "✎"/"+ Deadline"
// button (renderCard's allowDeadlineEdit option) opens this small modal
// instead of leaving it permanently blank or requiring re-configuring the
// source's selectors. Reuses the same .modal/.modal__box shell as
// openJobDayModal, just with a plain date input instead of a card grid.
function openDeadlineEditor(articleId, currentDeadline) {
  document.getElementById("deadline-editor-modal")?.remove();

  const wrapper = document.createElement("div");
  wrapper.id = "deadline-editor-modal";
  wrapper.className = "modal";
  wrapper.innerHTML = `
    <div class="modal__box" style="max-width: 360px;">
      <div class="modal__head">
        <h3>Application deadline</h3>
        <button class="modal__close">&times;</button>
      </div>
      <div class="field">
        <label>Deadline date</label>
        <input type="date" id="deadline-editor-input" value="${escapeHtml(currentDeadline)}">
      </div>
      <div style="margin-top: 14px; display: flex; gap: 8px;">
        <button id="deadline-editor-save" class="primary">Save</button>
        <button id="deadline-editor-clear" class="danger">Clear</button>
        <button id="deadline-editor-cancel">Cancel</button>
      </div>
      <div id="deadline-editor-error" class="error-text"></div>
    </div>`;
  document.body.appendChild(wrapper);

  const close = () => wrapper.remove();
  wrapper.addEventListener("click", (e) => { if (e.target === wrapper) close(); });
  wrapper.querySelector(".modal__close").addEventListener("click", close);
  wrapper.querySelector("#deadline-editor-cancel").addEventListener("click", close);

  const save = async (deadline) => {
    const errorEl = wrapper.querySelector("#deadline-editor-error");
    try {
      await api(`/api/articles/${articleId}/deadline`, { method: "PUT", body: JSON.stringify({ deadline }) });
      close();
      // The day-detail modal (if open) rendered its cards once from
      // already-fetched data and won't pick up the change on its own --
      // close it rather than leave it showing a stale deadline.
      document.getElementById("job-day-modal")?.remove();
      await loadJobs();
    } catch (e) {
      errorEl.textContent = e.message;
    }
  };

  wrapper.querySelector("#deadline-editor-save").addEventListener("click", () => {
    save(wrapper.querySelector("#deadline-editor-input").value || null);
  });
  wrapper.querySelector("#deadline-editor-clear").addEventListener("click", () => save(null));
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest(".deadline-edit-btn");
  if (btn) openDeadlineEditor(Number(btn.dataset.id), btn.dataset.deadline || "");
});

// Same shell as openDeadlineEditor, a plain text field instead of a date
// picker -- remuneration is free text (a range, a currency, "brut"/"net",
// ...), not a single parseable value at entry time (see PUT .../remuneration).
// Splits an already-stored remuneration string back into "amount" +
// "period" for re-editing -- best-effort, matching the same monthly
// markers the server's own normalization (_MONTHLY_RE in web.py) looks
// for, so a value that round-tripped through this editor before (which
// always appends " / year" or " / month", see save() below) reopens with
// the right period pre-selected instead of always defaulting back to
// "year". A scraped value with its own period wording (e.g. "3 000 €/mois"
// from extract_remuneration()) is also detected as monthly here, even
// though its text isn't touched -- only the *newly saved* text gets the
// explicit suffix appended.
function splitRemuneration(text) {
  const isMonthly = /mensuel|mois|month/i.test(text);
  const amount = text.replace(/\s*\/\s*(year|month)\s*$/i, "").trim();
  return { amount, period: isMonthly ? "month" : "year" };
}

function openRemunerationEditor(articleId, current) {
  document.getElementById("remuneration-editor-modal")?.remove();
  const { amount, period } = splitRemuneration(current);

  const wrapper = document.createElement("div");
  wrapper.id = "remuneration-editor-modal";
  wrapper.className = "modal";
  wrapper.innerHTML = `
    <div class="modal__box" style="max-width: 380px;">
      <div class="modal__head">
        <h3>Pay / remuneration</h3>
        <button class="modal__close">&times;</button>
      </div>
      <div class="field">
        <label>Amount (number or range) -- e.g. "35 000 - 40 000 €"</label>
        <input type="text" id="remuneration-editor-input" value="${escapeHtml(amount)}" style="width:100%">
      </div>
      <div class="field">
        <label>Period</label>
        <select id="remuneration-editor-period" style="width:100%">
          <option value="year"${period === "year" ? " selected" : ""}>Per year (annual)</option>
          <option value="month"${period === "month" ? " selected" : ""}>Per month</option>
        </select>
      </div>
      <p class="hint">
        Pay stats always compares figures on an annual basis -- a per-month
        amount is automatically multiplied by 12 for averages/charts, it
        isn't stored or shown that way here.
      </p>
      <div style="margin-top: 14px; display: flex; gap: 8px;">
        <button id="remuneration-editor-save" class="primary">Save</button>
        <button id="remuneration-editor-clear" class="danger">Clear</button>
        <button id="remuneration-editor-cancel">Cancel</button>
      </div>
      <div id="remuneration-editor-error" class="error-text"></div>
    </div>`;
  document.body.appendChild(wrapper);

  const close = () => wrapper.remove();
  wrapper.addEventListener("click", (e) => { if (e.target === wrapper) close(); });
  wrapper.querySelector(".modal__close").addEventListener("click", close);
  wrapper.querySelector("#remuneration-editor-cancel").addEventListener("click", close);

  const save = async (remuneration) => {
    const errorEl = wrapper.querySelector("#remuneration-editor-error");
    try {
      await api(`/api/articles/${articleId}/remuneration`, { method: "PUT", body: JSON.stringify({ remuneration }) });
      close();
      document.getElementById("job-day-modal")?.remove();
      await loadJobs();
    } catch (e) {
      errorEl.textContent = e.message;
    }
  };

  wrapper.querySelector("#remuneration-editor-save").addEventListener("click", () => {
    const amountVal = wrapper.querySelector("#remuneration-editor-input").value.trim();
    if (!amountVal) {
      save(null);
      return;
    }
    const periodVal = wrapper.querySelector("#remuneration-editor-period").value;
    save(`${amountVal} / ${periodVal}`);
  });
  wrapper.querySelector("#remuneration-editor-clear").addEventListener("click", () => save(null));
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest(".remuneration-edit-btn");
  if (btn) openRemunerationEditor(Number(btn.dataset.id), btn.dataset.remuneration || "");
});

// Manual set/correct/clear for a job offer's location -- same shape as
// openRemunerationEditor above, just a single free-text field (no
// period-style second dimension to a location the way pay has
// annual/monthly). sources.location_selector (see scraper.scrape_css)
// covers this automatically for the few CSS sources that expose one;
// this is the reliable path for everything else.
function openCityEditor(articleId, current) {
  document.getElementById("city-editor-modal")?.remove();

  // Suggests cities already used elsewhere in Jobs (a plain HTML
  // <datalist>, same "suggestions, not an enum" pattern this app's
  // existing-groups datalists already use) -- built from state.jobs
  // (already fetched for the list view) rather than a new API call, same
  // as every other client-side-only suggestion list in this file.
  const existingCities = [...new Set(state.jobs.map((a) => a.city).filter(Boolean))].sort();
  const datalistHtml = `<datalist id="existing-cities">${existingCities.map((c) => `<option value="${escapeHtml(c)}">`).join("")}</datalist>`;

  const wrapper = document.createElement("div");
  wrapper.id = "city-editor-modal";
  wrapper.className = "modal";
  wrapper.innerHTML = `
    <div class="modal__box" style="max-width: 360px;">
      <div class="modal__head">
        <h3>Location</h3>
        <button class="modal__close">&times;</button>
      </div>
      <div class="field">
        <label>City -- e.g. "Nantes - 44"</label>
        <input type="text" id="city-editor-input" value="${escapeHtml(current)}" list="existing-cities" style="width:100%">
        ${datalistHtml}
      </div>
      <div style="margin-top: 14px; display: flex; gap: 8px;">
        <button id="city-editor-save" class="primary">Save</button>
        <button id="city-editor-clear" class="danger">Clear</button>
        <button id="city-editor-cancel">Cancel</button>
      </div>
      <div id="city-editor-error" class="error-text"></div>
    </div>`;
  document.body.appendChild(wrapper);

  const close = () => wrapper.remove();
  wrapper.addEventListener("click", (e) => { if (e.target === wrapper) close(); });
  wrapper.querySelector(".modal__close").addEventListener("click", close);
  wrapper.querySelector("#city-editor-cancel").addEventListener("click", close);

  const save = async (city) => {
    const errorEl = wrapper.querySelector("#city-editor-error");
    try {
      await api(`/api/articles/${articleId}/city`, { method: "PUT", body: JSON.stringify({ city }) });
      close();
      document.getElementById("job-day-modal")?.remove();
      await loadJobs();
    } catch (e) {
      errorEl.textContent = e.message;
    }
  };

  wrapper.querySelector("#city-editor-save").addEventListener("click", () => {
    save(wrapper.querySelector("#city-editor-input").value.trim() || null);
  });
  wrapper.querySelector("#city-editor-clear").addEventListener("click", () => save(null));
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest(".city-edit-btn");
  if (btn) openCityEditor(Number(btn.dataset.id), btn.dataset.city || "");
});

// Manual URL correction -- most useful for the "Manually added" Jobs
// source (no real link existed at import time, just a Google-search
// fallback) but reachable from any Jobs card. No "Clear" button here,
// unlike remuneration/city/deadline -- articles.link is NOT NULL UNIQUE,
// there's no concept of an empty link, only a replaced one.
function openLinkEditor(articleId, current) {
  document.getElementById("link-editor-modal")?.remove();

  const wrapper = document.createElement("div");
  wrapper.id = "link-editor-modal";
  wrapper.className = "modal";
  wrapper.innerHTML = `
    <div class="modal__box" style="max-width: 480px;">
      <div class="modal__head">
        <h3>Article URL</h3>
        <button class="modal__close">&times;</button>
      </div>
      <div class="field">
        <label>URL</label>
        <input type="text" id="link-editor-input" value="${escapeHtml(current)}" style="width:100%">
      </div>
      <div style="margin-top: 14px; display: flex; gap: 8px;">
        <button id="link-editor-save" class="primary">Save</button>
        <button id="link-editor-cancel">Cancel</button>
      </div>
      <div id="link-editor-error" class="error-text"></div>
    </div>`;
  document.body.appendChild(wrapper);

  const close = () => wrapper.remove();
  wrapper.addEventListener("click", (e) => { if (e.target === wrapper) close(); });
  wrapper.querySelector(".modal__close").addEventListener("click", close);
  wrapper.querySelector("#link-editor-cancel").addEventListener("click", close);

  wrapper.querySelector("#link-editor-save").addEventListener("click", async () => {
    const errorEl = wrapper.querySelector("#link-editor-error");
    const link = wrapper.querySelector("#link-editor-input").value.trim();
    if (!link) {
      errorEl.textContent = "URL can't be empty.";
      return;
    }
    try {
      await api(`/api/articles/${articleId}/link`, { method: "PUT", body: JSON.stringify({ link }) });
      close();
      document.getElementById("job-day-modal")?.remove();
      await loadJobs();
    } catch (e) {
      errorEl.textContent = e.message;
    }
  });
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest(".link-edit-btn");
  if (btn) openLinkEditor(Number(btn.dataset.id), btn.dataset.link || "");
});

// Topic-tag edits on a Jobs card. Delegated, since cards are re-rendered on
// every renderJobDays() call. PUT replaces the whole list, so each edit
// sends the complete resulting set rather than a delta.
// Opening text of a saved page snapshot, used as a Jobs card's description.
// Saved pages begin with site chrome (navigation, "Rechercher", cookie notices),
// so when the offer's own title shows up near the top the text starts there.
function savedTextDescription(article, max = 320) {
  let text = article.saved_text.replace(/\s+/g, " ").trim();
  const at = article.title ? text.indexOf(article.title) : -1;
  if (at > 0 && at < 1000) text = text.slice(at);
  if (text.length <= max) return text;
  return text.slice(0, max).replace(/\s+\S*$/, "") + "…";
}

function currentCardTags(articleId) {
  const a = state.jobs.find((x) => x.id === Number(articleId));
  return a ? [...(a.job_tag_list || [])] : [];
}
async function setJobCardTags(articleId, tags) {
  await api(`/api/articles/${articleId}/job-tags`, { method: "PUT", body: JSON.stringify({ tags }) });
  await loadJobs();
}
document.addEventListener("change", async (e) => {
  const add = e.target.closest(".job-tag-add");
  if (!add || !add.value) return;
  await setJobCardTags(add.dataset.id, [...currentCardTags(add.dataset.id), add.value]);
});
function currentCardTopics(articleId) {
  const a = state.jobs.find((x) => x.id === Number(articleId));
  return a ? [...(a.topic_list || [])] : [];
}
async function setJobCardTopics(articleId, topics) {
  await api(`/api/articles/${articleId}/topics`, { method: "PUT", body: JSON.stringify({ topics }) });
  await loadJobs();
}
// Enter adds a topic (typed freely or picked from the suggestions list).
document.addEventListener("keydown", async (e) => {
  const input = e.target.closest(".topic-add");
  if (!input || e.key !== "Enter") return;
  e.preventDefault();
  const value = input.value.trim();
  if (!value) return;
  await setJobCardTopics(input.dataset.id, [...currentCardTopics(input.dataset.id), value]);
});
document.addEventListener("click", async (e) => {
  const rm = e.target.closest(".topic-remove");
  if (!rm) return;
  e.stopPropagation();
  await setJobCardTopics(rm.dataset.id, currentCardTopics(rm.dataset.id).filter((t) => t !== rm.dataset.topic));
});
document.addEventListener("click", async (e) => {
  const rm = e.target.closest(".job-tag-remove");
  if (!rm) return;
  e.stopPropagation();
  await setJobCardTags(rm.dataset.id, currentCardTags(rm.dataset.id).filter((t) => t !== rm.dataset.tag));
});

document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".archive-btn");
  if (!btn) return;
  if (!confirm("Archive this job offer? It will be hidden from the Jobs list (reversible from the \"📁 Show archived\" toggle). Any saved page text will also be deleted.")) return;
  await api(`/api/articles/${btn.dataset.id}/archive`, { method: "PUT" });
  document.getElementById("job-day-modal")?.remove();
  await loadJobs();
});

// No confirm() here, same reasoning as Unhide -- a one-click reversible
// toggle, not a decision that benefits from a speed bump.
document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".unarchive-btn");
  if (!btn) return;
  await api(`/api/articles/${btn.dataset.id}/archive`, { method: "DELETE" });
  document.getElementById("job-day-modal")?.remove();
  await loadJobs();
});

// Hide/Unhide -- no confirm() dialog, unlike Archive: nothing here is
// destructive (pay/saved text are never touched) and it's a one-click
// reversible toggle, not a "decided, done with it" action.
document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".hide-btn");
  if (!btn) return;
  await api(`/api/articles/${btn.dataset.id}/hide`, { method: "PUT" });
  document.getElementById("job-day-modal")?.remove();
  await loadJobs();
});
document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".unhide-btn");
  if (!btn) return;
  await api(`/api/articles/${btn.dataset.id}/hide`, { method: "DELETE" });
  document.getElementById("job-day-modal")?.remove();
  await loadJobs();
});

// Paste-HTML modal for save_article_text -- a server-side fetch of the
// offer's own URL was tried first and dropped: job/thesis listing pages
// are disproportionately likely to sit behind Cloudflare/DataDome or a
// login wall (same class of block this app has already hit for several
// whole *sources* -- see "don't build a bypass" elsewhere in this file),
// so a plain fetch silently failed far more often than this app's other
// server-side fetches do. Same fix shape as PSNProfiles' import in the
// sibling achievement-vault project: the user pastes a page they already
// have open (View Page Source, or Ctrl+A/copy the rendered page) instead
// of the server trying to fetch it itself.
function openSaveTextModal(articleId, existingTitle) {
  document.getElementById("save-text-modal")?.remove();

  const wrapper = document.createElement("div");
  wrapper.id = "save-text-modal";
  wrapper.className = "modal";
  wrapper.innerHTML = `
    <div class="modal__box" style="max-width: 640px;">
      <div class="modal__head">
        <h3>${existingTitle ? "Re-save" : "Save"} page text</h3>
        <button class="modal__close">&times;</button>
      </div>
      <p class="hint">
        Open the offer's own link, then either "View Page Source" (Ctrl+U /
        Cmd+Option+U) and copy everything, or just select-all/copy the
        rendered page -- paste it below. This is extracted to plain text
        and stored locally, so it survives even if the listing is later
        taken down or edited.
      </p>
      <textarea id="save-text-modal-input" rows="10" style="width:100%; font-family:monospace; font-size:12px;" placeholder="Paste page source or copied page text here..."></textarea>
      <div style="margin-top: 14px; display: flex; gap: 8px;">
        <button id="save-text-modal-save" class="primary">Save</button>
        <button id="save-text-modal-cancel">Cancel</button>
      </div>
      <div id="save-text-modal-error" class="error-text"></div>
    </div>`;
  document.body.appendChild(wrapper);

  const close = () => wrapper.remove();
  wrapper.addEventListener("click", (e) => { if (e.target === wrapper) close(); });
  wrapper.querySelector(".modal__close").addEventListener("click", close);
  wrapper.querySelector("#save-text-modal-cancel").addEventListener("click", close);

  wrapper.querySelector("#save-text-modal-save").addEventListener("click", async () => {
    const errorEl = wrapper.querySelector("#save-text-modal-error");
    const html = wrapper.querySelector("#save-text-modal-input").value;
    if (!html.trim()) {
      errorEl.textContent = "Paste something first.";
      return;
    }
    try {
      await api(`/api/articles/${articleId}/save-text`, { method: "POST", body: JSON.stringify({ html }) });
      close();
      document.getElementById("saved-text-modal")?.remove();
      await loadJobs();
    } catch (e) {
      errorEl.textContent = e.message;
    }
  });
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest(".save-text-btn");
  if (btn) openSaveTextModal(Number(btn.dataset.id), false);
});

function openSavedTextViewer(articleId) {
  const article = state.jobs.find((a) => a.id === articleId);
  if (!article) return;
  document.getElementById("saved-text-modal")?.remove();

  const wrapper = document.createElement("div");
  wrapper.id = "saved-text-modal";
  wrapper.className = "modal";
  wrapper.innerHTML = `
    <div class="modal__box article-viewer-box">
      <div class="modal__head">
        <h3>${escapeHtml(article.title)}</h3>
        <button class="modal__close">&times;</button>
      </div>
      <div class="article-viewer-actions">
        <button id="saved-text-repaste" data-id="${article.id}">✎ Re-paste</button>
        <button id="saved-text-delete" class="danger" data-id="${article.id}">🗑 Delete snapshot</button>
        <a href="${escapeHtml(article.link)}" target="_blank" rel="noopener noreferrer" class="hint">Open the live page instead ↗</a>
      </div>
      <pre class="saved-text-body">${escapeHtml(article.saved_text)}</pre>
    </div>`;
  document.body.appendChild(wrapper);
  const close = () => wrapper.remove();
  wrapper.addEventListener("click", (e) => { if (e.target === wrapper) close(); });
  wrapper.querySelector(".modal__close").addEventListener("click", close);
  wrapper.querySelector("#saved-text-repaste").addEventListener("click", () => {
    openSaveTextModal(article.id, true);
  });
  wrapper.querySelector("#saved-text-delete").addEventListener("click", async () => {
    if (!confirm("Delete this saved page text? This doesn't archive or remove the job offer itself.")) return;
    await api(`/api/articles/${article.id}/save-text`, { method: "DELETE" });
    close();
    await loadJobs();
  });
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest(".view-text-btn");
  if (btn) openSavedTextViewer(Number(btn.dataset.id));
});

function updateJobSelectedCount() {
  document.getElementById("job-selected-count").textContent = selectedJobIds.size;
}

document.getElementById("job-select-toggle-btn").addEventListener("click", () => {
  jobSelectMode = !jobSelectMode;
  document.getElementById("job-select-toggle-btn").textContent = jobSelectMode ? "✕ Cancel select" : "☑ Select";
  document.getElementById("job-archive-selected-btn").style.display = jobSelectMode ? "" : "none";
  document.getElementById("job-hide-selected-btn").style.display = jobSelectMode ? "" : "none";
  if (!jobSelectMode) selectedJobIds.clear();
  updateJobSelectedCount();
  renderJobDays();
});

// Delegated (checkboxes are re-created on every renderJobDays() call, same
// reasoning as .archive-btn/.deadline-edit-btn above using delegation
// instead of per-element binding).
document.addEventListener("change", (e) => {
  const box = e.target.closest(".job-select");
  if (!box) return;
  const id = Number(box.dataset.id);
  box.checked ? selectedJobIds.add(id) : selectedJobIds.delete(id);
  updateJobSelectedCount();
});

// Clicking anywhere in the card -- image included -- toggles selection
// (easier to hit than the small checkbox itself), which the checkbox
// above still handles when clicked directly -- excluded here so the two
// paths don't double-toggle each other back to the original state.
// data-select-id only exists on a .card while selectMode is on (see
// renderCard), so this is a no-op outside select mode. preventDefault
// stops the title <a> underneath from navigating away while you're
// trying to select, not browse.
document.addEventListener("click", (e) => {
  if (e.target.closest(".job-select")) return;
  const card = e.target.closest(".card[data-select-id]");
  if (!card) return;
  e.preventDefault();
  const id = Number(card.dataset.selectId);
  const box = card.querySelector(".job-select");
  box.checked = !box.checked;
  box.checked ? selectedJobIds.add(id) : selectedJobIds.delete(id);
  updateJobSelectedCount();
});

document.getElementById("job-archive-selected-btn").addEventListener("click", async () => {
  if (selectedJobIds.size === 0) return;
  if (!confirm(`Archive ${selectedJobIds.size} selected job offer(s)? They'll be hidden from the Jobs list.`)) return;
  await Promise.all(
    [...selectedJobIds].map((id) => api(`/api/articles/${id}/archive`, { method: "PUT" }))
  );
  jobSelectMode = false;
  document.getElementById("job-select-toggle-btn").textContent = "☑ Select";
  document.getElementById("job-archive-selected-btn").style.display = "none";
  document.getElementById("job-hide-selected-btn").style.display = "none";
  await loadJobs();
});

// No confirm() here, unlike Archive above -- hiding never touches pay or
// saved text and is a one-click reversible toggle (see the "Show hidden"
// toggle), not a "decided, done with it" action.
document.getElementById("job-hide-selected-btn").addEventListener("click", async () => {
  if (selectedJobIds.size === 0) return;
  await Promise.all(
    [...selectedJobIds].map((id) => api(`/api/articles/${id}/hide`, { method: "PUT" }))
  );
  jobSelectMode = false;
  document.getElementById("job-select-toggle-btn").textContent = "☑ Select";
  document.getElementById("job-archive-selected-btn").style.display = "none";
  document.getElementById("job-hide-selected-btn").style.display = "none";
  await loadJobs();
});

// Pay-stats popup -- the quick in-Jobs view. Uses the same full-width SVG
// chart + mean/variance line as the Data Analysis page (renderStatChart /
// renderStatLine, defined further down) so there is one chart system, not
// two. Fetches fresh each open: the numeric pay parsing lives server-side.
// Unlike the Data Analysis page it keeps archived offers (job_stats'
// default), since it's a retrospective check of everything ever recorded.
async function openJobStatsModal() {
  document.getElementById("job-stats-modal")?.remove();
  const stats = await api("/api/jobs/stats");
  const withPay = stats.filter((s) => s.avg !== null && s.histogram && s.histogram.length);
  const hues = sourceHues(stats.map((s) => s.tag));
  const fmt = (n) => n !== null && n !== undefined ? Math.round(n).toLocaleString() : "—";

  const charts = withPay.map((s) => {
    const bars = s.histogram.map((b) => ({ label: b.min.toLocaleString(), count: b.count }));
    return `
      <div class="chart-section">
        <h4>${escapeHtml(s.tag)}</h4>
        ${renderStatChart({ bars, hue: hues[s.tag], curve: buildGaussianCurve(s.histogram, s.avg, s.stdev), rotateLabels: s.histogram.length > 8 })}
        ${renderStatLine({ mean: s.avg, variance: s.variance, stdev: s.stdev }, s.with_remuneration, " /yr")}
      </div>`;
  }).join("");

  const rows = stats.map((s) => `
    <tr>
      <td>${escapeHtml(s.tag)}</td><td>${s.count}</td><td>${s.with_remuneration}</td>
      <td>${fmt(s.avg)}</td><td>${fmt(s.median)}</td><td>${fmt(s.min)}</td>
      <td>${fmt(s.max)}</td><td>${fmt(s.stdev)}</td><td>${fmt(s.variance)}</td>
    </tr>`).join("");

  const wrapper = document.createElement("div");
  wrapper.id = "job-stats-modal";
  wrapper.className = "modal";
  wrapper.innerHTML = `
    <div class="modal__box" style="max-width: 900px;">
      <div class="modal__head">
        <h3>Pay stats by tag</h3>
        <button class="modal__close">&times;</button>
      </div>
      <p class="hint">
        Includes archived offers -- a recorded pay figure stays real after the
        listing closes, so counts can run higher than the normal Jobs list.
        Best-effort: remuneration is scraped or entered by hand, normalized to
        an annual figure (monthly ×12). A multi-tagged offer counts under each
        of its tags. Variance / std dev need at least 2 values, else "—".
      </p>
      ${stats.length === 0
        ? `<p class="empty-state">No job offers yet.</p>`
        : withPay.length === 0
          ? `<p class="empty-state">No offers with a parseable pay value yet -- nothing to chart.</p>`
          : charts}
      ${stats.length > 0 ? `
        <table class="stats-table">
          <thead><tr>
            <th>Tag</th><th>Total</th><th>With pay</th><th>Mean</th><th>Median</th>
            <th>Min</th><th>Max</th><th>Std dev</th><th>Variance</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>` : ""}
    </div>`;
  document.body.appendChild(wrapper);
  const close = () => wrapper.remove();
  wrapper.addEventListener("click", (e) => { if (e.target === wrapper) close(); });
  wrapper.querySelector(".modal__close").addEventListener("click", close);
}

document.getElementById("job-stats-btn").addEventListener("click", openJobStatsModal);

// -------------------------------------------------------------------------
// Data Analysis (dedicated top-level tab, Jobs-side analytics: pay by tag,
// offer counts by tag, volume by source, offers over time). Separate from
// the "Pay stats" popup above -- that one stays as a quick in-Jobs check
// without leaving the tab; this page is meant to be browsed on its own, so
// everything renders inline instead of needing to be reopened per chart.
// GET /api/jobs/analysis backs the volume/time-series charts; pay-by-tag
// and offers-by-tag both reuse GET /api/jobs/stats (already grouped by
// tag for the pay breakdown, so offers-by-tag needs no separate query).
// -------------------------------------------------------------------------

// -------------------------------------------------------------------------
// Statistical helpers -- vanilla JS, no charting library (same "no new
// dependency" convention as the rest of this app). These are real
// statistics, not decoration: a proper Gaussian density fit for the pay
// histograms (fit to that same histogram's own mean/stdev, not a
// cosmetic smoothing), and a least-squares quadratic trend fit for the
// time series, computed from a closed-form 3x3 normal-equations solve
// (exactly 3 unknowns for a quadratic, so no general matrix library is
// needed -- Cramer's rule handles a fixed 3x3 system directly).
// -------------------------------------------------------------------------

function gaussianPdf(x, mean, stdev) {
  if (!stdev) return 0;
  const z = (x - mean) / stdev;
  return Math.exp(-0.5 * z * z) / (stdev * Math.sqrt(2 * Math.PI));
}

function det3(m) {
  return m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
    - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
    + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]);
}

// Least-squares fit of y = a*x^2 + b*x + c over {x,y} points, via the
// normal equations' 3x3 closed-form solve (Cramer's rule). Falls back to
// a flat line at the mean when there aren't enough points to fit a real
// quadratic, or when the system is singular (e.g. every x identical) --
// callers still get *something* consistent to draw rather than a crash.
function quadraticFit(points) {
  const n = points.length;
  if (n < 3) {
    const meanY = points.reduce((s, p) => s + p.y, 0) / (n || 1);
    return { a: 0, b: 0, c: meanY };
  }
  let sx = 0, sx2 = 0, sx3 = 0, sx4 = 0, sy = 0, sxy = 0, sx2y = 0;
  for (const { x, y } of points) {
    const x2 = x * x;
    sx += x; sx2 += x2; sx3 += x2 * x; sx4 += x2 * x2;
    sy += y; sxy += x * y; sx2y += x2 * y;
  }
  const A = [[sx4, sx3, sx2], [sx3, sx2, sx], [sx2, sx, n]];
  const B = [sx2y, sxy, sy];
  const D = det3(A);
  if (Math.abs(D) < 1e-9) {
    return { a: 0, b: 0, c: sy / n };
  }
  const withCol = (col, vals) => A.map((row, i) => row.map((v, j) => (j === col ? vals[i] : v)));
  return {
    a: det3(withCol(0, B)) / D,
    b: det3(withCol(1, B)) / D,
    c: det3(withCol(2, B)) / D,
  };
}

function meanVarClient(values) {
  const n = values.length;
  if (n === 0) return { mean: null, variance: null, stdev: null };
  const mean = values.reduce((s, v) => s + v, 0) / n;
  if (n < 2) return { mean, variance: null, stdev: null };
  const variance = values.reduce((s, v) => s + (v - mean) ** 2, 0) / (n - 1);
  return { mean, variance, stdev: Math.sqrt(variance) };
}

function renderStatLine({ mean, variance, stdev }, n, unit = "") {
  const fmt = (v) => v === null || v === undefined ? "—" : Math.round(v).toLocaleString() + unit;
  return `<p class="chart-stats">Mean: <strong>${fmt(mean)}</strong> &nbsp;·&nbsp; Variance: <strong>${fmt(variance)}</strong> &nbsp;·&nbsp; Std dev: <strong>${fmt(stdev)}</strong> &nbsp;·&nbsp; n=${n}</p>`;
}

// -------------------------------------------------------------------------
// Chart rendering -- a single full-width (responsive viewBox) SVG bar
// chart with gridlines, axis labels, and an optional overlaid curve
// (Gaussian fit for histograms, quadratic trend fit for time series).
// Logical canvas is fixed-size (CHART_W x CHART_H); the <svg> itself
// scales to its container's full width via width="100%", which is what
// makes every graph on this page "full length" rather than boxed into a
// small fixed-width tile the way an earlier version of this page was.
// -------------------------------------------------------------------------

const CHART_W = 1000, CHART_H = 320;
const CHART_MARGIN = { top: 16, right: 16, bottom: 72, left: 60 };
const PLOT_W = CHART_W - CHART_MARGIN.left - CHART_MARGIN.right;
const PLOT_H = CHART_H - CHART_MARGIN.top - CHART_MARGIN.bottom;

// x position (in logical SVG units) for fractional bar index `i` out of
// `nBars` total bars -- shared by both the bars themselves and any curve
// overlay, so a curve's x values land in the exact same coordinate space
// as the bars it's meant to trace.
function chartXAt(i, nBars) {
  return CHART_MARGIN.left + (PLOT_W / nBars) * i;
}

function renderStatChart({ bars, hue = 220, curve = null, rotateLabels = false, yFormat = (n) => Math.round(n).toLocaleString() }) {
  if (bars.length === 0) return `<p class="empty-state">Nothing to chart.</p>`;
  const barW = PLOT_W / bars.length;
  const maxCount = Math.max(...bars.map((b) => b.count), curve ? Math.max(...curve.map((p) => p.y)) : 0, 1);
  const yAt = (count) => CHART_MARGIN.top + PLOT_H - (count / maxCount) * PLOT_H;

  const barsHtml = bars.map((b, i) => {
    const x = chartXAt(i, bars.length);
    const h = (b.count / maxCount) * PLOT_H;
    return `<rect x="${(x + barW * 0.06).toFixed(1)}" y="${yAt(b.count).toFixed(1)}" width="${(barW * 0.88).toFixed(1)}" height="${h.toFixed(1)}" rx="1.5" fill="hsl(${hue}, 70%, 55%)"><title>${escapeHtml(b.label)}: ${b.count}</title></rect>`;
  }).join("");

  // Skips a tick whose *displayed* (rounded) value is identical to the
  // one drawn just before it -- a real bug on low-count charts (a sparse
  // histogram bin spread, e.g.): dividing into 4 fixed fractions of a
  // small maxCount (3, say) rounds to "0, 1, 2, 2, 3", showing "2" twice.
  // Fewer visible gridlines on a sparse chart is more honest than a
  // duplicate label.
  let lastYLabel = null;
  const yTicksHtml = [0, 0.25, 0.5, 0.75, 1].map((f) => {
    const count = maxCount * f;
    const label = yFormat(count);
    if (label === lastYLabel) return "";
    lastYLabel = label;
    const y = yAt(count);
    return `<line x1="${CHART_MARGIN.left}" y1="${y.toFixed(1)}" x2="${CHART_W - CHART_MARGIN.right}" y2="${y.toFixed(1)}" class="chart-grid"/>
      <text x="${CHART_MARGIN.left - 8}" y="${y.toFixed(1)}" class="chart-ytick" text-anchor="end" dominant-baseline="middle">${label}</text>`;
  }).join("");

  const tickEvery = Math.max(1, Math.ceil(bars.length / (rotateLabels ? 24 : 14)));
  const xLabelsHtml = bars.map((b, i) => {
    if (i % tickEvery !== 0) return "";
    const x = chartXAt(i, bars.length) + barW / 2;
    const y = CHART_H - CHART_MARGIN.bottom + 18;
    return rotateLabels
      ? `<text x="${x.toFixed(1)}" y="${y}" class="chart-xtick" text-anchor="end" transform="rotate(-40 ${x.toFixed(1)} ${y})">${escapeHtml(b.label)}</text>`
      : `<text x="${x.toFixed(1)}" y="${y}" class="chart-xtick" text-anchor="middle">${escapeHtml(b.label)}</text>`;
  }).join("");

  const curveHtml = curve && curve.length
    ? `<path d="${curve.map((p, i) => `${i === 0 ? "M" : "L"} ${p.x.toFixed(1)} ${yAt(p.y).toFixed(1)}`).join(" ")}" class="chart-curve"/>`
    : "";

  return `
    <svg viewBox="0 0 ${CHART_W} ${CHART_H}" width="100%" class="stat-chart" preserveAspectRatio="xMidYMid meet">
      ${yTicksHtml}
      <line x1="${CHART_MARGIN.left}" y1="${CHART_MARGIN.top}" x2="${CHART_MARGIN.left}" y2="${CHART_H - CHART_MARGIN.bottom}" class="chart-axis"/>
      <line x1="${CHART_MARGIN.left}" y1="${CHART_H - CHART_MARGIN.bottom}" x2="${CHART_W - CHART_MARGIN.right}" y2="${CHART_H - CHART_MARGIN.bottom}" class="chart-axis"/>
      ${barsHtml}
      ${curveHtml}
      ${xLabelsHtml}
    </svg>`;
}

// Samples the Gaussian density fit to a histogram's own mean/stdev across
// its full range, scaled to "expected count per bin" (pdf * bin width *
// n) so it sits on the same count axis as the bars -- the standard
// histogram-plus-fitted-density overlay, not a cosmetic smoothing curve.
function buildGaussianCurve(histogram, mean, stdev) {
  if (!stdev || histogram.length === 0) return null;
  const n = histogram.reduce((s, b) => s + b.count, 0);
  const binWidth = histogram[0].max - histogram[0].min;
  const lo = histogram[0].min, hi = histogram[histogram.length - 1].max;
  const samples = 100;
  const pts = [];
  for (let i = 0; i <= samples; i++) {
    const x = lo + (hi - lo) * (i / samples);
    const expected = gaussianPdf(x, mean, stdev) * binWidth * n;
    const fractionalBin = (x - lo) / binWidth;
    pts.push({ x: chartXAt(fractionalBin, histogram.length), y: expected });
  }
  return pts;
}

// Quadratic least-squares trend over a sequence of counts (one per bar,
// in order) -- a real curve fit, not a moving average, clamped to >=0
// since a fitted parabola can dip below zero where actual counts can't.
function buildTrendCurve(counts) {
  if (counts.length < 2) return null;
  const { a, b, c } = quadraticFit(counts.map((y, x) => ({ x, y })));
  const samples = 80;
  const pts = [];
  for (let i = 0; i <= samples; i++) {
    const xIdx = (counts.length - 1) * (i / samples);
    pts.push({ x: chartXAt(xIdx, counts.length), y: Math.max(0, a * xIdx * xIdx + b * xIdx + c) });
  }
  return pts;
}

// Manual bin/granularity controls -- requested directly ("allow to
// manually change bin for all graph"), covering every chart that has a
// real "bin" concept: the pay histograms' bin *width* (currency units,
// backs job_stats' bin_size) and the two time series' bucket
// *granularity* (week/month/quarter, backs jobs_analysis' granularity).
// Purely categorical charts (Offers by tag, Volume by source, Pay by
// city) have no bin concept at all -- one bar per real category can't be
// re-binned, so there's deliberately no control for those. Persisted to
// localStorage like this page's other preferences, so a chosen bin size
// survives a reload instead of silently resetting to 500/month.
const ANALYSIS_BIN_KEY = "newsbot-analysis-bin-size";
const ANALYSIS_GRANULARITY_KEY = "newsbot-analysis-granularity";

function loadAnalysisBinSize() {
  const stored = Number(localStorage.getItem(ANALYSIS_BIN_KEY));
  return stored > 0 ? stored : 500;
}
function loadAnalysisGranularity() {
  const stored = localStorage.getItem(ANALYSIS_GRANULARITY_KEY);
  return ["week", "month", "quarter"].includes(stored) ? stored : "month";
}

let analysisBinSize = loadAnalysisBinSize();
let analysisGranularity = loadAnalysisGranularity();

// Excludes "Untagged" and the same 5 low-signal tags Jobs' own tag filter
// hides by default (JOB_TAG_HIDE_DEFAULT) from this page's tag-grouped
// charts. "Untagged" was excluded first (it's typically this page's single
// largest bucket, visually dominating every chart). Reported directly as a
// regression ("you when back to displya porfessorship, internship etc"):
// Jobs' own chip filter is a per-browser, client-side preference, so new
// Professorship/Internship/etc offers scraped after the one-time bulk
// archive (see "job tags" above) kept reappearing here even though the
// Jobs tab itself still hides them by default -- this page had never
// excluded that set at all, only "Untagged". Hardcoded here rather than
// reading the user's live localStorage toggle state: that preference can
// legitimately differ per browser/device, while this page's own definition
// of "noise" should stay consistent regardless of which browser opens it.
function excludeLowSignalTags(items, tagKey) {
  return items.filter((i) => i[tagKey] !== "Untagged" && !JOB_TAG_HIDE_DEFAULT.includes(i[tagKey]));
}

async function loadAnalysis() {
  const status = document.getElementById("analysis-status");
  status.textContent = "Loading...";
  try {
    localStorage.setItem(ANALYSIS_BIN_KEY, String(analysisBinSize));
    localStorage.setItem(ANALYSIS_GRANULARITY_KEY, analysisGranularity);

    // include_archived=0 on both pay routes -- this page tracks the
    // active pipeline, not a retrospective pay archive the way the "📊 Pay
    // stats" popup (unparameterized, still defaults to including
    // archived) deliberately does. See job_stats'/job_pay_by_city's own
    // docstrings for why the two surfaces disagree on purpose.
    const [statsRaw, analysis, payByCity] = await Promise.all([
      api(`/api/jobs/stats?include_archived=0&bin_size=${analysisBinSize}`),
      api(`/api/jobs/analysis?granularity=${analysisGranularity}`),
      api("/api/jobs/pay-by-city?include_archived=0"),
    ]);
    status.textContent = "";

    const stats = excludeLowSignalTags(statsRaw, "tag");

    // --- Pay by tag: one full-width histogram + Gaussian fit per tag ---
    const withPay = stats.filter((s) => s.avg !== null && s.histogram && s.histogram.length);
    const tagHues = sourceHues(stats.map((s) => s.tag));
    document.getElementById("analysis-pay-charts").innerHTML = withPay.length === 0
      ? `<p class="empty-state">No offers with a parseable pay value yet -- nothing to chart.</p>`
      : withPay.map((s) => {
          const bars = s.histogram.map((b) => ({ label: b.min.toLocaleString(), count: b.count }));
          const curve = buildGaussianCurve(s.histogram, s.avg, s.stdev);
          const chart = renderStatChart({ bars, hue: tagHues[s.tag], curve, rotateLabels: s.histogram.length > 8 });
          return `
            <div class="chart-section">
              <h4>${escapeHtml(s.tag)}</h4>
              ${chart}
              ${renderStatLine({ mean: s.avg, variance: s.variance, stdev: s.stdev }, s.with_remuneration, " /yr")}
            </div>`;
        }).join("");

    // --- Pay by city: one bar per city (its own mean pay), no curve --
    // city is a nominal category exactly like tag/source elsewhere on
    // this page, so the same "no curve, categories aren't ordered"
    // reasoning applies. Only cities with at least one parseable pay
    // value are charted (a city with offers but no pay data has nothing
    // to put on a "mean pay" axis) -- unlike job_tag's "Untagged" bucket,
    // there's no "Unknown city" fallback to lump the rest into.
    const cityPayBars = payByCity.filter((c) => c.avg !== null).map((c) => ({ label: c.city, count: c.avg }));
    document.getElementById("analysis-pay-by-city").innerHTML = cityPayBars.length === 0
      ? `<p class="empty-state">No offers with both a city and a parseable pay value yet.</p>`
      : renderStatChart({ bars: cityPayBars, hue: 210, rotateLabels: true, yFormat: (n) => Math.round(n).toLocaleString() + " €" })
        + renderStatLine(meanVarClient(cityPayBars.map((b) => b.count)), cityPayBars.length, " €");

    // --- Offers by tag: plain counts, no curve (categories aren't ordered) ---
    const tagBars = stats.map((s) => ({ label: s.tag, count: s.count })).sort((a, b) => b.count - a.count);
    document.getElementById("analysis-tag-distribution").innerHTML = tagBars.length === 0
      ? `<p class="empty-state">No job offers yet.</p>`
      : renderStatChart({ bars: tagBars, hue: 255, rotateLabels: true })
        + renderStatLine(meanVarClient(tagBars.map((b) => b.count)), tagBars.length);

    // --- Volume by source: plain counts, no curve ---
    const sourceBars = analysis.by_source.map((s) => ({ label: s.source, count: s.count }));
    document.getElementById("analysis-source-volume").innerHTML = sourceBars.length === 0
      ? `<p class="empty-state">No job offers yet.</p>`
      : renderStatChart({ bars: sourceBars, hue: 160, rotateLabels: true })
        + renderStatLine(meanVarClient(sourceBars.map((b) => b.count)), sourceBars.length);

    // --- Offers over time: quadratic trend over pub_date months ---
    const timeBars = analysis.over_time.map((m) => ({ label: m.month, count: m.total }));
    document.getElementById("analysis-time-series").innerHTML = timeBars.length === 0
      ? `<p class="empty-state">No dated job offers yet.</p>`
      : renderStatChart({ bars: timeBars, hue: 30, curve: buildTrendCurve(timeBars.map((b) => b.count)) })
        + renderStatLine(meanVarClient(timeBars.map((b) => b.count)), timeBars.length);

    // --- New jobs over time: quadratic trend over first_seen_at months ---
    const newJobBars = analysis.new_jobs_over_time.map((m) => ({ label: m.month, count: m.count }));
    document.getElementById("analysis-new-jobs-time").innerHTML = newJobBars.length === 0
      ? `<p class="empty-state">No job offers yet.</p>`
      : renderStatChart({ bars: newJobBars, hue: 300, curve: buildTrendCurve(newJobBars.map((b) => b.count)) })
        + renderStatLine(meanVarClient(newJobBars.map((b) => b.count)), newJobBars.length);
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
}

document.getElementById("analysis-refresh-btn").addEventListener("click", loadAnalysis);

document.getElementById("analysis-bin-size").value = analysisBinSize;
document.getElementById("analysis-granularity").value = analysisGranularity;

document.getElementById("analysis-bin-size").addEventListener("change", (e) => {
  const v = Number(e.target.value);
  analysisBinSize = v > 0 ? v : 500;
  e.target.value = analysisBinSize;
  loadAnalysis();
});
document.getElementById("analysis-granularity").addEventListener("change", (e) => {
  analysisGranularity = e.target.value;
  loadAnalysis();
});

// Lightweight tag creation reachable straight from the Jobs tab, instead of
// only via Settings' full tag-management list (move/rename/delete) --
// same POST /api/job-tags Settings' own "Add tag" already uses, just a
// smaller modal for the one common action (adding a new tag) without
// leaving the Jobs tab. Newly created tags start last in sort_order (the
// server default, see JobTagIn), so they're checked last by classify_job()
// -- reordering still only happens from Settings' move-up/move-down.
function openNewJobTagModal() {
  document.getElementById("new-job-tag-modal")?.remove();

  const wrapper = document.createElement("div");
  wrapper.id = "new-job-tag-modal";
  wrapper.className = "modal";
  wrapper.innerHTML = `
    <div class="modal__box" style="max-width: 420px;">
      <div class="modal__head">
        <h3>New job tag</h3>
        <button class="modal__close">&times;</button>
      </div>
      <div class="field">
        <label>Tag name</label>
        <input type="text" id="new-job-tag-modal-name" style="width:100%">
      </div>
      <div class="field">
        <label>Keywords (comma-separated) -- matched against a listing's title</label>
        <input type="text" id="new-job-tag-modal-keywords" placeholder="e.g. postdoc, post-doc, postdoctoral" style="width:100%">
      </div>
      <div style="margin-top: 14px; display: flex; gap: 8px;">
        <button id="new-job-tag-modal-save" class="primary">Add tag</button>
        <button id="new-job-tag-modal-cancel">Cancel</button>
      </div>
      <div id="new-job-tag-modal-error" class="error-text"></div>
    </div>`;
  document.body.appendChild(wrapper);

  const close = () => wrapper.remove();
  wrapper.addEventListener("click", (e) => { if (e.target === wrapper) close(); });
  wrapper.querySelector(".modal__close").addEventListener("click", close);
  wrapper.querySelector("#new-job-tag-modal-cancel").addEventListener("click", close);

  wrapper.querySelector("#new-job-tag-modal-save").addEventListener("click", async () => {
    const errorEl = wrapper.querySelector("#new-job-tag-modal-error");
    const name = wrapper.querySelector("#new-job-tag-modal-name").value.trim();
    const keywords = wrapper.querySelector("#new-job-tag-modal-keywords").value
      .split(",").map((k) => k.trim()).filter(Boolean);
    if (!name) {
      errorEl.textContent = "Tag name is required.";
      return;
    }
    try {
      await api("/api/job-tags", { method: "POST", body: JSON.stringify({ name, keywords }) });
      close();
      // Refreshes state.jobTags (used by renderCard's job-tag-select
      // options) and re-renders so the new tag is immediately assignable,
      // without needing a full Settings-tab round-trip first.
      await loadJobTags();
      renderJobDays();
    } catch (e) {
      errorEl.textContent = e.message;
    }
  });
}

document.getElementById("job-new-tag-btn").addEventListener("click", openNewJobTagModal);

// Sponsored Indeed ("pagead") links carry no stable job id (see
// scraper.scrape_email's docstring) -- a same-titled import from one of
// those is staged server-side instead of silently created, so the user
// can tell "genuinely the same posting" from "coincidentally the same
// title" by hand. This button/count only shows up when there's actually
// something to review.
async function refreshPendingDuplicatesCount() {
  const pending = await api("/api/jobs/pending-duplicates");
  const btn = document.getElementById("job-review-duplicates-btn");
  document.getElementById("job-review-duplicates-count").textContent = pending.length;
  btn.style.display = pending.length ? "" : "none";
  return pending;
}

async function openPendingDuplicatesModal() {
  document.getElementById("pending-duplicates-modal")?.remove();
  const pending = await refreshPendingDuplicatesCount();

  const rows = pending.map((p) => `
    <div class="dup-pair" data-id="${p.id}">
      <div class="dup-pair__col">
        <div class="dup-pair__label">New import</div>
        <div class="dup-pair__title">${escapeHtml(p.title)}</div>
        <a href="${escapeHtml(p.link)}" target="_blank" rel="noopener noreferrer" class="hint">Open link ↗</a>
        <div class="hint">${p.pub_date ? escapeHtml(fullDate(p.pub_date)) : "No date"}</div>
      </div>
      <div class="dup-pair__col">
        <div class="dup-pair__label">Already in Jobs</div>
        <div class="dup-pair__title">${escapeHtml(p.matched_title || "(deleted)")}</div>
        ${p.matched_link ? `<a href="${escapeHtml(p.matched_link)}" target="_blank" rel="noopener noreferrer" class="hint">Open link ↗</a>` : ""}
        <div class="hint">${p.matched_pub_date ? escapeHtml(fullDate(p.matched_pub_date)) : ""}</div>
      </div>
      <div class="dup-pair__actions">
        <button class="dup-discard-btn" data-id="${p.id}" title="Same posting -- don't import this copy">🗑 Discard</button>
        <button class="dup-import-btn primary" data-id="${p.id}" title="Different posting -- import it too">+ Import anyway</button>
      </div>
    </div>`).join("");

  const wrapper = document.createElement("div");
  wrapper.id = "pending-duplicates-modal";
  wrapper.className = "modal";
  wrapper.innerHTML = `
    <div class="modal__box" style="max-width: 720px;">
      <div class="modal__head">
        <h3>Possible duplicate imports</h3>
        <button class="modal__close">&times;</button>
      </div>
      <p class="hint">
        These Indeed listings are sponsored ("pagead") links, which carry no
        stable job id to dedup by automatically -- unlike a regular Indeed
        link, the exact same posting gets a different tracking URL in every
        email. Each one below has the same title as an offer already in
        Jobs. Compare the links, then discard it if it's the same posting,
        or import it if it's genuinely a different one that just happens to
        share a title.
      </p>
      ${pending.length === 0 ? `<p class="empty-state">Nothing to review.</p>` : `<div class="dup-list">${rows}</div>`}
    </div>`;
  document.body.appendChild(wrapper);
  const close = () => wrapper.remove();
  wrapper.addEventListener("click", (e) => { if (e.target === wrapper) close(); });
  wrapper.querySelector(".modal__close").addEventListener("click", close);

  wrapper.querySelectorAll(".dup-discard-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api(`/api/jobs/pending-duplicates/${btn.dataset.id}`, { method: "DELETE" });
      btn.closest(".dup-pair").remove();
      await refreshPendingDuplicatesCount();
      if (!wrapper.querySelector(".dup-pair")) close();
    });
  });
  wrapper.querySelectorAll(".dup-import-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api(`/api/jobs/pending-duplicates/${btn.dataset.id}/import`, { method: "POST" });
      btn.closest(".dup-pair").remove();
      await refreshPendingDuplicatesCount();
      await loadJobs();
      if (!wrapper.querySelector(".dup-pair")) close();
    });
  });
}

document.getElementById("job-review-duplicates-btn").addEventListener("click", openPendingDuplicatesModal);

document.getElementById("job-cal-today")?.addEventListener("click", () => {
  jobCalMonth = new Date(new Date().getFullYear(), new Date().getMonth(), 1);
  renderJobCalendar();
});
document.getElementById("job-cal-prev")?.addEventListener("click", () => {
  jobCalMonth = new Date(jobCalMonth.getFullYear(), jobCalMonth.getMonth() - 1, 1);
  renderJobCalendar();
});
document.getElementById("job-cal-next")?.addEventListener("click", () => {
  jobCalMonth = new Date(jobCalMonth.getFullYear(), jobCalMonth.getMonth() + 1, 1);
  renderJobCalendar();
});

// -------------------------------------------------------------------------
// Favorites / Folders
// -------------------------------------------------------------------------

async function loadFolders() {
  state.folders = await api("/api/folders");
  const container = document.getElementById("folder-items");
  const allItem = `<div class="folder-item ${state.activeFolderId === null ? "active" : ""}" data-folder="">All favorites <span class="count"></span></div>`;
  const items = state.folders.map((f) => `
    <div class="folder-item ${state.activeFolderId === f.id ? "active" : ""}" data-folder="${f.id}">
      ${escapeHtml(f.name)} <span class="count">${f.article_count}</span>
    </div>`).join("");
  container.innerHTML = allItem + items;

  container.querySelectorAll(".folder-item").forEach((el) => {
    el.addEventListener("click", () => {
      state.activeFolderId = el.dataset.folder ? Number(el.dataset.folder) : null;
      loadFolders();
      loadFavoritesGrid();
    });
  });
}

async function loadFavoritesGrid() {
  const url = state.activeFolderId !== null
    ? `/api/articles?favorites_only=true&folder_id=${state.activeFolderId}`
    : `/api/articles?favorites_only=true`;
  const favorites = await api(url);
  favorites.forEach((a) => { a.is_favorite = true; });
  const grid = document.getElementById("favorites-grid");
  const empty = document.getElementById("favorites-empty");

  if (favorites.length === 0) {
    grid.innerHTML = "";
    empty.style.display = "block";
    return;
  }
  empty.style.display = "none";
  const hues = sourceHues(favorites.map((a) => a.source_name));
  grid.innerHTML = favorites.map((a) => `
    <div>
      ${renderCard(a, hues, { showFavoriteToggle: false })}
      <select class="folder-picker" data-id="${a.id}">
        <option value="">No folder</option>
        ${state.folders.map((f) => `<option value="${f.id}" ${f.id === a.folder_id ? "selected" : ""}>${escapeHtml(f.name)}</option>`).join("")}
      </select>
      <button class="unfav-btn" data-id="${a.id}">Remove from favorites</button>
    </div>
  `).join("");

  grid.querySelectorAll(".folder-picker").forEach((sel) => {
    sel.addEventListener("change", async () => {
      const folderId = sel.value ? Number(sel.value) : null;
      await api(`/api/articles/${sel.dataset.id}/favorite`, { method: "PUT", body: JSON.stringify({ folder_id: folderId }) });
      loadFolders();
    });
  });
  grid.querySelectorAll(".unfav-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api(`/api/articles/${btn.dataset.id}/favorite`, { method: "DELETE" });
      loadFolders();
      loadFavoritesGrid();
    });
  });
}

async function loadFavorites() {
  await loadFolders();
  await loadFavoritesGrid();
}

document.getElementById("new-folder-btn").addEventListener("click", async () => {
  const name = prompt("Folder name?");
  if (!name) return;
  try {
    await api("/api/folders", { method: "POST", body: JSON.stringify({ name }) });
    loadFolders();
  } catch (e) {
    alert(e.message);
  }
});

// -------------------------------------------------------------------------
// Sources
// -------------------------------------------------------------------------

const HEALTH_BADGE = {
  ok: { icon: "🟢", label: "Working" },
  empty: { icon: "🟡", label: "Fetched OK, but found 0 articles — selectors may need updating" },
  error: { icon: "🔴", label: "Failing" },
};

function renderHealthBadge(s) {
  if (!s.last_checked_at) {
    return `<span class="health-badge" title="Not checked yet">⚪ Not checked yet</span>`;
  }
  const info = HEALTH_BADGE[s.last_status] || HEALTH_BADGE.ok;
  const tooltip = s.last_error ? `${info.label}: ${s.last_error}` : info.label;
  return `<span class="health-badge" title="${escapeHtml(tooltip)}">${info.icon} ${escapeHtml(timeAgo(s.last_checked_at))}</span>`;
}

function renderLatestArticle(s) {
  if (!s.latest_title || !s.latest_link) {
    return `<span class="latest-article hint">No articles scraped yet.</span>`;
  }
  return `<span class="latest-article">Latest: <a href="${escapeHtml(s.latest_link)}" target="_blank" rel="noopener noreferrer">${escapeHtml(s.latest_title)}</a>${s.latest_pub_date ? ` · ${escapeHtml(timeAgo(s.latest_pub_date))}` : ""}</span>`;
}

function renderSourceRow(s) {
  return `
    <div class="source-row ${s.enabled ? "" : "disabled"}" data-status="${s.last_status || "unchecked"}" data-group="${escapeHtml(s.group_tag || "")}">
      <span class="name">${escapeHtml(s.name)}</span>
      <span class="meta">${s.kind}${s.user_agent ? " · custom UA" : ""}</span>
      <input type="text" class="group-input" data-id="${s.id}" list="existing-groups-inline"
             value="${escapeHtml(s.group_tag || "")}" placeholder="Group...">
      ${renderHealthBadge(s)}
      <button class="edit-btn" data-id="${s.id}">Edit</button>
      <button class="toggle-btn" data-id="${s.id}" data-enabled="${s.enabled}">${s.enabled ? "Disable" : "Enable"}</button>
      <button class="danger delete-btn" data-id="${s.id}">Delete</button>
      ${renderLatestArticle(s)}
    </div>`;
}

async function loadSources() {
  state.sources = await api("/api/sources");
  // Cached here (not fetched fresh in sourceFormHtml/editSourceFormHtml,
  // which build their HTML synchronously) so the "Forced job tag" select
  // in the Add/Edit forms has real options to offer without its own
  // round-trip -- same pattern state.sources already uses for the
  // existing-groups datalist.
  state.jobTags = await api("/api/job-tags");
  const websites = state.sources.filter((s) => s.category === "website");
  const newsletters = state.sources.filter((s) => s.category === "newsletter");
  const social = state.sources.filter((s) => s.category === "social");
  const jobs = state.sources.filter((s) => s.category === "job");

  document.getElementById("website-sources").innerHTML = websites.map(renderSourceRow).join("") || '<p class="hint">None yet.</p>';
  document.getElementById("newsletter-sources").innerHTML = newsletters.map(renderSourceRow).join("") || '<p class="hint">None yet.</p>';
  document.getElementById("social-sources").innerHTML = social.map(renderSourceRow).join("") || '<p class="hint">None yet.</p>';
  document.getElementById("job-sources").innerHTML = jobs.map(renderSourceRow).join("") || '<p class="hint">None yet.</p>';

  document.querySelectorAll(".edit-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const source = state.sources.find((s) => s.id === Number(btn.dataset.id));
      if (source) openEditSourceForm(source, btn.closest(".source-row"));
    });
  });
  document.querySelectorAll(".toggle-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const enabled = btn.dataset.enabled === "true";
      await api(`/api/sources/${btn.dataset.id}`, { method: "PUT", body: JSON.stringify({ enabled: !enabled }) });
      loadSources();
    });
  });
  document.querySelectorAll(".delete-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("Delete this source and all its articles?")) return;
      await api(`/api/sources/${btn.dataset.id}`, { method: "DELETE" });
      loadSources();
    });
  });

  // Inline group editing, right on the row -- no need to open the full
  // Edit panel just to (re)assign which group a source belongs to.
  const existingGroups = [...new Set(state.sources.map((s) => s.group_tag).filter(Boolean))].sort();
  document.getElementById("existing-groups-inline").innerHTML =
    existingGroups.map((g) => `<option value="${escapeHtml(g)}">`).join("");
  document.querySelectorAll(".group-input").forEach((input) => {
    input.addEventListener("change", async () => {
      await api(`/api/sources/${input.dataset.id}`, {
        method: "PUT",
        body: JSON.stringify({ group_tag: input.value.trim() || "" }),
      });
      loadSources();
    });
  });

  renderGroupFilterChips();
  applySourceFilters();
}

function renderGroupFilterChips() {
  const groups = [...new Set(state.sources.map((s) => s.group_tag).filter(Boolean))].sort();
  const container = document.getElementById("group-filter-chips");
  if (groups.length === 0) {
    container.innerHTML = "";
    return;
  }
  container.innerHTML = `<button class="chip chip-all active" data-group="__all__">All groups</button>` +
    groups.map((g) => `<button class="chip" data-group="${escapeHtml(g)}">${escapeHtml(g)}</button>`).join("");
  container.querySelectorAll(".chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      container.querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      applySourceFilters();
    });
  });
}

function applySourceFilters() {
  const activeHealth = document.querySelector("#health-filter-chips .chip.active");
  const status = activeHealth ? activeHealth.dataset.health : "__all__";
  const activeGroup = document.querySelector("#group-filter-chips .chip.active");
  const group = activeGroup ? activeGroup.dataset.group : "__all__";

  document.querySelectorAll(".source-row").forEach((row) => {
    const statusOk = status === "__all__" || row.dataset.status === status;
    const groupOk = group === "__all__" || row.dataset.group === group;
    row.style.display = statusOk && groupOk ? "" : "none";
  });
}

document.querySelectorAll("#health-filter-chips .chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    document.querySelectorAll("#health-filter-chips .chip").forEach((c) => c.classList.remove("active"));
    chip.classList.add("active");
    applySourceFilters();
  });
});

function sourceFormHtml() {
  return `
    <div class="card-form">
      <h3>Add a source</h3>
      <div class="field">
        <label>Name</label>
        <input type="text" id="new-source-name" placeholder="e.g. CERN">
      </div>
      <div class="field">
        <label>Category</label>
        <select id="new-source-category">
          <option value="website">Website</option>
          <option value="newsletter">Newsletter</option>
          <option value="social">Social Media</option>
          <option value="job">Job/thesis offer</option>
        </select>
      </div>
      <div class="field">
        <label>Group (optional)</label>
        <input type="text" id="new-source-group" list="existing-groups" placeholder="e.g. CNRS Labs, Gravitational Wave">
        <datalist id="existing-groups">
          ${[...new Set(state.sources.map((s) => s.group_tag).filter(Boolean))].sort()
            .map((g) => `<option value="${escapeHtml(g)}">`).join("")}
        </datalist>
        <p class="hint">Your own free-form label for organizing sources — separate from Category above. Used to group/filter sources and articles.</p>
      </div>
      <div id="social-hint" class="hint" style="display:none; margin-bottom: 10px;">
        Works well: Mastodon (paste the profile URL, e.g.
        <code>https://mastodon.social/@user</code>), YouTube (paste the
        channel's RSS URL directly if you have it, e.g.
        <code>...youtube.com/feeds/videos.xml?channel_id=...</code>), Reddit
        (paste the subreddit URL, e.g. <code>reddit.com/r/Physics</code>).
        Not supported: Twitter/X, Instagram, Bluesky, Threads — they don't
        expose a public feed, and this app won't try to bypass anti-bot
        measures to scrape them.
      </div>
      <div id="new-forced-job-tag-field" class="field" style="display:none;">
        <label>Always tag as (optional)</label>
        <select id="new-source-forced-job-tag">
          <option value="">Auto-classify from title keywords</option>
          ${state.jobTags.map((t) => `<option value="${escapeHtml(t.name)}">${escapeHtml(t.name)}</option>`).join("")}
        </select>
        <p class="hint">Skips keyword matching entirely for this source — every listing gets this tag, regardless of title wording. Use this when a whole page is reliably one type of offer (a lab's own "Theses" page, e.g.).</p>
      </div>
      <div class="field">
        <label>URL</label>
        <input type="text" id="new-source-url" placeholder="https://example.com/news" style="width: 320px">
      </div>
      <div class="field">
        <button id="detect-feed-btn">Auto-detect feed</button>
        <button id="use-as-feed-btn">This URL is already a feed</button>
        <button id="manual-selectors-btn">Configure manually</button>
      </div>
      <div id="detect-status" class="hint"></div>
      <div id="manual-selectors" style="display:none; margin-top: 12px;">
        <p class="hint">Fill in CSS selectors for the article listing page, or click "🎯 Pick" to open a live preview and click the element you want instead of hand-typing one.</p>
        ${selectorFieldHtml("add", "article", "Article container selector")}
        ${selectorFieldHtml("add", "title", "Title selector")}
        ${selectorFieldHtml("add", "link", "Link selector")}
        <div class="field"><label>Base URL (for relative links)</label><input type="text" id="sel-base" style="width:100%"></div>
        ${selectorFieldHtml("add", "date", "Date selector (optional)")}
        ${selectorFieldHtml("add", "date-year", "Year selector (optional -- only for pages that group listings under a separate year heading instead of repeating the year on each item, e.g. Subatech's actualites page)")}
        ${selectorFieldHtml("add", "deadline", "Application deadline selector (optional -- Jobs only, e.g. a \"closes on...\" date on the listing)")}
        ${selectorFieldHtml("add", "location", "Location/city selector (optional -- Jobs only)")}
        ${selectorFieldHtml("add", "image", "Image selector (optional)")}
      </div>
      <div class="field">
        <label>Default image / logo URL (optional -- Feed only)</label>
        <input type="text" id="new-source-logo-url" style="width:100%">
        <p class="hint">Shown on Feed cards whenever an individual article has no image of its own. Has no effect on Jobs, which never show a fallback picture. Can also be added later from the Sources list.</p>
      </div>
      <input type="hidden" id="detected-feed-url">
      <div style="margin-top: 14px;">
        <button id="save-source-btn" class="primary">Add source</button>
        <button id="cancel-source-btn">Cancel</button>
      </div>
      <div id="source-form-error" class="error-text"></div>
    </div>`;
}

function editSourceFormHtml(s) {
  return `
    <div class="card-form">
      <h3>Edit ${escapeHtml(s.name)}</h3>
      <div class="field">
        <label>Name</label>
        <input type="text" id="edit-name" value="${escapeHtml(s.name)}">
      </div>
      <div class="field">
        <label>Category</label>
        <select id="edit-category">
          <option value="website" ${s.category === "website" ? "selected" : ""}>Website</option>
          <option value="newsletter" ${s.category === "newsletter" ? "selected" : ""}>Newsletter</option>
          <option value="social" ${s.category === "social" ? "selected" : ""}>Social Media</option>
          <option value="job" ${s.category === "job" ? "selected" : ""}>Job/thesis offer</option>
        </select>
      </div>
      <div class="field">
        <label>Group (optional)</label>
        <input type="text" id="edit-group" list="existing-groups" value="${escapeHtml(s.group_tag || "")}">
        <datalist id="existing-groups">
          ${[...new Set(state.sources.map((x) => x.group_tag).filter(Boolean))].sort()
            .map((g) => `<option value="${escapeHtml(g)}">`).join("")}
        </datalist>
      </div>
      <div id="edit-forced-job-tag-field" class="field" style="${s.category === "job" ? "" : "display:none; "}">
        <label>Always tag as (optional)</label>
        <select id="edit-source-forced-job-tag">
          <option value="">Auto-classify from title keywords</option>
          ${state.jobTags.map((t) => `<option value="${escapeHtml(t.name)}" ${s.forced_job_tag === t.name ? "selected" : ""}>${escapeHtml(t.name)}</option>`).join("")}
        </select>
        <p class="hint">Skips keyword matching entirely — every listing from this source gets this tag. Use for a source that's reliably one type of offer (a lab's own "Theses" page, e.g.).</p>
      </div>
      <div class="field">
        <label>Kind</label>
        <select id="edit-kind">
          <option value="rss" ${s.kind === "rss" ? "selected" : ""}>RSS/Atom feed</option>
          <option value="css" ${s.kind === "css" ? "selected" : ""}>CSS selectors</option>
        </select>
      </div>
      <div class="field">
        <label>URL</label>
        <input type="text" id="edit-url" value="${escapeHtml(s.url)}" style="width:100%">
      </div>
      <div id="edit-css-fields" style="${s.kind === "css" ? "" : "display:none; "}margin-top: 12px;">
        <p class="hint">Only used when Kind is "CSS selectors". Click "🎯 Pick" to open a live preview and click the element you want instead of hand-typing a selector.</p>
        ${selectorFieldHtml("edit", "article", "Article container selector", s.article_selector)}
        ${selectorFieldHtml("edit", "title", "Title selector", s.title_selector)}
        ${selectorFieldHtml("edit", "link", "Link selector", s.link_selector)}
        <div class="field"><label>Base URL (for relative links)</label><input type="text" id="edit-sel-base" value="${escapeHtml(s.base_url || "")}" style="width:100%"></div>
        ${selectorFieldHtml("edit", "date", "Date selector (optional)", s.date_selector)}
        ${selectorFieldHtml("edit", "date-year", "Year selector (optional -- see \"Add a source\" for when this is needed)", s.date_year_selector)}
        ${selectorFieldHtml("edit", "deadline", "Application deadline selector (optional -- Jobs only)", s.deadline_selector)}
        ${selectorFieldHtml("edit", "location", "Location/city selector (optional -- Jobs only)", s.location_selector)}
        ${selectorFieldHtml("edit", "image", "Image selector (optional)", s.image_selector)}
      </div>
      <div class="field">
        <label>User-Agent override (optional)</label>
        <input type="text" id="edit-user-agent" value="${escapeHtml(s.user_agent || "")}" style="width:100%">
      </div>
      <div class="field">
        <label>Default image / logo URL (optional -- Feed only)</label>
        <input type="text" id="edit-logo-url" value="${escapeHtml(s.logo_url || "")}" style="width:100%">
        <p class="hint">Shown on Feed cards whenever an individual article has no image of its own. Has no effect on Jobs, which never show a fallback picture.</p>
      </div>
      <div style="margin-top: 14px;">
        <button id="edit-save-btn" class="primary">Save changes</button>
        <button id="edit-cancel-btn">Cancel</button>
      </div>
      <div id="edit-form-error" class="error-text"></div>
    </div>`;
}

function openEditSourceForm(source, rowEl) {
  // Only one inline edit form open at a time.
  document.querySelectorAll(".inline-edit-form").forEach((el) => el.remove());

  const wrapper = document.createElement("div");
  wrapper.className = "inline-edit-form";
  wrapper.innerHTML = editSourceFormHtml(source);
  rowEl.insertAdjacentElement("afterend", wrapper);
  wrapper.scrollIntoView({ behavior: "smooth", block: "nearest" });

  document.getElementById("edit-kind").addEventListener("change", (e) => {
    document.getElementById("edit-css-fields").style.display = e.target.value === "css" ? "block" : "none";
  });

  document.getElementById("edit-category").addEventListener("change", (e) => {
    document.getElementById("edit-forced-job-tag-field").style.display = e.target.value === "job" ? "block" : "none";
  });

  document.getElementById("edit-cancel-btn").addEventListener("click", () => {
    wrapper.remove();
  });

  document.getElementById("edit-save-btn").addEventListener("click", async () => {
    const errorEl = document.getElementById("edit-form-error");
    errorEl.textContent = "";

    const name = document.getElementById("edit-name").value.trim();
    const url = document.getElementById("edit-url").value.trim();
    if (!name || !url) {
      errorEl.textContent = "Name and URL are required.";
      return;
    }

    const body = {
      name,
      category: document.getElementById("edit-category").value,
      group_tag: document.getElementById("edit-group").value.trim() || null,
      kind: document.getElementById("edit-kind").value,
      url,
      article_selector: document.getElementById("edit-sel-article").value.trim() || null,
      title_selector: document.getElementById("edit-sel-title").value.trim() || null,
      link_selector: document.getElementById("edit-sel-link").value.trim() || null,
      base_url: document.getElementById("edit-sel-base").value.trim() || null,
      date_selector: document.getElementById("edit-sel-date").value.trim() || null,
      date_year_selector: document.getElementById("edit-sel-date-year").value.trim() || null,
      deadline_selector: document.getElementById("edit-sel-deadline").value.trim() || null,
      location_selector: document.getElementById("edit-sel-location").value.trim() || null,
      forced_job_tag: document.getElementById("edit-source-forced-job-tag").value || null,
      image_selector: document.getElementById("edit-sel-image").value.trim() || null,
      user_agent: document.getElementById("edit-user-agent").value.trim() || null,
      logo_url: document.getElementById("edit-logo-url").value.trim() || null,
    };

    try {
      await api(`/api/sources/${source.id}`, { method: "PUT", body: JSON.stringify(body) });
      wrapper.remove();
      loadSources();
    } catch (e) {
      errorEl.textContent = e.message;
    }
  });
}

document.getElementById("add-source-btn").addEventListener("click", () => {
  const form = document.getElementById("add-source-form");
  form.innerHTML = sourceFormHtml();
  form.style.display = "block";

  document.getElementById("new-source-category").addEventListener("change", (e) => {
    document.getElementById("social-hint").style.display = e.target.value === "social" ? "block" : "none";
    document.getElementById("new-forced-job-tag-field").style.display = e.target.value === "job" ? "block" : "none";
  });

  document.getElementById("detect-feed-btn").addEventListener("click", async () => {
    const url = document.getElementById("new-source-url").value.trim();
    if (!url) return;
    const status = document.getElementById("detect-status");
    status.textContent = "Looking for a feed...";
    try {
      const { feed_url } = await api("/api/sources/detect-feed", { method: "POST", body: JSON.stringify({ url }) });
      if (feed_url) {
        status.textContent = `Feed found: ${feed_url}`;
        document.getElementById("detected-feed-url").value = feed_url;
        document.getElementById("manual-selectors").style.display = "none";
      } else {
        status.textContent = "No feed found automatically -- fill in the selectors below.";
        document.getElementById("detected-feed-url").value = "";
        document.getElementById("manual-selectors").style.display = "block";
      }
    } catch (e) {
      status.textContent = `Error: ${e.message}`;
    }
  });

  document.getElementById("use-as-feed-btn").addEventListener("click", () => {
    const url = document.getElementById("new-source-url").value.trim();
    const status = document.getElementById("detect-status");
    if (!url) {
      status.textContent = "Enter a URL first.";
      return;
    }
    document.getElementById("detected-feed-url").value = url;
    document.getElementById("manual-selectors").style.display = "none";
    status.textContent = `Using as feed: ${url} (not verified — this skips auto-detection entirely).`;
  });

  document.getElementById("manual-selectors-btn").addEventListener("click", () => {
    document.getElementById("detected-feed-url").value = "";
    document.getElementById("manual-selectors").style.display = "block";
    document.getElementById("detect-status").textContent = "Configuring manually — fill in the selectors below.";
  });

  document.getElementById("cancel-source-btn").addEventListener("click", () => {
    form.style.display = "none";
    form.innerHTML = "";
  });

  document.getElementById("save-source-btn").addEventListener("click", async () => {
    const name = document.getElementById("new-source-name").value.trim();
    const category = document.getElementById("new-source-category").value;
    const url = document.getElementById("new-source-url").value.trim();
    const feedUrl = document.getElementById("detected-feed-url").value;
    const errorEl = document.getElementById("source-form-error");
    errorEl.textContent = "";

    if (!name || !url) {
      errorEl.textContent = "Name and URL are required.";
      return;
    }

    const groupTag = document.getElementById("new-source-group").value.trim() || null;
    const forcedJobTag = document.getElementById("new-source-forced-job-tag")?.value || null;
    const logoUrl = document.getElementById("new-source-logo-url").value.trim() || null;
    const body = feedUrl
      ? { name, category, kind: "rss", url: feedUrl, group_tag: groupTag, forced_job_tag: forcedJobTag, logo_url: logoUrl }
      : {
          name, category, kind: "css", url,
          article_selector: document.getElementById("sel-article").value.trim(),
          title_selector: document.getElementById("sel-title").value.trim(),
          link_selector: document.getElementById("sel-link").value.trim(),
          base_url: document.getElementById("sel-base").value.trim() || url,
          date_selector: document.getElementById("sel-date").value.trim() || null,
          date_year_selector: document.getElementById("sel-date-year").value.trim() || null,
          deadline_selector: document.getElementById("sel-deadline").value.trim() || null,
          location_selector: document.getElementById("sel-location").value.trim() || null,
          image_selector: document.getElementById("sel-image").value.trim() || null,
          group_tag: groupTag,
          forced_job_tag: forcedJobTag,
          logo_url: logoUrl,
        };

    try {
      await api("/api/sources", { method: "POST", body: JSON.stringify(body) });
      form.style.display = "none";
      form.innerHTML = "";
      loadSources();
    } catch (e) {
      errorEl.textContent = e.message;
    }
  });
});

// -------------------------------------------------------------------------
// Settings
// -------------------------------------------------------------------------

async function loadSettings() {
  const settings = await api("/api/settings");
  document.getElementById("setting-hour").value = settings.digest_hour;
  document.getElementById("setting-minute").value = settings.digest_minute;
  document.getElementById("setting-timezone").value = settings.digest_timezone;
  document.getElementById("setting-channel").value = settings.news_channel_id || "";

  const { subscribers, smtp_configured } = await api("/api/newsletter/subscribers");
  document.getElementById("smtp-status").textContent = smtp_configured
    ? "SMTP is configured -- subscribers will receive the daily digest by email."
    : "SMTP isn't configured in .env yet -- subscribers are saved, but no email will send until it is (see CLAUDE.md).";
  document.getElementById("subscribers-list").innerHTML = subscribers.length
    ? subscribers.map((email) => `<div class="source-row"><span class="name">${escapeHtml(email)}</span><button class="danger unsub-btn" data-email="${escapeHtml(email)}">Unsubscribe</button></div>`).join("")
    : '<p class="hint">No subscribers yet.</p>';

  document.querySelectorAll(".unsub-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api("/api/newsletter/unsubscribe", { method: "DELETE", body: JSON.stringify({ email: btn.dataset.email }) });
      loadSettings();
    });
  });

  await loadJobTags();
}

// -------------------------------------------------------------------------
// Job tags management (Settings tab) -- keyword -> tag mapping used by
// Jobs' auto-tagging (see scraper.classify_job). Each tag's keywords are
// edited as one comma-separated text field, matching the app's existing
// "plain text input, split on save" convention (same shape as e.g. the
// group_tag inputs elsewhere) rather than a separate add/remove-per-
// keyword widget.
// -------------------------------------------------------------------------

async function loadJobTags() {
  // Already ordered by sort_order/priority (GET /api/job-tags) -- the
  // order shown here *is* the order classify_job() checks them in, so
  // move-up/move-down directly controls classification priority (e.g.
  // "Post-Doc checked before PhD").
  const tags = await api("/api/job-tags");
  state.jobTags = tags;
  const list = document.getElementById("job-tags-list");
  list.innerHTML = tags.length
    ? tags.map((t, i) => `
        <div class="source-row" data-tag-id="${t.id}">
          <div class="job-tag-move">
            <button class="move-job-tag-btn" data-dir="up" ${i === 0 ? "disabled" : ""} title="Higher priority">&#9650;</button>
            <button class="move-job-tag-btn" data-dir="down" ${i === tags.length - 1 ? "disabled" : ""} title="Lower priority">&#9660;</button>
          </div>
          <input type="text" class="job-tag-name" value="${escapeHtml(t.name)}" style="width: 140px">
          <input type="text" class="job-tag-keywords" value="${escapeHtml(t.keywords.join(", "))}" style="flex: 1" placeholder="keyword1, keyword2, ...">
          <button class="save-job-tag-btn">Save</button>
          <button class="danger delete-job-tag-btn">Delete</button>
        </div>`).join("")
    : '<p class="hint">No tags yet.</p>';

  list.querySelectorAll(".move-job-tag-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const row = btn.closest(".source-row");
      await api(`/api/job-tags/${row.dataset.tagId}/move?direction=${btn.dataset.dir}`, { method: "POST" });
      loadJobTags();
    });
  });

  list.querySelectorAll(".save-job-tag-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const row = btn.closest(".source-row");
      const id = row.dataset.tagId;
      const name = row.querySelector(".job-tag-name").value.trim();
      const keywords = row.querySelector(".job-tag-keywords").value.split(",").map((k) => k.trim()).filter(Boolean);
      const status = document.getElementById("job-tags-status");
      try {
        await api(`/api/job-tags/${id}`, { method: "PUT", body: JSON.stringify({ name, keywords }) });
        status.textContent = "Saved. Click \"Reapply tags to existing listings\" to apply the change retroactively.";
        loadJobTags();
      } catch (e) {
        status.textContent = `Error: ${e.message}`;
      }
    });
  });

  list.querySelectorAll(".delete-job-tag-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const row = btn.closest(".source-row");
      await api(`/api/job-tags/${row.dataset.tagId}`, { method: "DELETE" });
      loadJobTags();
    });
  });
}

document.getElementById("add-job-tag-btn").addEventListener("click", async () => {
  const status = document.getElementById("job-tags-status");
  const nameEl = document.getElementById("new-job-tag-name");
  const keywordsEl = document.getElementById("new-job-tag-keywords");
  const name = nameEl.value.trim();
  const keywords = keywordsEl.value.split(",").map((k) => k.trim()).filter(Boolean);
  if (!name) {
    status.textContent = "Tag name is required.";
    return;
  }
  try {
    await api("/api/job-tags", { method: "POST", body: JSON.stringify({ name, keywords }) });
    nameEl.value = "";
    keywordsEl.value = "";
    status.textContent = "Tag added.";
    loadJobTags();
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
});

document.getElementById("retag-jobs-btn").addEventListener("click", async () => {
  const status = document.getElementById("job-tags-status");
  status.textContent = "Reapplying tags…";
  try {
    const result = await api("/api/job-tags/retag", { method: "POST" });
    status.textContent = `Done -- ${result.updated} job listing(s) re-checked.`;
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
});

document.getElementById("save-settings-btn").addEventListener("click", async () => {
  const status = document.getElementById("settings-status");
  try {
    await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({
        digest_hour: Number(document.getElementById("setting-hour").value),
        digest_minute: Number(document.getElementById("setting-minute").value),
        digest_timezone: document.getElementById("setting-timezone").value.trim(),
        news_channel_id: Number(document.getElementById("setting-channel").value) || 0,
      }),
    });
    status.textContent = "Saved.";
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
});

document.getElementById("subscribe-btn").addEventListener("click", async () => {
  const input = document.getElementById("subscribe-email");
  const email = input.value.trim();
  if (!email) return;
  try {
    await api("/api/newsletter/subscribe", { method: "POST", body: JSON.stringify({ email }) });
    input.value = "";
    loadSettings();
  } catch (e) {
    alert(e.message);
  }
});

// -------------------------------------------------------------------------
// Debug -- "is the daily job actually going to run, and did it last time",
// without SSHing in. Built after a real incident: a manually-run process
// (not under systemd) died silently overnight when its SSH session dropped,
// and there was no record anywhere of what happened. Reads GET /api/debug,
// which reports live off the running Discord bot / scheduler objects, not
// just DB state -- see that route's docstring in web.py for why.
// -------------------------------------------------------------------------

function formatDuration(seconds) {
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
  return `${Math.floor(seconds / 86400)}d ${Math.floor((seconds % 86400) / 3600)}h`;
}

function debugRow(label, value) {
  return `<div class="source-row"><span class="name">${escapeHtml(label)}</span><span class="meta">${value}</span></div>`;
}

async function loadDebug() {
  const status = document.getElementById("debug-status");
  status.textContent = "Loading…";
  let d;
  try {
    d = await api("/api/debug");
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
    return;
  }
  status.textContent = `Updated ${new Date().toLocaleTimeString()}`;

  document.getElementById("debug-process").innerHTML =
    debugRow("Server time (UTC)", escapeHtml(d.server_time_utc)) +
    debugRow("Started", `${escapeHtml(fullDate(d.process_started_at))} (up ${formatDuration(d.process_uptime_seconds)})`);

  const disc = d.discord;
  document.getElementById("debug-discord").innerHTML =
    debugRow("Token configured", disc.token_configured ? "✅ Yes" : "🔴 No -- DISCORD_TOKEN missing from .env") +
    debugRow("Gateway connected", disc.gateway_ready ? `✅ Yes, as ${escapeHtml(disc.user)}` : "🔴 No -- on_ready() never fired, nothing is scheduled") +
    (disc.gateway_ready ? debugRow("Latency", disc.latency_ms != null ? `${disc.latency_ms} ms` : "unknown") : "") +
    (disc.gateway_ready ? debugRow("Guilds", disc.guild_count) : "") +
    debugRow("News channel ID", disc.news_channel_id || "(not set)");

  const sch = d.scheduler;
  document.getElementById("debug-scheduler").innerHTML =
    debugRow("Configured time", `${String(sch.configured_hour).padStart(2, "0")}:${String(sch.configured_minute).padStart(2, "0")} ${escapeHtml(sch.configured_timezone)}`) +
    debugRow("Loop running", sch.loop_running ? "✅ Yes" : "🔴 No") +
    debugRow("Iterations so far", sch.loop_iterations_so_far) +
    debugRow("Next scheduled run", sch.next_scheduled_run ? escapeHtml(fullDate(sch.next_scheduled_run)) : "unknown") +
    debugRow("Last digest run date", sch.last_digest_run_date ? escapeHtml(sch.last_digest_run_date) : "never recorded") +
    '<p class="hint">"Configured time" reflects Settings right now -- if it was changed without a restart since, the loop above is still running on the older time.</p>';

  const art = d.articles;
  document.getElementById("debug-articles").innerHTML =
    debugRow("Total articles", art.total_count) +
    (art.latest
      ? debugRow("Most recently scraped", `${escapeHtml(art.latest.title)} (${escapeHtml(timeAgo(art.latest.first_seen_at))})`)
      : debugRow("Most recently scraped", "none yet"));

  document.getElementById("debug-sources").innerHTML = d.sources.length
    ? d.sources.map((s) => `
        <div class="source-row ${s.enabled ? "" : "disabled"}">
          <span class="name">${escapeHtml(s.name)}</span>
          <span class="meta">${escapeHtml(s.category)}${s.enabled ? "" : " · disabled"}</span>
          ${renderHealthBadge(s)}
        </div>`).join("")
    : '<p class="hint">No sources configured.</p>';
}

document.getElementById("debug-refresh-btn").addEventListener("click", loadDebug);

// -------------------------------------------------------------------------
// Feed card size (Small/Medium/Large, persisted)
// -------------------------------------------------------------------------

const FEED_SIZE_KEY = "newsbot-feed-size";

function applyFeedSize(size) {
  document.getElementById("feed-days").dataset.size = size;
  document.querySelectorAll("#size-toggle .view-toggle-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.size === size);
  });
  localStorage.setItem(FEED_SIZE_KEY, size);
}

document.querySelectorAll("#size-toggle .view-toggle-btn").forEach((btn) => {
  btn.addEventListener("click", () => applyFeedSize(btn.dataset.size));
});

// -------------------------------------------------------------------------
// Selector picker: point-and-click CSS-selector picking for the Add/Edit
// source forms' manual CSS-selector fields, backed by
// GET /api/sources/preview-html + static/picker-inject.js (see web.py and
// that file's own comments for the mechanics). One shared modal instance
// reused across every "🎯 Pick" button on the page -- only one form (Add
// or Edit) is ever open at a time in this app, so there's never a need for
// more than one picker either.
// -------------------------------------------------------------------------

const FIELD_LABELS = {
  article: "Article container", title: "Title", link: "Link",
  date: "Date", "date-year": "Year", deadline: "Application deadline", image: "Image",
};

let pickerIframe = null;
let pickerIframeUrl = null;
let pickerReady = false;
let pickerPendingMode = null; // queued picker-mode message if sent before "picker-ready"
let pickerWaitingField = null; // { formType, field } while a pick is in flight

function pickerFieldInput(formType, field) {
  const id = (formType === "edit" ? "edit-sel-" : "sel-") + field;
  return document.getElementById(id);
}

function pickerContainerSelector(formType) {
  return (pickerFieldInput(formType, "article")?.value || "").trim();
}

function sendPickerMode(mode, containerSelector) {
  const message = { type: "picker-mode", mode, containerSelector };
  if (!pickerReady) {
    pickerPendingMode = message;
    return;
  }
  pickerIframe.contentWindow.postMessage(message, "*");
}

function openPicker(formType, field) {
  const urlInput = document.getElementById(formType === "edit" ? "edit-url" : "new-source-url");
  const url = (urlInput?.value || "").trim();
  if (!url) {
    alert("Enter a URL first.");
    return;
  }

  pickerWaitingField = { formType, field };
  const status = document.getElementById("picker-status");

  if (pickerIframeUrl !== url) {
    // (Re)build the modal for a new/changed URL -- switching which field
    // you're picking for on the *same* URL reuses the already-loaded
    // iframe instead (see below) so it doesn't re-fetch on every click.
    document.getElementById("picker-modal")?.remove();
    pickerReady = false;
    pickerIframeUrl = url;

    const wrapper = document.createElement("div");
    wrapper.id = "picker-modal";
    wrapper.className = "modal picker-modal";
    wrapper.innerHTML = `
      <div class="modal__box">
        <div class="modal__head">
          <h3>Pick an element</h3>
          <button class="modal__close">&times;</button>
        </div>
        <div class="picker-status" id="picker-status">Loading preview…</div>
        <div class="picker-frame-wrap">
          <iframe id="picker-iframe" sandbox="allow-scripts" src="/api/sources/preview-html?url=${encodeURIComponent(url)}"></iframe>
        </div>
      </div>`;
    document.body.appendChild(wrapper);
    wrapper.addEventListener("click", (e) => { if (e.target === wrapper) closePicker(); });
    wrapper.querySelector(".modal__close").addEventListener("click", closePicker);
    pickerIframe = document.getElementById("picker-iframe");
  }

  const mode = field === "article" ? "container" : "child";
  const containerSelector = mode === "child" ? pickerContainerSelector(formType) : null;
  sendPickerMode(mode, containerSelector);

  const label = FIELD_LABELS[field];
  const currentStatus = document.getElementById("picker-status");
  if (currentStatus) {
    currentStatus.innerHTML = `Click the element to use for <strong>${escapeHtml(label)}</strong> on the preview below.`;
  }
}

function closePicker() {
  document.getElementById("picker-modal")?.remove();
  pickerIframe = null;
  pickerIframeUrl = null;
  pickerReady = false;
  pickerPendingMode = null;
  pickerWaitingField = null;
}

window.addEventListener("message", (e) => {
  if (!pickerIframe || e.source !== pickerIframe.contentWindow) return;
  const data = e.data || {};
  if (data.type === "picker-ready") {
    pickerReady = true;
    if (pickerPendingMode) {
      pickerIframe.contentWindow.postMessage(pickerPendingMode, "*");
      pickerPendingMode = null;
    }
  } else if (data.type === "picker-selected" && pickerWaitingField) {
    const { formType, field } = pickerWaitingField;
    const input = pickerFieldInput(formType, field);
    if (input) input.value = data.selector;
    pickerWaitingField = null;
    const status = document.getElementById("picker-status");
    if (status) {
      const label = FIELD_LABELS[field];
      const countMsg = data.matchCount > 1
        ? `matches ${data.matchCount} elements on the page`
        : data.matchCount === 1 ? "matches 1 element" : "didn't uniquely match anything -- check it by hand";
      status.innerHTML = `Set <strong>${escapeHtml(label)}</strong> to <code>${escapeHtml(data.selector || "(itself)")}</code> (${countMsg}). Click "🎯 Pick" on another field, or close this preview.`;
    }
  }
});

document.addEventListener("click", (e) => {
  const btn = e.target.closest(".pick-btn");
  if (btn) openPicker(btn.dataset.form, btn.dataset.field);
});

// -------------------------------------------------------------------------
// Init
// -------------------------------------------------------------------------

applyFeedSize(localStorage.getItem(FEED_SIZE_KEY) || "medium");
loadFeed();
