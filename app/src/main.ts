import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { getCurrentWindow, UserAttentionType } from "@tauri-apps/api/window";
import { openUrl, revealItemInDir } from "@tauri-apps/plugin-opener";

type VideoStatus = "queued" | "downloading" | "done" | "failed" | "cancelled" | "paused";

// Sidebar navigation buckets. "issues" groups failed + cancelled so problem
// rows live in one place instead of two near-empty views.
type Filter = "all" | "queued" | "downloading" | "done" | "paused" | "issues";

// Sub-elements of a row that get patched individually. Rows are rendered as
// a static skeleton once, then only the cells that actually changed are
// touched -- rebuilding a row's whole innerHTML on every progress tick (the
// old approach) forced layout/paint of the entire row several times a second
// per active download, and nuked CSS transitions mid-animation.
interface RowCells {
  indexBadge: HTMLElement;
  thumb: HTMLImageElement;
  title: HTMLElement;
  url: HTMLElement;
  size: HTMLElement;
  duration: HTMLElement;
  progress: HTMLElement;
  status: HTMLElement;
  actions: HTMLElement;
}

interface ProgressRefs {
  fill: HTMLElement | null;
  pct: HTMLElement | null;
  speed: HTMLElement | null;
  detail: HTMLElement | null;
  eta: HTMLElement | null;
}

interface VideoRow {
  url: string;
  index: number;
  status: VideoStatus;
  title: string | null;
  duration: number | null;
  filesize: number | null;
  thumbnail: string | null;
  destPath: string | null;
  error: string | null;
  percent: number | null;
  speed: string | null;
  eta: string | null;
  downloaded: string | null;
  total: string | null;
  el: HTMLDivElement;
  cells: RowCells;
  // What the DOM currently shows -- lets upsertVideo skip expensive cell
  // rebuilds when nothing structural changed.
  renderedStatus: VideoStatus | null;
  renderedDestPath: string | null;
  progressMode: "bar" | "indet" | "static" | null;
  prog: ProgressRefs | null;
}

const videos = new Map<string, VideoRow>();
let currentFilter: Filter = "all";
let searchQuery = "";
let scanning = false;

const listingUrlInput = document.querySelector<HTMLInputElement>("#listing-url")!;
const pagesRangeInput = document.querySelector<HTMLInputElement>("#pages-range")!;
const destDirInput = document.querySelector<HTMLInputElement>("#dest-dir")!;
const concurrencyInput = document.querySelector<HTMLInputElement>("#concurrency")!;
const rateLimitInput = document.querySelector<HTMLInputElement>("#rate-limit")!;
const qualitySelect = document.querySelector<HTMLSelectElement>("#quality-select")!;
const searchInput = document.querySelector<HTMLInputElement>("#search-input")!;
const browseBtn = document.querySelector<HTMLButtonElement>("#browse-btn")!;
const fetchBtn = document.querySelector<HTMLButtonElement>("#fetch-btn")!;
const downloadBtn = document.querySelector<HTMLButtonElement>("#download-btn")!;
const pauseBtn = document.querySelector<HTMLButtonElement>("#pause-btn")!;
const retryFailedBtn = document.querySelector<HTMLButtonElement>("#retry-failed-btn")!;
const clearBtn = document.querySelector<HTMLButtonElement>("#clear-btn")!;
const statusMsg = document.querySelector<HTMLParagraphElement>("#status-msg")!;
const videoList = document.querySelector<HTMLDivElement>("#video-list")!;
const emptyState = document.querySelector<HTMLDivElement>("#empty-state")!;
const emptyStateText = document.querySelector<HTMLParagraphElement>("#empty-state-text")!;
const nav = document.querySelector<HTMLElement>("#nav")!;
const toastStack = document.querySelector<HTMLDivElement>("#toast-stack")!;
const engineStatus = document.querySelector<HTMLSpanElement>("#engine-status")!;
const engineStatusText = document.querySelector<HTMLSpanElement>("#engine-status-text")!;
const sparklineLine = document.querySelector<SVGPolylineElement>("#sparkline-line")!;
const sparklineFill = document.querySelector<SVGPolygonElement>("#sparkline-fill")!;

const paginationInfo = document.querySelector<HTMLSpanElement>("#pagination-info")!;
const pageIndicator = document.querySelector<HTMLSpanElement>("#page-indicator")!;
const pageSizeSelect = document.querySelector<HTMLSelectElement>("#page-size-select")!;
const pageFirstBtn = document.querySelector<HTMLButtonElement>("#page-first-btn")!;
const pagePrevBtn = document.querySelector<HTMLButtonElement>("#page-prev-btn")!;
const pageNextBtn = document.querySelector<HTMLButtonElement>("#page-next-btn")!;
const pageLastBtn = document.querySelector<HTMLButtonElement>("#page-last-btn")!;

// ---- pagination ---------------------------------------------------------------
//
// A scan can find hundreds of videos, but only ~50 of them are ever on
// screen at once. Every row still gets a full VideoRow (data + a built but
// possibly detached DOM element) the moment it's known -- only the rows for
// the *current page* actually live under #video-list. Everything else's
// element sits unattached, so it costs a few detached DOM nodes but zero
// layout/paint, and progress ticks for off-page rows are skipped entirely
// (see upsertVideo's `row.el.isConnected` check) instead of doing wasted
// work on markup nobody can see.
const PAGE_SIZES = [50, 100, 200, 500] as const;
let pageSize: number = PAGE_SIZES[0];
let currentPage = 1;
let filteredUrls: string[] = [];
let listDirty = true;

function recomputeFilteredList() {
  filteredUrls = [];
  for (const row of videos.values()) {
    if (matchesFilter(row) && matchesSearch(row)) filteredUrls.push(row.url);
  }
  listDirty = false;
}

function totalPages(): number {
  return Math.max(1, Math.ceil(filteredUrls.length / pageSize));
}

function clampCurrentPage() {
  const pages = totalPages();
  if (currentPage > pages) currentPage = pages;
  if (currentPage < 1) currentPage = 1;
}

// Rebuilds #video-list to hold exactly this page's rows (existing DOM
// elements are moved, not recreated, so this is cheap even called often),
// then fully refreshes each of those rows -- catches up any row whose data
// changed while it was off-page and skipped.
function renderCurrentPage() {
  if (listDirty) recomputeFilteredList();
  clampCurrentPage();
  const start = (currentPage - 1) * pageSize;
  const pageUrls = filteredUrls.slice(start, start + pageSize);

  videoList.replaceChildren(...pageUrls.map((url) => videos.get(url)!.el));
  for (const url of pageUrls) {
    const row = videos.get(url)!;
    updateRowMeta(row);
    const statusChanged = row.status !== row.renderedStatus;
    const revealChanged = row.status === "done" && row.destPath !== row.renderedDestPath;
    if (statusChanged || revealChanged) {
      rebuildStatusAndActions(row);
      rebuildProgressCell(row);
    } else if (row.status === "downloading") {
      // Already built for this status -- just refresh the numbers instead
      // of tearing down and recreating .progress-fill, which would reset
      // its CSS transition and visibly flicker every other still-
      // downloading row on the page whenever any single row's status
      // change forces this whole function to run.
      patchProgressCell(row);
    }
  }

  updatePaginationControls();
  updateEmptyState();
}

