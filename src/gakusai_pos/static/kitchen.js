// 厨房端末（PC2）：注文カード、状態変更、取消表示、売切切替
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const ROLE = "kitchen";
  const api = (path, opts = {}) => POS.api(path, { ...opts, role: ROLE });
  const LATE_MS = 10 * 60 * 1000; // 10分を超えたら経過時間を赤く

  let orders = new Map(); // 画面に出す注文（準備中・未確認の取消）
  let products = [];
  const fresh = new Map(); // 新着の強調表示 id -> 期限
  let busy = new Set(); // 送信中の注文ID（ボタンの連打防止）

  // ============================================================ 描画
  function isShown(o) {
    if (o.category === "goods") return false; // 物販はその場で渡すので厨房には出さない
    return o.status === "waiting" || (o.status === "cancelled" && !o.cancel_ack_at);
  }

  // 割引券（価格がマイナスの行）は作る物ではないので厨房には出さない
  const goodsOf = (o) => o.items.filter((i) => i.unit_price > 0);

  function cardHtml(o) {
    const items = goodsOf(o).map((i) =>
      `<li>${POS.esc(i.name)}<span class="q num">×${i.qty}</span></li>`).join("");
    const isNew = fresh.has(o.id) && fresh.get(o.id) > Date.now();
    const dis = busy.has(o.id) ? "disabled" : "";
    let acts = "";
    if (o.status === "waiting") {
      acts = `<button type="button" class="main btn-ok" data-act="deliver" data-id="${o.id}" ${dis}>受渡完了</button>`;
    } else if (o.status === "cancelled") {
      acts = `<button type="button" class="main btn-danger" data-act="ack" data-id="${o.id}" ${dis}>確認（作らない）</button>`;
    }
    return `<article class="order-card ${o.status}${isNew ? " new" : ""}" data-id="${o.id}">
      <div class="no num">${o.call_no}</div>
      <div class="meta">
        <span class="num">会計 ${POS.hhmm(o.paid_at)}</span>
        <span class="elapsed num" data-paid="${o.paid_ts}"></span>
        ${o.is_test ? '<span class="badge badge-test test">テスト</span>' : ""}
      </div>
      ${o.status === "cancelled" ? '<div class="cancel-label">取消</div>' : ""}
      <ul class="items">${items}</ul>
      <div class="acts">${acts}</div>
    </article>`;
  }

  function render() {
    const list = [...orders.values()].filter(isShown).sort((a, b) => a.id - b.id);
    $("list-waiting").innerHTML = list.length
      ? list.map(cardHtml).join("")
      : `<div class="empty-col">準備中の注文はありません</div>`;
    $("count-waiting").textContent = list.filter((o) => o.status !== "cancelled").length;
    renderSummary();
    tickElapsed();
  }

  // K-08 商品別の準備中の数
  function renderSummary() {
    const sum = new Map();
    for (const o of orders.values()) {
      if (o.status !== "waiting") continue;
      for (const i of goodsOf(o)) sum.set(i.name, (sum.get(i.name) || 0) + i.qty);
    }
    $("kd-summary").innerHTML = sum.size
      ? [...sum.entries()].map(([n, q]) => `<span class="chip">${POS.esc(n)}<b class="num">${q}</b></span>`).join("")
      : `<span class="muted">準備中の商品はありません</span>`;
  }

  function tickElapsed() {
    const now = POS.serverNow();
    document.querySelectorAll(".elapsed[data-paid]").forEach((el) => {
      const ms = now - Number(el.dataset.paid);
      el.textContent = `経過 ${POS.elapsed(ms)}`;
      el.classList.toggle("late", ms > LATE_MS);
    });
  }
  setInterval(tickElapsed, 1000);

  // ============================================================ 操作
  const ACTIONS = {
    deliver: { current: "waiting", to: "delivered" },
  };

  async function changeStatus(id, current, to) {
    busy.add(id);
    render();
    try {
      const o = await api(`/api/orders/${id}/status`, { method: "POST", body: { current, to } });
      upsert(o);
      if (to === "delivered") POS.toast(`呼出番号 ${o.call_no} 受渡完了`, "ok", 1500);
    } catch (e) {
      POS.sound.error();
      POS.toast(e.message, "error", 3500);
      if (e.data && e.data.order) upsert(e.data.order); // 最新の状態を表示
    } finally {
      busy.delete(id);
      render();
    }
  }

  document.querySelector(".kd-cols").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-act]");
    if (!b || b.disabled) return;
    const id = Number(b.dataset.id);
    const act = b.dataset.act;
    if (ACTIONS[act]) {
      await changeStatus(id, ACTIONS[act].current, ACTIONS[act].to);
    } else if (act === "ack") {
      busy.add(id);
      try { upsert(await api(`/api/orders/${id}/ack`, { method: "POST" })); }
      catch (ex) { POS.toast(ex.message, "error"); }
      finally { busy.delete(id); render(); }
    }
  });

  function upsert(o) {
    if (o.deleted) orders.delete(o.id);
    else if (isShown(o)) orders.set(o.id, o);
    else orders.delete(o.id);
  }

  // ============================================================ 売切切替（K-09）
  const foodOnly = (list) => list.filter((p) => p.category === "food");

  function renderSoldOut() {
    $("soldout-list").innerHTML = products.length ? products.map((p) => `
      <li><span>${POS.esc(p.name)}${p.stock !== null ? `<small class="muted">（残り${p.stock}）</small>` : ""}</span>
        <button type="button" class="${p.sold_out ? "btn-danger" : ""}" data-id="${p.id}" data-sold="${p.sold_out ? 1 : 0}">
          ${p.sold_out ? "売切中 → 販売再開" : "販売中 → 売切にする"}</button></li>`).join("")
      : `<li class="muted">商品が登録されていません</li>`;
  }
  $("btn-soldout").addEventListener("click", async () => {
    $("ov-soldout").classList.remove("hidden");
    try { products = foodOnly(await api("/api/products")); } catch {}
    renderSoldOut();
  });
  $("soldout-list").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-id]");
    if (!b) return;
    const sold = b.dataset.sold !== "1";
    const p = products.find((x) => x.id === Number(b.dataset.id));
    if (sold && !confirm(`「${p ? p.name : ""}」を売切にしますか？（受付で選べなくなります）`)) return;
    b.disabled = true;
    try {
      const np = await api(`/api/products/${b.dataset.id}/sold-out`, { method: "POST", body: { sold_out: sold } });
      products = products.map((x) => (x.id === np.id ? np : x));
    } catch (ex) { POS.toast(ex.message, "error"); }
    renderSoldOut();
  });

  // ============================================================ 最近受け渡した注文（K-05）
  let recentOpen = false;
  async function loadRecent() {
    const list = await api("/api/orders?status=delivered&limit=10");
    $("recent-list").innerHTML = list.length ? list.map((o) => `
      <li class="history-item">
        <span class="no num">${o.call_no}</span>
        <span class="info">
          <span class="muted num">会計 ${POS.hhmm(o.paid_at)} ／ 受渡 ${POS.hhmm(o.delivered_at)}</span>
          <span>${goodsOf(o).map((i) => `${POS.esc(i.name)}×${i.qty}`).join("、")}</span>
        </span>
        <span class="acts"><button type="button" class="btn-small" data-id="${o.id}">準備中に戻す</button></span>
      </li>`).join("") : `<li class="muted" style="padding:16px">まだありません</li>`;
  }
  $("btn-recent").addEventListener("click", async () => {
    recentOpen = true;
    $("ov-recent").classList.remove("hidden");
    try { await loadRecent(); } catch (e) { POS.toast(e.message, "error"); }
  });
  $("recent-list").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-id]");
    if (!b) return;
    b.disabled = true;
    await changeStatus(Number(b.dataset.id), "delivered", "waiting");
    await loadRecent().catch(() => {});
  });

  document.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => {
    b.closest(".overlay").classList.add("hidden");
    if (b.closest("#ov-recent")) recentOpen = false;
  }));

  // ============================================================ 通知
  function onMessage(msg) {
    if (msg.type === "order_created") {
      const o = msg.order;
      if (isShown(o)) {
        orders.set(o.id, o);
        fresh.set(o.id, Date.now() + 5000);
        setTimeout(render, 5100);
        POS.sound.newOrder(); // K-06
      }
      render();
    } else if (msg.type === "order_updated") {
      const o = msg.order;
      const before = orders.get(o.id);
      if (o.status === "cancelled" && !o.cancel_ack_at && (!before || before.status !== "cancelled")) {
        POS.sound.alert(); // K-07 取消は目立たせる
        POS.toast(`呼出番号 ${o.call_no} が取り消されました`, "error", 4000);
      }
      upsert(o);
      render();
      if (recentOpen) loadRecent().catch(() => {});
    } else if (msg.type === "product_updated") {
      const p = msg.product;
      products = products.filter((x) => x.id !== p.id);
      if (!p.deleted && p.category === "food") products.push(p);
      products.sort((a, b) => a.sort_order - b.sort_order || a.id - b.id);
      renderSoldOut();
    } else if (msg.type === "settings_updated") {
      $("shop-name").textContent = msg.settings.shop_name || "";
      $("test-badge").classList.toggle("hidden", !msg.settings.test_mode);
    }
  }

  async function sync() {
    try {
      const [list, prods] = await Promise.all([api("/api/orders?status=open"), api("/api/products")]);
      orders = new Map(list.map((o) => [o.id, o]));
      products = foodOnly(prods);
      render();
      renderSoldOut();
      if (recentOpen) await loadRecent();
    } catch (e) {
      if (e.status !== 401) POS.toast(e.message, "error");
    }
  }

  render();
  POS.keepAwake();
  POS.connect({ onMessage, onSync: sync });
})();
