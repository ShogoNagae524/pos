// 受付端末（PC1）：商品入力（ボタン・QR）、会計、履歴・取消
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const ROLE = "reception";
  const api = (path, opts = {}) => POS.api(path, { ...opts, role: ROLE });

  const KEY_CART = "pos.reception.cart";
  const KEY_PENDING = "pos.reception.pending";
  const KEY_CAM = "pos.reception.camera";

  let products = new Map(); // id -> product
  let byCode = new Map(); // code -> product
  // カートは [{product_id, qty}]。ブラウザを閉じても残す（第10章）
  let cart = POS.store.get(KEY_CART, []);
  let nextQty = 1; // R-04 数字を押してから商品を追加
  let pending = POS.store.get(KEY_PENDING, null); // 送信中の会計 {client_uuid, items, total}
  let historyOpen = false;

  // ============================================================ 商品
  async function loadProducts() {
    const list = await api("/api/products");
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
      <button type="button" class="product-btn${p.sold_out ? " sold-out" : ""}" data-id="${p.id}">
        <span>${POS.esc(p.name)}</span>
        <span class="price num">${POS.yen(p.price)}</span>
        ${p.stock !== null && !p.sold_out ? `<span class="stock">残り${p.stock}</span>` : ""}
      </button>`).join("");
  }

  $("product-grid").addEventListener("click", (e) => {
    const btn = e.target.closest(".product-btn");
    if (btn) addProduct(products.get(Number(btn.dataset.id)), "button");
  });

  function addProduct(p, via) {
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
    if (via === "qr") POS.toast(`${p.name} ×${qty}`, "ok", 1200);
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
  function saveCart() { POS.store.set(KEY_CART, cart); }

  function cartTotals() {
    let total = 0, count = 0;
    for (const l of cart) {
      const p = products.get(l.product_id);
      if (p) total += p.price * l.qty;
      count += l.qty;
    }
    return { total, count };
  }

  function cartProblems() {
    return cart.filter((l) => { const p = products.get(l.product_id); return !p || p.sold_out; });
  }

  function renderCart() {
    const ul = $("cart-list");
    if (!cart.length) {
      ul.innerHTML = `<li class="cart-empty">商品ボタンを押すか、QRカードをかざしてください</li>`;
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
    const { total, count } = cartTotals();
    $("total-amount").textContent = POS.yen(total);
    $("total-detail").textContent = `${count}点・金券${POS.tickets(total)}枚`;
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
    const rows = cart.map((l) => {
      const p = products.get(l.product_id);
      return `<tr><td>${POS.esc(p.name)}</td><td class="num">×${l.qty}</td>
              <td class="num">${POS.yen(p.price * l.qty)}</td></tr>`;
    }).join("");
    $("checkout-lines").innerHTML = rows;
    const { total } = cartTotals();
    $("checkout-total").innerHTML =
      `合計 ${POS.yen(total)}<span class="tickets">＝ 金券 ${POS.tickets(total)}枚</span>`;
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
        setSaveStatus("保存に失敗しました。自動で再送信しています…（番号が出るまで番号札を渡さないでください）", "error");
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
    $("callno-label").textContent = fresh ? "呼出番号" : "呼出番号（再表示）";
    $("callno-value").textContent = order.call_no;
    const items = order.items.map((i) => `${i.name}×${i.qty}`).join("、");
    $("callno-detail").textContent =
      `${items}　${POS.yen(order.total)}（金券${order.tickets}枚）${order.is_test ? "【テスト】" : ""}`;
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
      const open = o.status === "waiting" || o.status === "cooking";
      return `<li class="history-item">
        <span class="no num">${o.call_no}</span>
        <span class="info">
          <span><span class="badge badge-${o.status}">${POS.STATUS_LABEL[o.status]}</span>
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
      if (!confirm(`呼出番号 ${o.call_no}（${items}／${POS.yen(o.total)}）を取り消しますか？\n金券${o.tickets}枚を手でお返しください。`)) return;
      b.disabled = true;
      try {
        await api(`/api/orders/${o.id}/cancel`, { method: "POST", body: { current: o.status } });
        POS.toast(`呼出番号 ${o.call_no} を取り消しました。金券${o.tickets}枚を返してください`, "warn", 4000);
      } catch (ex) {
        POS.sound.error();
        POS.toast(ex.message, "error", 3500);
      }
      await loadHistory().catch(() => {});
    }
  });

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
    if (!camPref.on) { setCamVisible(false); return; }
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
      $("cam-msg").textContent = "QRカードをかざしてください（20〜30cm）";
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
    const p = byCode.get(code);
    if (!p) {
      POS.sound.error();
      flash("ng");
      POS.toast("未登録のコード", "error");
      $("cam-msg").textContent = `未登録のコード: ${raw.slice(0, 30)}`;
      return;
    }
    if (addProduct(p, "qr")) {
      POS.sound.ok();
      flash("ok");
      $("cam-msg").textContent = `${p.name} を追加しました`;
    } else {
      flash("ng");
      $("cam-msg").textContent = `${p.name} は売切です`;
    }
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
    if (document.visibilityState === "visible" && camPref.on && !stream) startCamera();
  });

  // ============================================================ 通知
  function applySettings(s) {
    $("shop-name").textContent = s.shop_name || "";
    $("test-badge").classList.toggle("hidden", !s.test_mode);
  }

  function onMessage(msg) {
    if (msg.type === "product_updated") {
      const p = msg.product;
      if (p.deleted) { products.delete(p.id); byCode.delete(p.code.toUpperCase()); }
      else { products.set(p.id, p); byCode.set(p.code.toUpperCase(), p); }
      renderProducts();
      renderCart();
    } else if (msg.type === "settings_updated") {
      applySettings(msg.settings);
    } else if (msg.type === "order_created" || msg.type === "order_updated") {
      if (historyOpen) loadHistory().catch(() => {});
    }
  }

  async function sync() {
    try {
      await loadProducts();
      applySettings(await api("/api/settings"));
      if (historyOpen) await loadHistory();
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