function updatePaginationControls() {
  const pages = totalPages();
  const total = filteredUrls.length;
  const start = total === 0 ? 0 : (currentPage - 1) * pageSize + 1;
  const end = Math.min(total, currentPage * pageSize);
  paginationInfo.textContent = total === 0 ? "0 videos" : `Showing ${start}-${end} of ${total}`;
  pageIndicator.textContent = `Page ${currentPage} of ${pages}`;
  pageFirstBtn.disabled = currentPage <= 1;
  pagePrevBtn.disabled = currentPage <= 1;
  pageNextBtn.disabled = currentPage >= pages;
  pageLastBtn.disabled = currentPage >= pages;
}

function goToPage(page: number) {
  if (listDirty) recomputeFilteredList();
  const pages = totalPages();
  currentPage = Math.max(1, Math.min(page, pages));
  renderCurrentPage();
}

pageFirstBtn.addEventListener("click", () => goToPage(1));
pagePrevBtn.addEventListener("click", () => goToPage(currentPage - 1));
pageNextBtn.addEventListener("click", () => goToPage(currentPage + 1));
pageLastBtn.addEventListener("click", () => goToPage(totalPages()));

pageSizeSelect.addEventListener("change", () => {
  const parsed = parseInt(pageSizeSelect.value, 10);
  pageSize = (PAGE_SIZES as readonly number[]).includes(parsed) ? parsed : PAGE_SIZES[0];
  currentPage = 1;
  listDirty = true;
  renderCurrentPage();
  saveSettings();
});

const navCountEls: Record<Filter, HTMLElement> = {
  all: document.querySelector<HTMLElement>("#nav-count-all")!,
  queued: document.querySelector<HTMLElement>("#nav-count-queued")!,
  downloading: document.querySelector<HTMLElement>("#nav-count-downloading")!,
  done: document.querySelector<HTMLElement>("#nav-count-done")!,
  paused: document.querySelector<HTMLElement>("#nav-count-paused")!,
  issues: document.querySelector<HTMLElement>("#nav-count-issues")!,
};

const statSpeedEl = document.querySelector<HTMLElement>("#stat-speed")!;
const statPeakEl = document.querySelector<HTMLElement>("#stat-peak")!;
const statSizeEl = document.querySelector<HTMLElement>("#stat-size")!;

// ---- themed custom dropdown --------------------------------------------------
//
// A plain <select>'s open popup is drawn by the OS (WebView2 on Windows), not
// the page, so no amount of CSS on the app's side can theme it -- it always
// shows up as a plain grey system list, clashing hard with the rest of the
// UI. This keeps the original <select> as the real source of truth (value,
// change events, everything main.ts already reads/listens to keeps working
// unchanged) but hides it and drives a fully-themed trigger + option list
// built and styled by us instead.
function initCustomSelect(select: HTMLSelectElement) {
  const wrap = document.createElement("div");
  wrap.className = "custom-select";
  select.insertAdjacentElement("beforebegin", wrap);
  wrap.appendChild(select);
  select.classList.add("native-select-hidden");
  select.tabIndex = -1;

  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "select-trigger";
  trigger.setAttribute("aria-haspopup", "listbox");
  trigger.setAttribute("aria-expanded", "false");
  trigger.innerHTML =
    `<span class="select-trigger-label"></span>` +
    `<svg class="select-caret" viewBox="0 0 20 20" width="11" height="11" aria-hidden="true">` +
    `<path d="m5.5 8 4.5 4.5L14.5 8" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" /></svg>`;
  wrap.appendChild(trigger);
  const label = trigger.querySelector<HTMLElement>(".select-trigger-label")!;

  const list = document.createElement("ul");
  list.className = "select-options";
  list.setAttribute("role", "listbox");
  list.hidden = true;
  // Appended to <body>, not to wrap -- a plain position:absolute child gets
  // its stacking resolved wherever the nearest real stacking-context
  // ancestor happens to be, which is exactly what let the list-panel's own
  // backdrop-filter (a stacking-context trigger) paint on top of this menu
  // even with a higher z-index set here. Fixed positioning computed from
  // the trigger's own screen rect sidesteps every ancestor's stacking/
  // overflow entirely, which is the only fix that's robust regardless of
  // which panel a given select happens to live inside.
  document.body.appendChild(list);

  function syncLabel() {
    const selected = select.options[select.selectedIndex];
    label.textContent = selected ? selected.textContent ?? selected.value : "";
  }

  function buildOptions() {
    list.replaceChildren(
      ...Array.from(select.options, (opt) => {
        const li = document.createElement("li");
        li.className = "select-option";
        li.setAttribute("role", "option");
        li.dataset.value = opt.value;
        li.textContent = opt.textContent ?? opt.value;
        if (opt.value === select.value) li.classList.add("active", "highlighted");
        return li;
      })
    );
  }

  function closeList() {
    list.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
  }

  function positionList() {
    const rect = trigger.getBoundingClientRect();
    const estimatedHeight = Math.min(220, list.scrollHeight || 220);
    const spaceBelow = window.innerHeight - rect.bottom;
    // Most of these fields sit near the bottom of their panel (the
    // pagination bar especially) -- open upward when there's more room
    // above than below so the panel doesn't get clipped by the window edge.
    const openUpward = spaceBelow < estimatedHeight && rect.top > spaceBelow;
    list.style.left = `${rect.left}px`;
    list.style.width = `${rect.width}px`;
    if (openUpward) {
      list.style.top = "";
      list.style.bottom = `${window.innerHeight - rect.top + 6}px`;
    } else {
      list.style.bottom = "";
      list.style.top = `${rect.bottom + 6}px`;
    }
  }

  function openList() {
    buildOptions();
    list.hidden = false;
    positionList();
    trigger.setAttribute("aria-expanded", "true");
    list.querySelector(".highlighted")?.scrollIntoView({ block: "nearest" });
  }

  function moveHighlight(delta: number) {
    const items = Array.from(list.querySelectorAll<HTMLLIElement>(".select-option"));
    if (items.length === 0) return;
    const current = Math.max(
      0,
      items.findIndex((li) => li.classList.contains("highlighted"))
    );
    const next = Math.max(0, Math.min(items.length - 1, current + delta));
    items.forEach((li) => li.classList.remove("highlighted"));
    items[next].classList.add("highlighted");
    items[next].scrollIntoView({ block: "nearest" });
  }

  function commitHighlighted() {
    const hi = list.querySelector<HTMLLIElement>(".select-option.highlighted");
    if (!hi) return;
    select.value = hi.dataset.value ?? select.value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    syncLabel();
    closeList();
  }

  trigger.addEventListener("click", () => {
    if (list.hidden) openList();
    else closeList();
  });

  list.addEventListener("click", (event) => {
    const li = (event.target as HTMLElement).closest<HTMLLIElement>(".select-option");
    if (!li) return;
    select.value = li.dataset.value ?? select.value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    syncLabel();
    closeList();
  });

  trigger.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeList();
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      if (list.hidden) openList();
      else moveHighlight(1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      if (!list.hidden) moveHighlight(-1);
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (list.hidden) openList();
      else commitHighlighted();
    }
  });

  document.addEventListener("click", (event) => {
    if (!wrap.contains(event.target as Node)) closeList();
  });
  window.addEventListener("resize", closeList);
  // capture:true catches scrolling in any ancestor (e.g. the video list),
  // not just the window itself -- the fixed-position panel doesn't track
  // scroll offsets, so it must close rather than drift out of place.
  window.addEventListener("scroll", closeList, true);

  // Covers programmatic value changes from outside this widget (e.g.
  // loadSettings restoring a saved quality/page-size) that set select.value
  // directly and dispatch "change" themselves -- without this, the visible
  // trigger button kept showing whatever was selected at page load instead
  // of the restored value, even though select.value (and therefore actual
  // app behavior) was already correct.
  select.addEventListener("change", syncLabel);

  syncLabel();
}

