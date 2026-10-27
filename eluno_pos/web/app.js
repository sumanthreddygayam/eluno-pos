/* Eluno till screen. Plain JS, no build step: every number and rule comes from the till API.
   Money from the API is integer paise; this file only formats it. */
"use strict";

// ------------------------------------------------------------------ helpers
const $ = (sel, root) => (root || document).querySelector(sel);

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k in el && typeof v !== "string") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

function put(target, ...kids) {
  const el = typeof target === "string" ? $(target) : target;
  el.replaceChildren(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false));
  return el;
}

const inr = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const rs = (paise) => (paise < 0 ? "−₹" : "₹") + inr.format(Math.abs(paise || 0) / 100);
const toPaise = (rupees) => Math.round(parseFloat(String(rupees).replace(/,/g, "")) * 100);
const enc = encodeURIComponent;
const titleCase = (s) => String(s || "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
const method = (m) => ({ upi: "UPI", card: "Card", cash: "Cash", none: "No charge" }[m] || titleCase(m));
const hhmm = (iso) => (iso ? iso.slice(11, 16) : "");
const dioptre = (cd) => (cd === null || cd === undefined ? "" : (cd > 0 ? "+" : cd < 0 ? "−" : "") + (Math.abs(cd) / 100).toFixed(2));

const store = {
  get(k) { try { return localStorage.getItem("eluno." + k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem("eluno." + k, v); } catch (e) { /* private mode: not remembered */ } },
};

let toastTimer = null;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 3500);
}

const STATUS_CHIP = {
  draft: ["Draft", ""], payment_pending: ["Waiting on terminal", "warn"], paid: ["Paid, Shopify pending", "warn"],
  synced: ["Done", "ok"], cancelled: ["Cancelled", ""], voided: ["Voided", "bad"],
  requested: ["Requested", "warn"], money_returned: ["Money returned, Shopify pending", "warn"], failed: ["Failed", "bad"],
};
function statusChip(status) {
  const [label, cls] = STATUS_CHIP[status] || [status, ""];
  return h("span", { class: "chip " + cls, text: label });
}

function errorBox(err, extra) {
  const issues = (err.details && err.details.issues) || [];
  return h("div", { class: "notice bad" },
    h("b", { text: err.message || String(err) }),
    issues.map((i) => h("div", { class: "hint" }, (i.pair_ref ? "Pair " + i.pair_ref + ": " : "") + i.message + (i.hint ? " " + i.hint : ""))),
    extra || null);
}

// ------------------------------------------------------------------ API + network log
const NET = [];

