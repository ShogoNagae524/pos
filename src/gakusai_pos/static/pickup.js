// 物販の受渡表示（PC5）：準備中・受け渡し可能の受付番号を表示するだけ（操作はPC4で行う）
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const ROLE = "pickup";
  const api = (path, opts = {}) => POS.api(path, { ...opts, role: ROLE });

  let orders = new Map(); // 準備中・受渡可能の物販の注文
  const fresh = new Map(); // 受渡可能になったばかりの強調表示 id -> 期限

  const isShown = (o) => o.category === "goods" && (o.status === "waiting" || o.status === "ready");

  function render() {
    for (const st of ["waiting", "ready"]) {
      const list = [...orders.values()].filter((o) => o.status === st).sort((a, b) => a.id - b.id);
      $(`list-${st}`).innerHTML = list.length
        ? list.map((o) => {
          const isNew = fresh.has(o.id) && fresh.get(o.id) > Date.now();
          return `<span class="pk-no num${isNew ? " new" : ""}">${o.call_no}</span>`;
        }).join("")
        : `<div class="pk-empty">${st === "waiting" ? "準備中の番号はありません" : "受け渡し可能な番号はありません"}</div>`;
    }
  }

  function upsert(o) {
    const before = orders.get(o.id);
    if (!o.deleted && isShown(o)) {
      orders.set(o.id, o);
      if (o.status === "ready" && (!before || before.status !== "ready")) {
        fresh.set(o.id, Date.now() + 5000);
        setTimeout(render, 5100);
        POS.sound.newOrder(); // できあがりを知らせる
      }
    } else {
      orders.delete(o.id);
    }
  }

  function onMessage(msg) {
    if (msg.type === "order_created" || msg.type === "order_updated") {
      upsert(msg.order);
      render();
    } else if (msg.type === "settings_updated") {
      $("shop-name").textContent = msg.settings.shop_name || "";
      $("test-badge").classList.toggle("hidden", !msg.settings.test_mode);
    }
  }

  async function sync() {
    try {
      const list = await api("/api/orders?status=open");
      orders = new Map(list.filter(isShown).map((o) => [o.id, o]));
      render();
    } catch (e) {
      if (e.status !== 401) POS.toast(e.message, "error");
    }
  }

  render();
  POS.keepAwake();
  POS.connect({ onMessage, onSync: sync });
})();