// ---- themed number stepper ---------------------------------------------------
//
// Same reasoning as initCustomSelect above: a number input's native up/down
// arrows are OS-drawn and can't be themed. Hides them and adds two small
// buttons of our own that nudge the value by `step`, clamped to min/max.
function initNumberStepper(input: HTMLInputElement) {
  const wrap = document.createElement("div");
  wrap.className = "number-stepper";
  input.insertAdjacentElement("beforebegin", wrap);
  wrap.appendChild(input);
  input.classList.add("stepper-input");

  const controls = document.createElement("div");
  controls.className = "stepper-controls";
  controls.innerHTML = `
    <button type="button" class="stepper-btn" data-dir="1" tabindex="-1" aria-label="Increase">
      <svg viewBox="0 0 20 20" width="9" height="9" aria-hidden="true"><path d="M4 12l6-6 6 6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" /></svg>
    </button>
    <button type="button" class="stepper-btn" data-dir="-1" tabindex="-1" aria-label="Decrease">
      <svg viewBox="0 0 20 20" width="9" height="9" aria-hidden="true"><path d="M4 8l6 6 6-6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" /></svg>
    </button>
  `;
  wrap.appendChild(controls);

  function nudge(dir: number) {
    const stepSize = Number(input.step) || 1;
    const min = input.min !== "" ? Number(input.min) : -Infinity;
    const max = input.max !== "" ? Number(input.max) : Infinity;
    const current = Number(input.value) || 0;
    // Rounds off float drift from fractional steps like 0.1 (e.g. 0.1 + 0.2).
    const next = Math.round(Math.min(max, Math.max(min, current + dir * stepSize)) * 1000) / 1000;
    input.value = String(next);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  controls.addEventListener("click", (event) => {
    const btn = (event.target as HTMLElement).closest<HTMLButtonElement>(".stepper-btn");
    if (btn) nudge(Number(btn.dataset.dir));
  });
}

initCustomSelect(qualitySelect);
initCustomSelect(pageSizeSelect);
initNumberStepper(concurrencyInput);
initNumberStepper(rateLimitInput);

// ---- frameless window controls ----------------------------------------------

// getCurrentWindow() throws outside a real Tauri webview (e.g. the vite dev
// URL opened in a plain browser) -- guard so the rest of the UI still works.
let appWindow: ReturnType<typeof getCurrentWindow> | null = null;
try {
  appWindow = getCurrentWindow();
  const win = appWindow;
  document.querySelector("#win-min")?.addEventListener("click", () => {
    win.minimize().catch(() => {});
  });
  document.querySelector("#win-max")?.addEventListener("click", () => {
    win.toggleMaximize().catch(() => {});
  });
  document.querySelector("#win-close")?.addEventListener("click", () => {
    win.close().catch(() => {});
  });
} catch {
  /* not running inside Tauri */
}

// ---- accent themes ----------------------------------------------------------

const THEMES = ["volt", "cyan", "magenta", "amberfire", "violetstorm"] as const;
type Theme = (typeof THEMES)[number];
const themePicker = document.querySelector<HTMLDivElement>("#theme-picker")!;

function applyTheme(theme: Theme) {
  // "volt" is the stylesheet's baseline -- no attribute needed.
  if (theme === "volt") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  for (const dot of themePicker.querySelectorAll(".theme-dot")) {
    dot.classList.toggle("active", (dot as HTMLElement).dataset.theme === theme);
  }
}

themePicker.addEventListener("click", (event) => {
  const dot = (event.target as HTMLElement).closest<HTMLButtonElement>(".theme-dot");
  if (!dot) return;
  applyTheme((dot.dataset.theme ?? "volt") as Theme);
  saveSettings();
});

function currentTheme(): Theme {
  return (document.documentElement.dataset.theme ?? "volt") as Theme;
}

// ---- settings persistence --------------------------------------------------

const SETTINGS_KEY = "veloci.settings.v1";

function loadSettings() {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    if (!raw) return;
    const s = JSON.parse(raw);
    if (typeof s.destDir === "string") destDirInput.value = s.destDir;
    if (typeof s.concurrency === "number") concurrencyInput.value = String(s.concurrency);
    if (typeof s.quality === "string") {
      qualitySelect.value = s.quality;
      qualitySelect.dispatchEvent(new Event("change"));
    }
    if (typeof s.rateLimit === "number" && s.rateLimit > 0) rateLimitInput.value = String(s.rateLimit);
    if (typeof s.theme === "string" && (THEMES as readonly string[]).includes(s.theme)) {
      applyTheme(s.theme as Theme);
    }
    if (typeof s.pageSize === "number" && (PAGE_SIZES as readonly number[]).includes(s.pageSize)) {
      pageSize = s.pageSize;
      pageSizeSelect.value = String(s.pageSize);
      pageSizeSelect.dispatchEvent(new Event("change"));
    }
  } catch {
    /* corrupt settings are not worth surfacing -- fall back to defaults */
  }
}

function saveSettings() {
  localStorage.setItem(
    SETTINGS_KEY,
    JSON.stringify({
      destDir: destDirInput.value.trim(),
      concurrency: Number(concurrencyInput.value) || 4,
      quality: qualitySelect.value,
      rateLimit: Number(rateLimitInput.value) || 0,
      theme: currentTheme(),
      pageSize,
    })
  );
}

for (const el of [destDirInput, concurrencyInput, qualitySelect, rateLimitInput]) {
  el.addEventListener("change", saveSettings);
}
destDirInput.addEventListener("input", saveSettings);

// yt-dlp wants e.g. "2.5M"; empty/zero means no cap (omit the flag).
function rateLimitArg(): string | null {
  const mbps = Number(rateLimitInput.value);
  if (!mbps || mbps <= 0) return null;
  return `${mbps}M`;
}

// ---- helpers ---------------------------------------------------------------

function sendCommand(payload: Record<string, unknown>) {
  return invoke("send_engine_command", { payload });
}