async function call(method, path, body, label) {
  let res;
  try {
    res = await fetch(path, {
      method,
      headers: body !== undefined && body !== null ? { "Content-Type": "application/json" } : {},
      body: body !== undefined && body !== null ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    logNet(label || method + " " + path, [], null, "unreachable");
    throw { code: "offline", message: "Can't reach the till server. Check the connection and try again." };
  }
  let net = [];
  try { net = JSON.parse(res.headers.get("X-Eluno-Network") || "[]"); } catch (e) { net = []; }
  const ms = parseInt(res.headers.get("X-Eluno-Ms") || "", 10);
  const ctype = res.headers.get("Content-Type") || "";
  const data = ctype.includes("json") ? await res.json() : await res.text();
  const err = res.ok ? null : (data && data.error) || { code: "http_" + res.status, message: String(data) };
  logNet(label || method + " " + path, net, isNaN(ms) ? null : ms, err && err.code);
  if (err) {
    err.status = res.status;
    // "503 on POST http://10.0.0.5:8787/admin/api/2026-10/orders.json" reads better as "503 on POST /orders.json"
    if (err.message) err.message = String(err.message).replace(/https?:\/\/[^\s/]+(\/admin\/api\/[^/]+)?/g, "");
    throw err;
  }
  return { data, ms: isNaN(ms) ? null : ms, net };
}
const api = async (method, path, body, label) => (await call(method, path, body, label)).data;

function logNet(label, calls, ms, error) {
  NET.unshift({ at: new Date(), label, calls, ms, error });
  NET.length = Math.min(NET.length, 80);
  renderNet();
}

function renderNet() {
  const last = NET.find((e) => e.calls.length) || NET[0];
  const retries = NET.slice(0, 20).reduce((n, e) => n + e.calls.filter((c) => c.retry_in !== undefined).length, 0);
  put("#netlog-summary", last ? last.label + (last.ms !== null ? " · " + (last.ms / 1000).toFixed(2) + " s" : "") +
    " · " + last.calls.length + " upstream call(s)" + (retries ? " · " + retries + " retries absorbed recently" : "") : "no calls yet");
  put("#netlog-body", NET.map((e) => h("div", { class: "netentry" },
    h("div", { class: "head" },
      h("b", { text: e.label }),
      h("span", { class: "muted", text: e.at.toLocaleTimeString() }),
      e.ms !== null ? h("span", { text: (e.ms / 1000).toFixed(2) + " s" }) : null,
      e.error ? h("span", { class: "chip bad", text: e.error }) : null),
    e.calls.length ? h("div", { class: "netcalls" }, e.calls.map((c) => {
      const failed = typeof c.status !== "number" || c.status >= 400;
      const label = c.service + " " + c.call + " → " + c.status + (c.retry_in !== undefined ? " · retried after " + c.retry_in + " s" : "");
      return h("span", { class: "netcall" + (c.retry_in !== undefined ? " retry" : failed ? " fail" : ""), text: label });
    })) : null)));
}

// ------------------------------------------------------------------ state
const S = {
  meta: null, stores: [], staff: [], store: null, staffId: null, catalog: null, sandbox: null,
  customer: null, pairs: [], channel: "walk_in", acks: new Set(), quote: null, quoteError: null,
  bill: null, payError: null, busy: null, lastPayMs: null, lastPayNet: [],
};
const newPair = () => ({ frame: "", lensType: "", index: "", coating: "", wearer: "" });
const staffName = (id) => { const s = S.staff.find((x) => x.staff_id === id); return s ? s.name + " (" + id + ")" : id; };
const managers = () => S.staff.filter((s) => s.role === "store_manager");
const reasons = (kind) => Object.entries((S.meta.reason_codes || {})[kind] || {});

function options(select, items, selected, blank) {
  put(select, blank !== undefined ? h("option", { value: "", text: blank }) : null,
    items.map(([value, label]) => h("option", { value, text: label, selected: value === selected })));
}

// ------------------------------------------------------------------ start-up
async function init() {
  document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  $("#netlog-toggle").addEventListener("click", () => $("#netlog").classList.toggle("collapsed"));
  try {
    S.meta = await api("GET", "/meta", null, "Start-up");
    S.stores = (await api("GET", "/stores", null, "Stores")).stores;
  } catch (e) {
    put("main", errorBox(e));
    return;
  }
  put("#bizdate", "Business date " + S.meta.business_date);
  $("#dayend-date").value = S.meta.business_date;
  options($("#channel"), S.meta.channels.map((c) => [c, titleCase(c)]), "walk_in");
  options($("#store"), S.stores.map((s) => [s.store_code, s.store_code + " · " + s.name]), store.get("store") || "HSR");
  $("#store").addEventListener("change", onStoreChange);
  $("#staff").addEventListener("change", () => { S.staffId = $("#staff").value; store.set("staff." + S.store, S.staffId); scheduleQuote(); });
  wireBill();
  wireRefund();
  wireTill();
  wireDayEnd();
  $("#bills-reload").addEventListener("click", loadBills);
  $("#sync-all").addEventListener("click", syncAll);
  $("#audit-close").addEventListener("click", () => { $("#audit-card").hidden = true; });
  if (S.meta.sandbox) {
    $("#demo-tab").hidden = false;
    await loadSandbox();
  } else {
    put("#mode", h("span", { class: "chip info", text: "Live backend" }));
  }
  await onStoreChange();
  renderNet();
}

async function onStoreChange() {
  if (S.bill && ["payment_pending"].includes(S.bill.status)) {
    $("#store").value = S.store;
    toast("Finish or cancel the bill waiting on the terminal first.");
    return;
  }
  S.store = $("#store").value;
  store.set("store", S.store);
  try {
    S.staff = (await api("GET", "/staff?store=" + S.store, null, "Staff")).staff;
  } catch (e) { S.staff = []; toast(e.message); }
  const saved = store.get("staff." + S.store);
  const pick = S.staff.find((s) => s.staff_id === saved) || S.staff.find((s) => s.role === "sales") || S.staff[0];
  options($("#staff"), S.staff.map((s) => [s.staff_id, s.name + " · " + titleCase(s.role)]), pick && pick.staff_id);
  S.staffId = pick && pick.staff_id;
  options($("#disc-approver"), S.staff.filter((s) => s.can_override_discount || s.role === "store_manager")
    .map((s) => [s.staff_id, s.name + " (" + s.staff_id + ")"]), "", "Choose…");
  options($("#disc-reason"), reasons("discount_override").map(([k, v]) => [k, v]), "", "Choose…");
  newBill();
  const active = document.querySelector(".tab.active").id.replace("tab-", "");
  if (active !== "bill") showTab(active);
}

function showTab(name) {
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
  if (name === "bills") loadBills();
  if (name === "refund") loadRecentForRefund();
  if (name === "till") loadTill();
  if (name === "demo") loadSandbox();
  if (name === "bill") loadCatalog();
}

// ================================================================== NEW BILL
function wireBill() {
  $("#lookup-form").addEventListener("submit", (e) => { e.preventDefault(); lookup($("#phone").value); });
  $("#add-pair").addEventListener("click", () => { S.pairs.push(newPair()); renderPairs(); });
  $("#refresh-stock").addEventListener("click", async () => { await loadCatalog(); scheduleQuote(); });
  $("#channel").addEventListener("change", () => { S.channel = $("#channel").value; cartChanged(); });
  ["#disc-pct", "#disc-approver", "#disc-reason"].forEach((id) => $(id).addEventListener("change", cartChanged));
  $("#disc-clear").addEventListener("click", () => {
    $("#disc-pct").value = ""; $("#disc-approver").value = ""; $("#disc-reason").value = "";
    $("#discount-box").open = false; cartChanged();
  });
}

function newBill() {
  if (S.bill && S.bill.status === "draft") discardDraft(S.bill);
  Object.assign(S, { customer: null, pairs: [newPair()], acks: new Set(), quote: null, quoteError: null, bill: null, payError: null,
    busy: null, lastPayMs: null, lastPayNet: [], channel: "walk_in" });
  $("#phone").value = "";
  $("#channel").value = "walk_in";
  $("#disc-pct").value = ""; $("#disc-approver").value = ""; $("#disc-reason").value = "";
  $("#discount-box").open = false;
  put("#customer");
  loadCatalog();
  renderAll();
}

async function loadCatalog() {
  if (!S.store) return;
  try {
    S.catalog = await api("GET", "/catalog?store=" + S.store, null, "Stock and lenses");
  } catch (e) { toast("Stock: " + e.message); }
  renderPairs();
}

async function lookup(phone) {
  phone = String(phone || "").trim();
  if (!phone || lookupLocked()) return;
  if (S.bill && S.bill.status !== "draft") { newBill(); $("#phone").value = phone; }  // the next customer after a finished sale
  put("#customer", h("div", { class: "busy" }, h("div", { class: "spinner" }), "Looking up…"));
  try {
    S.customer = await api("GET", "/customers/" + enc(phone), null, "Customer lookup");
    $("#phone").value = S.customer.customer.phone;
  } catch (e) {
    S.customer = null;
    put("#customer", errorBox(e));
    renderAll();
    return;
  }
  renderCustomer();
  cartChanged();
}

function renderCustomer() {
  const c = S.customer;
  if (!c) { put("#customer"); return; }
  const cust = c.customer;
  const rx = c.prescription;
  const eye = (name, e) => h("tr", {}, h("th", { text: name }), h("td", { text: dioptre(e.sph) }), h("td", { text: dioptre(e.cyl) }),
    h("td", { text: e.axis === null || e.axis === undefined ? "" : e.axis + "°" }), h("td", { text: dioptre(e.add) }));
  put("#customer",
    h("div", { class: "row spread" },
      h("div", {}, h("div", { class: "big", text: cust.name }), h("div", { class: "muted", text: cust.phone + " · lifetime " + rs(cust.lifetime_value) })),
      h("div", {}, cust.tags.map((t) => h("span", { class: "chip info", text: t })))),
    cust.flags.length ? h("div", {}, cust.flags.map((f) => h("span", { class: "chip warn", text: f }))) : null,
    rx ? h("div", {},
      h("h3", { text: "Prescription " + rx.rx_id + " · " + titleCase(rx.source) + " · valid until " + rx.valid_until }),
      h("div", { class: "tablewrap" }, h("table", { class: "rx" },
        h("tr", {}, h("th", {}), h("th", { text: "SPH" }), h("th", { text: "CYL" }), h("th", { text: "Axis" }), h("th", { text: "ADD" })),
        eye("Right (OD)", rx.od), eye("Left (OS)", rx.os))),
      rx.pd ? h("p", { class: "small muted", text: "PD " + rx.pd + " mm" + (rx.notes ? " · " + rx.notes : "") }) : null)
      : h("div", { class: "notice warn" }, h("b", { text: "No valid prescription on file." }), h("div", { class: "hint", text: "Frames and sunglasses can still be billed; a prescription lens can't." })),
    c.skipped_prescriptions.length ? h("p", { class: "small muted", text: "Not used: " + c.skipped_prescriptions.map((s) => s.rx_id + " (" + s.reason + ")").join("; ") }) : null,
    c.flags.map(issueNotice));
}

// ------------------------------------------------------------------ pairs
function lensOptions(type, index) {
  const all = (S.catalog && S.catalog.lenses) || [];
  const types = [...new Set(all.map((l) => l.type))];
  const indices = [...new Set(all.filter((l) => l.type === type).map((l) => l.index))];
  const coatings = all.filter((l) => l.type === type && l.index === index);
  return { types, indices, coatings };
}

function frameLabel(f) {
  const here = f.available + " here" + (f.held ? " (" + f.held + " reserved)" : "");
  const elsewhere = !f.available && Object.keys(f.elsewhere).length
    ? " · at " + Object.entries(f.elsewhere).map(([k, v]) => k + " " + v).join(", ") : "";
  return f.sku + " · " + f.description + (f.category === "sunglasses" ? " (sunglasses)" : "") + " · " + rs(f.mrp) + " · " + here + elsewhere;
}

function renderPairs() {
  const locked = cartLocked();
  $("#cart").disabled = locked;
  $("#lookup-form").querySelectorAll("input, button").forEach((el) => { el.disabled = lookupLocked(); });
  const frames = (S.catalog && S.catalog.frames) || [];
  put("#pairs", S.pairs.map((p, i) => {
    const { types, indices, coatings } = lensOptions(p.lensType, p.index);
    const set = (k) => (e) => {
      p[k] = e.target.value;
      if (k === "lensType") {
        const o = lensOptions(p.lensType, "");
        p.index = o.indices.includes("1.60") ? "1.60" : o.indices[0] || "";
        p.coating = (lensOptions(p.lensType, p.index).coatings[0] || {}).coating || "";
      }
      if (k === "index") p.coating = (lensOptions(p.lensType, p.index).coatings.find((c) => c.coating === p.coating) ||
        lensOptions(p.lensType, p.index).coatings[0] || {}).coating || "";
      renderPairs();
      cartChanged();
    };
    const sel = (value, items, blank, onchange, disabled) => {
      const s = h("select", { onchange, disabled });
      options(s, items, value, blank);
      return s;
    };
    return h("div", { class: "pair" },
      h("div", { class: "pair-head" }, h("span", { text: "Pair " + (i + 1) }),
        S.pairs.length > 1 ? h("button", { type: "button", class: "ghost small", text: "Remove", onclick: () => { S.pairs.splice(i, 1); renderPairs(); cartChanged(); } }) : null),
      h("div", { class: "pair-grid" },
        h("label", { class: "field full" }, "Frame",
          sel(p.frame, frames.map((f) => [f.sku, frameLabel(f)]), "No frame (lens only)", set("frame"))),
        h("label", { class: "field" }, "Lens",
          sel(p.lensType, types.map((t) => [t, titleCase(t)]), "No lens", set("lensType"))),
        h("label", { class: "field" }, "Index",
          sel(p.index, indices.map((x) => [x, x]), undefined, set("index"), !p.lensType)),
        h("label", { class: "field" }, "Coating",
          sel(p.coating, coatings.map((c) => [c.coating, titleCase(c.coating) + " · " + rs(c.mrp)]), undefined, set("coating"), !p.lensType)),
        h("label", { class: "field full" }, "Wearer's mobile, if not the customer (family bill)",
          h("input", { type: "tel", value: p.wearer, placeholder: "Leave empty if the customer wears it",
            onchange: (e) => { p.wearer = e.target.value.trim(); cartChanged(); } }))));
  }));
}

function lookupLocked() {
  return !!S.busy || !!(S.bill && S.bill.status === "payment_pending");
}

function cartLocked() {
  return !!(S.bill && ["payment_pending", "paid", "synced", "voided"].includes(S.bill.status)) || !!S.busy;
}

function billRequest() {
  const pct = parseFloat($("#disc-pct").value);
  const manual = pct > 0 ? { bps: Math.round(pct * 100), approver_id: $("#disc-approver").value, reason_code: $("#disc-reason").value } : null;
  return {
    store_code: S.store, staff_id: S.staffId, customer_phone: S.customer && S.customer.customer.phone, channel: S.channel,
    pairs: S.pairs.filter((p) => p.frame || p.lensType).map((p) => ({
      frame_sku: p.frame || null,
      lens: p.lensType ? p.lensType + "/" + p.index + "/" + p.coating : null,
      wearer_phone: p.wearer || null,
    })),
    manual_discount: manual, acknowledge: [...S.acks],
  };
}

// ------------------------------------------------------------------ quote
let quoteTimer = null;
let quoteSeq = 0;

function cartChanged() {
  if (S.bill && S.bill.status === "draft") { discardDraft(S.bill); S.bill = null; }
  S.payError = null;
  scheduleQuote();
}

function scheduleQuote() {
  clearTimeout(quoteTimer);
  quoteTimer = setTimeout(runQuote, 300);
}

async function runQuote() {
  if (cartLocked()) return;
  const req = billRequest();
  if (!S.customer || !req.pairs.length) { S.quote = null; S.quoteError = null; renderAll(); return; }
  const md = req.manual_discount;
  if (md && (!md.approver_id || !md.reason_code)) {
    S.quote = null;
    S.quoteError = { message: "Manager's discount: choose who approved it and why, or remove it." };
    renderAll();
    return;
  }
  const seq = ++quoteSeq;
  put("#quote", h("div", { class: "busy" }, h("div", { class: "spinner" }), "Pricing…"));
  try {
    const q = await api("POST", "/quotes", req, "Price check");
    if (seq !== quoteSeq) return;
    S.quote = q; S.quoteError = null;
    const confirmable = new Set(q.issues.filter((i) => i.severity === "confirm").map((i) => i.code));
    S.acks = new Set([...S.acks].filter((c) => confirmable.has(c)));
  } catch (e) {
    if (seq !== quoteSeq) return;
    if (e.code === "bad_acknowledgement") { S.acks.clear(); return runQuote(); }
    S.quote = null; S.quoteError = e;
  }
  renderAll();
}

function issueNotice(i) {
  const cls = i.severity === "block" ? "bad" : i.severity === "confirm" ? "warn" : "info";
  const box = h("div", { class: "notice " + cls },
    h("b", { text: (i.pair_ref ? "Pair " + i.pair_ref + ": " : "") + i.message }),
    i.hint ? h("div", { class: "hint", text: i.hint }) : null);
  if (i.severity === "confirm" && S.quote && S.quote.issues.includes(i)) {
    box.append(h("label", {}, h("input", { type: "checkbox", checked: S.acks.has(i.code), disabled: cartLocked(),
      onchange: (e) => { if (e.target.checked) S.acks.add(i.code); else S.acks.delete(i.code); cartChanged(); } }),
    "Checked with the customer / optometrist (recorded under your name)"));
  }
  return box;
}

function renderQuote() {
  const q = S.quote;
  if (S.quoteError) { put("#quote", errorBox(S.quoteError)); return; }
  if (!q) {
    put("#quote", h("p", { class: "muted", text: S.customer ? "Pick a frame or a lens." : "Look up a customer and pick a frame or lens." }));
    return;
  }
  const t = q.totals;
  const applied = q.offers.filter((o) => o.applied);
  const others = q.offers.filter((o) => !o.applied);
  put("#quote",
    h("div", { class: "tablewrap" }, h("table", {},
      h("tr", {}, h("th", { text: "Item" }), h("th", { class: "num", text: "MRP" }), h("th", { class: "num", text: "Discount" }), h("th", { class: "num", text: "Amount" })),
      q.lines.map((l) => [
        h("tr", {}, h("td", {}, l.description, l.wearer !== q.customer.name ? h("div", { class: "small muted", text: "for " + l.wearer }) : null),
          h("td", { class: "num", text: rs(l.mrp) }), h("td", { class: "num", text: l.discount ? "−" + rs(l.discount) : "" }), h("td", { class: "num", text: rs(l.total) })),
        h("tr", { class: "sub" }, h("td", { colspan: 4, text: "GST " + (l.rate_bps / 100) + "% included: taxable " + rs(l.taxable) + ", CGST " + rs(l.cgst) + ", SGST " + rs(l.sgst) })),
      ]),
      h("tr", { class: "sub" }, h("td", { text: "Total MRP" }), h("td", { class: "num", text: rs(t.mrp) }), h("td", { class: "num", text: t.discount ? "−" + rs(t.discount) : "" }), h("td", {})),
      h("tr", { class: "sub" }, h("td", { colspan: 3, text: "of which GST (CGST " + rs(t.cgst) + " + SGST " + rs(t.sgst) + ")" }), h("td", { class: "num", text: rs(t.tax) })),
      h("tr", { class: "total" }, h("td", { colspan: 3, text: "To pay" }), h("td", { class: "num", text: rs(t.net) })))),
    q.offers.length ? h("div", {},
      h("h3", { text: "Offers (" + titleCase(q.offer_policy) + ")" }),
      applied.map((o) => h("div", { class: "offer applied" }, h("div", { class: "grow" }, h("div", { class: "name", text: o.name + " · −" + rs(o.amount_paise) }), h("div", { class: "why", text: o.why })))),
      others.length ? h("details", {}, h("summary", { class: "small", text: others.length + " other offer(s) not applied, and why" }),
        others.map((o) => h("div", { class: "offer" }, h("div", { class: "grow" }, h("div", { class: "name", text: o.name }), h("div", { class: "why", text: o.why }))))) : null) : null,
    q.issues.length ? h("div", {}, h("h3", { text: "Checks" }), q.issues.map(issueNotice)) : h("p", { class: "small muted", text: "Prescription, lens limits and stock checked: nothing to flag." }),
    q.blocked ? h("p", { class: "small", text: "Fix the red items (or tick the amber ones) to take payment." }) : null);
}

// ------------------------------------------------------------------ payment
function renderPayment() {
  const b = S.bill;
  const e = S.payError;
  const canPay = S.quote && !S.quote.blocked && !S.busy && (!b || b.status === "draft");
  const buttons = (label) => h("div", {},
    label ? h("p", { class: "small muted", text: label }) : null,
    h("div", { class: "paybuttons" },
      ["upi", "card", "cash"].map((m) => h("button", { class: "primary", disabled: !canPay, text: method(m), onclick: () => pay(m) }))));
  const parts = [];

  if (S.busy) {
    parts.push(h("div", { class: "busy" }, h("div", { class: "spinner" }), S.busy));
  } else if (b && b.status === "synced") {
    parts.push(h("div", { class: "result ok" },
      h("h3", { text: "Paid. Invoice " + b.invoice_no }),
      h("div", { text: rs(b.total) + " by " + method(b.payment_method) + (b.charge_id ? " · terminal ref " + b.charge_id : "") }),
      h("div", { class: "small", text: "Recorded in Shopify as " + b.shopify_order_name + "; the frame is off Shopify's stock." }),
      S.lastPayMs !== null ? h("div", { class: "small", text: "From pressing Pay to the invoice: " + (S.lastPayMs / 1000).toFixed(2) + " s" + retriesNote(S.lastPayNet) }) : null),
    invoiceButtons(b), h("div", { class: "row" }, h("button", { class: "primary", text: "Next customer", onclick: newBill })));
  } else if (b && b.status === "paid") {
    parts.push(h("div", { class: "result warn" },
      h("h3", { text: "Paid. Invoice " + b.invoice_no + " issued" }),
      h("div", { text: rs(b.total) + " by " + method(b.payment_method) + ". The customer can leave with the invoice." }),
      h("div", { class: "small", text: "Shopify hasn't accepted the order yet" + (b.last_error ? " (" + b.last_error + ")" : "") +
        ". It's kept and sent on the next sync; the frame stays reserved until then." })),
    invoiceButtons(b),
    h("div", { class: "row" }, h("button", { text: "Send to Shopify now", onclick: syncThisBill }), h("button", { class: "primary", text: "Next customer", onclick: newBill })));
  } else if (b && b.status === "payment_pending") {
    parts.push(h("div", { class: "result warn" },
      h("h3", { text: "Waiting on the card machine" }),
      h("div", { text: (e && e.message) || "The terminal hasn't confirmed this payment." }),
      h("div", { class: "small", text: "Trying again reuses the same bill reference, so the customer can't be charged twice. The frame stays reserved." })),
    h("div", { class: "row" }, h("button", { class: "primary", text: "Try " + method(b.payment_method) + " again", onclick: () => pay(b.payment_method) })),
    managerCancel(b));
  } else if (b && b.status === "cancelled") {
    parts.push(h("div", { class: "result" }, h("h3", { text: "Bill cancelled" }), h("div", { class: "small", text: "Nothing was charged; any reserved frame is back on sale." })),
      h("div", { class: "row" }, h("button", { class: "primary", text: "Next customer", onclick: newBill })));
  } else {
    if (e && e.code === "payment_declined") {
      parts.push(h("div", { class: "result bad" }, h("h3", { text: "Payment declined" }), h("div", { text: e.message })));
      parts.push(buttons("Try again, or another way:"));
      if (b) parts.push(h("div", { class: "row" }, h("button", { class: "danger", text: "Customer won't pay: cancel bill", onclick: () => cancelBill(b) })));
    } else {
      if (e) {
        const extra = e.code === "till_not_open" ? h("button", { class: "small", text: "Open the cash drawer", onclick: () => showTab("till") })
          : e.code === "quote_changed" || e.code === "stock_held" || e.code === "bill_blocked" ? h("button", { class: "small", text: "Re-check price and stock", onclick: async () => { S.payError = null; await loadCatalog(); runQuote(); } })
            : null;
        parts.push(errorBox(e, extra));
      }
      parts.push(S.quote && !S.quote.blocked ? h("div", {}, h("div", { class: "big", text: rs(S.quote.totals.net) }), buttons()) : buttons(S.quote ? null : "Price the bill first."));
    }
  }
  put("#payment", parts);
}

function retriesNote(net) {
  const r = (net || []).filter((c) => c.retry_in !== undefined);
  if (!r.length) return "";
  return " (including " + r.length + " retr" + (r.length > 1 ? "ies" : "y") + ": " + r.map((c) => c.service + " " + c.status).join(", ") + ")";
}

function invoiceButtons(b) {
  return h("div", { class: "row wrap" },
    h("a", { href: "/invoices/" + enc(b.invoice_no) + ".html", target: "_blank", rel: "noopener" }, h("button", { text: "Print invoice" })),
    h("a", { href: "/invoices/" + enc(b.invoice_no) + ".txt", target: "_blank", rel: "noopener" }, h("button", { class: "ghost", text: "Plain-text copy" })));
}

function managerCancel(b) {
  const approver = h("select", {});
  options(approver, managers().map((m) => [m.staff_id, m.name + " (" + m.staff_id + ")"]), "", "Store manager…");
  return h("details", {},
    h("summary", { text: "Customer gave up: cancel (store manager)" }),
    h("p", { class: "small muted", text: "Check the card machine shows no payment for this bill first. If a charge did go through, it carries this bill's reference and has to be refunded on the terminal." }),
    h("div", { class: "row wrap" }, approver,
      h("button", { class: "danger", text: "Cancel bill", onclick: () => {
        if (!approver.value) { toast("Choose the store manager."); return; }
        cancelBill(b, approver.value);
      } })));
}

async function pay(method) {
  if (S.busy) return;
  S.busy = method === "cash" ? "Recording cash payment…" : "Waiting for the customer to pay on the " + (method === "upi" ? "UPI QR" : "card machine") + "…";
  S.payError = null;
  renderAll();
  try {
    if (!S.bill || S.bill.status === "cancelled") {
      S.bill = (await api("POST", "/bills", billRequest(), "Save bill")).bill;
    }
    const r = await call("POST", "/bills/" + S.bill.bill_id + "/pay", { method, staff_id: S.staffId }, "Pay by " + method.toUpperCase());
    S.bill = r.data; S.lastPayMs = r.ms; S.lastPayNet = r.net;
  } catch (e) {
    S.payError = e;
    if (S.bill) { try { S.bill = await api("GET", "/bills/" + S.bill.bill_id, null, "Bill status"); } catch (_) { /* keep what we had */ } }
  }
  S.busy = null;
  renderAll();
  loadCatalog();
}

async function syncThisBill() {
  try {
    await api("POST", "/sync", {}, "Send to Shopify");
    S.bill = await api("GET", "/bills/" + S.bill.bill_id, null, "Bill status");
    toast(S.bill.status === "synced" ? "In Shopify as " + S.bill.shopify_order_name : "Shopify still not accepting: " + (S.bill.last_error || ""));
  } catch (e) { toast(e.message); }
  renderAll();
}

async function cancelBill(b, approverId) {
  try {
    S.bill = await api("POST", "/bills/" + b.bill_id + "/cancel", { staff_id: S.staffId, why: approverId ? "terminal stuck; manager confirmed no payment" : "customer did not proceed", approver_id: approverId || null }, "Cancel bill");
    S.payError = null;
  } catch (e) { S.payError = e; }
  renderAll();
  loadCatalog();
}

function discardDraft(b) {
  // the cart changed after a declined payment: drop that draft so its frame reservation is released
  api("POST", "/bills/" + b.bill_id + "/cancel", { staff_id: b.staff_id, why: "cart changed before payment" }, "Discard old draft")
    .then(loadCatalog).catch(() => {});
}

let pairsLocked = null;
function renderAll() {
  if (pairsLocked !== cartLocked()) { pairsLocked = cartLocked(); renderPairs(); }  // don't rebuild the editor under the user's fingers
  renderQuote();
  renderPayment();
}

// ================================================================== BILLS TODAY
async function loadBills() {
  put("#bills", h("div", { class: "busy" }, h("div", { class: "spinner" }), "Loading…"));
  let out, holds;
  try {
    [out, holds] = await Promise.all([api("GET", "/bills?store=" + S.store, null, "Bills today"), api("GET", "/holds?store=" + S.store, null, "Reserved frames")]);
  } catch (e) { put("#bills", errorBox(e)); return; }
  if (!out.bills.length) put("#bills", h("p", { class: "muted", text: "No bills yet today at " + S.store + "." }));
  else put("#bills", h("div", { class: "tablewrap" }, h("table", {},
    h("tr", {}, ["Time", "Invoice", "Customer", "Items", "Total", "Paid by", "Status", ""].map((x, i) => h("th", { class: i === 4 ? "num" : "", text: x }))),
    out.bills.map((b) => h("tr", {},
      h("td", { text: hhmm(b.paid_at || b.created_at) }),
      h("td", {}, b.invoice_no || h("span", { class: "muted", text: "–" }), b.shopify_order_name ? h("div", { class: "small muted", text: b.shopify_order_name }) : null),
      h("td", {}, b.customer_name, h("div", { class: "small muted", text: staffName(b.staff_id) + " · " + titleCase(b.channel) })),
      h("td", { class: "small", text: b.items.join("; ") }),
      h("td", { class: "num" }, rs(b.total), b.refunded ? h("div", { class: "small", style: "color:var(--bad)", text: "refunded " + rs(b.refunded) }) : null),
      h("td", { text: b.payment_method ? method(b.payment_method) : "" }),
      h("td", {}, statusChip(b.status), b.last_error && ["paid", "payment_pending"].includes(b.status) ? h("div", { class: "small muted", text: b.last_error }) : null),
      h("td", {}, billActions(b)))))));
  const frames = {};
  ((S.catalog && S.catalog.frames) || []).forEach((f) => { frames[f.inventory_item_id] = f.sku + " " + f.description; });
  put("#holds", holds.holds.length ? h("div", { class: "tablewrap" }, h("table", {},
    h("tr", {}, ["Frame", "Bill", "Bill status", "Since"].map((x) => h("th", { text: x }))),
    holds.holds.map((x) => h("tr", {}, h("td", { text: frames[x.inventory_item_id] || "item " + x.inventory_item_id }), h("td", { text: x.bill_id }),
      h("td", {}, statusChip(x.status)), h("td", { text: hhmm(x.created_at) })))))
    : h("p", { class: "muted", text: "None. Every frame on the shelf is free to sell." }));
}

function billActions(b) {
  const acts = [];
  if (b.invoice_no) acts.push(h("a", { href: "/invoices/" + enc(b.invoice_no) + ".html", target: "_blank", rel: "noopener" }, h("button", { class: "small", text: "Invoice" })));
  if (b.status === "synced") acts.push(h("button", { class: "small", text: "Refund", onclick: () => { showTab("refund"); findForRefund(b.invoice_no); } }));
  if (b.status === "paid") acts.push(h("button", { class: "small", text: "Send to Shopify", onclick: syncAll }),
    h("button", { class: "small danger", text: "Void", onclick: () => voidForm(b) }));
  if (b.status === "payment_pending") acts.push(h("button", { class: "small", text: "Retry payment", onclick: () => retryFromList(b) }),
    h("button", { class: "small danger", text: "Cancel", onclick: () => cancelForm(b) }));
  if (b.status === "draft") acts.push(h("button", { class: "small danger", text: "Cancel", onclick: () => cancelFromList(b) }));
  acts.push(h("button", { class: "small ghost", text: "History", onclick: () => showAudit(b.bill_id, (b.invoice_no || b.bill_id)) }));
  return h("div", { class: "row wrap" }, acts);
}

async function syncAll() {
  try {
    const out = await api("POST", "/sync", {}, "Send pending to Shopify");
    toast(out.results.length ? out.results.length + " pending item(s) processed" : "Nothing pending.");
  } catch (e) { toast(e.message); }
  loadBills();
}

async function retryFromList(b) {
  try {
    const r = await api("POST", "/bills/" + b.bill_id + "/pay", { method: b.payment_method, staff_id: S.staffId }, "Retry payment");
    toast("Bill is now: " + (STATUS_CHIP[r.status] || [r.status])[0] + (r.invoice_no ? " · " + r.invoice_no : ""));
  } catch (e) { toast(e.message); }
  loadBills();
}

async function cancelFromList(b, approverId) {
  try {
    await api("POST", "/bills/" + b.bill_id + "/cancel", { staff_id: S.staffId, why: "cancelled from the bills list", approver_id: approverId || null }, "Cancel bill");
    toast("Cancelled; reserved frames released.");
  } catch (e) { toast(e.message); }
  loadBills();
  loadCatalog();
}

function actionPanel(title, body) {
  $("#audit-card").hidden = false;
  put("#audit-title", title);
  put("#audit", body);
  $("#audit-card").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function cancelForm(b) {
  const approver = h("select", {});
  options(approver, managers().map((m) => [m.staff_id, m.name]), "", "Store manager…");
  actionPanel("Cancel " + b.bill_id + " (waiting on terminal)", [
    h("p", { class: "small", text: "A store manager confirms the card machine shows no payment for this bill." }),
    h("div", { class: "row wrap" }, approver, h("button", { class: "danger", text: "Cancel bill", onclick: () => {
      if (!approver.value) return toast("Choose the store manager.");
      cancelFromList(b, approver.value); $("#audit-card").hidden = true;
    } }))]);
}

function voidForm(b) {
  const approver = h("select", {});
  options(approver, managers().map((m) => [m.staff_id, m.name]), "", "Store manager…");
  const reason = h("select", {});
  options(reason, reasons("void"), "", "Reason…");
  actionPanel("Void " + b.invoice_no, [
    h("p", { class: "small", text: "For a paid bill that can't reach Shopify: the money goes back to the customer and the invoice number is kept as cancelled." }),
    h("div", { class: "row wrap" }, approver, reason, h("button", { class: "danger", text: "Void and return " + rs(b.total), onclick: async () => {
      if (!approver.value || !reason.value) return toast("Choose the manager and the reason.");
      try {
        await api("POST", "/bills/" + b.bill_id + "/void", { staff_id: S.staffId, approver_id: approver.value, reason_code: reason.value }, "Void bill");
        toast("Voided; money returned.");
        $("#audit-card").hidden = true;
      } catch (e) { toast(e.message); }
      loadBills();
    } }))]);
}

async function showAudit(ref, label) {
  try {
    const out = await api("GET", "/audit?ref=" + enc(ref), null, "History");
    actionPanel("History of " + label, out.events.length ? h("div", { class: "tablewrap" }, h("table", {},
      h("tr", {}, ["Time", "Who", "What", "Details"].map((x) => h("th", { text: x }))),
      out.events.map((ev) => h("tr", {}, h("td", { text: hhmm(ev.at) + ":" + (ev.at || "").slice(17, 19) }), h("td", { text: ev.actor }),
        h("td", {}, ev.action, ev.reason_code ? h("div", { class: "small muted", text: ev.reason_code }) : null),
        h("td", { class: "small", text: Object.entries(ev.details || {}).map(([k, v]) => k + ": " + (typeof v === "object" ? JSON.stringify(v) : v)).join(" · ") })))))
      : h("p", { class: "muted", text: "No events." }));
  } catch (e) { toast(e.message); }
}

// ================================================================== REFUND
let refundBill = null;

function wireRefund() {
  $("#refund-find").addEventListener("submit", (e) => { e.preventDefault(); findForRefund($("#refund-invoice").value); });
}

async function loadRecentForRefund() {
  try {
    const out = await api("GET", "/bills?store=" + S.store, null, "Bills today");
    const done = out.bills.filter((b) => b.status === "synced");
    put("#refund-recent", done.length ? [h("h3", { text: "Today's bills at " + S.store }),
      done.map((b) => h("div", { class: "checkline" },
        h("div", { class: "grow" }, h("b", { text: b.invoice_no }), " · " + b.customer_name + " · " + rs(b.total),
          b.refunded ? h("span", { class: "chip warn", text: "refunded " + rs(b.refunded) }) : null),
        h("button", { class: "small", text: "Select", onclick: () => findForRefund(b.invoice_no) })))] : null);
  } catch (e) { put("#refund-recent"); }
}

async function findForRefund(invoiceNo) {
  invoiceNo = String(invoiceNo || "").trim();
  if (!invoiceNo) return;
  $("#refund-invoice").value = invoiceNo;
  put("#refund-bill", h("div", { class: "busy" }, h("div", { class: "spinner" }), "Finding " + invoiceNo + "…"));
  try {
    refundBill = await api("GET", "/refunds/lookup?invoice_no=" + enc(invoiceNo) + "&store=" + S.store, null, "Find invoice");
  } catch (e) { refundBill = null; put("#refund-bill", errorBox(e)); return; }
  renderRefund();
}

function renderRefund(result) {
  const { bill, refunds, refunded_line_refs: done } = refundBill;
  const doneSet = new Set(done);
  const gross = (l) => l.mrp - l.discounts.reduce((n, d) => n + d[1], 0);
  const checks = {};
  const reason = h("select", {});
  options(reason, reasons("refund"), "", "Reason…");
  const approver = h("select", {});
  options(approver, managers().map((m) => [m.staff_id, m.name]), "", "Not needed");
  const note = h("input", { placeholder: "Note (optional)" });
  const total = h("b", {});
  const update = () => {
    const sum = bill.lines.filter((l) => checks[l.line_ref] && checks[l.line_ref].checked).reduce((n, l) => n + gross(l), 0);
    total.textContent = rs(sum);
  };
  const wrongStore = bill.store_code !== S.store;
  const lines = bill.lines.map((l) => {
    const cb = h("input", { type: "checkbox", checked: !doneSet.has(l.line_ref), disabled: doneSet.has(l.line_ref) || wrongStore, onchange: update });
    checks[l.line_ref] = cb;
    return h("label", { class: "checkline" }, cb,
      h("div", { class: "grow" }, h("div", { text: l.description }),
        h("div", { class: "small muted", text: (l.kind === "frame" ? "goes back on the shelf at " + bill.store_code : "lens: made to order, not restocked") +
          " · tax " + rs(l.cgst + l.sgst) + " reversed as charged" })),
      h("div", { class: "num" }, rs(gross(l)), doneSet.has(l.line_ref) ? h("div", {}, h("span", { class: "chip", text: "refunded" })) : null));
  });
  update();
  const submit = h("button", { class: "primary", text: "Refund selected lines", disabled: wrongStore || bill.lines.every((l) => doneSet.has(l.line_ref)), onclick: async () => {
    const chosen = bill.lines.filter((l) => checks[l.line_ref].checked).map((l) => l.line_ref);
    if (!chosen.length) return toast("Tick at least one line.");
    if (!reason.value) return toast("Choose a reason.");
    submit.disabled = true;
    submit.textContent = bill.payment_method === "cash" ? "Taking cash from the drawer…" : "Refunding on the terminal…";
    try {
      const r = await api("POST", "/refunds", { invoice_no: bill.invoice_no, store_code: S.store, staff_id: S.staffId, reason_code: reason.value,
        lines: chosen, approver_id: approver.value || null, note: note.value || null }, "Refund");
      refundBill = await api("GET", "/refunds/lookup?invoice_no=" + enc(bill.invoice_no) + "&store=" + S.store, null, "Find invoice");
      renderRefund(r);
    } catch (e) {
      put("#refund-result", errorBox(e));
      submit.disabled = false;
      submit.textContent = "Refund selected lines";
    }
  } });
  put("#refund-bill",
    h("div", { class: "row spread" }, h("div", {}, h("div", { class: "big", text: bill.invoice_no }),
      h("div", { class: "muted", text: bill.customer_name + " · " + bill.business_date + " · " + method(bill.payment_method || "") + " · " + rs(bill.total) })),
    statusChip(bill.status)),
    bill.source === "imported" ? h("div", { class: "notice info" }, h("b", { text: "Billed before the switch-over; read from Shopify." })) : null,
    wrongStore ? h("div", { class: "notice bad" }, h("b", { text: "Billed at " + bill.store_code + ". Refund it there: the frame restocks there and that drawer took the money." })) : null,
    h("div", { id: "refund-result" }, result ? h("div", { class: "result " + (result.status === "synced" ? "ok" : "warn") },
      h("h3", { text: "Refunded " + rs(result.amount) + " · credit note " + result.credit_note_no }),
      h("div", { class: "small", text: result.method === "cash" ? "Hand the cash back from the drawer." : "Returned to the customer's " + method(result.method) + " (terminal ref " + result.terminal_refund_id + ")." }),
      h("div", { class: "small", text: result.status === "synced" ? "Recorded in Shopify." : "Shopify not updated yet; it's retried on the next sync." }),
      h("a", { href: "/credit-notes/" + enc(result.credit_note_no) + ".txt", target: "_blank", rel: "noopener" }, h("button", { class: "small", text: "Print credit note" }))) : null),
    h("h3", { text: "Lines" }), lines,
    h("div", { class: "row wrap" },
      h("label", { class: "field" }, "Reason", reason),
      h("label", { class: "field" }, "Manager approval", approver),
      h("label", { class: "field grow" }, "Note", note)),
    h("div", { class: "row spread" }, h("div", {}, "Refund ", total, " to " + method(bill.payment_method || "")), submit),
    refunds.length ? [h("h3", { text: "Refunds so far" }), refunds.map((r) => h("div", { class: "checkline" },
      h("div", { class: "grow" }, h("b", { text: r.credit_note_no || r.refund_id }), " · " + rs(r.amount) + " · " + r.reason_code + " · lines " + r.lines.map((l) => l.line_ref).join(", ")),
      statusChip(r.status),
      r.credit_note_no ? h("a", { href: "/credit-notes/" + enc(r.credit_note_no) + ".txt", target: "_blank", rel: "noopener" }, h("button", { class: "small ghost", text: "Credit note" })) : null))] : null);
}

// ================================================================== TILL
function wireTill() {
  $("#till-reload").addEventListener("click", loadTill);
}

async function loadTill() {
  let t = null;
  try {
    t = await api("GET", "/tills/" + S.store, null, "Cash drawer");
  } catch (e) {
    if (e.code !== "till_not_open") { put("#till", errorBox(e)); return; }
  }
  renderTill(t);
}

function rupeeInput(placeholder, value) {
  return h("input", { type: "number", inputmode: "decimal", min: "0", step: "0.01", placeholder, value: value || "" });
}

async function tillAction(action, body, label) {
  try {
    const t = await api("POST", "/tills/" + S.store + "/" + action, Object.assign({ staff_id: S.staffId }, body), label);
    renderTill(t.status === "open" ? t : null, t.status !== "open" ? t : null);
  } catch (e) { toast(e.message); }
}

function renderTill(t, closed) {
  if (!t) {
    const float = rupeeInput("Opening float in ₹", "5000");
    put("#till",
      closed ? h("div", { class: "result " + (closed.variance_flagged ? "bad" : closed.variance_paise ? "warn" : "ok") },
        h("h3", { text: "Drawer closed" }),
        h("div", { text: "Expected " + rs(closed.expected_cash_paise) + " · counted " + rs(closed.counted_cash_paise) + " · variance " + rs(closed.variance_paise) }),
        closed.variance_flagged ? h("div", { class: "small", text: "Over the alert threshold: flagged to finance." }) : null) : null,
      h("p", { class: "muted", text: "No open cash drawer at " + S.store + ". Cash bills and cash refunds need one." }),
      h("div", { class: "row wrap" }, h("label", { class: "field" }, "Opening float (₹)", float),
        h("button", { class: "primary", text: "Open drawer", onclick: () => tillAction("open", { amount_paise: toPaise(float.value || 0) }, "Open drawer") })));
    return;
  }
  const kind = h("select", {});
  options(kind, [["paid-out", "Paid out (money taken from the drawer)"], ["paid-in", "Paid in (money added)"]], "paid-out");
  const reason = h("select", {});
  const fillReasons = () => options(reason, reasons(kind.value.replace("-", "_")), "", "Reason…");
  kind.addEventListener("change", fillReasons);
  fillReasons();
  const amount = rupeeInput("Amount in ₹");
  const note = h("input", { placeholder: "Note (needed for Other)" });
  const counted = rupeeInput("Counted cash in ₹");
  const closeNote = h("input", { placeholder: "Note about any difference" });
  const sign = { cash_sale: "Cash sale", cash_refund: "Cash refund", paid_in: "Paid in", paid_out: "Paid out" };
  put("#till",
    h("div", { class: "stats" },
      h("div", { class: "stat" }, h("span", { text: "Expected in drawer" }), h("b", { text: rs(t.expected_cash_paise) })),
      h("div", { class: "stat" }, h("span", { text: "Opening float" }), h("b", { text: rs(t.opening_float_paise) })),
      h("div", { class: "stat" }, h("span", { text: "Opened" }), h("b", { text: hhmm(t.opened_at) }), h("span", { text: staffName(t.opened_by) }))),
    t.movements.length ? h("div", { class: "tablewrap" }, h("table", {},
      h("tr", {}, ["Time", "What", "Ref", "By", "Amount"].map((x, i) => h("th", { class: i === 4 ? "num" : "", text: x }))),
      t.movements.map((m) => h("tr", {}, h("td", { text: hhmm(m.at) }), h("td", {}, sign[m.type] || m.type, m.reason ? h("div", { class: "small muted", text: m.reason + (m.note ? " · " + m.note : "") }) : null),
        h("td", { class: "small", text: m.ref }), h("td", { class: "small", text: m.staff_id }), h("td", { class: "num", text: rs(m.amount_paise) })))))
      : h("p", { class: "muted", text: "No cash movements yet." }),
    h("h3", { text: "Paid in / paid out" }),
    h("div", { class: "row wrap" }, kind, reason, amount, note,
      h("button", { text: "Record", onclick: () => {
        if (!reason.value || !amount.value) return toast("Choose a reason and an amount.");
        tillAction(kind.value, { amount_paise: toPaise(amount.value), reason_code: reason.value, note: note.value || null }, "Paid in/out");
      } })),
    h("h3", { text: "Close the drawer" }),
    h("div", { class: "row wrap" }, counted, closeNote,
      h("button", { class: "danger", text: "Count and close", onclick: () => {
        if (counted.value === "") return toast("Enter the counted cash.");
        tillAction("close", { amount_paise: toPaise(counted.value), note: closeNote.value || null }, "Close drawer");
      } })));
}

// ================================================================== DAY-END
function wireDayEnd() {
  $("#dayend-form").addEventListener("submit", (e) => { e.preventDefault(); loadDayEnd(); });
}

function methodTable(title, rows) {
  const entries = Object.entries(rows || {});
  if (!entries.length) return null;
  return h("div", {}, h("h3", { text: title }), h("div", { class: "tablewrap" }, h("table", {},
    h("tr", {}, ["", "Bills", "Sales", "Refunds", "Net"].map((x, i) => h("th", { class: i ? "num" : "", text: x }))),
    entries.map(([k, v]) => h("tr", {}, h("td", { text: method(k) }), h("td", { class: "num", text: v.bills }), h("td", { class: "num", text: rs(v.sales) }),
      h("td", { class: "num", text: v.refunds ? "−" + rs(v.refunds) : "" }), h("td", { class: "num", text: rs(v.net) }))))));
}

async function loadDayEnd() {
  const day = $("#dayend-date").value || S.meta.business_date;
  put("#dayend", h("div", { class: "busy" }, h("div", { class: "spinner" }), "Reading the day from Shopify…"));
  put("#channels");
  let r, ch;
  try {
    [r, ch] = await Promise.all([api("GET", "/reports/day-end?store=" + S.store + "&date=" + day, null, "Day-end"),
      api("GET", "/reports/channels?date=" + day, null, "Channels")]);
  } catch (e) { put("#dayend", errorBox(e)); return; }
  const sev = { error: "bad", warning: "warn", info: "info" };
  const stat = (label, value) => h("div", { class: "stat" }, h("span", { text: label }), h("b", { text: value }));
  const till = r.till;
  put("#dayend",
    h("div", { class: "stats" },
      stat("Bills", r.bills + (r.zero_value_bills ? " (" + r.zero_value_bills + " at ₹0)" : "")), stat("Net sales", rs(r.net_sales)),
      stat("Refunds", r.refunds.count + " · −" + rs(r.refunds.amount)), stat("Net after refunds", rs(r.net_after_refunds)),
      stat("Discounts", rs(r.discounts)), stat("GST (CGST + SGST)", rs(r.tax)), stat("Taxable value", rs(r.taxable)), stat("Average bill", rs(r.average_bill))),
    r.findings.length ? h("div", {}, h("h3", { text: "Checks" }), r.findings.map((f) => h("div", { class: "notice " + (sev[f.severity] || "info") }, h("b", { text: f.message }))))
      : h("div", { class: "notice ok" }, h("b", { text: "Shopify, the invoice series, credit notes and the drawer all agree." })),
    methodTable("By payment method", r.by_payment_method),
    methodTable("By channel", r.by_channel),
    Object.keys(r.discounts_by_offer).length ? h("div", {}, h("h3", { text: "Discounts by offer" }), h("div", { class: "tablewrap" }, h("table", {},
      Object.entries(r.discounts_by_offer).map(([k, v]) => h("tr", {}, h("td", { text: k }), h("td", { class: "num", text: rs(v) })))))) : null,
    till ? h("div", {}, h("h3", { text: "Cash drawer" }), h("dl", { class: "kv" },
      h("dt", { text: "Session" }), h("dd", { text: till.session_id + " (" + till.status + ")" }),
      h("dt", { text: "Float + sales − refunds + in − out" }), h("dd", { text: rs(till.opening_float) + " + " + rs(till.cash_sales) + " − " + rs(till.cash_refunds) + " + " + rs(till.paid_in) + " − " + rs(till.paid_out) }),
      h("dt", { text: "Expected" }), h("dd", { text: rs(till.expected) }),
      h("dt", { text: "Counted" }), h("dd", { text: till.counted === null ? "not closed yet" : rs(till.counted) }),
      h("dt", { text: "Variance" }), h("dd", { text: till.variance === null ? "–" : rs(till.variance) + (till.flagged ? " (flagged)" : "") }))) : null,
    r.invoices.length ? h("details", {}, h("summary", { text: r.invoices.length + " invoice(s)" + (r.credit_notes.length ? ", " + r.credit_notes.length + " credit note(s)" : "") }),
      h("p", { class: "small", text: r.invoices.join(", ") }), r.credit_notes.length ? h("p", { class: "small", text: r.credit_notes.join(", ") }) : null) : null);
  const chans = S.meta.channels;
  put("#channels", h("div", { class: "tablewrap" }, h("table", {},
    h("tr", {}, h("th", { text: "Store" }), chans.map((c) => h("th", { class: "num", text: titleCase(c) }))),
    Object.entries(ch).map(([st, row]) => h("tr", {}, h("td", { text: st }),
      chans.map((c) => h("td", { class: "num", text: row[c] ? row[c].bills + " · " + rs(row[c].net) : "–" })))))));
}

// ================================================================== DEMO CONTROLS
async function loadSandbox() {
  try {
    S.sandbox = await api("GET", "/admin/sandbox", null, "Demo status");
  } catch (e) { put("#sandbox", errorBox(e)); return; }
  renderSandbox();
}

function renderSandbox() {
  const sb = S.sandbox;
  put("#mode", h("span", { class: "chip " + (sb.mock_flaky ? "warn" : "ok"), text: "Demo · mock 429/503 " + (sb.mock_flaky ? "ON" : "off") }),
    sb.armed.length ? h("span", { class: "chip bad", text: sb.armed.length + " fault(s) armed" }) : null);
  put("#sample-phones", sb.sample_customers.map((c) => h("option", { value: c.phone, text: c.name + (c.tags.length ? " · " + c.tags.join(", ") : "") })));
  const flaky = h("input", { type: "checkbox", checked: sb.mock_flaky });
  const arm = async (name) => {
    try { S.sandbox = await api("POST", "/admin/faults", { fault: name }, name === "clear" ? "Clear faults" : "Arm: " + name); } catch (e) { toast(e.message); }
    renderSandbox();
  };
  put("#sandbox",
    h("h3", { text: "The mock's own failures" }),
    h("p", { text: sb.mock_flaky ? "On: about 10% of Shopify writes answer 429 (too many requests) and ~30% of new terminal charges answer 503 once. The till retries them; watch the Network bar at the bottom."
      : "Off: the mock answers every call first time." }),
    h("h3", { text: "Failures the mock can't produce" }),
    h("p", { class: "small muted", text: "Arm one, then bill as usual. Each fires on the next matching call." }),
    h("div", { class: "row wrap" }, sb.available.map((f) => h("button", { text: f.label, onclick: () => arm(f.name) }))),
    sb.armed.length ? h("div", { class: "notice warn" }, h("b", { text: "Armed" }),
      sb.armed.map((r) => h("div", { class: "hint", text: r.kind + " on " + r.call + (r.left > 1000 ? " (until cleared)" : " (next " + r.left + ")") })),
      h("button", { class: "small", text: "Clear all", onclick: () => arm("clear") })) : h("p", { class: "small muted", text: "Nothing armed." }),
    sb.log.length ? h("details", {}, h("summary", { class: "small", text: "What fired" }), sb.log.slice().reverse().map((l) => h("div", { class: "small", text: l }))) : null,
    h("h3", { text: "Start over" }),
    h("div", { class: "row wrap" }, h("label", { class: "row" }, flaky, "Mock 429/503 on"),
      h("button", { class: "danger", text: "Reset everything to the seed data", onclick: async () => {
        if (!confirm("Wipe every bill, refund and drawer and restart the mock?")) return;
        try {
          S.sandbox = await api("POST", "/admin/reset", { flaky: flaky.checked }, "Reset demo");
          toast("Reset. Fresh Shopify, store tool and terminal.");
        } catch (e) { toast(e.message); }
        renderSandbox();
        newBill();
      } })),
    h("h3", { text: "Things to try" }),
    h("ul", { class: "small" },
      h("li", { text: "Priority customer, campaign offer: Karthik +919000000005 at HSR, FR-0003 with progressive 1.60 blue cut." }),
      h("li", { text: "Lens the prescription can't take: Sanjay +919000000021 at BR, progressive 1.56: blocked with the indices that work." }),
      h("li", { text: "External prescription with an odd axis: Suresh +919000000003 at JN, single vision 1.56: tick to confirm." }),
      h("li", { text: "Out of stock here: FR-0009 at HSR shows 0 and where it is in stock." }),
      h("li", { text: "Last frame, two tills: open this page in two tabs at HSR, pick FR-0005 in both, arm 'slow UPI' and pay on one; pay on the other while it waits." }),
      h("li", { text: "Declined card: arm it, pay by card, then pay by UPI. The frame stays reserved in between." }),
      h("li", { text: "Shopify down after payment: arm it, pay; the invoice is issued, the bill waits. Clear it and press 'Send to Shopify'." }),
      h("li", { text: "Cash: open the drawer first (Cash drawer tab), bill by cash, refund by cash, close with a count, then run Day-end." })),
    h("h3", { text: "Sample customers" }),
    h("div", { class: "tablewrap" }, h("table", {}, sb.sample_customers.map((c) => h("tr", {},
      h("td", { text: c.name }), h("td", { class: "small", text: c.phone }), h("td", { class: "small muted", text: c.tags.join(", ") }),
      h("td", {}, h("button", { class: "small", text: "Bill", onclick: () => { showTab("bill"); lookup(c.phone); } })))))));
}

init();
