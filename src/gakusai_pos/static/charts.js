// 売上グラフ（管理画面）：累計売上の折れ線グラフと、30分ごとの売上の棒グラフ。
// 外部ライブラリを使わずSVGで描く（会場でインターネットにつながらなくても動くように）。
// data は /admin/api/sales-series の結果。
"use strict";

const SalesCharts = (() => {
  const NS = "http://www.w3.org/2000/svg";
  const H = 260;
  const M = { top: 16, right: 20, bottom: 44, left: 76 };
  const TICK_SLOTS = [1, 2, 4, 6, 12, 24, 48]; // 目盛りの間隔（30分の何本ごと）

  const yen = (n) => `${Number(n).toLocaleString("ja-JP")}円`;
  const ms = (s) => new Date(s).getTime(); // "YYYY-MM-DDTHH:MM" は端末の現地時刻として読む
  const two = (n) => String(n).padStart(2, "0");
  const hm = (t) => { const d = new Date(t); return `${two(d.getHours())}:${two(d.getMinutes())}`; };
  const md = (t) => { const d = new Date(t); return `${d.getMonth() + 1}/${d.getDate()}`; };
  const dayKey = (t) => new Date(t).toDateString();

  function el(name, attrs = {}, parent = null) {
    const e = document.createElementNS(NS, name);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (parent) parent.appendChild(e);
    return e;
  }

  // 縦軸の目盛り：1・2・2.5・5 × 10^k のきりのよい間隔で、5本以内
  function yScale(v) {
    if (v <= 0) v = 1000;
    const raw = v / 5;
    const p = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * p).find((m) => m >= raw);
    const max = Math.ceil(v / step) * step;
    const ticks = [];
    for (let t = 0; t <= max + step / 2; t += step) ticks.push(t);
    return { max, ticks };
  }

  // 横軸は30分ごとに同じ幅の枠を並べる。売上のない夜中などを飛ばした所（時刻が続かない所）には区切り線を引く
  function frame(box, data) {
    const W = Math.max(320, box.clientWidth);
    const n = data.buckets.length;
    const slot = (W - M.left - M.right) / n;
    const x = (i) => M.left + i * slot; // 枠 i の左端（i = n で右端）
    const gaps = [];
    for (let i = 1; i < n; i++) if (data.buckets[i].start !== data.buckets[i - 1].end) gaps.push(i);
    const svg = el("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, class: "viz-svg" });
    return { W, n, slot, x, gaps, svg };
  }

  // 縦軸（金額）：目盛り線は控えめに、ラベルは文字色
  function drawY(svg, W, ticks, y) {
    const g = el("g", { class: "viz-axis" }, svg);
    for (const v of ticks) {
      el("line", { x1: M.left, x2: W - M.right, y1: y(v), y2: y(v), class: v === 0 ? "viz-base" : "viz-grid" }, g);
      const t = el("text", { x: M.left - 8, y: y(v) + 4, "text-anchor": "end" }, g);
      t.textContent = yen(Math.round(v));
    }
  }

  // 横軸（時刻）：幅に合わせて30分〜24時間ごとに目盛り。区切りの後と最初の目盛りには日付も出す
  function drawX(svg, data, f) {
    const { W, n, slot, x, gaps } = f;
    const g = el("g", { class: "viz-axis" }, svg);
    const maxTicks = Math.max(2, Math.floor((W - M.left - M.right) / 60));
    const k = TICK_SLOTS.find((s) => n / s <= maxTicks) || TICK_SLOTS[TICK_SLOTS.length - 1];
    const segStarts = new Set([0, ...gaps]);
    // 区切り線（時刻が飛んでいる所）
    for (const i of gaps) {
      el("line", { x1: x(i), x2: x(i), y1: M.top, y2: H - M.bottom, class: "viz-gap" }, g);
    }
    let prevDay = null;
    let lastPx = -Infinity;
    const label = (i, t) => {
      const px = x(i);
      if (px - lastPx < 48 || px > W - M.right + 1) return;
      lastPx = px;
      el("line", { x1: px, x2: px, y1: H - M.bottom, y2: H - M.bottom + 5, class: "viz-base" }, g);
      const tl = el("text", { x: px, y: H - M.bottom + 18, "text-anchor": "middle" }, g);
      tl.textContent = hm(t);
      if (prevDay !== dayKey(t)) {
        const dl = el("text", { x: px, y: H - M.bottom + 34, "text-anchor": "middle", class: "viz-date" }, g);
        dl.textContent = md(t);
        prevDay = dayKey(t);
      }
    };
    for (let i = 0; i <= n; i++) {
      const t = i < n ? ms(data.buckets[i].start) : ms(data.buckets[n - 1].end);
      const d = new Date(t);
      const slotNo = d.getHours() * 2 + Math.floor(d.getMinutes() / 30);
      if (segStarts.has(i) || (d.getMinutes() % 30 === 0 && slotNo % k === 0)) label(i, t);
    }
  }

  // ツールチップ：値を先に強く、ラベルを後に。文字は textContent で入れる
  function tooltip(box) {
    let tip = box.querySelector(".viz-tip");
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "viz-tip hidden";
      tip.setAttribute("role", "status");
      box.appendChild(tip);
    }
    return {
      show(px, py, title, rows) {
        tip.replaceChildren();
        const h = document.createElement("div");
        h.className = "viz-tip-title";
        h.textContent = title;
        tip.appendChild(h);
        for (const [value, label] of rows) {
          const r = document.createElement("div");
          r.className = "viz-tip-row";
          const v = document.createElement("strong");
          v.textContent = value;
          const l = document.createElement("span");
          l.textContent = label;
          r.append(v, l);
          tip.appendChild(r);
        }
        tip.classList.remove("hidden");
        const bw = box.clientWidth;
        const tw = tip.offsetWidth;
        let left = px + 12;
        if (left + tw > bw - 4) left = px - tw - 12;
        tip.style.left = `${Math.max(4, left)}px`;
        tip.style.top = `${Math.max(4, py - tip.offsetHeight - 10)}px`;
      },
      hide() { tip.classList.add("hidden"); },
    };
  }

  // ポインタと左右キーで「今見ている位置」を動かす共通の仕組み
  function interact(svg, box, count, nearest, onActive) {
    let active = -1;
    const set = (i) => {
      active = i;
      onActive(i);
    };
    svg.setAttribute("tabindex", "0");
    svg.addEventListener("pointermove", (e) => {
      const r = svg.getBoundingClientRect();
      set(nearest(e.clientX - r.left, e.clientY - r.top));
    });
    svg.addEventListener("pointerleave", () => set(-1));
    svg.addEventListener("blur", () => set(-1));
    svg.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      const d = e.key === "ArrowRight" ? 1 : -1;
      set(active < 0 ? (d > 0 ? 0 : count - 1) : Math.min(count - 1, Math.max(0, active + d)));
    });
  }

  function empty(box, msg) {
    box.replaceChildren();
    const p = document.createElement("p");
    p.className = "viz-empty";
    p.textContent = msg;
    box.appendChild(p);
  }

  // ------------------------------------------------------------ 累計売上（折れ線）
  function renderCumulative(box, data) {
    if (!data.buckets.length || !data.amount) { empty(box, "この期間の売上はありません"); return; }
    const f = frame(box, data);
    const { W, x, svg } = f;
    const { max, ticks } = yScale(data.amount);
    const y = (v) => H - M.bottom - (v / max) * (H - M.top - M.bottom);
    drawY(svg, W, ticks, y);
    drawX(svg, data, f);

    // 期間の始めを0円とし、各30分の終わりの時点の累計をつなぐ（点 i は枠 i-1 の右端）
    const pts = [{ i: 0, t: ms(data.buckets[0].start), v: 0, orders: 0 }];
    let orders = 0;
    data.buckets.forEach((b, j) => {
      orders += b.orders;
      pts.push({ i: j + 1, t: ms(b.end), v: b.cumulative, orders });
    });
    el("path", {
      d: pts.map((p, k) => `${k ? "L" : "M"}${x(p.i).toFixed(1)},${y(p.v).toFixed(1)}`).join(""),
      class: "viz-line",
    }, svg);

    // 最後の値だけ直接ラベルを付ける
    const lastP = pts[pts.length - 1];
    el("circle", { cx: x(lastP.i), cy: y(lastP.v), r: 4, class: "viz-dot" }, svg);
    const endLabel = el("text", {
      x: x(lastP.i) - 8, y: y(lastP.v) - 10, "text-anchor": "end", class: "viz-label",
    }, svg);
    endLabel.textContent = yen(lastP.v);

    const cross = el("line", { y1: M.top, y2: H - M.bottom, class: "viz-cross hidden" }, svg);
    const dot = el("circle", { r: 5, class: "viz-dot viz-dot-active hidden" }, svg);
    svg.setAttribute("aria-label", `累計売上の折れ線グラフ。期間の合計 ${yen(data.amount)}`);
    box.replaceChildren(svg);
    const tip = tooltip(box);
    interact(svg, box, pts.length, (px) => {
      let best = 0;
      for (let i = 1; i < pts.length; i++) if (Math.abs(x(pts[i].i) - px) < Math.abs(x(pts[best].i) - px)) best = i;
      return best;
    }, (i) => {
      const on = i >= 0;
      cross.classList.toggle("hidden", !on);
      dot.classList.toggle("hidden", !on);
      if (!on) { tip.hide(); return; }
      const p = pts[i];
      cross.setAttribute("x1", x(p.i));
      cross.setAttribute("x2", x(p.i));
      dot.setAttribute("cx", x(p.i));
      dot.setAttribute("cy", y(p.v));
      tip.show(x(p.i), y(p.v), `${md(p.t)} ${hm(p.t)} 時点`, [
        [yen(p.v), `累計（金券${Math.floor(p.v / 50)}枚）`],
        [`${p.orders}件`, "累計の注文数"],
      ]);
    });
  }

  // ------------------------------------------------------------ 30分ごとの売上（棒）
  function renderBars(box, data) {
    if (!data.buckets.length || !data.amount) { empty(box, "この期間の売上はありません"); return; }
    const f = frame(box, data);
    const { W, x, svg } = f;
    const peak = Math.max(...data.buckets.map((b) => b.amount));
    const { max, ticks } = yScale(peak);
    const y = (v) => H - M.bottom - (v / max) * (H - M.top - M.bottom);
    drawY(svg, W, ticks, y);
    drawX(svg, data, f);

    const base = y(0);
    const bars = [];
    let peakDone = false;
    const g = el("g", {}, svg);
    data.buckets.forEach((b, i) => {
      // 隣の棒との間に2pxのすき間、上の角だけ4pxの丸み
      const x0 = x(i) + 1;
      const x1 = x(i + 1) - 1;
      const w = Math.max(1, x1 - x0);
      let bar = null;
      if (b.amount > 0) {
        const top = y(b.amount);
        const r = Math.min(4, w / 2, base - top);
        bar = el("path", {
          d: `M${x0},${base}V${top + r}Q${x0},${top} ${x0 + r},${top}H${x1 - r}Q${x1},${top} ${x1},${top + r}V${base}Z`,
          class: "viz-bar",
        }, g);
      }
      bars.push({ b, x0, x1, bar });
      if (b.amount === peak && !peakDone) {
        peakDone = true; // 一番多い棒にだけ金額を付ける
        const lbl = el("text", { x: (x0 + x1) / 2, y: y(b.amount) - 6, "text-anchor": "middle", class: "viz-label" }, svg);
        lbl.textContent = yen(b.amount);
      }
    });

    const hover = el("rect", { y: M.top, height: H - M.top - M.bottom, class: "viz-hover hidden" }, svg);
    svg.insertBefore(hover, g);
    svg.setAttribute("aria-label", `30分ごとの売上の棒グラフ。最も多い30分は ${yen(peak)}`);
    box.replaceChildren(svg);
    const tip = tooltip(box);
    // 当たり判定は棒そのものより広く、その30分の列全体
    interact(svg, box, bars.length, (px) => {
      const i = bars.findIndex((c) => px >= c.x0 - 1 && px <= c.x1 + 1);
      return i;
    }, (i) => {
      bars.forEach((c, j) => c.bar && c.bar.classList.toggle("active", j === i));
      if (i < 0) { hover.classList.add("hidden"); tip.hide(); return; }
      const c = bars[i];
      hover.setAttribute("x", c.x0 - 1);
      hover.setAttribute("width", c.x1 - c.x0 + 2);
      hover.classList.remove("hidden");
      tip.show((c.x0 + c.x1) / 2, y(c.b.amount), `${md(ms(c.b.start))} ${hm(ms(c.b.start))}〜${hm(ms(c.b.end))}`, [
        [yen(c.b.amount), `売上（金券${c.b.tickets}枚）`],
        [`${c.b.orders}件`, "注文数"],
      ]);
    });
  }

  return { renderCumulative, renderBars, hm, md, ms, yen };
})();