function formatDuration(seconds: number | null): string {
  if (seconds == null) return "-";
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}:${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function formatBytes(bytes: number | null): string {
  if (bytes == null || Number.isNaN(bytes) || bytes < 0) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
}

// yt-dlp's human-readable size/rate strings look like "10.52MiB" or
// "512.00KiB/s" -- parse back to raw bytes so the sidebar throughput stat can
// sum across every active download instead of just showing one row's number.
function parseHumanBytes(text: string | null): number | null {
  if (!text) return null;
  const match = text.trim().match(/^([\d.]+)\s*([KMGT]?i?B)(?:\/s)?$/i);
  if (!match) return null;
  const value = parseFloat(match[1]);
  if (Number.isNaN(value)) return null;
  const unit = match[2].toUpperCase(); // "B", "KIB", "MB", "GIB", ...
  const multipliers: Record<string, number> = {
    B: 1,
    KIB: 1024,
    KB: 1000,
    MIB: 1024 ** 2,
    MB: 1000 ** 2,
    GIB: 1024 ** 3,
    GB: 1000 ** 3,
    TIB: 1024 ** 4,
    TB: 1000 ** 4,
  };
  return value * (multipliers[unit] ?? 1);
}

function parsePercent(text: string | null): number | null {
  if (!text) return null;
  const value = parseFloat(text.replace("%", "").trim());
  return Number.isNaN(value) ? null : value;
}

function statusLabel(status: VideoStatus): string {
  switch (status) {
    case "queued":
      return "Queued";
    case "downloading":
      return "Downloading";
    case "done":
      return "Done";
    case "failed":
      return "Failed";
    case "cancelled":
      return "Cancelled";
    case "paused":
      return "Paused";
  }
}

// yt-dlp reports "N/A" (sometimes "NA") for the total when a server doesn't
// send Content-Length -- common for direct-media links on older WordPress
// sites. Percent then pins at "0.0%" forever even though bytes are actively
// flowing, which reads as a stuck/runaway download. Detect that and switch
// to an indeterminate (unknown-length) bar instead of a percent-based one.
function hasKnownTotal(total: string | null): boolean {
  if (!total) return false;
  const trimmed = total.trim().toUpperCase();
  return trimmed !== "N/A" && trimmed !== "NA" && trimmed !== "UNKNOWN";
}

// ---- toasts -----------------------------------------------------------------

const TOAST_ICONS: Record<string, string> = {
  success: `<svg class="toast-icon" viewBox="0 0 20 20" width="15" height="15" aria-hidden="true"><circle cx="10" cy="10" r="8" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="m6.5 10.5 2.3 2.3 4.7-5.3" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  error: `<svg class="toast-icon" viewBox="0 0 20 20" width="15" height="15" aria-hidden="true"><circle cx="10" cy="10" r="8" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M7.2 7.2l5.6 5.6m0-5.6-5.6 5.6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>`,
  info: `<svg class="toast-icon" viewBox="0 0 20 20" width="15" height="15" aria-hidden="true"><circle cx="10" cy="10" r="8" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M10 9v5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/><circle cx="10" cy="6.2" r="1" fill="currentColor"/></svg>`,
};

function showToast(kind: "success" | "error" | "info", title: string, body?: string) {
  const toast = document.createElement("div");
  toast.className = `toast toast-${kind}`;
  toast.innerHTML = `${TOAST_ICONS[kind]}<span><span class="toast-title"></span><span class="toast-body"></span></span>`;
  toast.querySelector(".toast-title")!.textContent = title;
  const bodyEl = toast.querySelector<HTMLElement>(".toast-body")!;
  if (body) bodyEl.textContent = body;
  else bodyEl.remove();
  toastStack.appendChild(toast);
  // Cap the visible stack so a burst of completions doesn't flood the corner.
  while (toastStack.children.length > 4) toastStack.firstElementChild!.remove();
  setTimeout(() => {
    toast.classList.add("leaving");
    toast.addEventListener("animationend", () => toast.remove(), { once: true });
  }, 4200);
}

// Two-note completion chime, synthesized on the fly (no audio asset needed).
function playChime() {
  try {
    const ctx = new AudioContext();
    const gain = ctx.createGain();
    gain.gain.value = 0.05;
    gain.connect(ctx.destination);
    [660, 990].forEach((freq, i) => {
      const osc = ctx.createOscillator();
      osc.type = "sine";
      osc.frequency.value = freq;
      osc.connect(gain);
      osc.start(ctx.currentTime + i * 0.18);
      osc.stop(ctx.currentTime + i * 0.18 + 0.16);
    });
    setTimeout(() => ctx.close().catch(() => {}), 900);
  } catch {
    /* audio unavailable -- the toast still shows */
  }
}

// ---- engine status ----------------------------------------------------------

let engineOnline = false;

function setEngineState(state: "starting" | "online" | "error", label: string) {
  engineStatus.dataset.state = state;
  engineStatusText.textContent = label;
}

function markEngineOnline() {
  if (!engineOnline) {
    engineOnline = true;
    setEngineState("online", "Engine online");
  }
}

// ---- rendering ---------------------------------------------------------------

function matchesFilter(row: VideoRow): boolean {
  switch (currentFilter) {
    case "all":
      return true;
    case "issues":
      return row.status === "failed" || row.status === "cancelled";
    default:
      return row.status === currentFilter;
  }
}

function matchesSearch(row: VideoRow): boolean {
  if (!searchQuery) return true;
  const haystack = `${row.title ?? ""} ${row.url}`.toLowerCase();
  return haystack.includes(searchQuery);
}

// Filter/search changes always jump back to page 1 -- staying on, say,
// page 4 of the old view when the underlying set just changed size would
// often land on a page that no longer exists.
function refreshVisibility() {
  currentPage = 1;
  listDirty = true;
  renderCurrentPage();
}

// Reads filteredUrls (already recomputed by whatever called into
// renderCurrentPage before this) rather than re-scanning every video --
// pagination already did that work.
function updateEmptyState() {
  const anyVisible = filteredUrls.length > 0;
  emptyState.classList.toggle("visible", !anyVisible);
  if (anyVisible) return;
  if (videos.size === 0) {
    emptyStateText.textContent = "Paste a listing URL above and hit Scan to get started.";
  } else if (searchQuery) {
    emptyStateText.textContent = "No videos match your search.";
  } else {
    emptyStateText.textContent = "Nothing in this view yet.";
  }
}

const ICONS = {
  download: `<svg viewBox="0 0 20 20" width="13" height="13" aria-hidden="true"><path d="M10 3v10m0 0 4-4m-4 4-4-4M4 16h12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" /></svg>`,
  resume: `<svg viewBox="0 0 20 20" width="13" height="13" aria-hidden="true"><path d="M6 4.5v11l9-5.5-9-5.5Z" fill="currentColor" /></svg>`,
  pause: `<svg viewBox="0 0 20 20" width="13" height="13" aria-hidden="true"><rect x="6" y="4.5" width="2.6" height="11" rx="0.8" fill="currentColor" /><rect x="11.4" y="4.5" width="2.6" height="11" rx="0.8" fill="currentColor" /></svg>`,
  cancel: `<svg viewBox="0 0 20 20" width="13" height="13" aria-hidden="true"><path d="M5 5l10 10M15 5 5 15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" /></svg>`,
  del: `<svg viewBox="0 0 20 20" width="13" height="13" aria-hidden="true"><path d="M5 6h10M8 6V4.5a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1V6m-7 0 .6 9.4a1 1 0 0 0 1 .9h5.8a1 1 0 0 0 1-.9L15 6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" /></svg>`,
  reveal: `<svg viewBox="0 0 20 20" width="13" height="13" aria-hidden="true"><path d="M3 6.5A1.5 1.5 0 0 1 4.5 5h3l1.5 2h6.5A1.5 1.5 0 0 1 17 8.5v5A1.5 1.5 0 0 1 15.5 15h-11A1.5 1.5 0 0 1 3 13.5v-7Z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" /></svg>`,
  copy: `<svg viewBox="0 0 20 20" width="13" height="13" aria-hidden="true"><rect x="7" y="7" width="9" height="9" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M13 7V5.5A1.5 1.5 0 0 0 11.5 4h-6A1.5 1.5 0 0 0 4 5.5v6A1.5 1.5 0 0 0 5.5 13H7" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>`,
};

// Static markup only (no user-controlled strings) -- safe for innerHTML.
function actionButton(action: string, cls: string, title: string, icon: string): string {
  return `<button class="icon-btn ${cls}" data-action="${action}" title="${title}">${icon}</button>`;
}

function buildRowSkeleton(el: HTMLDivElement): RowCells {
  el.innerHTML = `
    <span class="col-index"><span class="index-badge"></span></span>
    <span class="col-title">
      <img class="video-thumb" loading="lazy" alt="" hidden />
      <span class="col-title-text">
        <span class="video-title"></span>
        <span class="video-url" title="Open the source page in your browser"></span>
      </span>
    </span>
    <span class="col-size"></span>
    <span class="col-duration"></span>
    <span class="col-progress"></span>
    <span class="col-status"></span>
    <span class="col-actions"></span>
  `;
  const thumb = el.querySelector<HTMLImageElement>(".video-thumb")!;
  // Some hosts 403 hotlinked images -- just collapse the slot instead of
  // showing a broken-image glyph.
  thumb.addEventListener("error", () => {
    thumb.hidden = true;
  });
  return {
    indexBadge: el.querySelector(".index-badge")!,
    thumb,
    title: el.querySelector(".video-title")!,
    url: el.querySelector(".video-url")!,
    size: el.querySelector(".col-size")!,
    duration: el.querySelector(".col-duration")!,
    progress: el.querySelector(".col-progress")!,
    status: el.querySelector(".col-status")!,
    actions: el.querySelector(".col-actions")!,
  };
}

// Title/URL/size/duration text. textContent assignment is markup-inert, so
// titles scraped from third-party pages can't inject anything.
function updateRowMeta(row: VideoRow) {
  const label = row.title ?? (row.url.length > 80 ? row.url.slice(0, 77) + "..." : row.url);
  row.cells.indexBadge.textContent = String(row.index);
  row.cells.title.textContent = label;
  row.cells.title.title = row.title ?? row.url;
  row.cells.url.textContent = row.url;
  row.cells.size.textContent = formatBytes(row.filesize ?? parseHumanBytes(row.total));
  row.cells.duration.textContent = formatDuration(row.duration);
  if (row.thumbnail && row.cells.thumb.src !== row.thumbnail) {
    row.cells.thumb.src = row.thumbnail;
    row.cells.thumb.hidden = false;
  }
}

function rebuildStatusAndActions(row: VideoRow) {
  row.el.dataset.status = row.status;
  row.cells.status.innerHTML = `<span class="status-pill status-${row.status}">${statusLabel(row.status)}</span>`;

  const canDownload = row.status === "queued" || row.status === "failed" || row.status === "cancelled";
  const canResume = row.status === "paused";
  const canPauseVideo = row.status === "downloading";
  const canCancel = row.status === "downloading";
  const canDelete = row.status !== "downloading";
  const canReveal = row.status === "done" && !!row.destPath;

  row.cells.actions.innerHTML = `
    ${canDownload ? actionButton("download", "icon-btn-download", "Download this video", ICONS.download) : ""}
    ${canResume ? actionButton("resume", "icon-btn-resume", "Resume this download", ICONS.resume) : ""}
    ${canPauseVideo ? actionButton("pause_video", "icon-btn-pause", "Pause this download", ICONS.pause) : ""}
    ${canCancel ? actionButton("cancel", "icon-btn-cancel", "Cancel this download", ICONS.cancel) : ""}
    ${canReveal ? actionButton("reveal", "icon-btn-reveal", "Show file in folder", ICONS.reveal) : ""}
    ${actionButton("copy", "icon-btn-copy", "Copy video page link", ICONS.copy)}
    ${canDelete ? actionButton("delete", "icon-btn-delete", "Remove this video from the list", ICONS.del) : ""}
  `;
  row.renderedStatus = row.status;
  row.renderedDestPath = row.destPath;
}

function progressDetailText(row: VideoRow, known: boolean): string {
  if (known) {
    return row.downloaded && row.total ? `${row.downloaded} / ${row.total}` : row.downloaded ?? "";
  }
  return row.downloaded ? `${row.downloaded} downloaded (size unknown)` : "size unknown";
}

// Patch the already-built progress bar in place (fast path: several events
// per second per active download hit this).
function patchProgressCell(row: VideoRow) {
  const known = hasKnownTotal(row.total);
  const wantMode = known ? "bar" : "indet";
  if (row.progressMode !== wantMode || !row.prog) {
    rebuildProgressCell(row);
    return;
  }
  const pct = Math.max(0, Math.min(100, row.percent ?? 0));
  if (row.prog.fill && known) row.prog.fill.style.width = `${pct}%`;
  if (row.prog.pct) row.prog.pct.textContent = `${pct.toFixed(1)}%`;
  if (row.prog.speed) row.prog.speed.textContent = row.speed ?? "-";
  if (row.prog.detail) row.prog.detail.textContent = progressDetailText(row, known);
  if (row.prog.eta) row.prog.eta.textContent = `ETA ${row.eta ?? "-"}`;
}

function rebuildProgressCell(row: VideoRow) {
  const cell = row.cells.progress;
  if (row.status === "downloading") {
    const known = hasKnownTotal(row.total);
    row.progressMode = known ? "bar" : "indet";
    cell.innerHTML = known
      ? `<div class="progress-track"><div class="progress-fill"></div><span class="progress-pct"></span></div>
         <div class="progress-meta"><span class="progress-speed"></span><span class="progress-detail"></span><span class="progress-eta"></span></div>`
      : `<div class="progress-track progress-track-indeterminate"><div class="progress-fill progress-fill-indeterminate"></div></div>
         <div class="progress-meta"><span class="progress-speed"></span><span class="progress-detail"></span></div>`;
    row.prog = {
      fill: cell.querySelector(".progress-fill"),
      pct: cell.querySelector(".progress-pct"),
      speed: cell.querySelector(".progress-speed"),
      detail: cell.querySelector(".progress-detail"),
      eta: cell.querySelector(".progress-eta"),
    };
    patchProgressCell(row);
    return;
  }

  row.progressMode = "static";
  row.prog = null;
  const div = document.createElement("div");
  if (row.status === "done") {
    div.className = "progress-static progress-static-done";
    div.textContent = "Complete";
  } else if (row.status === "failed") {
    div.className = "progress-static progress-static-failed";
    div.title = row.error ?? "";
    div.textContent = row.error ? row.error.slice(0, 90) : "Download failed";
  } else if (row.status === "cancelled") {
    div.className = "progress-static progress-static-cancelled";
    div.textContent = "Cancelled";
  } else if (row.status === "paused") {
    const detail =
      row.downloaded && hasKnownTotal(row.total) ? `${row.downloaded} / ${row.total}` : row.downloaded ?? "";
    div.className = "progress-static progress-static-paused";
    div.textContent = `Paused${detail ? ": " + detail : ""}`;
  } else {
    div.className = "progress-static progress-static-queued";
    div.textContent = "Waiting…";
  }
  cell.replaceChildren(div);
}

// "total" deliberately isn't here even though a "progress" event carries it:
// it's not part of matchesSearch's haystack and isn't shown by
// updateRowMeta (size comes from filesize), so treating it as "metadata"
// made metaTouched (below) true on every single progress tick instead of
// just real title/duration/filesize/thumbnail arrivals.
const META_KEYS = ["title", "duration", "filesize", "thumbnail"] as const;
const PROGRESS_KEYS = ["percent", "speed", "eta", "downloaded", "total"] as const;

// batchIndex only drives the staggered entrance animation. autoRepaginate
// lets a batch insert (a scan streaming in dozens/hundreds of rows) defer
// the (relatively) expensive filteredUrls recompute + page render until the
// whole batch has landed, instead of doing it once per row.
function upsertVideo(
  url: string,
  patch: Partial<Omit<VideoRow, "url" | "el" | "cells" | "index">>,
  batchIndex = 0,
  autoRepaginate = true
) {
  let row = videos.get(url);
  let created = false;
  if (!row) {
    created = true;
    const el = document.createElement("div");
    el.className = "video-row";
    el.setAttribute("role", "row");
    el.dataset.url = url;
    // Staggered entrance for batch inserts (scan results cascading in);
    // capped so late rows in a 500-item scan don't wait seconds to appear.
    el.style.animationDelay = `${Math.min(batchIndex * 18, 420)}ms`;
    row = {
      url,
      index: videos.size + 1,
      status: "queued",
      title: null,
      duration: null,
      filesize: null,
      thumbnail: null,
      destPath: null,
      error: null,
      percent: null,
      speed: null,
      eta: null,
      downloaded: null,
      total: null,
      el,
      cells: buildRowSkeleton(el),
      renderedStatus: null,
      renderedDestPath: null,
      progressMode: null,
      prog: null,
    };
    videos.set(url, row);
    // Not attached anywhere yet -- renderCurrentPage() below decides whether
    // this row's page is the one currently on screen and mounts it there.
  }

  Object.assign(row, patch);

  const statusChanged = row.status !== row.renderedStatus;
  const revealChanged = row.status === "done" && row.destPath !== row.renderedDestPath;
  const metaTouched = META_KEYS.some((k) => k in patch);

  // Cell patches only matter for a row that's actually mounted on the
  // current page right now -- an off-page row doing this work every
  // progress tick was pure waste nobody could see. Once it's paginated
  // back into view, renderCurrentPage() gives it a full refresh anyway, so
  // nothing is lost by skipping this while it's off-page.
  if (row.el.isConnected) {
    if (created || metaTouched) updateRowMeta(row);
    if (created || statusChanged || revealChanged) {
      rebuildStatusAndActions(row);
      rebuildProgressCell(row);
    } else if (row.status === "downloading" && PROGRESS_KEYS.some((k) => k in patch)) {
      patchProgressCell(row);
    }
  }

  // A new row, a status change, or (only while actively searching) a title
  // arriving late can all change which page this row belongs on or whether
  // it matches the current filter/search at all -- anything else (plain
  // progress ticks) can't move a row between pages.
  const needsRepaginate = created || statusChanged || revealChanged || (searchQuery !== "" && metaTouched);
  if (needsRepaginate) {
    listDirty = true;
    if (autoRepaginate) renderCurrentPage();
  }

  markStatsDirty();
}

// Clearing hundreds of finished videos fires one remove_video command per
// row, each answered by its own "removed" event -- removeVideo used to
// renumber (O(n)) and fully re-render on every single one of those,
// turning a bulk clear into O(n^2) work and a multi-second freeze. Instead,
// just mark the batch dirty here and let the same 250ms tick that already
// flushes stats (below) coalesce however many removals landed since the
// last flush into one renumber + one render.
let removalsPending = false;

function removeVideo(url: string) {
  if (!videos.has(url)) return;
  videos.delete(url);
  removalsPending = true;
  markStatsDirty();
}

function flushRemovals() {
  if (!removalsPending) return;
  removalsPending = false;
  // Renumber the remaining rows so their index badges stay a contiguous
  // 1..N sequence -- only the JS field here; renderCurrentPage's per-row
  // refresh (below) writes the actual badge text for whichever of them are
  // currently on-page.
  let i = 1;
  for (const remaining of videos.values()) {
    remaining.index = i;
    i += 1;
  }
  listDirty = true;
  renderCurrentPage();
}

function clearVideoList() {
  videos.clear();
  currentPage = 1;
  listDirty = true;
  renderCurrentPage();
  markStatsDirty();
}

// ---- stats (batched: recomputed at most 4x/sec no matter how fast progress
// events arrive) ---------------------------------------------------------------

let statsDirty = false;
let lastTotalSpeed = 0;
let peakSpeed = 0;

function markStatsDirty() {
  statsDirty = true;
}

function setCount(el: HTMLElement, value: number) {
  const text = String(value);
  if (el.textContent === text) return;
  el.textContent = text;
  el.classList.remove("bump");
  void el.offsetWidth; // restart the pop animation
  el.classList.add("bump");
}

function flushStats() {
  statsDirty = false;
  const counts: Record<VideoStatus, number> = {
    queued: 0,
    downloading: 0,
    done: 0,
    failed: 0,
    cancelled: 0,
    paused: 0,
  };
  let totalSpeed = 0;
  let totalSize = 0;
  let anySize = false;
  for (const row of videos.values()) {
    counts[row.status] += 1;
    if (row.status === "downloading") {
      // row.speed is yt-dlp's own "_speed_str", which already ends in "/s"
      // (e.g. "1.05MiB/s") -- parseHumanBytes strips that suffix itself.
      const bytesPerSec = parseHumanBytes(row.speed);
      if (bytesPerSec != null) totalSpeed += bytesPerSec;
    }
    if (row.filesize != null) {
      totalSize += row.filesize;
      anySize = true;
    }
  }
  setCount(navCountEls.all, videos.size);
  setCount(navCountEls.queued, counts.queued);
  setCount(navCountEls.downloading, counts.downloading);
  setCount(navCountEls.done, counts.done);
  setCount(navCountEls.paused, counts.paused);
  setCount(navCountEls.issues, counts.failed + counts.cancelled);
  statSpeedEl.textContent = `${formatBytes(totalSpeed)}/s`;
  statSizeEl.textContent = anySize ? formatBytes(totalSize) : "-";
  lastTotalSpeed = totalSpeed;
  if (totalSpeed > peakSpeed) {
    peakSpeed = totalSpeed;
    statPeakEl.textContent = `${formatBytes(peakSpeed)}/s`;
  }
  updateEmptyState();
}

setInterval(() => {
  if (removalsPending) flushRemovals();
  if (statsDirty) flushStats();
}, 250);

// ---- throughput sparkline (last 48 seconds, 1 sample/sec) ---------------------

const SPARK_LEN = 48;
const sparkSamples: number[] = new Array(SPARK_LEN).fill(0);

function renderSparkline() {
  const max = Math.max(...sparkSamples, 1);
  const pts: string[] = [];
  for (let i = 0; i < SPARK_LEN; i++) {
    const x = (i / (SPARK_LEN - 1)) * 96;
    const y = 27 - (sparkSamples[i] / max) * 25;
    pts.push(`${x.toFixed(1)},${y.toFixed(1)}`);
  }
  const line = pts.join(" ");
  sparklineLine.setAttribute("points", line);
  sparklineFill.setAttribute("points", `0,28 ${line} 96,28`);
}

setInterval(() => {
  sparkSamples.push(lastTotalSpeed);
  sparkSamples.shift();
  renderSparkline();
}, 1000);
renderSparkline();

// ---- sidebar navigation -------------------------------------------------------

nav.addEventListener("click", (event) => {
  const button = (event.target as HTMLElement).closest<HTMLButtonElement>(".nav-item");
  if (!button) return;
  currentFilter = (button.dataset.filter ?? "all") as Filter;
  for (const item of nav.querySelectorAll(".nav-item")) {
    item.classList.toggle("active", item === button);
  }
  refreshVisibility();
});

// ---- search ---------------------------------------------------------------------

searchInput.addEventListener("input", () => {
  searchQuery = searchInput.value.trim().toLowerCase();
  refreshVisibility();
});

searchInput.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    searchInput.value = "";
    searchQuery = "";
    refreshVisibility();
    searchInput.blur();
  }
});

