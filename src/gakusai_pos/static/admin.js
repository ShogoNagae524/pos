// 管理端末（PC3）：商品・注文・売上・レジ締め・ログ・設定・接続状況・バックアップ
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const A = "/admin/api";
  const api = POS.api;
  const esc = POS.esc;

  const KIND_LABEL = {
    order_created: "会計", manual_order: "手入力", status_changed: "状態変更", status_undone: "戻し",
    order_cancelled: "取消", cancel_ack: "取消の確認", sold_out: "売切切替",
    product_added: "商品追加", product_updated: "商品変更", product_deleted: "商品削除",
    settings_updated: "設定変更", closing: "レジ締め", order_deleted: "注文削除",
    orders_purged: "注文一括削除", terminal_connected: "端末接続", terminal_disconnected: "端末切断",
    session_registered: "端末登録", backup: "バックアップ",
  };

  let tab = "home";
  let products = [];
  let dates = [];

  const fmtWait = (s) => (s === null || s === undefined ? "—" : `${Math.floor(s / 60)}分${String(s % 60).padStart(2, "0")}秒`);
  const today = () => {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  };
  const fail = (e) => { POS.sound.error(); POS.toast(e.message, "error", 4000); };

  // ============================================================ タブ
  function showTab(name) {
    tab = name;
    document.querySelectorAll("#tabs [data-tab]").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    document.querySelectorAll("[data-pane]").forEach((p) => p.classList.toggle("hidden", p.dataset.pane !== name));
    POS.store.set("pos.admin.tab", name);
    refreshTab();
  }
  $("tabs").addEventListener("click", (e) => { const b = e.target.closest("[data-tab]"); if (b) showTab(b.dataset.tab); });

  function refreshTab() {
    const f = { home: loadHome, products: loadProducts, orders: loadOrders, sales: loadSales, logs: () => loadLogs(true), settings: loadSettings }[tab];
    if (f) f().catch(fail);
  }

  setInterval(() => { $("clock").textContent = new Date().toLocaleTimeString("ja-JP"); }, 1000);

  // ============================================================ ホーム
  let knownConnected = new Map(); // 端末キー -> 接続中か（切れたら警告音 A-11）

  async function loadHome() {
    const info = await api(`${A}/info`);
    applySettings(info.settings);
    const warn = [];
    if (!info.passcode_changed) warn.push(`<div class="warn-box">接続用パスコードが初期値のままです。「設定」で変更してください（初期値はサーバー起動時のターミナルに表示されています）。</div>`);
    if (!products.length) {
      products = await api(`${A}/products`);
    }
    if (!products.length) warn.push(`<div class="warn-box">商品が登録されていません。「商品」タブで登録してから営業を始めてください。</div>`);
    if (info.settings.test_mode) warn.push(`<div class="warn-box">テストモードです。本番の前に「設定」でテストモードを切ってください。</div>`);
    $("home-warnings").innerHTML = warn.join("");

    $("urls").innerHTML = info.urls.length ? info.urls.map((u) => `
      <div class="url-box" style="margin-bottom:10px">
        <img src="${A}/qr.png?data=${encodeURIComponent(u)}&box=6" alt="${esc(u)} のQRコード">
        <span class="url">${esc(u)}</span>
      </div>`).join("") : `<div class="danger-box">ネットワークのIPアドレスが見つかりません。Wi-Fiにつないでからサーバーを起動し直してください。</div>`;

    renderTerminals(info.terminals);

    const b = info.backup;
    const last = b.last_manual && (!b.last_auto || b.last_manual.at > b.last_auto.at) ? b.last_manual : b.last_auto;
    $("backup-info").innerHTML = `
      <div>自動バックアップ（${Math.round(b.interval_seconds / 60)}分ごと）最終: <strong class="num">${b.last_auto ? esc(b.last_auto.at.replace("T", " ")) : "まだありません"}</strong></div>
      <div>手動バックアップ最終: <strong class="num">${b.last_manual ? esc(b.last_manual.at.replace("T", " ")) : "—"}</strong></div>
      ${last ? `<div class="muted" style="word-break:break-all">保存先: ${esc(last.path)}${last.usb_path ? `<br>USB: ${esc(last.usb_path)}` : ""}</div>` : ""}
      ${last && last.usb_error ? `<div class="danger-box">USBへの保存に失敗: ${esc(last.usb_error)}</div>` : ""}
      ${b.last_error ? `<div class="danger-box">自動バックアップに失敗: ${esc(b.last_error)}</div>` : ""}`;

    const s = await api(`${A}/summary?date=${today()}`);
    $("home-sales").innerHTML = `
      <div class="stat-row">
        <div class="stat"><div class="k">注文数</div><div class="v num">${s.orders}件</div></div>
        <div class="stat"><div class="k">売上</div><div class="v num">${POS.yen(s.amount)}</div><div class="s num">金券${s.tickets}枚</div></div>
        <div class="stat"><div class="k">平均待ち時間</div><div class="v num">${fmtWait(s.avg_wait_seconds)}</div></div>
      </div>
      <div class="muted">取消 ${s.cancelled_count}件・テスト ${s.test_count}件（売上から除外）</div>`;
  }

  function renderTerminals(list) {
    let lost = false;
    for (const t of list) {
      const key = `${t.role}:${t.tid}`;
      if (knownConnected.get(key) && !t.connected) lost = true;
      knownConnected.set(key, t.connected);
    }
    if (lost) { POS.sound.alert(); POS.toast("受付・厨房の端末との接続が切れました", "error", 5000); }
    $("terminals").innerHTML = list.length ? `<table class="data">
      <tr><th>端末</th><th>状態</th><th>最終通信</th><th>IP</th></tr>
      ${list.map((t) => `<tr>
        <td>${esc(t.label)} <small class="muted">${esc(t.tid)}</small></td>
        <td><span class="dot ${t.connected ? "on" : "off"}"></span>${t.connected ? "接続中" : "切断"}</td>
        <td class="num">${new Date(t.last_seen).toLocaleTimeString("ja-JP")}</td>
        <td class="num">${esc(t.ip || "")}</td></tr>`).join("")}</table>`
      : `<p class="muted">まだ接続した端末はありません</p>`;
  }

  // 接続状況は画面に関係なく定期的に確認する
  setInterval(async () => {
    try {
      const info = await api(`${A}/info`);
      if (tab === "home") renderTerminals(info.terminals);
      else {
        for (const t of info.terminals) {
          const key = `${t.role}:${t.tid}`;
          if (knownConnected.get(key) && !t.connected) { POS.sound.alert(); POS.toast(`${t.label} との接続が切れました`, "error", 5000); }
          knownConnected.set(key, t.connected);
        }
      }
    } catch {}
  }, 5000);

  $("btn-backup").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try {
      const r = await api(`${A}/backup`, { method: "POST" });
      POS.toast(r.usb_error ? `PC3には保存しました（USB失敗: ${r.usb_error}）` : "バックアップしました", r.usb_error ? "warn" : "ok", 4000);
      await loadHome();
    } catch (ex) { fail(ex); }
    e.target.disabled = false;
  });

  // ============================================================ 商品（A-01〜A-03, A-13）
  async function loadProducts() {
    products = await api(`${A}/products`);
    renderProducts();
    renderManualItems();
  }

  function renderProducts() {
    const t = $("product-table");
    if (!products.length) { t.innerHTML = `<tr><td class="muted">商品はまだありません</td></tr>`; return; }
    t.innerHTML = `<tr><th>コード</th><th>商品名</th><th>価格</th><th>表示順</th><th>作れる数</th><th>売切</th><th></th></tr>` +
      products.map((p) => `<tr data-id="${p.id}">
        <td class="num"><strong>${esc(p.code)}</strong></td>
        <td><input name="name" value="${esc(p.name)}" maxlength="40" style="width:100%;min-width:140px"></td>
        <td><input name="price" type="number" step="50" min="50" value="${p.price}" style="width:100px"></td>
        <td><input name="sort_order" type="number" value="${p.sort_order}" style="width:80px"></td>
        <td><input name="stock" type="number" min="0" value="${p.stock ?? ""}" placeholder="上限なし" style="width:100px"></td>
        <td><button type="button" data-act="soldout" class="btn-small ${p.sold_out ? "btn-danger" : ""}">${p.sold_out ? "売切中" : "販売中"}</button></td>
        <td><div class="row" style="flex-wrap:nowrap">
          <button type="button" data-act="save" class="btn-small btn-primary">保存</button>
          <button type="button" data-act="delete" class="btn-small btn-ghost-danger">削除</button></div></td>
      </tr>`).join("");
  }

  $("product-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const price = Number(f.price.value);
    if (price <= 0 || price % 50 !== 0) { fail(new Error("価格は50円の倍数で入力してください")); return; }
    const body = { name: f.name.value, price, stock: f.stock.value === "" ? null : Number(f.stock.value) };
    try {
      const p = await api(`${A}/products`, { method: "POST", body });
      POS.toast(`${p.code} ${p.name} を追加しました`, "ok");
      f.reset();
      f.name.focus();
      await loadProducts();
    } catch (ex) { fail(ex); }
  });

  $("product-table").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-act]");
    if (!b) return;
    const tr = b.closest("tr");
    const id = Number(tr.dataset.id);
    const p = products.find((x) => x.id === id);
    try {
      if (b.dataset.act === "save") {
        const price = Number(tr.querySelector("[name=price]").value);
        if (price <= 0 || price % 50 !== 0) throw new Error("価格は50円の倍数で入力してください");
        const stockVal = tr.querySelector("[name=stock]").value;
        const body = {
          name: tr.querySelector("[name=name]").value,
          price,
          sort_order: Number(tr.querySelector("[name=sort_order]").value),
        };
        if (stockVal === "") body.clear_stock = true; else body.stock = Number(stockVal);
        await api(`${A}/products/${id}`, { method: "PATCH", body });
        POS.toast("保存しました（受付・厨房に反映済み）", "ok");
      } else if (b.dataset.act === "soldout") {
        await api(`${A}/products/${id}`, { method: "PATCH", body: { sold_out: !p.sold_out } });
      } else if (b.dataset.act === "delete") {
        if (!confirm(`「${p.name}」を削除しますか？\n（売れたことがある商品は売上データに残ります）`)) return;
        await api(`${A}/products/${id}`, { method: "DELETE" });
        POS.toast("削除しました", "ok");
      }
      await loadProducts();
    } catch (ex) { fail(ex); }
  });

  // ============================================================ 注文（A-07, A-14）
  function fillDateSelect(sel, list, firstLabel) {
    const cur = sel.value;
    sel.innerHTML = `<option value="">${firstLabel}</option>` + list.map((d) => `<option value="${d}">${d}</option>`).join("");
    if (list.includes(cur)) sel.value = cur;
  }

  async function loadDates() {
    const s = await api(`${A}/summary`);
    dates = s.dates;
    if (!dates.includes(today())) dates = [today(), ...dates];
    fillDateSelect($("order-date"), dates, "全営業日");
    fillDateSelect($("purge-date"), dates, "全営業日");
    fillDateSelect($("sales-date"), dates, "全期間");
  }

  let orderList = [];
  async function loadOrders() {
    await loadDates();
    if (!products.length) products = await api(`${A}/products`);
    renderManualItems();
    const f = $("order-filter");
    const qs = new URLSearchParams({ date: f.date.value, status: f.status.value, q: f.q.value, test: f.test.value });
    orderList = await api(`${A}/orders?${qs}`);
    $("order-count").textContent = `${orderList.length}件（新しい順、最大500件）`;
    $("order-table").innerHTML = `<tr><th>ID</th><th>営業日</th><th>番号</th><th>会計</th><th>内容</th><th class="r">金額</th><th>状態</th><th>待ち時間</th><th></th></tr>` +
      orderList.map((o) => {
        const wait = o.wait_seconds ?? (o.status === "waiting" || o.status === "cooking"
          ? Math.floor((POS.serverNow() - o.paid_ts) / 1000) : null);
        return `<tr>
        <td class="num">${o.id}</td>
        <td class="num">${o.business_date}</td>
        <td class="num"><strong>${o.call_no}</strong></td>
        <td class="num">${POS.hhmm(o.paid_at)}</td>
        <td>${o.items.map((i) => `${esc(i.name)}×${i.qty}`).join("、")}
          ${o.source === "manual" ? '<span class="badge">手入力</span>' : ""}
          ${o.is_test ? '<span class="badge badge-test">テスト</span>' : ""}</td>
        <td class="r num">${POS.yen(o.total)}<br><small class="muted">${o.tickets}枚</small></td>
        <td><span class="badge badge-${o.status}">${POS.STATUS_LABEL[o.status]}</span></td>
        <td class="num">${o.status === "cancelled" ? "—" : fmtWait(wait)}</td>
        <td>${o.status === "delivered" || o.status === "cancelled"
          ? `<button type="button" class="btn-small btn-ghost-danger" data-del="${o.id}">削除</button>` : ""}</td>
      </tr>`;
      }).join("");
  }

  $("order-filter").addEventListener("submit", (e) => { e.preventDefault(); loadOrders().catch(fail); });

  $("order-table").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-del]");
    if (!b) return;
    const o = orderList.find((x) => x.id === Number(b.dataset.del));
    if (!confirm(`注文ID ${o.id}（${o.business_date} 呼出番号${o.call_no}・${POS.yen(o.total)}）を完全に削除しますか？\n売上集計からも消えます。元に戻せません。`)) return;
    try { await api(`${A}/orders/${o.id}`, { method: "DELETE" }); POS.toast("削除しました", "ok"); await loadOrders(); }
    catch (ex) { fail(ex); }
  });

  function renderManualItems() {
    const box = $("manual-items");
    const f = $("manual-form");
    if (!f.business_date.value) f.business_date.value = today();
    const prev = {};
    box.querySelectorAll("input[data-pid]").forEach((i) => { prev[i.dataset.pid] = i.value; });
    box.innerHTML = products.length ? products.map((p) => `
      <label style="display:flex;align-items:center;gap:6px">${esc(p.name)}（${POS.yen(p.price)}）
        <input type="number" min="0" max="99" data-pid="${p.id}" value="${prev[p.id] ?? ""}" placeholder="0" style="width:80px"></label>`).join("")
      : `<span class="muted">商品がありません</span>`;
    updateManualTotal();
  }
  function manualItems() {
    return [...$("manual-items").querySelectorAll("input[data-pid]")]
      .map((i) => ({ product_id: Number(i.dataset.pid), qty: Number(i.value || 0) }))
      .filter((i) => i.qty > 0);
  }
  function updateManualTotal() {
    const total = manualItems().reduce((s, i) => s + i.qty * (products.find((p) => p.id === i.product_id)?.price || 0), 0);
    $("manual-total").textContent = total ? `合計 ${POS.yen(total)}（金券${POS.tickets(total)}枚）` : "";
  }
  $("manual-items").addEventListener("input", updateManualTotal);

  $("manual-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const items = manualItems();
    if (!items.length) { fail(new Error("個数を入力してください")); return; }
    const body = {
      items, business_date: f.business_date.value, status: f.status.value,
      paid_time: f.paid_time.value || null,
      call_no: f.call_no.value === "" ? null : Number(f.call_no.value),
    };
    try {
      const o = await api(`${A}/orders`, { method: "POST", body });
      POS.toast(`登録しました（呼出番号 ${o.call_no}・${POS.yen(o.total)}）`, "ok", 4000);
      $("manual-items").querySelectorAll("input").forEach((i) => { i.value = ""; });
      f.call_no.value = "";
      updateManualTotal();
      await loadOrders();
    } catch (ex) { fail(ex); }
  });

  $("purge-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const statuses = [];
    if (f.delivered.checked) statuses.push("delivered");
    if (f.cancelled.checked) statuses.push("cancelled");
    if (!statuses.length) { fail(new Error("受渡済・取消のどちらかを選んでください")); return; }
    const body = { business_date: f.business_date.value || null, statuses, test: f.test.value };
    try {
      const pv = await api(`${A}/orders/purge-preview`, { method: "POST", body });
      if (!pv.count) { POS.toast("対象の注文はありません", "warn"); return; }
      if (!confirm(`対象: ${pv.count}件・${POS.yen(pv.amount)}（金券${pv.tickets}枚）\n\n完全に削除し、売上集計からも消えます。元に戻せません。\nCSVはもう保存しましたか？（「注文CSV」「明細CSV」）\n\n削除してよければOKを押してください。`)) return;
      const r = await api(`${A}/orders/purge`, { method: "POST", body });
      POS.toast(`${r.count}件を削除しました`, "ok", 4000);
      await loadOrders();
    } catch (ex) { fail(ex); }
  });

  // ============================================================ 売上・レジ締め（A-08, A-09）
  async function loadSales() {
    await loadDates();
    const date = $("sales-date").value;
    const s = await api(`${A}/summary?date=${date}`);
    $("sales-stats").innerHTML = `
      <div class="stat"><div class="k">注文数</div><div class="v num">${s.orders}件</div></div>
      <div class="stat"><div class="k">売上</div><div class="v num">${POS.yen(s.amount)}</div><div class="s num">金券 ${s.tickets}枚</div></div>
      <div class="stat"><div class="k">平均待ち時間</div><div class="v num">${fmtWait(s.avg_wait_seconds)}</div><div class="s">受渡済 ${s.delivered_count}件</div></div>
      <div class="stat"><div class="k">除外（取消）</div><div class="v num">${s.cancelled_count}件</div><div class="s num">${POS.yen(s.cancelled_amount)}</div></div>
      <div class="stat"><div class="k">除外（テスト）</div><div class="v num">${s.test_count}件</div></div>`;
    $("sales-products").innerHTML = `<tr><th>商品</th><th class="r">個数</th><th class="r">金額</th><th class="r">金券</th></tr>` +
      (s.products.map((p) => `<tr><td>${esc(p.name)}</td><td class="r num">${p.qty}</td><td class="r num">${POS.yen(p.amount)}</td><td class="r num">${p.tickets}枚</td></tr>`).join("")
        || `<tr><td colspan="4" class="muted">まだありません</td></tr>`);
    const maxH = Math.max(1, ...s.hourly.map((h) => h.orders));
    $("sales-hourly").innerHTML = `<tr><th>時間帯</th><th class="r">注文数</th><th style="width:50%"></th></tr>` +
      (s.hourly.map((h) => `<tr><td class="num">${h.hour}時台</td><td class="r num">${h.orders}</td>
        <td><div class="bar" style="width:${(h.orders / maxH) * 100}%"></div></td></tr>`).join("")
        || `<tr><td colspan="3" class="muted">まだありません</td></tr>`);
    $("sales-days").innerHTML = `<tr><th>営業日</th><th class="r">注文数</th><th class="r">売上</th><th class="r">金券</th></tr>` +
      (s.days.map((d) => `<tr><td class="num">${d.business_date} ${dayLabel(d.business_date)}</td><td class="r num">${d.orders}</td>
        <td class="r num">${POS.yen(d.amount)}</td><td class="r num">${d.tickets}枚</td></tr>`).join("")
        || `<tr><td colspan="4" class="muted">まだありません</td></tr>`);

    if (!$("closing-date").value) $("closing-date").value = today();
    await updateClosingExpected();
    await loadClosings();
  }
  $("sales-date").addEventListener("change", () => loadSales().catch(fail));
  $("btn-sales-reload").addEventListener("click", () => loadSales().catch(fail));

  let settingsCache = {};
  const dayLabel = (d) => (d === settingsCache.day1 ? "（1日目）" : d === settingsCache.day2 ? "（2日目）" : "");

  async function updateClosingExpected() {
    const d = $("closing-date").value;
    if (!d) return;
    const s = await api(`${A}/summary?date=${d}`);
    $("closing-expected").textContent = `この日の売上 ${POS.yen(s.amount)} ＝ 金券 ${s.tickets}枚（${s.orders}件）`;
  }
  $("closing-date").addEventListener("change", () => updateClosingExpected().catch(fail));

  async function loadClosings() {
    const list = await api(`${A}/closings`);
    $("closing-table").innerHTML = `<tr><th>記録時刻</th><th>営業日</th><th class="r">売上（枚）</th><th class="r">数えた枚数</th><th class="r">差</th><th>メモ</th></tr>` +
      (list.map((c) => `<tr><td class="num">${esc(c.at.replace("T", " "))}</td><td class="num">${c.business_date}</td>
        <td class="r num">${c.expected_tickets}</td><td class="r num">${c.counted_tickets}</td>
        <td class="r num" style="font-weight:800;color:${c.diff_tickets ? "var(--danger)" : "var(--ok)"}">${c.diff_tickets > 0 ? "+" : ""}${c.diff_tickets}枚</td>
        <td>${esc(c.note)}</td></tr>`).join("") || `<tr><td colspan="6" class="muted">まだありません</td></tr>`);
  }

  $("closing-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    try {
      const r = await api(`${A}/closing`, { method: "POST", body: {
        business_date: f.business_date.value, counted_tickets: Number(f.counted_tickets.value), note: f.note.value } });
      const ok = r.diff_tickets === 0;
      $("closing-result").innerHTML = `<div class="${ok ? "card" : "danger-box"}" style="font-size:1.2rem">
        売上 ${r.expected_tickets}枚（${POS.yen(r.expected_amount)}）／ 数えた枚数 ${r.counted_tickets}枚 ／
        差 <strong>${r.diff_tickets > 0 ? "+" : ""}${r.diff_tickets}枚（${POS.yen(r.diff_amount)}）</strong>
        ${ok ? "：一致しました" : ""}
        ${r.open_orders ? `<br>※ 受渡前の注文が${r.open_orders}件残っています` : ""}</div>`;
      if (ok) POS.sound.done(); else POS.sound.error();
      f.counted_tickets.value = "";
      f.note.value = "";
      await loadClosings();
    } catch (ex) { fail(ex); }
  });

  // ============================================================ 操作ログ（A-06）
  let lastLogId = 0;
  let logFilter = {};
  function logRow(ev) {
    const c = ev.content || {};
    let text;
    if (c.items) text = `番号${c.call_no ?? ""} ${c.items.join("、")} ${c.total !== undefined ? POS.yen(c.total) : ""}`;
    else if (c.from && c.to) text = `番号${c.call_no} ${POS.STATUS_LABEL[c.from]} → ${POS.STATUS_LABEL[c.to]}`;
    else if (ev.kind === "sold_out") text = `${c.name}：${c.sold_out ? "売切" : "販売再開"}`;
    else if (ev.kind === "orders_purged") text = `${c.count}件・${POS.yen(c.amount)} を削除`;
    else if (ev.kind === "closing") text = `${c.business_date} 売上${c.expected_tickets}枚／実数${c.counted_tickets}枚／差${c.diff_tickets}枚`;
    else text = JSON.stringify(c, null, 0);
    return `<tr>
      <td class="num" style="white-space:nowrap">${esc(ev.at.replace("T", " "))}</td>
      <td style="white-space:nowrap">${esc(ev.terminal)} <small class="muted">${esc(ev.terminal_id || "")}</small></td>
      <td style="white-space:nowrap">${esc(KIND_LABEL[ev.kind] || ev.kind)}</td>
      <td>${esc(text)}</td></tr>`;
  }

  async function loadLogs(reset) {
    const f = $("log-filter");
    if (reset) {
      logFilter = {
        terminal: f.terminal.value, kind: f.kind.value,
        since: f.since.value ? f.since.value + ":00" : "", until: f.until.value ? f.until.value + ":59" : "",
      };
      const list = await api(`${A}/events?${new URLSearchParams(logFilter)}`);
      lastLogId = list.length ? list[0].id : lastLogId;
      $("log-table").innerHTML = `<thead><tr><th>時刻</th><th>端末</th><th>種類</th><th>内容</th></tr></thead><tbody>` +
        (list.map(logRow).join("") || "") + "</tbody>";
      return;
    }
    const list = await api(`${A}/events?${new URLSearchParams({ ...logFilter, after_id: lastLogId })}`);
    if (!list.length) return;
    lastLogId = list[0].id;
    const tbody = $("log-table").querySelector("tbody");
    if (tbody) tbody.insertAdjacentHTML("afterbegin", list.map(logRow).join(""));
  }
  $("log-kind").innerHTML += Object.entries(KIND_LABEL).map(([k, v]) => `<option value="${k}">${v}</option>`).join("");
  $("log-filter").addEventListener("submit", (e) => { e.preventDefault(); loadLogs(true).catch(fail); });
  setInterval(() => { if (tab === "logs" && !logFilter.until) loadLogs(false).catch(() => {}); }, 3000);

  // ============================================================ 設定（A-05）
  async function loadSettings() {
    const s = await api(`${A}/settings`);
    settingsCache = s;
    const f = $("settings-form");
    f.shop_name.value = s.shop_name || "";
    f.day1.value = s.day1 || "";
    f.day2.value = s.day2 || "";
    f.call_no_start.value = s.call_no_start;
    f.test_mode.checked = s.test_mode;
    f.usb_backup_dir.value = s.usb_backup_dir || "";
    f.passcode.value = "";
    $("s-pass2").value = "";
  }

  $("settings-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const body = {
      shop_name: f.shop_name.value, day1: f.day1.value, day2: f.day2.value,
      call_no_start: Number(f.call_no_start.value || 1), test_mode: f.test_mode.checked,
      usb_backup_dir: f.usb_backup_dir.value,
    };
    if (f.passcode.value) {
      if (f.passcode.value !== $("s-pass2").value) { fail(new Error("パスコードが一致しません")); return; }
      if (!confirm("パスコードを変更すると、PC1・PC2は登録し直しになります。変更しますか？")) return;
      body.passcode = f.passcode.value;
    }
    try {
      await api(`${A}/settings`, { method: "PUT", body });
      POS.toast("保存しました", "ok");
      await loadSettings();
    } catch (ex) { fail(ex); }
  });

  function applySettings(s) {
    $("shop-name").textContent = s.shop_name || "";
    $("test-badge").classList.toggle("hidden", !s.test_mode);
    settingsCache = { ...settingsCache, ...s };
  }

  // ============================================================ 通知
  let refreshTimer = null;
  function softRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => {
      if (tab === "orders") loadOrders().catch(() => {});
      if (tab === "sales") loadSales().catch(() => {});
      if (tab === "home") loadHome().catch(() => {});
    }, 800);
  }

  POS.connect({
    onSync: () => { api(`${A}/settings`).then((s) => { settingsCache = s; applySettings(s); }).catch(() => {}); refreshTab(); },
    onMessage: (msg) => {
      if (msg.type === "settings_updated") applySettings(msg.settings);
      if (msg.type === "product_updated" && tab === "products") loadProducts().catch(() => {});
      if (msg.type === "product_updated" && tab !== "products") api(`${A}/products`).then((p) => { products = p; }).catch(() => {});
      if (msg.type === "order_created" || msg.type === "order_updated") softRefresh();
    },
  });

  showTab(POS.store.get("pos.admin.tab", "home"));
})();
