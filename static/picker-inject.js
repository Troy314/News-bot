// Injected into a sandboxed preview iframe (see web.py's
// GET /api/sources/preview-html) to power the point-and-click CSS-selector
// picker in the "Add source"/"Edit source" forms -- a webscraper.io-style
// "click the element you want, we figure out the selector" flow instead of
// hand-typing CSS selectors while reading page source.
//
// Runs inside the fetched target page's own document, which is loaded with
// sandbox="allow-scripts" and deliberately *no* allow-same-origin (see
// app.js's openPicker()) -- that combination gives this script a real,
// working document to manipulate (hover highlighting, click handling) but
// an opaque origin, so it can never reach the parent page's real cookies,
// localStorage, or DOM even though it runs real JS. The only channel back
// to the parent is postMessage, which works across opaque origins by
// design -- this script never tries anything else.
(function () {
  let mode = null; // "container" | "child" | null (picking inactive)
  let containerSelector = null; // set when mode === "child"
  let hovered = null;

  const HL_CLASS = "__picker_highlight__";
  const style = document.createElement("style");
  style.textContent = `.${HL_CLASS} { outline: 2px solid #ff5f5f !important; outline-offset: -1px !important; cursor: crosshair !important; }`;
  document.documentElement.appendChild(style);

  function clearHighlight() {
    if (hovered) hovered.classList.remove(HL_CLASS);
    hovered = null;
  }

  document.addEventListener("mouseover", (e) => {
    if (!mode) return;
    clearHighlight();
    hovered = e.target;
    hovered.classList.add(HL_CLASS);
  }, true);

  // "Article container" pick: the clicked element itself usually IS one
  // instance of a repeated card/row -- generalize its own tag+classes
  // until the selector matches more than one element on the page, trying
  // the fullest (most specific) class combination first and dropping
  // trailing classes one at a time. Walks up to the parent only if the
  // clicked element's own level never generalizes (e.g. the real
  // repeating element is a wrapping <li>, not the <a> that was clicked).
  function pickContainerSelector(el) {
    let node = el;
    let best = null;
    for (let depth = 0; depth < 5 && node && node.nodeType === 1 && node !== document.documentElement; depth++) {
      const tag = node.tagName.toLowerCase();
      const classes = Array.from(node.classList || []);
      for (let n = classes.length; n >= 0; n--) {
        const sel = n === 0 ? tag : `${tag}.${classes.slice(0, n).map((c) => CSS.escape(c)).join(".")}`;
        let count = 0;
        try { count = document.querySelectorAll(sel).length; } catch (err) { continue; }
        if (count > 1 && (!best || count > best.count)) best = { selector: sel, count };
      }
      if (best) break;
      node = node.parentElement;
    }
    if (!best) {
      const tag = el.tagName.toLowerCase();
      const classes = Array.from(el.classList || []);
      const sel = classes.length ? `${tag}.${classes.map((c) => CSS.escape(c)).join(".")}` : tag;
      best = { selector: sel, count: document.querySelectorAll(sel).length };
    }
    return best;
  }

  // Title/link/date/date-year/deadline/image pick: build a descendant path
  // from the clicked element up to (not past) `scope` -- one instance of
  // the article container, found via containerSelector -- stopping as soon
  // as the accumulated path uniquely identifies the element *within that
  // one container*. This mirrors exactly what scrape_css() itself does at
  // scrape time (container.select_one(field_selector)), so a selector
  // picked here is guaranteed to mean what the picker showed.
  function pickChildSelector(el, scope) {
    if (el === scope) return { selector: "", count: 1 };
    const parts = [];
    let node = el;
    while (node && node !== scope && node.nodeType === 1) {
      let sel = node.tagName.toLowerCase();
      const classes = Array.from(node.classList || []);
      if (classes.length) sel += `.${classes.map((c) => CSS.escape(c)).join(".")}`;
      parts.unshift(sel);
      const candidate = parts.join(" ");
      let matches = [];
      try { matches = Array.from(scope.querySelectorAll(candidate)); } catch (err) { matches = []; }
      if (matches.length === 1) return { selector: candidate, count: 1 };
      node = node.parentElement;
    }
    // Walked all the way to scope without finding a unique path (rare --
    // e.g. two visually-identical sibling elements) -- return what we
    // built anyway with its real match count, so the form field still
    // gets *something* useful and the count feedback shows the ambiguity.
    let count = 0;
    try { count = scope.querySelectorAll(parts.join(" ")).length; } catch (err) {}
    return { selector: parts.join(" "), count };
  }

  document.addEventListener("click", (e) => {
    if (!mode) return;
    e.preventDefault();
    e.stopPropagation();
    const el = e.target;
    let result;
    if (mode === "container") {
      result = pickContainerSelector(el);
    } else {
      let scope = document.body;
      if (containerSelector) {
        let matches = [];
        try { matches = Array.from(document.querySelectorAll(containerSelector)); } catch (err) { matches = []; }
        const found = matches.find((c) => c.contains(el));
        if (found) scope = found;
      }
      result = pickChildSelector(el, scope);
    }
    clearHighlight();
    window.parent.postMessage({ type: "picker-selected", selector: result.selector, matchCount: result.count }, "*");
  }, true);

  // Parent tells us which field we're picking for (and, for a child pick,
  // which container selector to scope the search inside) via postMessage
  // -- the only channel available across this iframe's opaque origin.
  window.addEventListener("message", (e) => {
    const data = e.data || {};
    if (data.type === "picker-mode") {
      mode = data.mode || null;
      containerSelector = data.containerSelector || null;
      clearHighlight();
    }
  });

  window.parent.postMessage({ type: "picker-ready" }, "*");
})();