// "/" from anywhere jumps to the search box (unless already typing somewhere).
document.addEventListener("keydown", (event) => {
  if (event.key !== "/") return;
  const active = document.activeElement;
  if (active instanceof HTMLInputElement || active instanceof HTMLSelectElement) return;
  event.preventDefault();
  searchInput.focus();
});

// ---- toolbar actions --------------------------------------------------------------

function setScanBusy(busy: boolean) {
  scanning = busy;
  fetchBtn.disabled = busy;
  fetchBtn.innerHTML = busy ? `<span class="spinner"></span>Scanning` : "Scan";
}

// "3" -> [3,3]; "2-10" -> [2,10]. Anything else (including empty) means
// "just scan the pasted URL as-is". Capped at 50 pages per run.
function parsePageRange(text: string): [number, number] | null {
  const match = text.trim().match(/^(\d+)(?:\s*-\s*(\d+))?$/);
  if (!match) return null;
  const start = parseInt(match[1], 10);
  const end = match[2] ? parseInt(match[2], 10) : start;
  if (start < 1 || end < start) return null;
  return [start, Math.min(end, start + 49)];
}

// WordPress-style pagination (all currently supported listing sites):
// <base>/page/N/. Page 1 is the bare base URL.
function pageUrl(base: string, n: number): string {
  const stripped = base.replace(/\/page\/\d+\/?$/, "/");
  if (n === 1) return stripped;
  return stripped.replace(/\/?$/, "/") + `page/${n}/`;
}

