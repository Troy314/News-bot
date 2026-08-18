// News Bot frontend -- vanilla JS, no build step. Talks to the FastAPI
// backend in web.py over fetch().

const state = {
  articles: [],
  sources: [],
  folders: [],
  activeFolderId: null,
  feedViewMode: "grid",
};

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s ?? "";
  return div.innerHTML;
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
    if (btn.dataset.view === "sources") loadSources();
    if (btn.dataset.view === "settings") loadSettings();
  });
});

// -------------------------------------------------------------------------
// Feed
// -------------------------------------------------------------------------

function renderCard(article, hues, { showFavoriteToggle = true } = {}) {
  const hue = hues[article.source_name] ?? 0;
  const image = article.image_url
    ? `<div class="card-image"><img src="${escapeHtml(article.image_url)}" alt="" loading="lazy" onerror="this.parentElement.remove()"></div>`
    : "";
  const favBtn = showFavoriteToggle
    ? `<button class="fav-btn" data-id="${article.id}" title="Favorite">${article.is_favorite ? "★" : "☆"}</button>`
    : "";
  const contentTag = article.content_tag
    ? `<span class="content-tag">${escapeHtml(article.content_tag)}</span>`
    : "";
  return `
    <div class="card" data-source="${escapeHtml(article.source_name)}" data-tag="${escapeHtml(article.content_tag || "")}">
      ${favBtn}
      ${image}
      <div class="card-body">
        <h3><a href="${escapeHtml(article.link)}" target="_blank" rel="noopener noreferrer">${escapeHtml(article.title)}</a></h3>
        <div class="card-meta">
          <span class="tag" style="--hue:${hue}">${escapeHtml(article.source_name)}</span>
          ${contentTag}
          <span title="${escapeHtml(timeAgo(article.pub_date))}">${escapeHtml(fullDate(article.pub_date))}</span>
        </div>
      </div>
    </div>`;
}

function renderChips(container, articles, onFilterChange) {
  const counts = {};
  articles.forEach((a) => { counts[a.source_name] = (counts[a.source_name] || 0) + 1; });
  const hues = sourceHues(Object.keys(counts));
  const names = Object.keys(counts).sort();

  container.innerHTML = `<button class="chip chip-all active" data-source="__all__">All</button>` +
    names.map((name) => `<button class="chip" data-hue="1" style="--hue:${hues[name]}" data-source="${escapeHtml(name)}">${escapeHtml(name)} <span style="opacity:.65">${counts[name]}</span></button>`).join("");

  const active = new Set();
  container.querySelectorAll(".chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const src = chip.dataset.source;
      if (src === "__all__") {
        active.clear();
        container.querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
      } else {
        chip.classList.toggle("active");
        active.has(src) ? active.delete(src) : active.add(src);
      }
      container.querySelector(".chip-all").classList.toggle("active", active.size === 0);
      onFilterChange(active);
    });
  });
  return hues;
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

function renderTimelineRow(article, hues) {
  const hue = hues[article.source_name] ?? 0;
  const thumb = article.image_url
    ? `<div class="timeline-thumb"><img src="${escapeHtml(article.image_url)}" alt="" loading="lazy" onerror="this.parentElement.remove()"></div>`
    : "";
  const contentTag = article.content_tag ? `<span class="content-tag">${escapeHtml(article.content_tag)}</span>` : "";
  return `
    <div class="timeline-row" data-source="${escapeHtml(article.source_name)}">
      <span class="timeline-dot" style="--hue:${hue}"></span>
      ${thumb}
      <div class="timeline-content">
        <h4><a href="${escapeHtml(article.link)}" target="_blank" rel="noopener noreferrer">${escapeHtml(article.title)}</a></h4>
        <div class="timeline-meta">
          <span class="tag" style="--hue:${hue}">${escapeHtml(article.source_name)}</span>
          ${contentTag}
          <span class="timeline-time" title="${escapeHtml(fullDate(article.pub_date))}">${escapeHtml(timeAgo(article.pub_date))}</span>
        </div>
      </div>
      <button class="timeline-fav-btn fav-btn" data-id="${article.id}" title="Favorite">${article.is_favorite ? "★" : "☆"}</button>
    </div>`;
}

function renderTimeline(articles, hues) {
  const groups = new Map();
  articles.forEach((a) => {
    const key = a.pub_date ? new Date(a.pub_date).toDateString() : "unknown";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(a);
  });

  let html = "";
  for (const [, group] of groups) {
    html += `<div class="timeline-day">
      <div class="timeline-day-label">${escapeHtml(dayLabel(group[0].pub_date))}</div>
      ${group.map((a) => renderTimelineRow(a, hues)).join("")}
    </div>`;
  }
  return html;
}

function applyFeedFilter(active) {
  document.querySelectorAll("#feed-grid .card, #feed-timeline .timeline-row").forEach((el) => {
    el.style.display = active.size === 0 || active.has(el.dataset.source) ? "" : "none";
  });
}

document.getElementById("view-grid-btn").addEventListener("click", () => setFeedView("grid"));
document.getElementById("view-timeline-btn").addEventListener("click", () => setFeedView("timeline"));

