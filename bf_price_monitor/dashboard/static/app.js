"use strict";

// Vanilla JS, no build step, no network calls except to this server.
// All text is inserted as text nodes, never as markup.

const TOKEN_HEADER = "x-bf-dashboard-token";
// The token lives in sessionStorage only: it survives a reload in this tab and
// ends with the tab. Every storage call is wrapped; without storage the
// in-memory variable below still works.
const TOKEN_KEY = "bf-dashboard-token";
const TOKEN_REJECTED = "Token missing or wrong. Paste the token from the terminal.";
// Fields the server can change (operations.EDITABLE_FIELDS), in form order.
const EDITABLE = ["target_price", "min_drop_percent", "channels"];
// Shown read-only: they have a runtime consumer but no editor yet.
const LIVE_READONLY = ["seller_policy", "atl_policy", "cooldown_hours"];
// Shown read-only under "not used yet": nothing reads them (design doc 6.1).
const UNUSED = [
  "cadence_minutes",
  "track_all_time_low",
  "drop_rule",
  "drop_threshold",
  "owner",
  "id",
];

let token = "";
let state = null; // last GET /api/watchlist body
let editing = null; // the watch (as loaded) shown in the edit dialog

const $ = (id) => document.getElementById(id);

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value);
  }
  for (const child of children) {
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

// --- talking to the server -------------------------------------------------

async function api(path, method, body) {
  const headers = {};
  const init = { method: method || "GET", headers };
  if (init.method !== "GET") {
    headers["content-type"] = "application/json";
    headers[TOKEN_HEADER] = token;
    init.body = JSON.stringify(body === undefined ? {} : body);
  }
  let response;
  try {
    response = await fetch(path, init);
  } catch (err) {
    return { status: 0, data: { error: "cannot reach the dashboard server" } };
  }
  let data = {};
  try {
    data = await response.json();
  } catch (err) {
    data = { error: "unexpected response" };
  }
  if (init.method !== "GET" && response.status === 403) {
    clearToken(); // wrong or missing: forget it and ask again, no automatic retry
    $("token-box").hidden = false;
  }
  return { status: response.status, data };
}

function saveToken(value) {
  token = value;
  try {
    sessionStorage.setItem(TOKEN_KEY, value);
  } catch (err) {
    // storage unavailable: the in-memory token is enough
  }
}

function clearToken() {
  token = "";
  try {
    sessionStorage.removeItem(TOKEN_KEY);
  } catch (err) {
    // nothing saved, nothing to clear
  }
}

function readTokenFromUrl() {
  const match = /(?:^|[#&])token=([\w-]+)/.exec(location.hash);
  if (match) {
    saveToken(match[1]);
    // keep the token out of history and the address bar
    history.replaceState(null, "", location.pathname + location.search);
    return;
  }
  try {
    token = sessionStorage.getItem(TOKEN_KEY) || "";
  } catch (err) {
    token = "";
  }
}

// --- banners ---------------------------------------------------------------

function banner(kind, text, ...actions) {
  $("banners").append(h("div", { class: "banner " + kind, role: "status" }, text, ...actions));
}

function clearBanners() {
  $("banners").replaceChildren();
}

function button(label, onclick) {
  return h("button", { type: "button", onclick }, label);
}

function describeResult(status, data) {
  if (status === 200) return null;
  if (status === 202) {
    const cause = data.reason || data.detail;
    const why = cause ? " (" + cause + ")" : "";
    banner(
      "warn",
      "Saved locally, not pushed" + why + ". Your change is queued and nothing is lost.",
      button("Retry push", retry)
    );
    return null;
  }
  if (status === 403) {
    banner("error", TOKEN_REJECTED);
    return TOKEN_REJECTED;
  }
  if (status === 409) {
    banner(
      "error",
      "This watch was changed somewhere else, so your edit was not applied. " +
        "Reload to see the latest version before editing again.",
      button("Reload", reload)
    );
    return "Not saved: the watch changed. Reload and try again.";
  }
  return data.error || "Request failed (" + status + ")";
}

// --- rendering -------------------------------------------------------------

function fmt(value) {
  return value === undefined || value === null ? "–" : String(value);
}

function renderStatus() {
  const clone = state.clone;
  const pill = $("clone-status");
  pill.textContent = clone.ok ? "clone ok (" + clone.branch + " @ " + clone.head + ")" : "clone problem";
  pill.className = "pill " + (clone.ok ? "ok" : "bad");
  clearBanners();
  if (!clone.ok) banner("error", "The dashboard clone cannot be used: " + clone.detail);
  if (state.pending_count > 0) {
    banner(
      "warn",
      "Saved locally, not pushed: " + state.pending_count + " change(s) are waiting.",
      button("Retry push", retry)
    );
  }
  if (!token) $("token-box").hidden = false;
}

function readonlyDetails(watch) {
  const live = LIVE_READONLY.filter((k) => k in watch).map((k) => k + ": " + fmt(watch[k]));
  const unused = UNUSED.filter((k) => k in watch).map((k) => k + ": " + fmt(watch[k]));
  return h(
    "td",
    {},
    h("div", {}, live.join(" · ") || "–"),
    h("details", {}, h("summary", {}, "not used yet"), h("div", { class: "muted" }, unused.join(" · ")))
  );
}

function renderRows() {
  const rows = $("watch-rows");
  rows.replaceChildren();
  $("empty").hidden = state.watches.length > 0;
  for (const watch of state.watches) {
    const on = watch.enabled !== false;
    rows.append(
      h(
        "tr",
        { class: on ? "" : "off" },
        h("td", {}, watch.site),
        h("td", {}, watch.query || watch.direct_url || ""),
        h("td", {}, fmt(watch.target_price)),
        h("td", {}, fmt(watch.min_drop_percent)),
        h("td", {}, (watch.channels || []).join(", ") || "–"),
        h("td", {}, button(on ? "On" : "Off", () => toggle(watch, !on))),
        readonlyDetails(watch),
        h("td", {}, button("Edit", () => openEdit(watch)), button("Remove", () => remove(watch)))
      )
    );
  }
}

function describeOp(op) {
  if (op.op === "add") return "add " + op.watch.site + ' "' + op.watch.query + '"';
  const target = op.expected;
  return op.op + " " + target.site + ' "' + (target.query || target.id) + '"';
}

function renderPending() {
  $("pending-section").hidden = state.pending.length === 0;
  $("pending-list").replaceChildren(
    ...state.pending.map((entry) => h("li", {}, describeOp(entry.op) + " (" + entry.created_at + ")"))
  );
}

function renderConflicts() {
  $("conflicts-section").hidden = state.conflicts.length === 0;
  $("conflicts-list").replaceChildren(
    ...state.conflicts.map((entry) =>
      h(
        "li",
        {},
        describeOp(entry.op) + " — " + entry.reason + " ",
        button("Dismiss", () => dismiss(entry.op_id))
      )
    )
  );
}

function channelBoxes(container, selected) {
  container.replaceChildren(h("legend", {}, "Channels"));
  for (const name of state.channels) {
    const box = h("input", { type: "checkbox", name: "channel", value: name });
    box.checked = selected.includes(name);
    container.append(h("label", { class: "inline" }, box, " " + name));
  }
}

function checkedChannels(container) {
  return [...container.querySelectorAll("input[name=channel]:checked")].map((b) => b.value);
}

function renderForms() {
  const select = $("add-form").elements.site;
  if (select.options.length === 0) {
    for (const site of state.sites) select.append(h("option", { value: site }, site));
  }
  if ($("add-channels").querySelectorAll("input").length === 0) channelBoxes($("add-channels"), []);
  const usable = state.clone.ok;
  for (const el of document.querySelectorAll("form button[type=submit]")) el.disabled = !usable;
}

function render() {
  renderStatus();
  renderRows();
  renderPending();
  renderConflicts();
  renderForms();
}

// --- actions ---------------------------------------------------------------

async function load() {
  const { status, data } = await api("/api/watchlist");
  if (status !== 200) {
    clearBanners();
    banner("error", data.error || "Could not load the watchlist (" + status + ").");
    return false;
  }
  state = data;
  render();
  return true;
}

async function reload() {
  const refresh = token ? await api("/api/refresh", "POST") : null;
  await load();
  if (refresh && refresh.status === 403) describeResult(refresh.status, refresh.data);
}

async function retry() {
  const { status, data } = await api("/api/retry", "POST");
  await load();
  describeResult(status, data);
}

async function send(path, body) {
  const { status, data } = await api(path, "POST", body);
  await load();
  return { error: describeResult(status, data), status };
}

async function toggle(watch, enable) {
  const action = enable ? "enable" : "disable";
  const result = await send("/api/watches/" + watch.id + "/" + action, { expected: watch });
  if (result.error && result.status !== 409 && result.status !== 403) banner("error", result.error);
}

async function remove(watch) {
  const label = watch.site + ' "' + (watch.query || watch.id) + '"';
  if (!confirm("Remove " + label + "? This is saved as a commit you can revert.")) return;
  const result = await send("/api/watches/" + watch.id + "/remove", { expected: watch });
  if (result.error && result.status !== 409 && result.status !== 403) banner("error", result.error);
}

async function dismiss(opId) {
  const { status, data } = await api("/api/conflicts/" + opId + "/dismiss", "POST");
  await load();
  describeResult(status, data);
}

function numberOrNull(raw) {
  const text = raw.trim();
  return text === "" ? null : Number(text);
}

async function submitAdd(event) {
  event.preventDefault();
  const form = $("add-form");
  const body = { site: form.elements.site.value, query: form.elements.query.value };
  const price = numberOrNull(form.elements.target_price.value);
  const drop = numberOrNull(form.elements.min_drop_percent.value);
  const channels = checkedChannels($("add-channels"));
  if (price !== null) body.target_price = price;
  if (drop !== null) body.min_drop_percent = drop;
  if (channels.length > 0) body.channels = channels;
  const result = await send("/api/watches", body);
  $("add-error").textContent = result.error || "";
  if (!result.error) form.reset();
}

function openEdit(watch) {
  editing = watch;
  const form = $("edit-form");
  $("edit-title").textContent = watch.site + ' "' + (watch.query || watch.id) + '"';
  form.elements.target_price.value = watch.target_price ?? "";
  form.elements.min_drop_percent.value = watch.min_drop_percent ?? "";
  channelBoxes($("edit-channels"), watch.channels || []);
  $("edit-error").textContent = "";
  $("edit-dialog").showModal();
}

async function submitEdit(event) {
  event.preventDefault();
  const form = $("edit-form");
  const next = {
    target_price: numberOrNull(form.elements.target_price.value),
    min_drop_percent: numberOrNull(form.elements.min_drop_percent.value),
    channels: checkedChannels($("edit-channels")),
  };
  const changes = {};
  for (const key of EDITABLE) {
    const before = editing[key] ?? null;
    const same = JSON.stringify(before) === JSON.stringify(next[key]);
    // an unset channel list stays unset when nothing is ticked
    const unsetChannels = key === "channels" && before === null && next.channels.length === 0;
    if (!same && !unsetChannels) changes[key] = next[key];
  }
  if (Object.keys(changes).length === 0) {
    $("edit-error").textContent = "Nothing changed.";
    return;
  }
  const result = await send("/api/watches/" + editing.id + "/update", { expected: editing, changes });
  if (result.error) {
    $("edit-error").textContent = result.error;
  } else {
    $("edit-dialog").close();
  }
}

function start() {
  readTokenFromUrl();
  $("btn-refresh").addEventListener("click", reload);
  $("btn-retry").addEventListener("click", retry);
  $("add-form").addEventListener("submit", submitAdd);
  $("edit-form").addEventListener("submit", submitEdit);
  $("edit-cancel").addEventListener("click", () => $("edit-dialog").close());
  $("btn-token").addEventListener("click", () => {
    const pasted = $("token-input").value.trim();
    $("token-input").value = "";
    if (pasted !== "") saveToken(pasted);
    else clearToken();
    $("token-box").hidden = token !== "";
  });
  reload();
}

start();