// A multi-page scan sends one add_listing per page; each answers with its own
// crawled_urls (or error) event, so busy-state clears when all have reported.
let expectedScanReplies = 0;
let receivedScanReplies = 0;

function noteScanReply() {
  receivedScanReplies += 1;
  if (receivedScanReplies >= expectedScanReplies) {
    setScanBusy(false);
    if (expectedScanReplies > 1) {
      showToast("success", "Scan complete", `${videos.size} video(s) across ${expectedScanReplies} pages.`);
    }
  }
}

async function startScan() {
  const url = listingUrlInput.value.trim();
  if (!url || scanning) return;
  const range = parsePageRange(pagesRangeInput.value);
  const pages: string[] = [];
  if (range) {
    for (let n = range[0]; n <= range[1]; n++) pages.push(pageUrl(url, n));
  } else {
    pages.push(url);
  }

  expectedScanReplies = pages.length;
  receivedScanReplies = 0;
  setScanBusy(true);
  // Each Scan starts a fresh view of just that run's videos -- otherwise
  // every previously scanned listing's rows would just keep piling up here
  // forever. The backend queue/history is untouched, only this list clears.
  clearVideoList();
  statusMsg.textContent = pages.length > 1 ? `Scanning ${pages.length} pages...` : "Scanning listing page...";
  try {
    // No max_items cap: let the extractor return everything it finds on the
    // page (bounded only by its own pagination safety net), rather than
    // silently truncating pages with more videos than some arbitrary limit.
    // The engine processes these sequentially, streaming results per page.
    for (const pageTarget of pages) {
      await sendCommand({ cmd: "add_listing", url: pageTarget });
    }
  } catch (err) {
    statusMsg.textContent = `Scan failed: ${err}`;
    showToast("error", "Scan failed", String(err));
    setScanBusy(false);
  }
}

