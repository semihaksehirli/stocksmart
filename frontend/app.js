/* StockSmart web front end — plain JavaScript, no build step.
 * Talks to the FastAPI backend mounted at /api (see main.py).
 * Set window.STOCKSMART_API before this script loads to point elsewhere.
 */
(() => {
  "use strict";

  const API = (window.STOCKSMART_API || "/api").replace(/\/$/, "");
  const $app = document.getElementById("app");
  const $modal = document.getElementById("modal");

  // ---------- session ----------
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
    del(k) { try { localStorage.removeItem(k); } catch { /* ignore */ } },
  };
  const session = {
    token: store.get("ss_token"),
    user: JSON.parse(store.get("ss_user") || "null"),
  };
  const isOwner = () => session.user?.role === "owner";

  function saveSession(token, user) {
    session.token = token; session.user = user;
    store.set("ss_token", token); store.set("ss_user", JSON.stringify(user));
  }
  function logout() {
    session.token = null; session.user = null;
    store.del("ss_token"); store.del("ss_user");
    location.hash = "#/login";
  }

  // ---------- API ----------
  class ApiError extends Error {}

  async function api(path, { method = "GET", body, raw = false } = {}) {
    const headers = {};
    if (session.token) headers.Authorization = `Bearer ${session.token}`;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    let res;
    try {
      res = await fetch(API + path, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
    } catch {
      throw new ApiError("Can't reach the StockSmart server. Check that it is running.");
    }
    if (res.status === 401 && path !== "/auth/login") {
      logout();
      throw new ApiError("Your session has ended. Log in again.");
    }
    if (!res.ok) {
      let msg = `Request failed (${res.status}).`;
      try {
        const data = await res.json();
        if (typeof data.detail === "string") msg = data.detail;
        else if (Array.isArray(data.detail)) msg = data.detail.map(d => `${d.loc?.at(-1) ?? "field"}: ${d.msg}`).join("; ");
      } catch { /* not JSON */ }
      throw new ApiError(msg);
    }
    if (raw) return res;
    return res.status === 204 ? null : res.json();
  }

  // ---------- helpers ----------
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const money = n => "$" + Number(n || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const int = n => Number(n || 0).toLocaleString("en-US");
  const when = iso => {
    const d = new Date(iso.endsWith("Z") || /[+-]\d\d:\d\d$/.test(iso) ? iso : iso + "Z");
    return d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  };
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

  function toast(msg, kind = "") {
    const el = document.createElement("div");
    el.className = `toast ${kind}`; el.textContent = msg; el.setAttribute("role", kind === "error" ? "alert" : "status");
    document.getElementById("toasts").append(el);
    setTimeout(() => el.remove(), 4200);
  }
  const fail = e => toast(e.message || String(e), "error");

  const ICONS = {
    dashboard: '<path d="M3 13h8V3H3zM13 21h8V11h-8zM3 21h8v-6H3zM13 3v6h8V3z"/>',
    products: '<path d="M21 8 12 3 3 8v8l9 5 9-5z"/><path d="m3 8 9 5 9-5M12 13v8"/>',
    record: '<path d="M12 5v14M5 12h14"/>',
    history: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l3 2"/>',
    suppliers: '<path d="M1 7h13v10H1zM14 10h4l3 3v4h-7z"/><circle cx="5.5" cy="18" r="2"/><circle cx="17.5" cy="18" r="2"/>',
    assistant: '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/>',
    pos: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M7 20h10M12 16v4M7 8h4"/>',
    copy: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/>',
    back: '<path d="M15 18l-6-6 6-6"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    download: '<path d="M12 3v12m0 0-4-4m4 4 4-4M4 19h16"/>',
    close: '<path d="M6 6l12 12M18 6 6 18"/>',
  };
  const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`;

  /** The shelf gauge: stock level as a bar, with a mark at the reorder level. */
  function gauge(qty, reorder, { big = false, max } = {}) {
    const scale = Math.max(max ?? 0, reorder * 3, qty, 1);
    const pct = Math.min(100, (qty / scale) * 100);
    const mark = Math.min(100, (reorder / scale) * 100);
    const low = qty <= reorder;
    return `<div class="gauge ${low ? "is-low" : ""} ${big ? "big" : ""}" title="${qty} on hand · reorder at ${reorder}">
      <div class="gauge-track" role="img" aria-label="${qty} on hand, reorder at ${reorder}">
        <div class="gauge-fill" style="width:${pct}%"></div>
        <div class="gauge-mark" style="left:calc(${mark}% - 1px)"></div>
      </div>
      <span class="gauge-qty">${qty}</span>
    </div>`;
  }

  // ---------- cached reference data ----------
  const cache = { suppliers: null };
  async function suppliers(force = false) {
    if (!cache.suppliers || force) cache.suppliers = await api("/suppliers");
    return cache.suppliers;
  }
  const supplierName = (list, id) => list.find(s => s.id === id)?.name ?? "";

  // ---------- shell ----------
  const NAV = [
    ["dashboard", "Dashboard"],
    ["products", "Products"],
    ["record", "Record stock"],
    ["history", "History"],
    ["suppliers", "Suppliers"],
    ["assistant", "Assistant"],
    ["pos", "Point of sale", "owner"],
  ];

  function shell(active) {
    $app.innerHTML = `
      <div class="shell">
        <aside class="side">
          <div class="brand"><span class="brand-mark" aria-hidden="true"></span>StockSmart</div>
          <nav class="nav" aria-label="Main">
            ${NAV.filter(n => !n[2] || isOwner()).map(([k, label]) => `<a href="#/${k}" ${k === active ? 'aria-current="page"' : ""} title="${label}">${icon(k)}<span>${label}</span></a>`).join("")}
          </nav>
          <div class="side-foot">
            <strong>${esc(session.user?.username)}</strong>
            <span class="hide-sm">${isOwner() ? "Owner" : "Employee"}</span>
            <button type="button" id="logout">Log out</button>
          </div>
        </aside>
        <main class="main" id="view"><p class="loading">Loading…</p></main>
      </div>`;
    document.getElementById("logout").onclick = logout;
    return document.getElementById("view");
  }

  // ---------- views ----------
  async function viewLogin() {
    $app.innerHTML = `
      <div class="login">
        <section class="login-art" aria-hidden="true">
          <div class="brand"><span class="brand-mark"></span>StockSmart</div>
          <div class="shelves">
            ${[["House Blend Beans", 24, 8], ["Oat Milk 64oz", 4, 6], ["Sea Salt Chips", 40, 15], ["To-Go Cups", 3, 5]].map(([n, q, r]) => `
              <div class="shelf"><div class="shelf-row"><strong>${n}</strong><span>${q} on hand</span></div>${gauge(q, r, { max: 45 }).replace('<span class="gauge-qty">' + q + "</span>", "")}</div>`).join("")}
          </div>
          <p class="login-tag">Know what’s on the shelf before it runs out.</p>
        </section>
        <section class="login-form">
          <form id="login-form" novalidate>
            <div><h1>Log in</h1><p class="sub">Use your StockSmart account.</p></div>
            <label class="field">Username<input class="input" name="username" autocomplete="username" required autofocus></label>
            <label class="field">Password<input class="input" name="password" type="password" autocomplete="current-password" required></label>
            <p class="err" id="login-err"></p>
            <button class="btn primary" type="submit">Log in</button>
            <p class="demo">Demo accounts: <button type="button" data-demo="owner">owner</button> (full access) or <button type="button" data-demo="employee">employee</button> (sales and deliveries).</p>
          </form>
        </section>
      </div>`;
    const form = document.getElementById("login-form");
    const err = document.getElementById("login-err");
    form.querySelectorAll("[data-demo]").forEach(b => b.onclick = () => {
      form.username.value = b.dataset.demo; form.password.value = b.dataset.demo + "1234"; form.requestSubmit();
    });
    form.onsubmit = async e => {
      e.preventDefault();
      err.textContent = "";
      const username = form.username.value.trim(), password = form.password.value;
      if (!username || !password) { err.textContent = "Enter your username and password."; return; }
      const btn = form.querySelector("button[type=submit]");
      btn.disabled = true; btn.textContent = "Logging in…";
      try {
        const t = await api("/auth/login", { method: "POST", body: { username, password } });
        session.token = t.access_token;
        const me = await api("/auth/me").catch(() => ({ username, role: t.role }));
        saveSession(t.access_token, { username: me.username, role: me.role });
        cache.suppliers = null;
        location.hash = "#/dashboard";
      } catch (ex) {
        err.textContent = ex.message;
        btn.disabled = false; btn.textContent = "Log in";
      }
    };
  }

  async function viewDashboard() {
    const view = shell("dashboard");
    const [d, low, sups] = await Promise.all([api("/reports/dashboard"), api("/alerts/low-stock"), suppliers()]);
    const n = low.length;
    const topMax = Math.max(1, ...d.top_sellers.map(t => t.units_sold));
    view.innerHTML = `
      <section class="hero ${n ? "" : "all-good"}">
        <h1 class="hero-line">${n ? `<span class="count">${plural(n, "item", "items")}</span> ${n === 1 ? "needs" : "need"} reordering` : `<span class="count">All stocked</span> nothing to reorder`}</h1>
        <p class="sub">${n ? "These are at or below their reorder level. The dark mark on each bar is where reordering starts." : "Every product is above its reorder level."}</p>
      </section>
      <div class="grid-2">
        <section class="panel">
          <h2>Reorder list</h2>
          ${n ? `<ul class="reorder-list">${low.map(p => {
            const s = sups.find(x => x.id === p.supplier_id);
            return `<li>
              <div><div class="pname">${esc(p.name)}</div><div class="supplier"><span class="sku">${esc(p.sku)}</span>${s ? ` · ${esc(s.name)}` : ""}</div></div>
              ${gauge(p.quantity, p.reorder_level)}
              ${s?.email ? `<a class="btn small" href="mailto:${esc(s.email)}?subject=${encodeURIComponent(`Reorder: ${p.name} (${p.sku})`)}">Email supplier</a>` : `<a class="btn small" href="#/record?product=${p.id}&type=delivery">Log delivery</a>`}
            </li>`; }).join("")}</ul>` : `<p class="empty">Nothing is running low.</p>`}
        </section>
        <div>
          <div class="stats">
            <div class="stat"><div class="stat-v">${int(d.product_count)}</div><div class="stat-l">Products</div></div>
            <div class="stat"><div class="stat-v">${int(d.units_on_hand)}</div><div class="stat-l">Units on hand</div></div>
            <div class="stat"><div class="stat-v">${int(d.units_sold)}</div><div class="stat-l">Units sold</div></div>
            <div class="stat"><div class="stat-v">${money(d.inventory_value_cost)}</div><div class="stat-l">Stock value at cost</div></div>
            <div class="stat"><div class="stat-v">${money(d.inventory_value_retail)}</div><div class="stat-l">Stock value at retail</div></div>
            <div class="stat"><div class="stat-v">${money(d.sales_revenue)}</div><div class="stat-l">Sales revenue</div></div>
          </div>
          <section class="panel">
            <h2>Top sellers</h2>
            ${d.top_sellers.length ? `<div class="bars">${d.top_sellers.map(t => `
              <div class="bar-row"><div class="bar-head"><span>${esc(t.name)}</span><span>${int(t.units_sold)} sold</span></div>
              <div class="bar" style="width:${(t.units_sold / topMax) * 100}%"></div></div>`).join("")}</div>` : `<p class="empty">No sales recorded yet.</p>`}
          </section>
        </div>
      </div>`;
  }

  async function viewProducts() {
    const view = shell("products");
    const [products, sups] = await Promise.all([api("/products"), suppliers()]);
    const cats = [...new Set(products.map(p => p.category))].sort();
    const maxQty = Math.max(1, ...products.map(p => p.quantity));
    view.innerHTML = `
      <div class="page-head">
        <div><h1>Products</h1><p class="sub">${plural(products.length, "product", "products")} in stock records.</p></div>
        ${isOwner() ? `<div class="toolbar" style="margin:0">
          <button class="btn" id="export">${icon("download")}Export CSV</button>
          <button class="btn primary" id="add">Add product</button></div>` : ""}
      </div>
      <div class="toolbar">
        <div class="search">${icon("search")}<input class="input" id="q" type="search" placeholder="Search by name or SKU" aria-label="Search products"></div>
        <select class="input" id="cat" aria-label="Category"><option value="">All categories</option>${cats.map(c => `<option>${esc(c)}</option>`).join("")}</select>
        <select class="input" id="status" aria-label="Stock status"><option value="">Any stock level</option><option value="low">Low stock only</option></select>
      </div>
      <div class="table-wrap"><table>
        <thead><tr><th>Product</th><th class="hide-sm">Category</th><th class="hide-sm">Supplier</th><th class="num">Price</th><th class="num hide-sm">Cost</th><th style="min-width:180px">On hand</th><th>Status</th>${isOwner() ? "<th><span class='sr-only' hidden>Actions</span></th>" : ""}</tr></thead>
        <tbody id="rows"></tbody>
      </table></div>`;

    const $rows = view.querySelector("#rows"), $q = view.querySelector("#q"), $cat = view.querySelector("#cat"), $st = view.querySelector("#status");
    const draw = () => {
      const q = $q.value.trim().toLowerCase(), c = $cat.value, st = $st.value;
      const list = products.filter(p => (!q || p.name.toLowerCase().includes(q) || p.sku.toLowerCase().includes(q)) && (!c || p.category === c) && (!st || p.is_low_stock));
      $rows.innerHTML = list.length ? list.map(p => `
        <tr>
          <td><div class="pname">${esc(p.name)}</div><div class="sku">${esc(p.sku)}</div></td>
          <td class="hide-sm">${esc(p.category)}</td>
          <td class="hide-sm">${esc(supplierName(sups, p.supplier_id)) || '<span class="hint">None</span>'}</td>
          <td class="num">${money(p.price)}</td>
          <td class="num hide-sm">${money(p.cost)}</td>
          <td>${gauge(p.quantity, p.reorder_level, { max: maxQty })}</td>
          <td>${p.is_low_stock ? '<span class="pill low">Reorder</span>' : '<span class="pill ok">In stock</span>'}</td>
          ${isOwner() ? `<td><div class="row-actions"><button class="btn small" data-edit="${p.id}">Edit</button><button class="btn small danger" data-del="${p.id}">Delete</button></div></td>` : ""}
        </tr>`).join("") : `<tr><td colspan="8" class="empty">No products match. Try a different search${isOwner() ? " or add a product" : ""}.</td></tr>`;
    };
    [$q, $cat, $st].forEach(el => el.addEventListener("input", draw));
    draw();

    if (isOwner()) {
      view.querySelector("#add").onclick = () => productForm(null, sups);
      view.querySelector("#export").onclick = exportCsv;
      $rows.addEventListener("click", async e => {
        const edit = e.target.closest("[data-edit]"), del = e.target.closest("[data-del]");
        if (edit) productForm(products.find(p => p.id === +edit.dataset.edit), sups);
        if (del) {
          const p = products.find(x => x.id === +del.dataset.del);
          confirmBox(`Delete ${p.name}?`, "This also deletes its stock history. It can't be undone.", "Delete product", async () => {
            await api(`/products/${p.id}`, { method: "DELETE" });
            toast(`Deleted ${p.name}.`); route();
          });
        }
      });
    }
  }

  async function exportCsv() {
    try {
      const res = await api("/reports/inventory.csv", { raw: true });
      const url = URL.createObjectURL(await res.blob());
      const a = Object.assign(document.createElement("a"), { href: url, download: `inventory-${new Date().toISOString().slice(0, 10)}.csv` });
      document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast("Exported inventory CSV.");
    } catch (e) { fail(e); }
  }

  function openModal(html, onSubmit) {
    $modal.innerHTML = html;
    const form = $modal.querySelector("form");
    const err = $modal.querySelector(".err");
    $modal.querySelectorAll("[data-close]").forEach(b => b.onclick = () => $modal.close());
    form.onsubmit = async e => {
      e.preventDefault();
      if (err) err.textContent = "";
      const btn = form.querySelector("button[type=submit]");
      btn.disabled = true;
      try { await onSubmit(form); $modal.close(); }
      catch (ex) { if (err) err.textContent = ex.message; else fail(ex); }
      finally { btn.disabled = false; }
    };
    $modal.showModal();
    form.querySelector("input:not([readonly]), select, textarea")?.focus();
  }

  const modalHead = title => `<div class="modal-head"><h2>${esc(title)}</h2><button type="button" class="btn small" data-close aria-label="Close">${icon("close")}</button></div>`;

  function confirmBox(title, text, action, run) {
    openModal(`<form method="dialog">${modalHead(title)}<p style="margin:0">${esc(text)}</p><p class="err"></p>
      <div class="modal-foot"><button type="button" class="btn" data-close>Cancel</button><button type="submit" class="btn primary">${esc(action)}</button></div></form>`, run);
  }

  function productForm(p, sups) {
    const editing = !!p;
    openModal(`<form novalidate>
      ${modalHead(editing ? `Edit ${p.name}` : "Add product")}
      <div class="form-grid">
        <label class="field">SKU<input class="input" name="sku" value="${esc(p?.sku)}" ${editing ? "readonly" : "required"} placeholder="COF-004"></label>
        <label class="field">Category<input class="input" name="category" value="${esc(p?.category ?? "General")}" list="cat-list"></label>
        <label class="field span-2">Name<input class="input" name="name" value="${esc(p?.name)}" required></label>
        <label class="field">Selling price ($)<input class="input" name="price" type="number" min="0" step="0.01" value="${p?.price ?? ""}" required></label>
        <label class="field">Cost ($)<input class="input" name="cost" type="number" min="0" step="0.01" value="${p?.cost ?? ""}" required></label>
        ${editing ? `<label class="field">On hand<input class="input" value="${p.quantity}" readonly><span class="hint">Change stock from Record stock.</span></label>`
                  : `<label class="field">Opening quantity<input class="input" name="quantity" type="number" min="0" step="1" value="0"></label>`}
        <label class="field">Reorder at<input class="input" name="reorder_level" type="number" min="0" step="1" value="${p?.reorder_level ?? 5}"></label>
        <label class="field span-2">Supplier<select class="input" name="supplier_id"><option value="">No supplier</option>${sups.map(s => `<option value="${s.id}" ${p?.supplier_id === s.id ? "selected" : ""}>${esc(s.name)}</option>`).join("")}</select></label>
      </div>
      <p class="err"></p>
      <div class="modal-foot"><button type="button" class="btn" data-close>Cancel</button><button type="submit" class="btn primary">${editing ? "Save changes" : "Add product"}</button></div>
    </form>`, async f => {
      const num = (v, fallback) => v === "" ? fallback : Number(v);
      const body = {
        name: f.name.value.trim(), category: f.category.value.trim() || "General",
        price: num(f.price.value, NaN), cost: num(f.cost.value, NaN),
        reorder_level: num(f.reorder_level.value, 5),
        supplier_id: f.supplier_id.value ? Number(f.supplier_id.value) : null,
      };
      if (!editing) { body.sku = f.sku.value.trim(); body.quantity = num(f.quantity.value, 0); }
      if (!editing && !body.sku) throw new Error("Enter a SKU.");
      if (!body.name) throw new Error("Enter a product name.");
      if (Number.isNaN(body.price) || Number.isNaN(body.cost)) throw new Error("Enter a selling price and a cost.");
      if (editing) { await api(`/products/${p.id}`, { method: "PATCH", body }); toast(`Saved ${body.name}.`); }
      else { await api("/products", { method: "POST", body }); toast(`Added ${body.name}.`); }
      route();
    });
  }

  async function viewRecord(params) {
    const view = shell("record");
    const products = await api("/products");
    const owner = isOwner();
    let type = ["sale", "delivery", "adjustment"].includes(params.get("type")) ? params.get("type") : "sale";
    if (type === "adjustment" && !owner) type = "sale";
    let selected = products.find(p => p.id === +params.get("product")) || null;

    view.innerHTML = `
      <div class="page-head"><div><h1>Record stock</h1><p class="sub">Log a sale, a delivery from a supplier, or ${owner ? "an adjustment for damaged or miscounted items" : "ask an owner to make adjustments"}.</p></div></div>
      <div class="record">
        <form class="panel" id="mv" novalidate>
          <div class="field" style="display:grid;gap:6px"><span style="font-size:13px;font-weight:600;color:var(--muted)">Type</span>
            <div class="seg" role="group" aria-label="Movement type">
              <button type="button" data-t="sale">Sale</button>
              <button type="button" data-t="delivery">Delivery</button>
              <button type="button" data-t="adjustment" ${owner ? "" : 'disabled title="Owners only"'}>Adjustment</button>
            </div>
          </div>
          <label class="field">Product
            <div class="combo">
              <input class="input" id="pq" role="combobox" aria-expanded="false" aria-controls="plist" autocomplete="off" placeholder="Type a name or SKU">
              <ul class="combo-list" id="plist" role="listbox" hidden></ul>
            </div>
          </label>
          <div class="field" style="display:grid;gap:6px"><span style="font-size:13px;font-weight:600;color:var(--muted)" id="qty-l">Quantity</span>
            <div class="stepper"><button type="button" data-step="-1" aria-label="Decrease">−</button><input id="qty" type="number" step="1" value="1" aria-labelledby="qty-l"><button type="button" data-step="1" aria-label="Increase">+</button></div>
            <span class="hint" id="qty-hint"></span>
          </div>
          <label class="field">Note (optional)<input class="input" id="note" maxlength="200" placeholder="e.g. Invoice #4471"></label>
          <p class="err" id="mv-err"></p>
          <div><button class="btn primary" type="submit" id="save">Save</button></div>
        </form>
        <aside class="panel preview" id="preview"></aside>
      </div>`;

    const $pq = view.querySelector("#pq"), $list = view.querySelector("#plist"), $qty = view.querySelector("#qty");
    const $prev = view.querySelector("#preview"), $err = view.querySelector("#mv-err"), $save = view.querySelector("#save"), $hint = view.querySelector("#qty-hint");
    let active = -1, matches = [];

    const LABEL = { sale: "Record sale", delivery: "Record delivery", adjustment: "Record adjustment" };
    const change = () => { const q = parseInt($qty.value, 10) || 0; return type === "sale" ? -q : q; };

    function paint() {
      $err.textContent = "";
      view.querySelectorAll("[data-t]").forEach(b => b.setAttribute("aria-pressed", b.dataset.t === type));
      $save.textContent = LABEL[type];
      $hint.textContent = type === "adjustment" ? "Use a negative number to remove items, positive to add." : "";
      $qty.min = type === "adjustment" ? "" : "1";
      if (!selected) { $prev.innerHTML = `<p class="hint" style="margin:0">Pick a product to see its stock level.</p>`; return; }
      const after = selected.quantity + change();
      const bad = after < 0;
      $prev.innerHTML = `
        <h3>${esc(selected.name)}</h3><div class="sku">${esc(selected.sku)}</div>
        <div style="margin-top:14px">${gauge(selected.quantity, selected.reorder_level, { big: true })}</div>
        <p class="hint" style="margin:6px 0 0">On hand now · reorder at ${selected.reorder_level}</p>
        <div class="after"><div class="hint">After this ${type}</div>
          <div class="after-v" style="color:${bad ? "var(--low)" : "inherit"}">${bad ? "Not enough stock" : after}</div>
          ${!bad && after <= selected.reorder_level ? '<span class="pill low">Will need reordering</span>' : ""}
        </div>`;
    }

    function openList(show) {
      $list.hidden = !show; $pq.setAttribute("aria-expanded", show);
    }
    function drawList() {
      const q = $pq.value.trim().toLowerCase();
      matches = products.filter(p => !q || p.name.toLowerCase().includes(q) || p.sku.toLowerCase().includes(q)).slice(0, 30);
      active = matches.length ? 0 : -1;
      $list.innerHTML = matches.length ? matches.map((p, i) => `<li role="option" id="opt-${p.id}" data-id="${p.id}" aria-selected="${i === active}"><span><span class="pname">${esc(p.name)}</span> <span class="sku">${esc(p.sku)}</span></span><span class="hint">${p.quantity} on hand</span></li>`).join("")
        : `<li class="hint" aria-disabled="true">No product matches “${esc($pq.value)}”.</li>`;
      openList(true);
    }
    function pick(p) {
      selected = p; $pq.value = `${p.name} (${p.sku})`; openList(false); paint(); $qty.focus(); $qty.select();
    }
    $pq.addEventListener("focus", () => { $pq.select(); drawList(); });
    $pq.addEventListener("input", () => { selected = null; paint(); drawList(); });
    $pq.addEventListener("keydown", e => {
      if ($list.hidden || !matches.length) return;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        active = (active + (e.key === "ArrowDown" ? 1 : -1) + matches.length) % matches.length;
        $list.querySelectorAll("li").forEach((li, i) => li.setAttribute("aria-selected", i === active));
        $list.children[active]?.scrollIntoView({ block: "nearest" });
      } else if (e.key === "Enter") { e.preventDefault(); pick(matches[active]); }
      else if (e.key === "Escape") openList(false);
    });
    $list.addEventListener("mousedown", e => { const li = e.target.closest("[data-id]"); if (li) { e.preventDefault(); pick(products.find(p => p.id === +li.dataset.id)); } });
    $pq.addEventListener("blur", () => setTimeout(() => openList(false), 100));

    view.querySelectorAll("[data-t]").forEach(b => b.onclick = () => { type = b.dataset.t; paint(); });
    view.querySelectorAll("[data-step]").forEach(b => b.onclick = () => {
      let v = (parseInt($qty.value, 10) || 0) + +b.dataset.step;
      if (type !== "adjustment" && v < 1) v = 1;
      if (type === "adjustment" && v === 0) v += +b.dataset.step;
      $qty.value = v; paint();
    });
    $qty.addEventListener("input", paint);

    view.querySelector("#mv").onsubmit = async e => {
      e.preventDefault(); $err.textContent = "";
      const q = parseInt($qty.value, 10);
      if (!selected) { $err.textContent = "Pick a product first."; $pq.focus(); return; }
      if (!q || (type !== "adjustment" && q < 1)) { $err.textContent = type === "adjustment" ? "Enter a number other than zero." : "Enter a quantity of 1 or more."; return; }
      $save.disabled = true;
      try {
        const m = await api("/movements", { method: "POST", body: { product_id: selected.id, type, quantity: q, note: view.querySelector("#note").value.trim() || null } });
        selected.quantity = m.quantity_after;
        toast(`Recorded ${type} of ${Math.abs(m.change)} × ${selected.name}. ${m.quantity_after} on hand.`);
        $qty.value = 1; view.querySelector("#note").value = ""; paint();
      } catch (ex) { $err.textContent = ex.message; }
      finally { $save.disabled = false; }
    };

    if (selected) $pq.value = `${selected.name} (${selected.sku})`;
    paint();
    if (!selected) $pq.focus();
  }

  async function viewHistory(params) {
    const view = shell("history");
    const pid = params.get("product") || "";
    const [products, moves] = await Promise.all([api("/products"), api(`/movements?limit=300${pid ? `&product_id=${pid}` : ""}`)]);
    const byId = Object.fromEntries(products.map(p => [p.id, p]));
    view.innerHTML = `
      <div class="page-head"><div><h1>History</h1><p class="sub">Every sale, delivery and adjustment, newest first.</p></div></div>
      <div class="toolbar">
        <select class="input" id="pf" aria-label="Filter by product"><option value="">All products</option>${products.map(p => `<option value="${p.id}" ${String(p.id) === pid ? "selected" : ""}>${esc(p.name)} (${esc(p.sku)})</option>`).join("")}</select>
        <select class="input" id="tf" aria-label="Filter by type"><option value="">All types</option><option value="sale">Sales</option><option value="delivery">Deliveries</option><option value="adjustment">Adjustments</option></select>
      </div>
      <div class="table-wrap"><table>
        <thead><tr><th>When</th><th>Product</th><th>Type</th><th class="num">Change</th><th class="num">On hand after</th><th class="hide-sm">Note</th></tr></thead>
        <tbody id="rows"></tbody></table></div>`;
    const $rows = view.querySelector("#rows"), $tf = view.querySelector("#tf");
    const draw = () => {
      const list = moves.filter(m => !$tf.value || m.type === $tf.value);
      $rows.innerHTML = list.length ? list.map(m => {
        const p = byId[m.product_id];
        return `<tr><td>${when(m.created_at)}</td>
          <td><div class="pname">${esc(p?.name ?? `Product ${m.product_id}`)}</div><div class="sku">${esc(p?.sku)}</div></td>
          <td><span class="pill ${m.type}">${m.type[0].toUpperCase() + m.type.slice(1)}</span></td>
          <td class="num ${m.change < 0 ? "chg-neg" : "chg-pos"}">${m.change > 0 ? "+" : "−"}${Math.abs(m.change)}</td>
          <td class="num">${m.quantity_after}</td><td class="hide-sm">${esc(m.note)}</td></tr>`;
      }).join("") : `<tr><td colspan="6" class="empty">No stock changes recorded yet. <a href="#/record">Record one</a>.</td></tr>`;
    };
    view.querySelector("#pf").onchange = e => { location.hash = `#/history${e.target.value ? `?product=${e.target.value}` : ""}`; };
    $tf.onchange = draw;
    draw();
  }

  async function viewSuppliers() {
    const view = shell("suppliers");
    const [sups, products] = await Promise.all([suppliers(true), api("/products")]);
    view.innerHTML = `
      <div class="page-head"><div><h1>Suppliers</h1><p class="sub">Who you reorder from.</p></div>${isOwner() ? '<button class="btn primary" id="add">Add supplier</button>' : ""}</div>
      <div class="table-wrap"><table>
        <thead><tr><th>Supplier</th><th>Email</th><th class="hide-sm">Phone</th><th class="num">Products</th><th class="num">Need reordering</th></tr></thead>
        <tbody>${sups.length ? sups.map(s => {
          const mine = products.filter(p => p.supplier_id === s.id), low = mine.filter(p => p.is_low_stock).length;
          return `<tr><td class="pname">${esc(s.name)}</td>
            <td>${s.email ? `<a href="mailto:${esc(s.email)}">${esc(s.email)}</a>` : '<span class="hint">None</span>'}</td>
            <td class="hide-sm">${s.phone ? `<a href="tel:${esc(s.phone)}">${esc(s.phone)}</a>` : '<span class="hint">None</span>'}</td>
            <td class="num">${mine.length}</td>
            <td class="num">${low ? `<span class="pill low">${low}</span>` : '<span class="hint">0</span>'}</td></tr>`;
        }).join("") : `<tr><td colspan="5" class="empty">No suppliers yet.${isOwner() ? " Add one to link it to products." : ""}</td></tr>`}</tbody>
      </table></div>`;
    if (isOwner()) view.querySelector("#add").onclick = () => openModal(`<form novalidate>
      ${modalHead("Add supplier")}
      <div class="form-grid">
        <label class="field span-2">Name<input class="input" name="sname" required></label>
        <label class="field">Email<input class="input" name="email" type="email"></label>
        <label class="field">Phone<input class="input" name="phone" type="tel"></label>
      </div><p class="err"></p>
      <div class="modal-foot"><button type="button" class="btn" data-close>Cancel</button><button type="submit" class="btn primary">Add supplier</button></div>
    </form>`, async f => {
      const name = f.sname.value.trim();
      if (!name) throw new Error("Enter the supplier's name.");
      await api("/suppliers", { method: "POST", body: { name, email: f.email.value.trim() || null, phone: f.phone.value.trim() || null } });
      toast(`Added supplier ${name}.`); cache.suppliers = null; route();
    });
  }

  const chatLog = [];
  async function viewAssistant() {
    const view = shell("assistant");
    view.innerHTML = `
      <div class="page-head"><div><h1>Assistant</h1><p class="sub">Ask about stock in plain English. It can also record sales and deliveries.</p></div>
        <div class="toolbar" style="margin:0">${chatLog.length ? '<button class="btn small" id="clear">New chat</button>' : ""}${isOwner() ? '<a class="btn small" href="#/ai">AI providers</a>' : ""}</div></div>
      <div class="chat">
        <div class="chat-log" id="log"></div>
        <form class="chat-form" id="ask"><input class="input" id="msg" placeholder="e.g. What's running low?" autocomplete="off" aria-label="Message"><button class="btn primary" type="submit">Send</button></form>
      </div>`;
    const $log = view.querySelector("#log"), $msg = view.querySelector("#msg");
    const examples = ["What's running low?", "Check COF-002", "Sold 3 SNK-001", "Received 12 DAI-002", "Give me a report"];
    const draw = () => {
      $log.innerHTML = chatLog.length ? chatLog.map(m => `<div class="msg ${m.who} ${m.thinking ? "thinking" : ""}">${esc(m.text)}${m.via ? `<div class="via" ${m.skipped?.length ? `title="${esc("Skipped: " + m.skipped.join(" | "))}"` : ""}>${esc(m.via)}</div>` : ""}</div>`).join("")
        : `<div class="chat-intro">Try one of these:<div class="chips">${examples.map(x => `<button type="button" class="chip">${esc(x)}</button>`).join("")}</div></div>`;
      $log.scrollTop = $log.scrollHeight;
      $log.querySelectorAll(".chip").forEach(c => c.onclick = () => send(c.textContent));
    };
    async function send(text) {
      text = text.trim(); if (!text) return;
      const history = chatLog.filter(m => !m.thinking && !m.error).slice(-8).map(m => ({ role: m.who === "user" ? "user" : "assistant", content: m.text }));
      chatLog.push({ who: "user", text }); const pending = { who: "bot", text: "Thinking…", thinking: true }; chatLog.push(pending);
      $msg.value = ""; draw();
      try {
        const r = await api("/assistant", { method: "POST", body: { message: text, history } });
        pending.text = r.reply; pending.thinking = false; pending.skipped = r.skipped;
        pending.via = r.mode === "ai" ? `Answered by ${r.provider}${r.model ? ` · ${r.model}` : ""}${r.skipped.length ? ` (${r.skipped.length} skipped)` : ""}`
                                      : "Built-in mode" + (isOwner() ? " · add a free AI provider for plain-English questions" : "");
      } catch (e) { pending.text = e.message; pending.thinking = false; pending.error = true; }
      draw(); $msg.focus();
    }
    view.querySelector("#ask").onsubmit = e => { e.preventDefault(); send($msg.value); };
    view.querySelector("#clear")?.addEventListener("click", () => { chatLog.length = 0; route(); });
    draw(); $msg.focus();
  }

  // ---------- AI providers for the assistant ----------
  let aiPresets = null;
  const aiStatus = p => {
    if (!p.enabled) return ["", "Off"];
    if (p.resting_until) return ["adjustment", `Resting until ${new Date(p.resting_until).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })}`];
    if (p.last_ok === true) return ["ok", "Working"];
    if (p.last_ok === false) return ["low", "Problem"];
    return ["", "Not tested"];
  };

  async function viewAi() {
    if (!isOwner()) { location.hash = "#/assistant"; return; }
    const view = shell("assistant");
    const [list, presets] = await Promise.all([api("/ai/providers"), aiPresets || api("/ai/presets")]);
    aiPresets = presets;
    const cards = presets.map(p => `<button type="button" class="prov" data-preset="${p.preset}"><span class="prov-name">${esc(p.label)}</span><span class="prov-desc">${esc(p.free)}</span></button>`).join("");
    view.innerHTML = `
      <a href="#/assistant" class="back">${icon("back")}Assistant</a>
      <div class="page-head"><div><h1>AI providers</h1><p class="sub">The assistant asks these in order. When one reaches its free limit it rests and the next one answers. If none can answer, the assistant uses built-in mode.</p></div>
        ${list.length ? '<button class="btn primary" id="add">Add a provider</button>' : ""}</div>
      ${list.length ? `<ol class="ai-list">${list.map((p, i) => {
        const [cls, label] = aiStatus(p);
        return `<li class="panel ai-row" data-id="${p.id}">
          <div class="ai-order"><button class="btn small" data-move="-1" ${i === 0 ? "disabled" : ""} aria-label="Move ${esc(p.name)} up">↑</button><button class="btn small" data-move="1" ${i === list.length - 1 ? "disabled" : ""} aria-label="Move ${esc(p.name)} down">↓</button></div>
          <div class="ai-main">
            <div class="ai-title"><span class="ai-n">${i + 1}</span><span class="pname">${esc(p.name)}</span>${label ? `<span class="pill ${cls}">${label}</span>` : ""}</div>
            <div class="sku">${esc(p.model)}</div>
            ${p.last_status ? `<div class="hint">${esc(p.last_status)}</div>` : ""}
          </div>
          <div class="row-actions">
            <label class="check"><input type="checkbox" data-toggle ${p.enabled ? "checked" : ""}> On</label>
            <button class="btn small" data-test>Test</button>
            <button class="btn small" data-edit>Edit</button>
            <button class="btn small danger" data-del>Remove</button>
          </div></li>`; }).join("")}</ol>
        <p class="hint" style="margin-top:14px">Your stock questions are sent to whichever provider answers. Free tiers can change; the Test button checks a provider still works.</p>`
      : `<section class="panel"><h2>Add a free AI provider</h2><p class="sub" style="margin-top:0">Each one gives you a free API key. Add two or three so the assistant keeps working when one runs out for the day.</p><div class="prov-grid" style="margin-top:14px">${cards}</div></section>`}`;

    view.querySelector("#add")?.addEventListener("click", () => chooseAiPreset(presets));
    view.querySelectorAll("[data-preset]").forEach(b => b.onclick = () => aiForm(presets.find(p => p.preset === b.dataset.preset)));
    view.querySelectorAll(".ai-row").forEach(row => {
      const id = +row.dataset.id, p = list.find(x => x.id === id);
      row.querySelectorAll("[data-move]").forEach(b => b.onclick = async () => {
        const ids = list.map(x => x.id), i = ids.indexOf(id), j = i + +b.dataset.move;
        [ids[i], ids[j]] = [ids[j], ids[i]];
        try { await api("/ai/providers/order", { method: "POST", body: { ids } }); route(); } catch (e) { fail(e); }
      });
      row.querySelector("[data-toggle]").onchange = async e => {
        try { await api(`/ai/providers/${id}`, { method: "PATCH", body: { enabled: e.target.checked } }); route(); } catch (ex) { fail(ex); }
      };
      row.querySelector("[data-test]").onclick = async e => {
        const b = e.target; b.disabled = true; b.textContent = "Testing…";
        try { const r = await api(`/ai/providers/${id}/test`, { method: "POST" }); toast(`${p.name}: ${r.message}`, r.ok ? "" : "error"); route(); }
        catch (ex) { fail(ex); b.disabled = false; b.textContent = "Test"; }
      };
      row.querySelector("[data-edit]").onclick = () => aiForm(presets.find(x => x.preset === p.preset) || presets.at(-1), p);
      row.querySelector("[data-del]").onclick = () => confirmBox(`Remove ${p.name}?`, "The assistant will stop using it. You can add it again later.", "Remove", async () => {
        await api(`/ai/providers/${id}`, { method: "DELETE" }); toast(`Removed ${p.name}.`); route();
      });
    });
  }

  function chooseAiPreset(presets) {
    openModal(`<form novalidate>${modalHead("Add a provider")}
      <div class="prov-grid">${presets.map((p, i) => `<label class="prov"><input type="radio" name="preset" value="${p.preset}" ${i === 0 ? "checked" : ""}><span class="prov-name">${esc(p.label)}</span><span class="prov-desc">${esc(p.free)}</span></label>`).join("")}</div>
      <p class="err"></p><div class="modal-foot"><button type="button" class="btn" data-close>Cancel</button><button type="submit" class="btn primary">Continue</button></div></form>`,
      async f => { const p = presets.find(x => x.preset === f.preset.value); setTimeout(() => aiForm(p), 0); });
  }

  function aiForm(preset, existing) {
    const editing = !!existing;
    const showUrl = preset.preset === "custom" || preset.preset === "ollama" || editing;
    openModal(`<form novalidate>${modalHead(editing ? `Edit ${existing.name}` : `Add ${preset.label}`)}
      <p class="sub" style="margin:0">${esc(preset.free)}${preset.note ? ` ${esc(preset.note)}` : ""}</p>
      <div class="form-grid">
        ${preset.needs_key ? `<label class="field span-2"><span>API key${preset.key_url ? ` <a href="${esc(preset.key_url)}" target="_blank" rel="noopener" class="hint">Get a free key</a>` : ""}</span>
          <input class="input" name="key" type="password" autocomplete="off" placeholder="${editing && existing.api_key ? `Saved (${esc(existing.api_key)}). Leave blank to keep.` : "Paste your key"}"></label>` : ""}
        ${showUrl ? `<label class="field span-2">Base URL<input class="input" name="url" value="${esc(existing?.base_url ?? preset.base_url)}" placeholder="https://…/v1"></label>` : ""}
        <label class="field span-2"><span>Model</span>
          <div style="display:flex;gap:8px"><input class="input" name="model" value="${esc(existing?.model ?? preset.model)}" placeholder="Press Load models"><button type="button" class="btn" id="load">Load models</button></div>
          <select class="input" id="msel" hidden aria-label="Available models"></select><span class="hint" id="mhint">Pick a model that supports tool (function) calling.</span></label>
        <label class="field span-2">Name<input class="input" name="pname" value="${esc(existing?.name ?? preset.label)}"></label>
      </div>
      <p class="err"></p>
      <div class="modal-foot"><button type="button" class="btn" data-close>Cancel</button><button type="submit" class="btn primary">${editing ? "Save" : "Add and test"}</button></div></form>`,
      async f => {
        const body = { name: f.pname.value.trim() || preset.label, model: f.model.value.trim(), api_key: f.key ? f.key.value.trim() : "", base_url: f.url ? f.url.value.trim() : "" };
        if (!body.model) throw new Error("Choose a model: press Load models or type its name.");
        if (editing) {
          if (!body.api_key) delete body.api_key;
          await api(`/ai/providers/${existing.id}`, { method: "PATCH", body }); toast(`Saved ${body.name}.`); route();
        } else {
          if (preset.needs_key && !body.api_key) throw new Error("Paste your API key.");
          const p = await api("/ai/providers", { method: "POST", body: { preset: preset.preset, ...body } });
          toast(`Added ${p.name}. Testing…`);
          api(`/ai/providers/${p.id}/test`, { method: "POST" }).then(r => { toast(`${p.name}: ${r.message}`, r.ok ? "" : "error"); route(); }).catch(fail);
          route();
        }
      });
    const form = $modal.querySelector("form");
    form.querySelector("#load").onclick = async e => {
      const b = e.target, hint = form.querySelector("#mhint");
      b.disabled = true; b.textContent = "Loading…";
      try {
        const r = await api("/ai/models", { method: "POST", body: { preset: preset.preset, api_key: form.key ? form.key.value.trim() : "", base_url: form.url ? form.url.value.trim() : "", provider_id: existing?.id ?? null } });
        const sel = form.querySelector("#msel");
        if (!form.model.value && r.models.length) form.model.value = r.models[0];
        sel.innerHTML = r.models.map(m => `<option ${m === form.model.value ? "selected" : ""}>${esc(m)}</option>`).join("");
        sel.hidden = !r.models.length;
        sel.onchange = () => { form.model.value = sel.value; };
        hint.textContent = `${r.models.length} models available${r.models.some(m => m.endsWith(":free")) ? "; “:free” ones cost nothing" : ""}. Pick one that supports tool calling.`;
        sel.focus();
      } catch (ex) { hint.textContent = ex.message; }
      finally { b.disabled = false; b.textContent = "Load models"; }
    };
  }

  // ---------- point of sale integrations ----------
  let posProviders = null;
  async function providers() {
    if (!posProviders) posProviders = await api("/pos/providers");
    return posProviders;
  }
  const ago = iso => {
    if (!iso) return "never";
    const d = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + "Z");
    const mins = Math.round((Date.now() - d) / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins} min ago`;
    if (mins < 1440) return `${Math.round(mins / 60)} h ago`;
    return when(iso);
  };
  const statusText = s => (s || "").replace(/^\d{4}-\d\d-\d\d \d\d:\d\d UTC · /, "");

  async function posAction(path, btn) {
    const label = btn?.textContent;
    if (btn) { btn.disabled = true; btn.textContent = "Working…"; }
    try { const r = await api(path, { method: "POST" }); toast(r.message || "Done."); return r; }
    catch (e) { fail(e); return null; }
    finally { if (btn) { btn.disabled = false; btn.textContent = label; } }
  }

  async function viewPos(params) {
    if (!isOwner()) { location.hash = "#/dashboard"; return; }
    if (params.get("c")) return viewPosDetail(+params.get("c"), params.get("tab") || "items");
    const view = shell("pos");
    const [conns, provs] = await Promise.all([api("/pos/connections"), providers()]);
    const provCards = provs.map(p => `
      <button type="button" class="prov" data-prov="${p.provider}">
        <span class="prov-name">${esc(p.label)}</span>
        <span class="prov-desc">${esc(p.description)}</span>
      </button>`).join("");
    view.innerHTML = `
      <div class="page-head"><div><h1>Point of sale</h1><p class="sub">Connect the tills you sell from. Sales come in automatically and lower your stock; StockSmart can send the new counts back so every POS shows the same numbers.</p></div>
        ${conns.length ? '<button class="btn primary" id="add">Connect a POS</button>' : ""}</div>
      ${conns.length ? `<div class="conns">${conns.map(connCard).join("")}</div>` : `
        <section class="panel"><h2>Choose your POS to get started</h2><div class="prov-grid">${provCards}</div></section>`}`;
    view.querySelector("#add")?.addEventListener("click", () => chooseProvider(provs));
    view.querySelectorAll("[data-prov]").forEach(b => b.onclick = () => connectionForm(provs.find(p => p.provider === b.dataset.prov)));
    view.querySelectorAll("[data-act]").forEach(b => b.onclick = async () => {
      const id = b.closest("[data-conn]").dataset.conn, act = b.dataset.act;
      if (act === "settings") return connectionForm(provs.find(p => p.provider === conns.find(c => c.id === +id).provider), conns.find(c => c.id === +id));
      if (act === "delete") {
        const c = conns.find(x => x.id === +id);
        return confirmBox(`Disconnect ${c.name}?`, "Stock already recorded stays. Item matches and sales activity for this connection are removed.", "Disconnect", async () => {
          await api(`/pos/connections/${id}`, { method: "DELETE" }); toast(`Disconnected ${c.name}.`); route();
        });
      }
      if (await posAction(`/pos/connections/${id}/${act}`, b)) route();
    });
  }

  function connCard(c) {
    const problems = c.events.unmatched + c.events.failed;
    return `<section class="panel conn" data-conn="${c.id}">
      <div class="conn-head">
        <div><h2><a href="#/pos?c=${c.id}">${esc(c.name)}</a></h2><div class="hint">${esc(c.provider_label)}${c.enabled ? "" : " · paused"}</div></div>
        <span class="pill ${c.last_status_ok ? "ok" : "low"}">${c.last_status_ok ? "Working" : "Needs attention"}</span>
      </div>
      <p class="conn-status">${esc(statusText(c.last_status)) || "No activity yet."}</p>
      <dl class="conn-facts">
        <div><dt>Sales recorded</dt><dd>${int(c.events.applied)}</dd></div>
        <div><dt>Need attention</dt><dd>${problems ? `<a href="#/pos?c=${c.id}&tab=activity" class="chg-neg">${problems}</a>` : "0"}</dd></div>
        <div><dt>Items matched</dt><dd>${c.items.linked}${c.items.unmatched ? ` <a href="#/pos?c=${c.id}" class="hint">(${c.items.unmatched} not yet)</a>` : ""}</dd></div>
        <div><dt>Last sales check</dt><dd>${c.capabilities.polling ? ago(c.last_synced_at) : "Webhook / CSV"}</dd></div>
      </dl>
      <div class="conn-flags hint">${[c.capabilities.polling ? (c.auto_sync ? "Checks for new sales every few minutes" : "Checks for sales only when you press Sync") : "Receives sales by webhook or CSV import",
        c.capabilities.push ? (c.push_stock ? "Sends stock counts to this POS" : "Doesn't send stock counts") : null].filter(Boolean).join(" · ")}</div>
      <div class="toolbar" style="margin:14px 0 0">
        ${c.capabilities.polling ? '<button class="btn small" data-act="sync">Sync sales now</button>' : ""}
        ${c.capabilities.catalog ? '<button class="btn small" data-act="catalog">Import items</button>' : ""}
        ${c.capabilities.push ? '<button class="btn small" data-act="push">Send stock counts</button>' : ""}
        <button class="btn small" data-act="test">Test connection</button>
        <a class="btn small" href="#/pos?c=${c.id}">Items & activity</a>
        <button class="btn small" data-act="settings">Settings</button>
        <button class="btn small danger" data-act="delete">Disconnect</button>
      </div>
    </section>`;
  }

  function chooseProvider(provs) {
    openModal(`<form novalidate>${modalHead("Connect a POS")}
      <div class="prov-grid">${provs.map(p => `<label class="prov"><input type="radio" name="prov" value="${p.provider}" ${p === provs[0] ? "checked" : ""}>
        <span class="prov-name">${esc(p.label)}</span><span class="prov-desc">${esc(p.description)}</span></label>`).join("")}</div>
      <p class="err"></p>
      <div class="modal-foot"><button type="button" class="btn" data-close>Cancel</button><button type="submit" class="btn primary">Continue</button></div></form>`,
      async f => { const p = provs.find(x => x.provider === f.prov.value); setTimeout(() => connectionForm(p), 0); });
  }

  function connectionForm(p, existing) {
    const editing = !!existing;
    const fieldHtml = p.fields.map(fl => {
      const val = editing ? (fl.secret ? "" : existing.settings[fl.key] || "") : fl.default || "";
      const ph = editing && fl.secret && existing.settings[fl.key] ? `Saved (${existing.settings[fl.key]}). Leave blank to keep.` : "";
      const input = fl.choices.length
        ? `<select class="input" name="f_${fl.key}">${fl.choices.map(c => `<option value="${esc(c.value)}" ${c.value === val ? "selected" : ""}>${esc(c.label)}</option>`).join("")}</select>`
        : `<input class="input" name="f_${fl.key}" value="${esc(val)}" ${fl.secret ? 'type="password" autocomplete="off"' : ""} placeholder="${esc(ph)}">`;
      return `<label class="field span-2"><span>${esc(fl.label)}${fl.required ? "" : ' <span class="hint">(optional)</span>'}</span>${input}${fl.help ? `<span class="hint">${esc(fl.help)}</span>` : ""}</label>`;
    }).join("");
    openModal(`<form novalidate>${modalHead(editing ? `${existing.name} settings` : `Connect ${p.label}`)}
      <div class="form-grid">
        <label class="field span-2">Name<input class="input" name="cname" value="${esc(existing?.name ?? p.label)}" required><span class="hint">Shown in history notes, e.g. “Front counter Square”.</span></label>
        ${fieldHtml}
        ${p.capabilities.polling ? `<label class="check span-2"><input type="checkbox" name="auto_sync" ${!editing || existing.auto_sync ? "checked" : ""}> Check for new sales automatically</label>` : ""}
        ${p.capabilities.push ? `<label class="check span-2"><input type="checkbox" name="push_stock" ${editing && existing.push_stock ? "checked" : ""}> Send StockSmart's stock counts to ${esc(p.label)} <span class="hint">Turn on once items are matched.</span></label>` : ""}
        ${editing ? `<label class="check span-2"><input type="checkbox" name="enabled" ${existing.enabled ? "checked" : ""}> Connection active</label>` : ""}
      </div>
      ${editing ? "" : '<p class="hint" style="margin:0">Only sales made after you connect are imported, so stock isn\'t counted twice.</p>'}
      <p class="err"></p>
      <div class="modal-foot"><button type="button" class="btn" data-close>Cancel</button><button type="submit" class="btn primary">${editing ? "Save settings" : "Connect"}</button></div></form>`,
      async f => {
        const settings = {};
        p.fields.forEach(fl => { settings[fl.key] = f[`f_${fl.key}`].value; });
        const body = { name: f.cname.value.trim(), settings,
          auto_sync: f.auto_sync ? f.auto_sync.checked : false, push_stock: f.push_stock ? f.push_stock.checked : false };
        if (!body.name) throw new Error("Give this connection a name.");
        if (editing) {
          body.enabled = f.enabled.checked;
          await api(`/pos/connections/${existing.id}`, { method: "PATCH", body });
          toast(`Saved ${body.name}.`); route();
        } else {
          const c = await api("/pos/connections", { method: "POST", body: { provider: p.provider, ...body } });
          toast(`Connected ${c.name}.`);
          if (p.capabilities.polling) api(`/pos/connections/${c.id}/test`, { method: "POST" }).then(r => toast(r.message)).catch(fail);
          location.hash = `#/pos?c=${c.id}&tab=setup`;
        }
      });
  }

  async function viewPosDetail(id, tab) {
    const view = shell("pos");
    const [conns, products, provs] = await Promise.all([api("/pos/connections"), api("/products"), providers()]);
    const c = conns.find(x => x.id === id);
    if (!c) { view.innerHTML = `<p class="empty">This connection no longer exists. <a href="#/pos">Back to point of sale</a></p>`; return; }
    const tabs = [["items", "Items"], ["activity", "Activity"], ["setup", "Setup"]];
    view.innerHTML = `
      <a href="#/pos" class="back">${icon("back")}Point of sale</a>
      <div class="page-head"><div><h1>${esc(c.name)}</h1><p class="sub">${esc(c.provider_label)} · ${esc(statusText(c.last_status)) || "No activity yet."}</p></div>
        <div class="toolbar" style="margin:0">
          ${c.capabilities.polling ? '<button class="btn" data-act="sync">Sync sales now</button>' : ""}
          ${c.capabilities.catalog ? '<button class="btn" data-act="catalog">Import items</button>' : ""}
        </div></div>
      <div class="seg tabs" role="tablist">${tabs.map(([k, l]) => `<button type="button" role="tab" aria-pressed="${k === tab}" data-tab="${k}">${l}${k === "activity" && c.events.unmatched + c.events.failed ? ` (${c.events.unmatched + c.events.failed})` : ""}${k === "items" && c.items.unmatched ? ` (${c.items.unmatched})` : ""}</button>`).join("")}</div>
      <div id="tab"></div>`;
    view.querySelectorAll("[data-tab]").forEach(b => b.onclick = () => { location.hash = `#/pos?c=${id}&tab=${b.dataset.tab}`; });
    view.querySelectorAll("[data-act]").forEach(b => b.onclick = async () => { if (await posAction(`/pos/connections/${id}/${b.dataset.act}`, b)) route(); });
    const $tab = view.querySelector("#tab");
    if (tab === "activity") return posActivity($tab, c, products);
    if (tab === "setup") return posSetup($tab, c, provs.find(p => p.provider === c.provider));
    return posItems($tab, c, products);
  }

  async function posItems($tab, c, products) {
    const items = await api(`/pos/connections/${c.id}/items`);
    const opts = `<option value="">Not matched</option>` + products.map(p => `<option value="${p.id}">${esc(p.name)} (${esc(p.sku)})</option>`).join("");
    $tab.innerHTML = `
      <p class="sub" style="margin:16px 0">Each ${esc(c.provider_label)} item needs a matching StockSmart product. Items whose SKU matches are paired automatically; pick the rest by hand.${c.capabilities.catalog ? "" : " Items appear here as sales arrive."}</p>
      <div class="table-wrap"><table>
        <thead><tr><th>${esc(c.provider_label)} item</th><th class="hide-sm">SKU in ${esc(c.provider_label)}</th><th style="min-width:240px">StockSmart product</th></tr></thead>
        <tbody>${items.length ? items.map(it => `<tr>
          <td class="pname">${esc(it.name) || `<span class="hint">${esc(it.external_item_id)}</span>`}</td>
          <td class="hide-sm sku">${esc(it.sku) || '<span class="hint">None</span>'}</td>
          <td><select class="input" data-link="${it.id}" aria-label="Product for ${esc(it.name)}">${opts.replace(`value="${it.product_id}"`, `value="${it.product_id}" selected`)}</select></td></tr>`).join("")
        : `<tr><td colspan="3" class="empty">${c.capabilities.catalog ? "No items yet. Press Import items to load them." : "No items yet."}</td></tr>`}</tbody>
      </table></div>`;
    $tab.querySelectorAll("[data-link]").forEach(sel => sel.onchange = async () => {
      try {
        await api(`/pos/items/${sel.dataset.link}`, { method: "PATCH", body: { product_id: sel.value ? +sel.value : null } });
        toast(sel.value ? "Matched. Press Retry under Activity to apply any held sales." : "Match removed.");
      } catch (e) { fail(e); }
    });
  }

  async function posActivity($tab, c, products) {
    const events = await api(`/pos/events?connection_id=${c.id}&limit=300`);
    const byId = Object.fromEntries(products.map(p => [p.id, p]));
    const problems = c.events.unmatched + c.events.failed;
    const LABEL = { applied: ["ok", "Recorded"], unmatched: ["adjustment", "Not matched"], failed: ["low", "Needs attention"] };
    $tab.innerHTML = `
      <div class="toolbar" style="margin:16px 0">
        <p class="sub" style="margin:0;flex:1">Every sale line received from ${esc(c.provider_label)}. Negative quantities are refunds.</p>
        ${problems ? `<button class="btn primary" id="retry">Retry ${problems} held sale${problems === 1 ? "" : "s"}</button>` : ""}
      </div>
      <div class="table-wrap"><table>
        <thead><tr><th>When</th><th>Item</th><th class="num">Qty</th><th>Status</th><th class="hide-sm">Order</th></tr></thead>
        <tbody>${events.length ? events.map(e => {
          const p = byId[e.product_id], [cls, lbl] = LABEL[e.status] || ["", e.status];
          return `<tr><td>${when(e.created_at)}</td>
            <td><div class="pname">${esc(p?.name || e.name || e.sku || "Unknown item")}</div><div class="sku">${esc(p?.sku || e.sku || "")}</div>${e.message ? `<div class="hint">${esc(e.message)}</div>` : ""}</td>
            <td class="num ${e.quantity < 0 ? "chg-pos" : ""}">${e.quantity < 0 ? `refund ${-e.quantity}` : e.quantity}</td>
            <td><span class="pill ${cls}">${lbl}</span></td><td class="hide-sm sku">${esc(e.order_id)}</td></tr>`;
        }).join("") : `<tr><td colspan="5" class="empty">No sales received yet.</td></tr>`}</tbody>
      </table></div>`;
    $tab.querySelector("#retry")?.addEventListener("click", async e => { if (await posAction(`/pos/connections/${c.id}/retry`, e.target)) route(); });
  }

  function posSetup($tab, c, p) {
    const local = /localhost|127\.0\.0\.1/.test(c.webhook_url);
    $tab.innerHTML = `<div class="grid-2" style="margin-top:16px">
      <section class="panel">
        <h2>How sales reach StockSmart</h2>
        ${c.capabilities.polling ? `<p style="margin-top:0">StockSmart ${c.auto_sync ? "checks " + esc(c.provider_label) + " for new sales every few minutes" : "checks for new sales when you press Sync sales now"}. That works anywhere, including on your own computer.</p>
          <p>For instant updates you can also add a webhook:</p>` : `<p style="margin-top:0">Send sales to this webhook address, or import a CSV sales export below.</p>`}
        <label class="field">Webhook URL
          <div style="display:flex;gap:8px"><input class="input" readonly value="${esc(c.webhook_url)}" id="wh"><button type="button" class="btn" id="copy" aria-label="Copy webhook URL">${icon("copy")}Copy</button></div></label>
        ${local ? `<p class="hint">This address only works on this computer. A POS needs a public address: deploy StockSmart (for example to Render) or use a tunnel such as ngrok, and set STOCKSMART_PUBLIC_URL.</p>` : ""}
        <p class="hint">${esc(p.webhook_help)}</p>
        ${c.provider === "generic" ? `<details><summary>Webhook format</summary><pre class="code">POST ${esc(c.webhook_url)}
Content-Type: application/json
X-StockSmart-Signature: sha256=&lt;HMAC-SHA256 of body with your secret&gt;

{"order_id": "1042",
 "lines": [{"sku": "COF-001", "quantity": 2},
           {"sku": "SNK-002", "quantity": -1}]}</pre></details>` : ""}
      </section>
      <section class="panel">
        <h2>Import a sales file</h2>
        <p style="margin-top:0" class="sub">A CSV export from any POS with a SKU column and a quantity column. Include an order or receipt number column so re-importing the same file never counts a sale twice.</p>
        <label class="field">CSV file<input class="input" type="file" accept=".csv,text/csv" id="csvf"></label>
        <p class="err" id="csverr"></p>
        <button class="btn primary" id="csvgo" disabled>Import sales</button>
      </section></div>`;
    $tab.querySelector("#copy").onclick = async () => {
      try { await navigator.clipboard.writeText(c.webhook_url); toast("Copied webhook URL."); }
      catch { $tab.querySelector("#wh").select(); }
    };
    const $f = $tab.querySelector("#csvf"), $go = $tab.querySelector("#csvgo"), $err = $tab.querySelector("#csverr");
    $f.onchange = () => { $go.disabled = !$f.files.length; $err.textContent = ""; };
    $go.onclick = async () => {
      const file = $f.files[0]; if (!file) return;
      if (file.size > 5_000_000) { $err.textContent = "That file is over 5 MB. Split it into smaller exports."; return; }
      $go.disabled = true; $go.textContent = "Importing…";
      try {
        const r = await api(`/pos/connections/${c.id}/import-csv`, { method: "POST", body: { csv: await file.text() } });
        toast("Imported: " + r.message); location.hash = `#/pos?c=${c.id}&tab=activity`;
      } catch (e) { $err.textContent = e.message; }
      finally { $go.disabled = false; $go.textContent = "Import sales"; }
    };
  }

  // ---------- router ----------
  const VIEWS = { dashboard: viewDashboard, products: viewProducts, record: viewRecord, history: viewHistory, suppliers: viewSuppliers, assistant: viewAssistant, pos: viewPos, ai: viewAi };

  async function route() {
    const [path, query] = (location.hash.replace(/^#\/?/, "") || "dashboard").split("?");
    if (!session.token) { if (path !== "login") location.hash = "#/login"; return viewLogin(); }
    if (path === "login") { location.hash = "#/dashboard"; return; }
    const fn = VIEWS[path] || viewDashboard;
    if ($modal.open) $modal.close();
    try { await fn(new URLSearchParams(query || "")); }
    catch (e) {
      if (!session.token) return;
      const view = document.getElementById("view");
      if (view) view.innerHTML = `<div class="panel"><h2>Couldn't load this page</h2><p class="sub">${esc(e.message)}</p><button class="btn" onclick="location.reload()">Try again</button></div>`;
      else fail(e);
    }
  }
  window.addEventListener("hashchange", route);
  route();
})();
