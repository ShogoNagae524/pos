// 受付端末（PC1 飲食）と物販端末（PC4）：商品入力（ボタン・QR）、会計、履歴・取消
// 物販は調理の工程がないので、会計した時点で受渡済になる（厨房には出ない）
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const ROLE = document.body.dataset.role === "goods" ? "goods" : "reception";
  const GOODS = ROLE === "goods";
  const CATEGORY = GOODS ? "goods" : "food"; // この端末で扱う商品の区分
  const NO_LABEL = GOODS ? "受付番号" : "呼出番号";
  const api = (path, opts = {}) => POS.api(path, { ...opts, role: ROLE });

  const KEY_CART = `pos.${ROLE}.cart`;
  const KEY_PENDING = `pos.${ROLE}.pending`;
  const KEY_CAM = `pos.${ROLE}.camera`;
  const KEY_PRE = `pos.${ROLE}.preorder`;
  const KEY_PRE_USED = `pos.${ROLE}.preorderUsed`;

  let products = new Map(); // id -> product
  let byCode = new Map(); // code -> product
  // カートは [{product_id, qty}]。ブラウザを閉じても残す（第10章）
  let cart = POS.store.get(KEY_CART, []);
  let nextQty = 1; // R-04 数字を押してから商品を追加
  let pending = POS.store.get(KEY_PENDING, null); // 送信中の会計 {client_uuid, items, total}
  let historyOpen = false;
  // カートに読み込んだ事前注文 {id, issues:[], priceChanged}。カートが空になったら消す
  let preorder = POS.store.get(KEY_PRE, null);
  if (!cart.length) preorder = null;
  // 会計済みの事前注文ID（同じQRで二重に会計しないよう、最新200件を覚えておく）
  let usedPreorders = POS.store.get(KEY_PRE_USED, []);

  // ============================================================ 商品
  async function loadProducts() {
    const list = (await api("/api/products")).filter((p) => p.category === CATEGORY);
    products = new Map(list.map((p) => [p.id, p]));
    byCode = new Map(list.map((p) => [p.code.toUpperCase(), p]));
    renderProducts();
    renderCart();
  }

  function renderProducts() {
    const grid = $("product-grid");
    const list = [...products.values()].sort((a, b) => a.sort_order - b.sort_order || a.id - b.id);
    $("no-products").classList.toggle("hidden", list.length > 0);
    grid.innerHTML = list.map((p) => `
      <button type="button" class="product-btn${p.sold_out ? " sold-out" : ""}${p.price < 0 ? " discount" : ""}" data-id="${p.id}">
        <span>${POS.esc(p.name)}</span>
        <span class="price num">${POS.yen(p.price)}</span>
        ${p.stock !== null && !p.sold_out ? `<span class="stock">残り${p.stock}</span>` : ""}
      </button>`).join("");
  }

  $("product-grid").addEventListener("click", (e) => {
    const btn = e.target.closest(".product-btn");
    if (btn) addProduct(products.get(Number(btn.dataset.id)));
  });

  function addProduct(p) {
    if (!p) return false;
    if (p.sold_out) {
      POS.sound.error();
      POS.toast(`「${p.name}」は売切です`, "error");
      return false;
    }
    const qty = nextQty;
    const line = cart.find((l) => l.product_id === p.id);
    if (line) line.qty = Math.min(line.qty + qty, 99);
    else cart.push({ product_id: p.id, qty });
    setNextQty(1);
    saveCart();
    renderCart();
    return true;
  }

  // ============================================================ 個数指定（R-04）
  function setNextQty(n) {
    nextQty = n;
    document.querySelectorAll("#qty-row [data-qty]").forEach((b) =>
      b.classList.toggle("active", Number(b.dataset.qty) === n));
    $("qty-pending").textContent = n > 1 ? `次の商品を ${n}個 追加` : "";
  }
  $("qty-row").addEventListener("click", (e) => {
    const b = e.target.closest("[data-qty]");
    if (!b) return;
    const n = Number(b.dataset.qty);
    setNextQty(nextQty === n ? 1 : n);
  });

  // ============================================================ カート
  function saveCart() {
    if (!cart.length) preorder = null;
    POS.store.set(KEY_CART, cart);
    if (preorder) POS.store.set(KEY_PRE, preorder);
    else POS.store.del(KEY_PRE);
  }

  // 価格がマイナスの商品は割引券。count は商品の点数、coupons は割引券の枚数
  function cartTotals() {
    let total = 0, count = 0, coupons = 0, items = 0;
    for (const l of cart) {
      const p = products.get(l.product_id);
      if (p) total += p.price * l.qty;
      if (p && p.price < 0) coupons += l.qty;
      else count += l.qty;
      if (p && p.price > 0) items += l.qty;
    }
    return { total, count, coupons, items };
  }

  // 割引券だけ・合計がマイナスの会計はできない（サーバーでも拒否する）
  function discountProblem() {
    const { total, coupons, items } = cartTotals();
    if (!coupons) return "";
    if (!items) return "割引券だけでは会計できません。商品を入れてください";
    if (total < 0) return "割引が合計金額を超えています（おつりは出せません）。割引券を減らしてください";
    return "";
  }

  function cartProblems() {
    return cart.filter((l) => { const p = products.get(l.product_id); return !p || p.sold_out; });
  }

  function renderCart() {
    const ul = $("cart-list");
    if (!cart.length) {
      ul.innerHTML = GOODS
        ? `<li class="cart-empty">商品ボタンを押してください</li>`
        : `<li class="cart-empty">商品ボタンを押すか、事前注文のQRをかざしてください</li>`;
    } else {
      ul.innerHTML = cart.map((l, i) => {
        const p = products.get(l.product_id);
        const name = p ? p.name : "（削除された商品）";
        const problem = !p ? "削除済み" : p.sold_out ? "売切" : "";
        return `<li class="cart-line${problem ? " problem" : ""}">
          <span class="name">${POS.esc(name)}${problem ? `（${problem}）` : ""}</span>
          <span class="sub num">${p ? POS.yen(p.price * l.qty) : ""}</span>
          <div class="ctrl">
            <button type="button" data-act="minus" data-i="${i}" aria-label="1個減らす">−</button>
            <span class="qty num">${l.qty}</span>
            <button type="button" data-act="plus" data-i="${i}" aria-label="1個増やす">＋</button>
            <button type="button" class="del btn-ghost-danger" data-act="del" data-i="${i}">削除</button>
          </div>
        </li>`;
      }).join("");
    }
    renderPreorderNote();
    const { total, count, coupons } = cartTotals();
    $("total-amount").textContent = POS.yen(total);
    $("total-detail").textContent = discountProblem() ||
      `${count}点・金券${POS.tickets(total)}枚${coupons ? `・割引券${coupons}枚` : ""}`;
    $("btn-checkout").disabled = cart.length === 0;
    $("btn-clear").disabled = cart.length === 0;
  }

  $("cart-list").addEventListener("click", (e) => {
    const b = e.target.closest("[data-act]");
    if (!b) return;
    const i = Number(b.dataset.i);
    const line = cart[i];
    if (!line) return;
    if (b.dataset.act === "plus") line.qty = Math.min(line.qty + 1, 99);
    if (b.dataset.act === "minus") {
      line.qty -= 1;
      if (line.qty <= 0) cart.splice(i, 1);
    }
    if (b.dataset.act === "del") cart.splice(i, 1);
    saveCart();
    renderCart();
  });

  $("btn-clear").addEventListener("click", () => {
    if (!cart.length || !confirm("カートを空にしますか？")) return;
    cart = [];
    setNextQty(1);
    saveCart();
    renderCart();
  });

  function renderPreorderNote() {
    const box = $("preorder-note");
    if (!preorder) { box.classList.add("hidden"); return; }
    const warn = preorder.issues.length > 0 || preorder.priceChanged;
    let html = `<strong>事前注文 ${POS.esc(preorder.id)}</strong> を読み込みました。内容をお客様と確認してから会計してください`;
    if (preorder.issues.length) {
      html += `<br>入れられなかった商品：${preorder.issues.map(POS.esc).join("、")}`;
    }
    if (preorder.priceChanged) {
      html += `<br>お客様のページの合計（${POS.yen(preorder.pageTotal)}）と今の価格が違います`;
    }
    box.innerHTML = html;
    box.classList.toggle("warn", warn);
    box.classList.remove("hidden");
  }

  // ============================================================ 事前注文QR
  // お客様が事前注文ページで作ったQR。カートに入れるだけで、会計はいつもどおり店員が確定する。
  // 中身はお客様が書き換えられるので、商品・価格・売切はすべてこの端末の最新の情報で判断する
  function preorderError(msg) {
    POS.sound.error();
    flash("ng");
    POS.toast(msg, "error", 4000);
    $("cam-msg").textContent = msg;
  }

  function loadPreorder(raw) {
    if (GOODS) {
      preorderError("飲食の事前注文QRです。飲食の受付（PC1）で読み込んでください");
      return;
    }
    const pre = PreorderCode.parse(raw);
    if (!pre) {
      preorderError("事前注文QRの形式が正しくありません。商品ボタンで入力してください");
      return;
    }
    if (preorder && preorder.id === pre.id) {
      POS.toast(`事前注文 ${pre.id} は読み込み済みです`, "warn");
      return;
    }
    if (usedPreorders.includes(pre.id)) {
      preorderError(`事前注文 ${pre.id} は会計済みです。追加の注文は商品ボタンで入力してください`);
      return;
    }
    if (cart.length) {
      preorderError("カートに商品があります。会計を済ませるか、カートを空にしてから読み込んでください");
      return;
    }
    const issues = [];
    const lines = [];
    let pageTotalNow = 0; // ページの商品を今の価格で計算した合計
    let allKnown = true;
    for (const l of pre.lines) {
      const p = byCode.get(l.code);
      if (!p) {
        allKnown = false;
        issues.push(`未登録のコード ${l.code}`);
        continue;
      }
      // 割引券は事前注文に入れられない（券を受け取ってから受付で入れる）
      if (p.price < 0) { issues.push(`${p.name}（割引券は券を受け取ってから入れてください）`); continue; }
      pageTotalNow += p.price * l.qty;
      if (p.sold_out) { issues.push(`${p.name}（売切）`); continue; }
      lines.push({ product_id: p.id, qty: l.qty });
    }
    if (!lines.length) {
      preorderError(`事前注文の商品をカートに入れられませんでした（${issues.join("、")}）`);
      return;
    }
    cart = lines;
    preorder = {
      id: pre.id,
      issues,
      pageTotal: pre.total,
      priceChanged: allKnown && pageTotalNow !== pre.total,
    };
    setNextQty(1);
    saveCart();
    renderCart();
    const warn = issues.length > 0 || preorder.priceChanged;
    if (warn) POS.sound.error(); else POS.sound.ok();
    flash(warn ? "ng" : "ok");
    $("cam-msg").textContent = `事前注文 ${pre.id} を読み込みました`;
    POS.toast(warn ? "事前注文を読み込みました。カートの注意を確認してください"
      : "事前注文を読み込みました。内容を確認して会計へ", warn ? "warn" : "ok", 3000);
  }

  function markPreorderUsed(id) {
    if (!id || usedPreorders.includes(id)) return;
    usedPreorders = [...usedPreorders, id].slice(-200);
    POS.store.set(KEY_PRE_USED, usedPreorders);
  }

  // 縦長の画面では合計をタップしてカートを開閉
  $("cart-total").addEventListener("click", () => {
    const c = $("cart");
    c.classList.toggle("open");
    $("open-hint").textContent = c.classList.contains("open") ? "カート ▼" : "カート ▲";
  });

  // ============================================================ 会計（R-21〜R-25）
  function show(id, on) { $(id).classList.toggle("hidden", !on); }

  $("btn-checkout").addEventListener("click", () => {
    if (!cart.length) return;
    const bad = cartProblems();
    if (bad.length) {
      POS.sound.error();
      POS.toast("売切・削除された商品がカートにあります。削除してください", "error", 3500);
      return;
    }
    const discountErr = discountProblem();
    if (discountErr) {
      POS.sound.error();
      POS.toast(discountErr, "error", 3500);
      return;
    }
    const rows = cart.map((l) => {
      const p = products.get(l.product_id);
      return `<tr${p.price < 0 ? ' class="discount"' : ""}><td>${POS.esc(p.name)}</td><td class="num">×${l.qty}</td>
              <td class="num">${POS.yen(p.price * l.qty)}</td></tr>`;
    }).join("");
    $("checkout-lines").innerHTML = rows;
    const { total, coupons } = cartTotals();
    $("checkout-total").innerHTML =
      `合計 ${POS.yen(total)}<span class="tickets">＝ 金券 ${POS.tickets(total)}枚</span>` +
      (coupons ? `<span class="coupons">＋ 割引券 ${coupons}枚を受け取る</span>` : "");
    setSaveStatus("");
    $("btn-paid").disabled = false;
    $("btn-back").disabled = false;
    show("ov-checkout", true);
  });

  $("btn-back").addEventListener("click", () => show("ov-checkout", false));

  function setSaveStatus(msg, kind = "") {
    const el = $("save-status");
    el.textContent = msg;
    el.className = `save-status ${kind}`;
    el.classList.toggle("hidden", !msg);
  }

  $("btn-paid").addEventListener("click", () => {
    $("btn-paid").disabled = true; // 二重送信防止：押したら即座に無効化
    $("btn-back").disabled = true;
    const { total } = cartTotals();
    pending = {
      client_uuid: POS.uuid(),
      items: cart.map((l) => ({ product_id: l.product_id, qty: l.qty })),
      total,
      preorder_id: preorder ? preorder.id : null,
    };
    POS.store.set(KEY_PENDING, pending);
    submitPending();
  });

  let retryTimer = null;
  let sending = false;
  async function submitPending() {
    clearTimeout(retryTimer);
    if (!pending || sending) return;
    sending = true;
    try { await doSubmit(); } finally { sending = false; }
  }

  async function doSubmit() {
    show("ov-checkout", true);
    $("btn-paid").disabled = true;
    $("btn-back").disabled = true;
    setSaveStatus("保存しています…");
    try {
      const order = await api("/api/orders", {
        method: "POST",
        body: { client_uuid: pending.client_uuid, items: pending.items },
      });
      markPreorderUsed(pending.preorder_id);
      pending = null;
      POS.store.del(KEY_PENDING);
      cart = [];
      saveCart();
      renderCart();
      show("ov-checkout", false);
      showCallNo(order, true);
      POS.sound.done();
    } catch (e) {
      if (e.network || e.status === 0 || e.status >= 500) {
        // 通信が切れた：同じIDで自動再送信（サーバーは1件として扱う）
        setSaveStatus(GOODS
          ? "保存に失敗しました。自動で再送信しています…（番号が出るまで商品を渡さないでください）"
          : "保存に失敗しました。自動で再送信しています…（番号が出るまで番号札を渡さないでください）", "error");
        retryTimer = setTimeout(submitPending, 2000);
      } else {
        // 売切などで受け付けられなかった：カートに戻して直してもらう
        pending = null;
        POS.store.del(KEY_PENDING);
        POS.sound.error();
        setSaveStatus(e.message, "error");
        $("btn-back").disabled = false;
        $("btn-paid").disabled = false;
        loadProducts().catch(() => {});
      }
    }
  }

  function showCallNo(order, fresh) {
    $("callno-label").textContent = fresh ? NO_LABEL : `${NO_LABEL}（再表示）`;
    $("callno-value").textContent = order.call_no;
    const items = order.items.map((i) => `${i.name}×${i.qty}`).join("、");
    $("callno-detail").textContent =
      `${items}　${POS.yen(order.total)}（金券${order.tickets}枚）${order.is_test ? "【テスト】" : ""}`;
    if (GOODS) {
      // 準備が要る商品を含む注文は準備中から始まり、できあがるとPC5に番号が出る
      $("callno-hint").textContent = order.status === "waiting" || order.status === "ready"
        ? "この番号を番号札に書いて渡してください。できあがると受渡表示（PC5）に番号が出ます（準備の要らない商品は今お渡しください）"
        : "商品をお渡しください";
    }
    show("ov-callno", true);
  }

  $("btn-next").addEventListener("click", () => {
    show("ov-callno", false);
    setNextQty(1);
  });

  // ============================================================ 履歴・取消（R-31〜R-33）
  let todayOrders = [];

  async function loadHistory() {
    todayOrders = await api("/api/orders?status=day");
    renderHistory();
  }

  function renderHistory() {
    const ul = $("history-list");
    if (!todayOrders.length) {
      ul.innerHTML = `<li class="muted" style="padding:16px">今日の注文はまだありません</li>`;
      return;
    }
    ul.innerHTML = todayOrders.map((o) => {
      // 飲食は受渡前だけ、物販は受渡済（返品）を取り消せる
      const open = GOODS ? ["waiting", "ready", "delivered"].includes(o.status)
        : o.status === "waiting" || o.status === "cooking";
      return `<li class="history-item">
        <span class="no num">${o.call_no}</span>
        <span class="info">
          <span><span class="badge badge-${o.status}">${POS.esc(o.status_label)}</span>
            ${o.is_test ? '<span class="badge badge-test">テスト</span>' : ""}
            <span class="muted num">${POS.hhmm(o.paid_at)}</span></span>
          <span>${o.items.map((i) => `${POS.esc(i.name)}×${i.qty}`).join("、")}</span>
          <span class="num">${POS.yen(o.total)}（金券${o.tickets}枚）</span>
        </span>
        <span class="acts">
          <button type="button" class="btn-small" data-act="recall" data-id="${o.id}">番号表示</button>
          ${open ? `<button type="button" class="btn-small btn-danger" data-act="cancel" data-id="${o.id}">取消</button>` : ""}
        </span>
      </li>`;
    }).join("");
  }

  $("btn-history").addEventListener("click", async () => {
    historyOpen = true;
    show("ov-history", true);
    try { await loadHistory(); } catch (e) { POS.toast(e.message, "error"); }
  });
  $("btn-history-close").addEventListener("click", () => { historyOpen = false; show("ov-history", false); });

  $("history-list").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-act]");
    if (!b) return;
    const o = todayOrders.find((x) => x.id === Number(b.dataset.id));
    if (!o) return;
    if (b.dataset.act === "recall") { showCallNo(o, false); return; }
    if (b.dataset.act === "cancel") {
      const items = o.items.map((i) => `${i.name}×${i.qty}`).join("、");
      const extra = GOODS ? "商品を返してもらい、" : "";
      const coupons = o.items.filter((i) => i.unit_price < 0).reduce((s, i) => s + i.qty, 0);
      const back = coupons ? `金券${o.tickets}枚と割引券${coupons}枚` : `金券${o.tickets}枚`;
      if (!confirm(`${NO_LABEL} ${o.call_no}（${items}／${POS.yen(o.total)}）を取り消しますか？\n${extra}${back}を手でお返しください。`)) return;
      b.disabled = true;
      try {
        await api(`/api/orders/${o.id}/cancel`, { method: "POST", body: { current: o.status } });
        POS.toast(`${NO_LABEL} ${o.call_no} を取り消しました。金券${o.tickets}枚を返してください`, "warn", 4000);
      } catch (ex) {
        POS.sound.error();
        POS.toast(ex.message, "error", 3500);
      }
      await loadHistory().catch(() => {});
    }
  });

  // ============================================================ 物販の準備・受渡（PC4のみ）
  // 準備中 → 受け渡し可能（PC5に表示）→ 受渡完了。押し間違いは1段階戻せる
  let prepOrders = []; // 準備中・受渡可能
  let prepDelivered = []; // 最近受け渡した、準備の工程を通った注文
  let prepOpen = false;

  const PREP_ACTIONS = {
    ready: { current: "waiting", to: "ready" },
    deliver: { current: "ready", to: "delivered" },
    back: { current: "ready", to: "waiting" },
    undeliver: { current: "delivered", to: "ready" },
  };

  async function loadPrep() {
    if (!GOODS) return;
    prepOrders = await api("/api/orders?status=open");
    if (prepOpen) prepDelivered = await api("/api/orders?status=delivered&limit=10");
    renderPrep();
  }

  function prepItem(o, buttons) {
    return `<li class="history-item">
      <span class="no num">${o.call_no}</span>
      <span class="info">
        <span class="muted num">会計 ${POS.hhmm(o.paid_at)}${o.is_test ? ' <span class="badge badge-test">テスト</span>' : ""}</span>
        <span>${o.items.map((i) => `${POS.esc(i.name)}×${i.qty}`).join("、")}</span>
      </span>
      <span class="acts">${buttons}</span>
    </li>`;
  }

  function renderPrep() {
    if (!GOODS) return;
    const waiting = prepOrders.filter((o) => o.status === "waiting");
    const ready = prepOrders.filter((o) => o.status === "ready");
    $("prep-count").textContent = waiting.length + ready.length;
    const empty = (t) => `<li class="muted" style="padding:12px">${t}</li>`;
    $("prep-waiting").innerHTML = waiting.map((o) => prepItem(o,
      `<button type="button" class="btn-primary" data-prep="ready" data-id="${o.id}">受け渡し可能にする</button>`)).join("")
      || empty("準備中の注文はありません");
    $("prep-ready").innerHTML = ready.map((o) => prepItem(o,
      `<button type="button" class="btn-small" data-prep="back" data-id="${o.id}">準備中に戻す</button>
       <button type="button" class="btn-ok" data-prep="deliver" data-id="${o.id}">受渡完了</button>`)).join("")
      || empty("受け渡し可能な注文はありません");
    $("prep-delivered").innerHTML = prepDelivered.map((o) => prepItem(o,
      `<button type="button" class="btn-small" data-prep="undeliver" data-id="${o.id}">受け渡し可能に戻す</button>`)).join("")
      || empty("まだありません");
  }

  if (GOODS) {
    $("btn-prep").addEventListener("click", async () => {
      prepOpen = true;
      show("ov-prep", true);
      try { await loadPrep(); } catch (e) { POS.toast(e.message, "error"); }
    });
    $("btn-prep-close").addEventListener("click", () => { prepOpen = false; show("ov-prep", false); });
    $("ov-prep").addEventListener("click", async (e) => {
      const b = e.target.closest("[data-prep]");
      if (!b || b.disabled) return;
      const act = PREP_ACTIONS[b.dataset.prep];
      b.disabled = true;
      try {
        const o = await api(`/api/orders/${b.dataset.id}/status`, { method: "POST", body: act });
        if (act.to === "ready") POS.toast(`受付番号 ${o.call_no} を受け渡し可能にしました（PC5に表示）`, "ok", 2000);
        if (act.to === "delivered") POS.toast(`受付番号 ${o.call_no} 受渡完了`, "ok", 1500);
      } catch (ex) {
        POS.sound.error();
        POS.toast(ex.message, "error", 3500);
      }
      await loadPrep().catch(() => {});
    });
  }

  // ============================================================ カメラ（R-02, R-03, R-05, R-06）
  const camPref = Object.assign({ on: true, facing: "user", mirror: true }, POS.store.get(KEY_CAM, {}));
  const video = $("video");
  const canvas = document.createElement("canvas");
  const cctx = canvas.getContext("2d", { willReadFrequently: true });
  let stream = null;
  let scanning = false;
  let last = { code: null, seenAt: 0, addedAt: 0 };

  function saveCamPref() { POS.store.set(KEY_CAM, camPref); }

  function updateCamButtons() {
    $("btn-camera").textContent = camPref.on ? "カメラ OFF" : "カメラ ON";
    $("btn-facing").textContent = camPref.facing === "user" ? "背面カメラへ" : "前面カメラへ";
    $("btn-mirror").classList.toggle("active", camPref.mirror);
    $("cam-box").classList.toggle("mirror", camPref.mirror);
  }

  function setCamVisible(on) { document.body.classList.toggle("no-cam", !on); }

  async function startCamera() {
    stopCamera();
    // 物販（PC4）は事前注文がないので、カメラは使わない（商品ボタンだけで入力）
    if (GOODS || !camPref.on) { setCamVisible(false); return; }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || typeof jsQR !== "function") {
      setCamVisible(false);
      POS.toast("この画面ではカメラが使えません（HTTPSで開いているか確認）。ボタンで入力してください", "warn", 4000);
      return;
    }
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: { facingMode: camPref.facing, width: { ideal: 1280 }, height: { ideal: 720 } },
      });
      video.srcObject = stream;
      await video.play();
      setCamVisible(true);
      $("cam-msg").textContent = "事前注文のQRをかざしてください（20〜30cm）";
      scanning = true;
      scanLoop();
    } catch (e) {
      setCamVisible(false);
      POS.toast("カメラが使えません。商品ボタンで入力してください", "warn", 4000);
    }
  }

  function stopCamera() {
    scanning = false;
    if (stream) stream.getTracks().forEach((t) => t.stop());
    stream = null;
    video.srcObject = null;
  }

  let lastScan = 0;
  function scanLoop() {
    if (!scanning) return;
    requestAnimationFrame(scanLoop);
    const now = performance.now();
    if (now - lastScan < 120 || video.readyState < 2 || !video.videoWidth) return;
    lastScan = now;
    const scale = Math.min(1, 640 / video.videoWidth);
    const w = Math.round(video.videoWidth * scale);
    const h = Math.round(video.videoHeight * scale);
    canvas.width = w;
    canvas.height = h;
    cctx.drawImage(video, 0, 0, w, h);
    const img = cctx.getImageData(0, 0, w, h);
    const hit = jsQR(img.data, w, h, { inversionAttempts: "attemptBoth" });
    if (hit && hit.data) onCode(hit.data.trim());
  }

  function flash(kind) {
    const box = $("cam-box");
    box.classList.remove("flash-ok", "flash-ng");
    void box.offsetWidth;
    box.classList.add(kind === "ok" ? "flash-ok" : "flash-ng");
    setTimeout(() => box.classList.remove("flash-ok", "flash-ng"), 600);
  }

  function onCode(raw) {
    const code = raw.toUpperCase();
    const now = Date.now();
    // 同じコードは、映像から消える（0.7秒見えない）まで、かつ前回の追加から1.5秒経つまで追加しない
    if (code === last.code && (now - last.seenAt < 700 || now - last.addedAt < 1500)) {
      last.seenAt = now;
      return;
    }
    last = { code, seenAt: now, addedAt: now };
    // 会計・番号表示中はカートを変えない
    if (!$("ov-checkout").classList.contains("hidden") || !$("ov-callno").classList.contains("hidden")) return;
    // カメラで読むのは事前注文のQRだけ。商品は商品ボタンで入れる
    if (PreorderCode.isPreorder(code)) { loadPreorder(code); return; }
    POS.sound.error();
    flash("ng");
    POS.toast("事前注文のQRではありません。商品は商品ボタンで入力してください", "error", 3000);
    $("cam-msg").textContent = "事前注文のQRではありません";
  }

  $("btn-camera").addEventListener("click", () => {
    camPref.on = !camPref.on;
    saveCamPref();
    updateCamButtons();
    startCamera();
  });
  $("btn-facing").addEventListener("click", () => {
    camPref.facing = camPref.facing === "user" ? "environment" : "user";
    camPref.mirror = camPref.facing === "user";
    saveCamPref();
    updateCamButtons();
    startCamera();
  });
  $("btn-mirror").addEventListener("click", () => {
    camPref.mirror = !camPref.mirror;
    saveCamPref();
    updateCamButtons();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && camPref.on && !GOODS && !stream) startCamera();
  });

  // ============================================================ 通知
  function applySettings(s) {
    $("shop-name").textContent = s.shop_name || "";
    $("test-badge").classList.toggle("hidden", !s.test_mode);
  }

  function onMessage(msg) {
    if (msg.type === "product_updated") {
      const p = msg.product;
      // 削除・区分の変更で、この端末の商品でなくなったものは外す
      if (p.deleted || p.category !== CATEGORY) { products.delete(p.id); byCode.delete(p.code.toUpperCase()); }
      else { products.set(p.id, p); byCode.set(p.code.toUpperCase(), p); }
      renderProducts();
      renderCart();
    } else if (msg.type === "settings_updated") {
      applySettings(msg.settings);
    } else if (msg.type === "order_created" || msg.type === "order_updated") {
      if (historyOpen) loadHistory().catch(() => {});
      if (GOODS && msg.order.category !== "food") loadPrep().catch(() => {});
    }
  }

  async function sync() {
    try {
      await loadProducts();
      applySettings(await api("/api/settings"));
      if (historyOpen) await loadHistory();
      await loadPrep();
      if (pending) submitPending();
    } catch (e) {
      if (e.status !== 401) POS.toast(e.message, "error");
    }
  }

  // ============================================================ 起動
  updateCamButtons();
  setNextQty(1);
  renderCart();
  startCamera();
  POS.keepAwake();
  POS.connect({ onMessage, onSync: sync });
  if (pending) submitPending(); // 開き直したら送信途中の会計を再送信
})();