fetchBtn.addEventListener("click", startScan);
listingUrlInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") startScan();
});

browseBtn.addEventListener("click", async () => {
  // Native folder picker via the dialog plugin. Invoked directly (the
  // @tauri-apps/plugin-dialog npm package is only a thin wrapper around this
  // same invoke call). Returns null when the user cancels.
  try {
    const selected = await invoke<string | null>("plugin:dialog|open", {
      options: {
        directory: true,
        multiple: false,
        title: "Choose the download folder",
        defaultPath: destDirInput.value.trim() || undefined,
      },
    });
    if (typeof selected === "string" && selected) {
      destDirInput.value = selected;
      saveSettings();
    }
  } catch (err) {
    showToast("error", "Couldn't open the folder picker", String(err));
  }
});

function requireDestDir(): string | null {
  const destDir = destDirInput.value.trim();
  if (!destDir) {
    statusMsg.textContent = "Set a destination folder first.";
    destDirInput.focus();
    return null;
  }
  return destDir;
}

downloadBtn.addEventListener("click", async () => {
  const destDir = requireDestDir();
  if (!destDir) return;
  const concurrency = Number(concurrencyInput.value) || 4;
  statusMsg.textContent = "Downloading queued videos...";
  try {
    await sendCommand({
      cmd: "start_downloads",
      dest_dir: destDir,
      concurrency,
      quality: qualitySelect.value,
      rate_limit: rateLimitArg(),
    });
  } catch (err) {
    statusMsg.textContent = `Download failed to start: ${err}`;
  }
});

pauseBtn.addEventListener("click", async () => {
  try {
    await sendCommand({ cmd: "pause" });
  } catch (err) {
    statusMsg.textContent = `Pause failed: ${err}`;
  }
});

retryFailedBtn.addEventListener("click", async () => {
  const destDir = requireDestDir();
  if (!destDir) return;
  const retryable = [...videos.values()].filter(
    (row) => row.status === "failed" || row.status === "cancelled"
  );
  if (retryable.length === 0) {
    statusMsg.textContent = "No failed or cancelled videos to retry.";
    return;
  }
  statusMsg.textContent = `Retrying ${retryable.length} video(s)...`;
  showToast("info", "Retrying downloads", `${retryable.length} failed/cancelled video(s) re-queued.`);
  for (const row of retryable) {
    await sendCommand({
      cmd: "download_single",
      url: row.url,
      dest_dir: destDir,
      concurrent_fragments: 4,
      quality: qualitySelect.value,
      rate_limit: rateLimitArg(),
    });
  }
});

clearBtn.addEventListener("click", async () => {
  // Active downloads are left alone -- only rows that aren't currently
  // downloading get cleared, so this can't orphan an in-flight yt-dlp
  // process. Each removal round-trips through the backend (remove_video)
  // so the queue database and the visible list stay in sync.
  const removable = [...videos.values()].filter((row) => row.status !== "downloading");
  if (removable.length === 0) {
    statusMsg.textContent = "Nothing to clear.";
    return;
  }
  statusMsg.textContent = `Clearing ${removable.length} video(s)...`;
  await Promise.all(removable.map((row) => sendCommand({ cmd: "remove_video", url: row.url })));
  statusMsg.textContent = `Cleared ${removable.length} video(s).`;
});

