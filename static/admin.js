// Admin page (Settings, Debug, Bêta, Correctifs), version display, beta banner and the phone bottom menu.
// ES module; app.js (classic script) owns the data loaders (loadSettings/loadDebug) and the main tab switching.
import { isBeta, setBeta, initBeta } from "/static/beta.js";

initBeta();

const $ = (id) => document.getElementById(id);
let version = "";
let releases = [];

// --- Admin sub-tabs -------------------------------------------------------
function showPanel(name) {
  document.querySelectorAll(".admin-tab").forEach((t) => {
    const on = t.dataset.panel === name;
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", on ? "true" : "false");
  });
  document.querySelectorAll(".admin-panel").forEach((p) => p.classList.toggle("active", p.id === `admin-${name}`));
  try { sessionStorage.setItem("newsbot-admin-panel", name); } catch (e) { /* optional */ }
  if (name === "settings" && window.loadSettings) window.loadSettings();
  if (name === "debug" && window.loadDebug) window.loadDebug();
  if (name === "beta" || name === "hotfix") renderVersions();
}

window.adminActivated = () => {
  let name = "settings";
  try { name = sessionStorage.getItem("newsbot-admin-panel") || name; } catch (e) { /* optional */ }
  showPanel(name);
};

document.querySelectorAll(".admin-tab").forEach((t) => t.addEventListener("click", () => showPanel(t.dataset.panel)));

// --- Version / changelog --------------------------------------------------
const isFix = (v) => /^\d+\.\d+\.\d+$/.test(v) && !v.endsWith(".0");

function releaseHtml(r) {
  const li = r.items.map((i) => `<li>${esc(i).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")}</li>`).join("");
  return `<section class="changelog__release"><h3>${esc(r.version)} <span class="muted">— ${esc(r.date)}</span></h3><ul>${li}</ul></section>`;
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function renderVersions() {
  $("beta-version").textContent = version || "?";
  $("hotfix-version").textContent = version || "?";
  $("changelog-list").innerHTML = releases.map(releaseHtml).join("") || '<p class="hint">Journal indisponible.</p>';
  const fixes = releases.filter((r) => isFix(r.version));
  $("hotfix-list").innerHTML = fixes.map(releaseHtml).join("");
  $("hotfix-latest").textContent = fixes.length
    ? `Dernier correctif : ${fixes[0].version} (${fixes[0].date}).`
    : "Aucun correctif depuis la dernière version de nouveautés.";
}

async function loadVersion() {
  try {
    const res = await fetch("/api/version");
    if (res.ok) {
      const data = await res.json();
      version = data.version;
      releases = data.releases || [];
    }
  } catch (e) { /* the version label is optional */ }
  $("app-version").textContent = version ? `v${version}` : "";
  renderVersions();
}

// --- Beta mode ------------------------------------------------------------
function syncBeta() {
  const on = isBeta();
  $("beta-banner").hidden = !on;
  $("beta-state").textContent = on ? "activé" : "désactivé";
  $("beta-toggle-btn").textContent = on ? "Quitter la bêta dans cet onglet" : "Activer la bêta dans cet onglet";
}
$("beta-toggle-btn").addEventListener("click", () => { setBeta(!isBeta()); syncBeta(); });
$("beta-off-btn").addEventListener("click", () => { setBeta(false); syncBeta(); });

// --- Phone bottom menu (<= 640 px; CSS hides it above) ---------------------------
const MAIN = [
  ["feed", "Feed", "📰"],
  ["favorites", "Favorites", "★"],
  ["jobs", "Jobs", "💼"],
  ["sources", "Sources", "📡"],
];
const MORE = [["analysis", "Data Analysis"], ["admin", "Admin"]];

function goTo(view) {
  const btn = document.querySelector(`.tab-btn[data-view="${view}"]`);
  if (btn) btn.click();
  closeSheet();
}

function closeSheet() {
  $("bottomnav-sheet").hidden = true;
  $("bottomnav-backdrop").hidden = true;
  syncNav();
}

function buildNav() {
  const nav = $("bottomnav");
  nav.innerHTML = MAIN.map(([v, label, icon]) =>
    `<button type="button" class="bottomnav__item" data-nav="${v}"><span class="bottomnav__icon" aria-hidden="true">${icon}</span><span class="bottomnav__label">${label}</span></button>`
  ).join("") +
    `<button type="button" class="bottomnav__item" data-nav="__more" aria-haspopup="true" aria-expanded="false"><span class="bottomnav__icon" aria-hidden="true">⋯</span><span class="bottomnav__label">Plus</span></button>`;
  $("bottomnav-sheet").innerHTML = MORE.map(([v, label]) =>
    `<button type="button" class="bottomnav__more" data-nav="${v}">${label}</button>`).join("") +
    `<div class="bottomnav__footer"><span id="nav-version"></span></div>`;
  nav.addEventListener("click", (e) => {
    const b = e.target.closest("[data-nav]");
    if (!b) return;
    if (b.dataset.nav === "__more") {
      const open = $("bottomnav-sheet").hidden;
      $("bottomnav-sheet").hidden = !open;
      $("bottomnav-backdrop").hidden = !open;
      syncNav();
    } else goTo(b.dataset.nav);
  });
  $("bottomnav-sheet").addEventListener("click", (e) => {
    const b = e.target.closest("[data-nav]");
    if (b) goTo(b.dataset.nav);
  });
  $("bottomnav-backdrop").addEventListener("click", closeSheet);
}

function syncNav() {
  const active = document.querySelector(".tab-btn.active")?.dataset.view;
  const sheetOpen = !$("bottomnav-sheet").hidden;
  document.querySelectorAll("#bottomnav [data-nav]").forEach((b) => {
    const v = b.dataset.nav;
    const on = v === "__more" ? (MORE.some(([m]) => m === active) || sheetOpen) : v === active && !sheetOpen;
    b.classList.toggle("active", on);
    if (v === "__more") b.setAttribute("aria-expanded", sheetOpen ? "true" : "false");
  });
  const nv = $("nav-version");
  if (nv) nv.textContent = version ? `Version ${version}` : "";
}

buildNav();
document.querySelectorAll(".tab-btn").forEach((b) => b.addEventListener("click", () => setTimeout(syncNav, 0)));
syncBeta();
loadVersion().then(syncNav);
syncNav();
