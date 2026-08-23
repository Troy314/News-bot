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

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s ?? "";
  return div.innerHTML;
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
    if (btn.dataset.view === "sources") loadSources();
    if (btn.dataset.view === "settings") loadSettings();
  });
});

// -------------------------------------------------------------------------
// Feed
// -------------------------------------------------------------------------

function renderCard(article, hues, { showFavoriteToggle = true, showContentTag = true, allowDeadlineEdit = false, allowArchive = false } = {}) {
  const hue = hues[article.source_name] ?? 0;
  const image = article.image_url
    ? `<div class="card-image"><img src="${escapeHtml(article.image_url)}" alt="" loading="lazy" onerror="this.parentElement.remove()"></div>`
    : "";
  const favBtn = showFavoriteToggle
    ? `<button class="fav-btn" data-id="${article.id}" title="Favorite">${article.is_favorite ? "★" : "☆"}</button>`
    : "";
  // content_tag is a physics-topic classification (neutrino, muon, ...) --
  // meaningless for a job/thesis-offer listing, so the Jobs tab suppresses
  // it via showContentTag:false rather than showing "General Physics" on
  // every card.
  const contentTag = article.content_tag && showContentTag
    ? `<span class="content-tag">${escapeHtml(article.content_tag)}</span>`
    : "";
  // job_tag (Fellowship/PhD/Internship/...) is only ever set on category
  // "job" articles (see scraper.classify_job) -- null/undefined everywhere
  // else, so this renders nothing extra on ordinary news cards.
  const jobTag = article.job_tag
    ? `<span class="job-tag">${escapeHtml(article.job_tag)}</span>`
    : "";
  const deadline = article.deadline
    ? `<span class="deadline-badge" data-passed="${new Date(article.deadline).getTime() < Date.now()}">Deadline: ${escapeHtml(fullDate(article.deadline))}</span>`
    : "";
  // Most job sources don't expose a deadline to scrape at all (see
  // CLAUDE.md's "Job deadlines") -- this lets the user set/correct/clear
  // one by hand instead of it just staying permanently blank. Only shown
  // where the caller opts in (Jobs tab), not on ordinary Feed cards.
  const deadlineEditBtn = allowDeadlineEdit
    ? `<button class="deadline-edit-btn" data-id="${article.id}" data-deadline="${escapeHtml(article.deadline ? isoDay(new Date(article.deadline)) : "")}" title="${article.deadline ? "Edit deadline" : "Set application deadline"}">${article.deadline ? "✎" : "+ Deadline"}</button>`
    : "";
  // Hides the offer from the Jobs tab (list + calendar) without deleting
  // it -- confirmed with a native confirm() before the API call actually
  // fires, same lightweight pattern the Sources tab's delete button
  // already uses in this app (not the instant-delete-with-undo toast flow
  // Collection-style apps use elsewhere, since there's no "undo" here --
  // just a reversible unarchive via the API if ever needed).
  const archiveBtn = allowArchive
    ? `<button class="archive-btn" data-id="${article.id}" title="Archive this offer">🗄 Archive</button>`
    : "";
  return `
    <div class="card" data-source="${escapeHtml(article.source_name)}" data-tag="${escapeHtml(article.content_tag || "")}" data-job-tag="${escapeHtml(article.job_tag || "Untagged")}">
      ${favBtn}
      ${image}
      <div class="card-body">
        <h3><a href="${escapeHtml(article.link)}" target="_blank" rel="noopener noreferrer">${escapeHtml(article.title)}</a></h3>
        <div class="card-meta">
          <span class="tag" style="--hue:${hue}">${escapeHtml(article.source_name)}</span>
          ${contentTag}
          ${jobTag}
          <span title="${escapeHtml(timeAgo(article.pub_date))}">${escapeHtml(fullDate(article.pub_date))}</span>
          ${deadline}
          ${deadlineEditBtn}
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
    const val = a[field] || emptyLabel;
    if (val) counts[val] = (counts[val] || 0) + 1;
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
function applyDayFilter(containerId, activeSourcesSet, activeTagsSet, noMatchElId, tagKey = "tag", tagMode = "include") {
  let anyVisible = false;
  document.querySelectorAll(`#${containerId} .day-group`).forEach((dayGroup) => {
    let dayGroupVisible = false;
    dayGroup.querySelectorAll(".card").forEach((card) => {
      const sourceOk = activeSourcesSet.size === 0 || activeSourcesSet.has(card.dataset.source);
      let tagOk = true;
      if (activeTagsSet) {
        tagOk = tagMode === "exclude"
          ? !activeTagsSet.has(card.dataset[tagKey])
          : (activeTagsSet.size === 0 || activeTagsSet.has(card.dataset[tagKey]));
      }
      const show = sourceOk && tagOk;
      card.classList.toggle("hidden", !show);
      if (show) dayGroupVisible = true;
    });
    dayGroup.style.display = dayGroupVisible ? "" : "none";
    if (dayGroupVisible) anyVisible = true;
  });
  document.getElementById(noMatchElId).style.display = anyVisible ? "none" : "block";
}

