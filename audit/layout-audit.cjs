// UI audit: layout (nothing sticks out of its group, 320–1280 px), accessibility basics (+ axe-core if given) and visual
// regression (screenshots compared with a baseline, new look and old look). Exit code 1 when something is found.
//
//   node audit/layout-audit.cjs --base http://localhost:4179 [--views audit/views.json] [--mock audit/mock.cjs]
//        [--widths 320,360,390,768,1280] [--looks new,old] [--a11y] [--axe /path/to/axe.min.js]
//        [--ignore ".pane-right.collapsed,.drawer"] [--skip heading-order,no-label] [--shots audit/shots] [--baseline audit/baseline] [--threshold 0.5] [--update-baseline] [--real-api]
//
// views.json : [{ "name": "create", "path": "/?create=event", "clicks": ["Interne"], "scrollSelector": ".modal__body", "covers": ["EventModal"] }]
//   clicks = visible texts clicked in order (or "title=…" / "label=…" for icon-only buttons; a leading "?" makes a click optional); covers = component names this view exercises (audit/check-coverage.mjs).
// mock.cjs   : module.exports = (pathname, method, viewSearch) => body   — the fake API (unknown routes → []; return { __status: 401 } to simulate a refused call, e.g. logged out).
// Playwright : `playwright` resolvable, or PLAYWRIGHT_PATH=/path/to/node_modules/playwright. Axe: AXE_PATH or --axe.
// Run it IN THE BACKGROUND with output to a file (minutes), and stop the server by PID afterwards.
const fs = require("fs");
const path = require("path");

const args = process.argv.slice(2);
const flag = (n) => args.includes(`--${n}`);
const opt = (n, d) => { const i = args.indexOf(`--${n}`); return i >= 0 && args[i + 1] && !args[i + 1].startsWith("--") ? args[i + 1] : d; };
const base = opt("base", "http://localhost:4179");
const views = require(path.resolve(opt("views", "audit/views.json")));
const realApi = flag("real-api");   // no fake API: the app talks to its real backend (run that backend on a COPY of the data)
const mock = realApi ? () => null : require(path.resolve(opt("mock", "audit/mock.cjs")));
const widths = opt("widths", "320,360,390,768,1280").split(",").map(Number);
const looks = opt("looks", "new").split(",").filter(Boolean);
const shotsDir = opt("shots", null), baseDir = opt("baseline", null), threshold = Number(opt("threshold", "0.5"));
const axePath = opt("axe", process.env.AXE_PATH || null);
const ignoreSel = opt("ignore", "");
const skipRules = opt("skip", "").split(",").filter(Boolean);   // a11y rules not enforced YET (heading-order,no-label,…): list them in the app's TODO, never hide silently   // comma-separated selectors skipped by the layout check (e.g. a closed off-canvas drawer: ".pane-right.collapsed")
const { chromium } = require(process.env.PLAYWRIGHT_PATH || "playwright");