// ---- per-row actions ----------------------------------------------------------

// Rows are created dynamically, so a single delegated listener on the list
// container handles clicks for every row's action buttons and source links.
videoList.addEventListener("click", async (event) => {
  const target = event.target as HTMLElement;
  const rowEl = target.closest<HTMLElement>(".video-row");
  const url = rowEl?.dataset.url;
  if (!url) return;

  const button = target.closest<HTMLButtonElement>("button[data-action]");
  if (!button) {
    // Clicking the little URL line opens the source page in the default
    // browser -- handy for eyeballing what a video actually is.
    if (target.closest(".video-url")) {
      openUrl(url).catch(() => showToast("error", "Couldn't open the page", url));
    }
    return;
  }

  const action = button.dataset.action;
  if (action === "download" || action === "resume") {
    // Resuming a paused video is just re-issuing the same download command --
    // yt-dlp's own --continue default picks up from the partial file already
    // on disk rather than starting over.
    const destDir = requireDestDir();
    if (!destDir) return;
    await sendCommand({
      cmd: "download_single",
      url,
      dest_dir: destDir,
      concurrent_fragments: 4,
      quality: qualitySelect.value,
      rate_limit: rateLimitArg(),
    });
  } else if (action === "cancel") {
    await sendCommand({ cmd: "cancel", url });
  } else if (action === "pause_video") {
    await sendCommand({ cmd: "pause_video", url });
  } else if (action === "delete") {
    await sendCommand({ cmd: "remove_video", url });
  } else if (action === "reveal") {
    const row = videos.get(url);
    if (row?.destPath) {
      try {
        await revealItemInDir(row.destPath);
      } catch (err) {
        showToast("error", "Couldn't open folder", String(err));
      }
    }
  } else if (action === "copy") {
    try {
      await navigator.clipboard.writeText(url);
      showToast("info", "Link copied", url);
    } catch {
      statusMsg.textContent = "Couldn't access the clipboard.";
    }
  }
});

// ---- engine events --------------------------------------------------------------

listen<Record<string, any>>("engine-event", (event) => {
  markEngineOnline();
  const payload = event.payload;
  switch (payload.event) {
    case "crawled_urls": {
      // Prefer the enriched per-video records (status + metadata from the
      // queue DB) so videos downloaded in earlier sessions show up as Done
      // with their title/thumbnail instead of silently re-queueing.
      const items: Array<Record<string, any>> =
        payload.videos ?? (payload.urls ?? []).map((u: string) => ({ url: u, status: "queued" }));
      // Batch insert: repagination and stats are deferred until every row
      // in this batch is in, rather than paying for a full page render on
      // every single one of a scan's (possibly hundreds of) results.
      items.forEach((v, i) =>
        upsertVideo(
          v.url,
          {
            status: (v.status ?? "queued") as VideoStatus,
            title: v.title ?? null,
            duration: v.duration ?? null,
            filesize: v.filesize ?? null,
            thumbnail: v.thumbnail ?? null,
            destPath: v.dest_path ?? null,
          },
          i,
          false
        )
      );
      listDirty = true;
      renderCurrentPage();
      flushStats();
      const known = items.filter((v) => v.status && v.status !== "queued").length;
      statusMsg.textContent =
        `Scanned listing: found ${payload.found}, added ${payload.added} new` +
        (known > 0 ? `, ${known} already known.` : ".");
      if (expectedScanReplies <= 1) {
        showToast("success", "Scan complete", `Found ${payload.found} video(s).`);
      }
      noteScanReply();
      break;
    }
    case "video_list": {
      // Fires with the *entire* historical queue (every video ever scanned,
      // across every session) -- same batching reasoning as crawled_urls:
      // one repagination at the end, not one per row.
      const rows: Array<Record<string, any>> = payload.videos ?? [];
      rows.forEach((v, i) =>
        upsertVideo(
          v.url,
          {
            status: v.status,
            destPath: v.dest_path,
            error: v.error,
            title: v.title,
            duration: v.duration,
            filesize: v.filesize,
            thumbnail: v.thumbnail,
          },
          i,
          false
        )
      );
      listDirty = true;
      renderCurrentPage();
      flushStats();
      break;
    }
    case "metadata": {
      upsertVideo(payload.url, {
        title: payload.title,
        duration: payload.duration,
        filesize: payload.filesize,
        thumbnail: payload.thumbnail,
      });
      break;
    }
    case "progress": {
      upsertVideo(payload.url, {
        status: "downloading",
        percent: parsePercent(payload.percent),
        speed: payload.speed,
        eta: payload.eta,
        downloaded: payload.downloaded,
        total: payload.total,
      });
      break;
    }
    case "download_done": {
      upsertVideo(payload.url, { status: "done", destPath: payload.dest_path, percent: 100 });
      const row = videos.get(payload.url);
      showToast("success", "Download complete", row?.title ?? payload.url);
      break;
    }
    case "download_failed": {
      upsertVideo(payload.url, { status: "failed", error: payload.error });
      const row = videos.get(payload.url);
      showToast("error", "Download failed", row?.title ?? payload.url);
      break;
    }
    case "cancelled": {
      upsertVideo(payload.url, { status: "cancelled" });
      break;
    }
    case "video_paused": {
      upsertVideo(payload.url, { status: "paused" });
      break;
    }
    case "removed": {
      removeVideo(payload.url);
      break;
    }
    case "paused": {
      statusMsg.textContent = "Paused: all active downloads stopped.";
      break;
    }
    case "queue_finished": {
      const done = payload.done ?? 0;
      const failed = payload.failed ?? 0;
      if (done + failed > 0) {
        showToast(
          failed > 0 ? "error" : "success",
          "Queue finished",
          `${done} completed${failed > 0 ? `, ${failed} failed` : ""}.`
        );
        playChime();
        // Flash the taskbar icon so a minimized/backgrounded app still
        // gets the user's eye.
        appWindow?.requestUserAttention(UserAttentionType.Informational).catch(() => {});
      }
      break;
    }
    case "error": {
      statusMsg.textContent = `Error: ${payload.message}`;
      // A scan page that errors out never emits crawled_urls, so count it
      // as a reply -- otherwise a multi-page scan with one dead page would
      // leave the Scan button stuck forever.
      if (scanning) {
        showToast("error", "Scan problem", payload.message);
        noteScanReply();
      }
      break;
    }
  }
});

listen<string>("engine-log", (event) => {
  console.warn("[engine]", event.payload);
});

// ---- startup ---------------------------------------------------------------------

loadSettings();
flushStats();
renderCurrentPage();

// Keep pinging until the engine answers -- it can take a moment to spawn on a
// cold start, and staying stuck on a one-shot check is what previously left
// the status showing unavailable forever.
let enginePingAttempts = 0;
async function pingEngine() {
  try {
    await sendCommand({ cmd: "list_videos" });
    markEngineOnline();
  } catch {
    enginePingAttempts += 1;
    if (enginePingAttempts >= 4) setEngineState("error", "Engine unavailable");
  }
}
pingEngine();
const enginePingTimer = setInterval(() => {
  if (engineOnline) {
    clearInterval(enginePingTimer);
    return;
  }
  pingEngine();
}, 3000);