function setFeedView(mode) {
  state.feedViewMode = mode;
  document.getElementById("view-grid-btn").classList.toggle("active", mode === "grid");
  document.getElementById("view-timeline-btn").classList.toggle("active", mode === "timeline");
  document.getElementById("feed-grid").style.display = mode === "grid" ? "" : "none";
  document.getElementById("feed-timeline").style.display = mode === "timeline" ? "" : "none";
}

async function loadFeed() {
  const articlesRaw = await api("/api/articles");
  // folder_id can legitimately be null for a favorite with no folder, so
  // favorite status needs its own dedicated fetch, not a folder_id check.
  const favRows = await api("/api/articles?favorites_only=true");
  const favSet = new Set(favRows.map((a) => a.id));
  articlesRaw.forEach((a) => { a.is_favorite = favSet.has(a.id); });

  state.articles = articlesRaw;
  const grid = document.getElementById("feed-grid");
  const timeline = document.getElementById("feed-timeline");
  const empty = document.getElementById("feed-empty");
  const chipsEl = document.getElementById("feed-chips");

  if (articlesRaw.length === 0) {
    grid.innerHTML = "";
    timeline.innerHTML = "";
    empty.style.display = "block";
    chipsEl.innerHTML = "";
    return;
  }
  empty.style.display = "none";

  const hues = renderChips(chipsEl, articlesRaw, applyFeedFilter);

  grid.innerHTML = articlesRaw.map((a) => renderCard(a, hues)).join("");
  timeline.innerHTML = renderTimeline(articlesRaw, hues);
  document.querySelectorAll("#feed-grid .fav-btn, #feed-timeline .fav-btn").forEach((btn) => {
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

function renderSourceRow(s) {
  return `
    <div class="source-row ${s.enabled ? "" : "disabled"}">
      <span class="name">${escapeHtml(s.name)}</span>
      <span class="meta">${s.kind}${s.user_agent ? " · custom UA" : ""}</span>
      ${renderHealthBadge(s)}
      <button class="toggle-btn" data-id="${s.id}" data-enabled="${s.enabled}">${s.enabled ? "Disable" : "Enable"}</button>
      <button class="danger delete-btn" data-id="${s.id}">Delete</button>
    </div>`;
}

async function loadSources() {
  state.sources = await api("/api/sources");
  const websites = state.sources.filter((s) => s.category === "website");
  const newsletters = state.sources.filter((s) => s.category === "newsletter");

  document.getElementById("website-sources").innerHTML = websites.map(renderSourceRow).join("") || '<p class="hint">None yet.</p>';
  document.getElementById("newsletter-sources").innerHTML = newsletters.map(renderSourceRow).join("") || '<p class="hint">None yet.</p>';

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
}

function sourceFormHtml() {
  return `
    <div class="card-form">
      <h3>Add a website or newsletter</h3>
      <div class="field">
        <label>Name</label>
        <input type="text" id="new-source-name" placeholder="e.g. CERN">
      </div>
      <div class="field">
        <label>Category</label>
        <select id="new-source-category">
          <option value="website">Website</option>
          <option value="newsletter">Newsletter</option>
        </select>
      </div>
      <div class="field">
        <label>Website URL</label>
        <input type="text" id="new-source-url" placeholder="https://example.com/news" style="width: 320px">
        <button id="detect-feed-btn">Auto-detect feed</button>
      </div>
      <div id="detect-status" class="hint"></div>
      <div id="manual-selectors" style="display:none; margin-top: 12px;">
        <p class="hint">No feed was found automatically. Fill in CSS selectors for the article listing page (see CLAUDE.md for how to find these in your browser's dev tools).</p>
        <div class="field"><label>Article container selector</label><input type="text" id="sel-article" style="width:100%"></div>
        <div class="field"><label>Title selector</label><input type="text" id="sel-title" style="width:100%"></div>
        <div class="field"><label>Link selector</label><input type="text" id="sel-link" style="width:100%"></div>
        <div class="field"><label>Base URL (for relative links)</label><input type="text" id="sel-base" style="width:100%"></div>
        <div class="field"><label>Date selector (optional)</label><input type="text" id="sel-date" style="width:100%"></div>
        <div class="field"><label>Image selector (optional)</label><input type="text" id="sel-image" style="width:100%"></div>
      </div>
      <input type="hidden" id="detected-feed-url">
      <div style="margin-top: 14px;">
        <button id="save-source-btn" class="primary">Add source</button>
        <button id="cancel-source-btn">Cancel</button>
      </div>
      <div id="source-form-error" class="error-text"></div>
    </div>`;
}

document.getElementById("add-source-btn").addEventListener("click", () => {
  const form = document.getElementById("add-source-form");
  form.innerHTML = sourceFormHtml();
  form.style.display = "block";

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
        document.getElementById("manual-selectors").style.display = "block";
      }
    } catch (e) {
      status.textContent = `Error: ${e.message}`;
    }
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

    const body = feedUrl
      ? { name, category, kind: "rss", url: feedUrl }
      : {
          name, category, kind: "css", url,
          article_selector: document.getElementById("sel-article").value.trim(),
          title_selector: document.getElementById("sel-title").value.trim(),
          link_selector: document.getElementById("sel-link").value.trim(),
          base_url: document.getElementById("sel-base").value.trim() || url,
          date_selector: document.getElementById("sel-date").value.trim() || null,
          image_selector: document.getElementById("sel-image").value.trim() || null,
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
}

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
// Init
// -------------------------------------------------------------------------

loadFeed();
