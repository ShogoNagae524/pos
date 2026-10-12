// 全画面で使う共通処理：API呼び出し、WebSocket（再接続・生存確認）、効果音、表示の補助
"use strict";

const POS = (() => {
  const TICKET = 50;
  let clockOffset = 0; // サーバー時刻 - 端末時刻（ミリ秒）

  // ------------------------------------------------------------ API
  class ApiError extends Error {
    constructor(status, data) {
      super((data && data.detail) || `エラー（${status}）`);
      this.status = status;
      this.data = data || {};
    }
  }

  async function api(path, { method = "GET", body, role } = {}) {
    let res;
    try {
      res = await fetch(path, {
        method,
        credentials: "same-origin",
        headers: body !== undefined ? { "Content-Type": "application/json" } : {},
        body: body !== undefined ? JSON.stringify(body) : undefined,
        cache: "no-store",
      });
    } catch (e) {
      const err = new ApiError(0, { detail: "サーバーにつながりません" });
      err.network = true;
      throw err;
    }
    let data = null;
    const text = await res.text();
    try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
    if (res.status === 401 && role) {
      location.href = `/login?role=${role}`;
      throw new ApiError(401, data);
    }
    if (!res.ok) throw new ApiError(res.status, data);
    return data;
  }

  // ------------------------------------------------------------ WebSocket
  // onMessage(msg) は通知ごと、onSync() は接続（再接続）のたびに呼ばれ、一覧を取り直す
  function connect({ onMessage, onSync }) {
    let ws = null;
    let retry = 0;
    let lastMsg = Date.now();
    let timer = null;

    const setConnected = (ok) => document.body.classList.toggle("disconnected", !ok);

    function open() {
      clearTimeout(timer);
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws`);
      ws.onopen = () => {
        retry = 0;
        lastMsg = Date.now();
        setConnected(true);
        if (onSync) onSync();
      };
      ws.onmessage = (ev) => {
        lastMsg = Date.now();
        let msg;
        try { msg = JSON.parse(ev.data); } catch { return; }
        if (msg.type === "ping") {
          clockOffset = msg.now - Date.now();
          try { ws.send(JSON.stringify({ type: "pong" })); } catch {}
          return;
        }
        if (msg.type === "hello" || msg.type === "pong") return;
        if (onMessage) onMessage(msg);
      };
      ws.onclose = (ev) => {
        setConnected(false);
        if (ev.code === 4401) {
          // 端末の登録が無効（パスコード変更など）
          const role = document.body.dataset.role;
          if (role && role !== "admin") location.href = `/login?role=${role}`;
        }
        const wait = Math.min(1000 * 2 ** retry, 10000);
        retry++;
        timer = setTimeout(open, wait);
      };
      ws.onerror = () => {};
    }

    // 生存確認：25秒何も届かなければ切れたとみなしてつなぎ直す
    setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN && Date.now() - lastMsg > 25000) {
        setConnected(false);
        try { ws.close(); } catch {}
      }
    }, 5000);

    // スマホの画面が消えて戻ったら、すぐ確認してつなぎ直す
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState !== "visible") return;
      if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {
        retry = 0;
        open();
      } else if (ws.readyState === WebSocket.OPEN && onSync) {
        onSync();
      }
    });
    window.addEventListener("online", () => { if (!ws || ws.readyState !== WebSocket.OPEN) { retry = 0; open(); } });

    open();
    return { send: (o) => ws && ws.readyState === WebSocket.OPEN && ws.send(JSON.stringify(o)) };
  }

  // ------------------------------------------------------------ 効果音（WebAudio。音声ファイル不要）
  let ctx = null;
  function audio() {
    if (!ctx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      ctx = new AC();
    }
    if (ctx.state === "suspended") ctx.resume();
    return ctx;
  }
  // ブラウザは操作があるまで音を出さないので、最初のタップで準備する
  ["pointerdown", "keydown"].forEach((t) =>
    window.addEventListener(t, () => audio(), { once: true, capture: true }));

  function tone(freq, start, dur, type = "sine", gain = 0.25) {
    const a = audio();
    if (!a) return;
    const o = a.createOscillator();
    const g = a.createGain();
    o.type = type;
    o.frequency.value = freq;
    const t = a.currentTime + start;
    g.gain.setValueAtTime(gain, t);
    g.gain.exponentialRampToValueAtTime(0.001, t + dur);
    o.connect(g).connect(a.destination);
    o.start(t);
    o.stop(t + dur + 0.02);
  }

  const sound = {
    ok() { tone(1320, 0, 0.12, "square", 0.12); },
    error() { tone(220, 0, 0.18, "sawtooth", 0.2); tone(180, 0.2, 0.25, "sawtooth", 0.2); },
    newOrder() { tone(880, 0, 0.15); tone(1175, 0.17, 0.15); tone(1568, 0.34, 0.3); },
    alert() { for (let i = 0; i < 3; i++) { tone(960, i * 0.3, 0.2, "square", 0.2); } },
    done() { tone(1046, 0, 0.12); tone(1568, 0.13, 0.25); },
  };

  // ------------------------------------------------------------ 表示の補助
  function toast(msg, kind = "", ms = 2500) {
    let box = document.getElementById("toast");
    if (!box) {
      box = document.createElement("div");
      box.id = "toast";
      document.body.appendChild(box);
    }
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.textContent = msg;
    box.appendChild(el);
    setTimeout(() => el.remove(), ms);
  }

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  }

  const yen = (n) => `${Number(n).toLocaleString("ja-JP")}円`;
  const tickets = (n) => Math.floor(n / TICKET);
  const hhmm = (iso) => (iso ? iso.slice(11, 16) : "");
  const serverNow = () => Date.now() + clockOffset;

  function elapsed(ms) {
    const s = Math.max(0, Math.floor(ms / 1000));
    const m = Math.floor(s / 60);
    return `${m}分${String(s % 60).padStart(2, "0")}秒`;
  }

  function uuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
      const r = (Math.random() * 16) | 0;
      return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
    });
  }

  const store = {
    get(key, def) {
      try { const v = localStorage.getItem(key); return v === null ? def : JSON.parse(v); }
      catch { return def; }
    },
    set(key, v) { try { localStorage.setItem(key, JSON.stringify(v)); } catch {} },
    del(key) { try { localStorage.removeItem(key); } catch {} },
  };

  // 画面の自動消灯を防ぐ（対応ブラウザのみ）
  async function keepAwake() {
    try {
      if ("wakeLock" in navigator) {
        await navigator.wakeLock.request("screen");
        document.addEventListener("visibilitychange", async () => {
          if (document.visibilityState === "visible") {
            try { await navigator.wakeLock.request("screen"); } catch {}
          }
        });
      }
    } catch {}
  }

  const STATUS_LABEL = { waiting: "未着手", cooking: "調理中", delivered: "受渡済", cancelled: "取消" };

  return { api, ApiError, connect, sound, toast, esc, yen, tickets, hhmm, elapsed, serverNow,
    uuid, store, keepAwake, STATUS_LABEL, TICKET };
})();