// ---- runs inside the page ----
function layoutCheck(ignore) {
  const out = [], W = document.documentElement.clientWidth;
  for (const e of document.querySelectorAll("body *")) {
    if (ignore && e.closest(ignore)) continue;
    const cs = getComputedStyle(e);
    if ((!e.offsetParent && cs.position !== "fixed") || cs.position === "absolute" || cs.position === "fixed") continue;
    const r = e.getBoundingClientRect(); if (!r.width || !r.height) continue;
    const par = e.parentElement; if (!par) continue;
    const pr = par.getBoundingClientRect(), pcs = getComputedStyle(par);
    if (/(auto|scroll|hidden|clip)/.test(pcs.overflowX)) continue;
    let scrolled = false;   // inside a scroll container (a wide table in a horizontally scrollable wrapper is intended)
    for (let a = par; a && a !== document.body; a = a.parentElement) if (/(auto|scroll|clip)/.test(getComputedStyle(a).overflowX)) { scrolled = true; break; }
    if (scrolled) continue;
    const label = `${e.tagName.toLowerCase()}.${String(e.className).split(" ")[0]} "${(e.textContent || "").trim().slice(0, 18)}"`;
    if (r.right > pr.right + 1.5) out.push(`${label} by ${Math.round(r.right - pr.right)}px in ${par.tagName.toLowerCase()}.${String(par.className).split(" ")[0]}`);
    else if (r.right > W + 1) out.push(`${label} beyond viewport`);
    if (/^(BUTTON|A|SPAN|LABEL)$/.test(e.tagName) && !/(hidden|clip)/.test(cs.overflowX)) {   // the element's OWN text sticks out of it (not hidden tooltips/pseudo-elements)
      const own = [...e.childNodes].some((n) => { if (n.nodeType !== 3 || !n.textContent.trim()) return false; const rg = document.createRange(); rg.selectNodeContents(n); return rg.getBoundingClientRect().right > r.right + 1; });
      if (own) out.push(`${label} text wider than its box`);
    }
  }
  // A page-sized scroll container whose content is wider than itself = the page scrolls sideways (the usual "something is outside the page" on phones).
  // Small scrollers (a wide table in its wrapper) are intended and skipped.
  for (const e of document.querySelectorAll("body *")) {
    if (ignore && e.closest(ignore)) continue;
    const cs = getComputedStyle(e);
    if (!/(auto|scroll)/.test(cs.overflowX) || !e.offsetParent || e.closest("[data-scroll-x]")) continue;   // data-scroll-x = a strip meant to scroll sideways (tabs)
    const r = e.getBoundingClientRect();
    if (r.width >= W * 0.7 && e.scrollWidth > e.clientWidth + 2) {
      const wide = [...e.querySelectorAll("*")].filter((c) => c.offsetParent && c.getBoundingClientRect().right > r.right + 2 && !/(auto|scroll|hidden|clip)/.test(getComputedStyle(c.parentElement).overflowX)).slice(0, 2).map((c) => `${c.tagName.toLowerCase()}.${String(c.className).split(" ")[0]} "${(c.textContent || "").trim().slice(0, 16)}"`);
      out.push(`${e.tagName.toLowerCase()}.${String(e.className).split(" ")[0]} scrolls sideways (content ${e.scrollWidth}px in ${e.clientWidth}px)${wide.length ? " — wider: " + wide.join(", ") : ""}`);
    }
  }
  return [...new Set(out)].slice(0, 12);
}
function a11yCheck() {
  const out = [];
  const visible = (e) => !!(e.offsetParent || getComputedStyle(e).position === "fixed");
  const text = (id) => (document.getElementById(id)?.textContent || "").trim();
  const nameOf = (e) => (e.getAttribute("aria-label") || (e.getAttribute("aria-labelledby") || "").split(" ").map(text).join(" ") || e.textContent || e.getAttribute("title") || e.querySelector("img[alt]")?.getAttribute("alt") || "").trim();
  const tag = (e) => `${e.tagName.toLowerCase()}${e.className ? "." + String(e.className).split(" ")[0] : ""}${e.getAttribute("type") ? `[${e.getAttribute("type")}]` : ""}${e.getAttribute("placeholder") ? ` placeholder="${e.getAttribute("placeholder").slice(0, 24)}"` : ""}${e.getAttribute("value") ? ` value="${e.getAttribute("value").slice(0, 16)}"` : ""} near "${(e.parentElement?.textContent || "").trim().slice(0, 24)}"`;
  for (const e of document.querySelectorAll("button, a[href], [role=button]")) if (visible(e) && !nameOf(e)) out.push(`[no-name] ${tag(e)} has no accessible name`);
  for (const e of document.querySelectorAll("input:not([type=hidden]), select, textarea")) {
    if (!visible(e)) continue;
    const labelled = e.getAttribute("aria-label") || e.getAttribute("aria-labelledby") || e.closest("label") || (e.id && document.querySelector(`label[for="${e.id}"]`)) || e.getAttribute("title");
    if (!labelled) out.push(`[no-label] ${tag(e)} has no label`);
  }
  for (const e of document.querySelectorAll("img")) if (visible(e) && !e.hasAttribute("alt")) out.push(`[alt] img ${e.getAttribute("src")?.slice(0, 30)} has no alt`);
  let last = 0;
  for (const h of document.querySelectorAll("h1,h2,h3,h4,h5,h6")) {
    if (!visible(h)) continue;
    const n = Number(h.tagName[1]);
    if (last && n > last + 1) out.push(`[heading-order] heading "${h.textContent.trim().slice(0, 20)}" jumps from h${last} to h${n}`);
    last = n;
  }
  for (const e of document.querySelectorAll("[role=dialog]")) if (!e.getAttribute("aria-label") && !e.getAttribute("aria-labelledby")) out.push("[dialog-name] dialog without a name");
  for (const e of document.querySelectorAll("[tabindex]")) if (Number(e.getAttribute("tabindex")) > 0) out.push(`[tabindex] ${tag(e)} has a positive tabindex`);
  const ids = new Map(); for (const e of document.querySelectorAll("[id]")) ids.set(e.id, (ids.get(e.id) || 0) + 1);
  for (const [id, n] of ids) if (n > 1) out.push(`[dup-id] duplicate id "${id}"`);
  return [...new Set(out)].slice(0, 12);
}
// fraction (%) of pixels that differ between two PNG data URLs
async function diffPercent([a, b]) {
  const load = (src) => new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = rej; i.src = src; });
  const [ia, ib] = await Promise.all([load(a), load(b)]);
  if (ia.width !== ib.width || ia.height !== ib.height) return 100;
  const px = (img) => { const c = document.createElement("canvas"); c.width = img.width; c.height = img.height; const x = c.getContext("2d"); x.drawImage(img, 0, 0); return x.getImageData(0, 0, img.width, img.height).data; };
  const da = px(ia), db = px(ib);
  let diff = 0;
  for (let i = 0; i < da.length; i += 4) if (Math.abs(da[i] - db[i]) > 16 || Math.abs(da[i + 1] - db[i + 1]) > 16 || Math.abs(da[i + 2] - db[i + 2]) > 16) diff++;
  return (diff / (da.length / 4)) * 100;
}