function applyFeedFilter() {
  applyDayFilter("feed-days", activeSources, activeTags, "feed-no-match");
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
    html += renderDayGroup(group, hues, isRecent);
  }
  daysEl.innerHTML = html;

  document.querySelectorAll("#feed-days .fav-btn").forEach((btn) => {
    btn.addEventListener("click", () => toggleFavorite(btn));
  });
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

function saveHiddenJobTags() {
  localStorage.setItem(JOB_TAG_HIDE_KEY, JSON.stringify([...activeJobTags]));
}

function applyJobFilter() {
  saveHiddenJobTags();
  applyDayFilter("job-days", activeJobSources, activeJobTags, "job-no-match", "jobTag", "exclude");
}

async function loadJobs() {
  const jobsRaw = await api("/api/articles?category=job");
  const favRows = await api("/api/articles?category=job&favorites_only=true");
  const favSet = new Set(favRows.map((a) => a.id));
  jobsRaw.forEach((a) => { a.is_favorite = favSet.has(a.id); });
  state.jobs = jobsRaw;

  const daysEl = document.getElementById("job-days");
  const empty = document.getElementById("job-empty");
  const noMatch = document.getElementById("job-no-match");
  const chipsEl = document.getElementById("job-chips");
  const tagChipsEl = document.getElementById("job-tag-chips");

  activeJobSources.clear();
  activeJobTags.clear();
  loadHiddenJobTags().forEach((t) => activeJobTags.add(t));
  noMatch.style.display = "none";

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
    field: "job_tag", activeSet: activeJobTags, onChange: applyJobFilter,
    allLabel: "All tags", emptyLabel: "Untagged",
  });

  const groups = groupByDay(jobsRaw);
  const openSince = Date.now() - OPEN_BY_DEFAULT_MS;
  let html = "";
  for (const [key, group] of groups) {
    // Most job-board pages have no reliable per-listing date at all (a
    // search-results page just lists whatever's currently open) -- the
    // undated "unknown" bucket defaults open so those aren't hidden behind
    // an extra click, unlike Feed where "old" undated news would be rare.
    const pubDate = group[0].pub_date;
    const isRecent = pubDate ? new Date(pubDate).getTime() >= openSince : true;
    html += renderDayGroup(group, jobHues, isRecent, { showContentTag: false, allowDeadlineEdit: true, allowArchive: true });
  }
  daysEl.innerHTML = html;

  document.querySelectorAll("#job-days .fav-btn").forEach((btn) => {
    btn.addEventListener("click", () => toggleFavorite(btn));
  });

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

  const withDeadline = state.jobs.filter((a) => a.deadline);
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

document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".archive-btn");
  if (!btn) return;
  if (!confirm("Archive this job offer? It will be hidden from the Jobs list (this can be undone later via the API, but there's no UI for that yet).")) return;
  await api(`/api/articles/${btn.dataset.id}/archive`, { method: "PUT" });
  document.getElementById("job-day-modal")?.remove();
  await loadJobs();
});

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
        ${selectorFieldHtml("add", "image", "Image selector (optional)")}
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
        ${selectorFieldHtml("edit", "image", "Image selector (optional)", s.image_selector)}
      </div>
      <div class="field">
        <label>User-Agent override (optional)</label>
        <input type="text" id="edit-user-agent" value="${escapeHtml(s.user_agent || "")}" style="width:100%">
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
      forced_job_tag: document.getElementById("edit-source-forced-job-tag").value || null,
      image_selector: document.getElementById("edit-sel-image").value.trim() || null,
      user_agent: document.getElementById("edit-user-agent").value.trim() || null,
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
    const body = feedUrl
      ? { name, category, kind: "rss", url: feedUrl, group_tag: groupTag, forced_job_tag: forcedJobTag }
      : {
          name, category, kind: "css", url,
          article_selector: document.getElementById("sel-article").value.trim(),
          title_selector: document.getElementById("sel-title").value.trim(),
          link_selector: document.getElementById("sel-link").value.trim(),
          base_url: document.getElementById("sel-base").value.trim() || url,
          date_selector: document.getElementById("sel-date").value.trim() || null,
          date_year_selector: document.getElementById("sel-date-year").value.trim() || null,
          deadline_selector: document.getElementById("sel-deadline").value.trim() || null,
          image_selector: document.getElementById("sel-image").value.trim() || null,
          group_tag: groupTag,
          forced_job_tag: forcedJobTag,
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