(async () => {
  const b = await chromium.launch();
  const axeSource = axePath ? fs.readFileSync(axePath, "utf8") : null;
  const helper = await b.newPage();
  let problems = 0;
  const say = (m) => { problems++; console.log(m); };
  for (const look of looks) for (const w of widths) for (const v of views) {
    const id = `${v.name}__${w}__${look}`;
    const p = await b.newPage({ viewport: { width: w, height: 900 } });
    const answer = (r) => {   // mock(pathname, method, search of the VIEW's url) → body
      const url = new URL(r.request().url());
      const body = mock(url.pathname, r.request().method(), new URL(base + v.path).search) ?? [];
      r.fulfill({ status: body && body.__status ? body.__status : 200, contentType: "application/json", body: JSON.stringify(body && body.__status ? { detail: "mock" } : body) });   // { __status: 401 } = a refused call
    };
    if (!realApi) {
      await p.route("**/api/**", answer);
      await p.route("**/auth/me", answer);
    }
    p.on("pageerror", (e) => say(`[${id}] PAGEERROR ${e.message.slice(0, 160)}`));
    const sep = v.path.includes("?") ? "&" : "?";
    await p.goto(`${base}${v.path}${looks.length > 1 || look === "old" ? `${sep}visuel=${look === "old" ? "ancien" : "nouveau"}` : ""}`);
    await p.waitForTimeout(900);
    for (let t of v.clicks || []) {   // "text", or "title=Modifier" / "label=Fermer" for icon-only buttons; a leading "?" = optional (e.g. "?label=Ouvrir la navigation": a phone drawer)
      const optional = t.startsWith("?"); if (optional) t = t.slice(1);
      const loc = t.startsWith("title=") ? p.getByTitle(t.slice(6)) : t.startsWith("label=") ? p.getByLabel(t.slice(6), { exact: true }) : p.getByText(t, { exact: false });
      await loc.filter({ visible: true }).first().click({ timeout: optional ? 600 : 3000 }).catch(() => { if (!optional) console.log(`[${id}] click not found: ${t}`); }); await p.waitForTimeout(400);
    }
    if (v.scrollSelector) await p.evaluate((sel) => { const e = document.querySelector(sel); if (e) e.scrollTop = e.scrollHeight; }, v.scrollSelector);
    const found = [...(await p.evaluate(layoutCheck, ignoreSel)).map((m) => `layout: ${m}`)];
    if (flag("a11y")) found.push(...(await p.evaluate(a11yCheck)).filter((m) => !skipRules.some((r) => m.startsWith(`[${r}]`))).map((m) => `a11y: ${m}`));
    if (flag("a11y") && axeSource) {
      await p.evaluate(axeSource);
      const res = await p.evaluate(() => window.axe.run(document, { resultTypes: ["violations"] }).then((r) => r.violations.filter((x) => ["serious", "critical"].includes(x.impact)).map((x) => `${x.id} (${x.impact}) ×${x.nodes.length}`)));
      found.push(...res.map((m) => `axe: ${m}`));
    }
    if (shotsDir || baseDir) {
      const png = await p.screenshot();
      if (shotsDir) { fs.mkdirSync(shotsDir, { recursive: true }); fs.writeFileSync(path.join(shotsDir, `${id}.png`), png); }
      if (baseDir) {
        const ref = path.join(baseDir, `${id}.png`);
        if (flag("update-baseline") || !fs.existsSync(ref)) { fs.mkdirSync(baseDir, { recursive: true }); fs.writeFileSync(ref, png); }
        else {
          const pct = await helper.evaluate(diffPercent, [`data:image/png;base64,${png.toString("base64")}`, `data:image/png;base64,${fs.readFileSync(ref).toString("base64")}`]).catch(() => 100);
          if (pct > threshold) found.push(`visual: ${pct.toFixed(2)}% of pixels differ from the baseline`);
        }
      }
    }
    if (found.length) { problems += found.length; console.log(`[${id}]\n  ` + [...new Set(found)].slice(0, 14).join("\n  ")); }
    await p.close();
  }
  await b.close();
  console.log(problems ? `${problems} problem(s)` : "ui audit: clean");
  process.exit(problems ? 1 : 0);
})();
