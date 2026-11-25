// 事前注文QRの形式（受付の読み取りと、書き出した事前注文ページの両方で使う）
//
//   GPOS1/<注文ID>/T<合計>/<商品コード>*<個数>+<商品コード>*<個数>...
//   例: GPOS1/K7Q2XM/T450/M001*1+M002*1
//
// QRに入れるのは商品コードと個数だけ。価格・商品名は入れず、受付では必ずサーバーの
// 最新の商品情報で計算し直す。T<合計> はお客様のページに出ていた合計で、
// 価格が変わっていないかの確認にだけ使う（会計の金額には使わない）。
// 中身はお客様が自由に書き換えられるので、形式に合わないものは丸ごと受け付けない。
"use strict";

(function (root) {
  const PREFIX = "GPOS1/";
  const MAX_LEN = 600; // これより長いQRは読まない
  const MAX_LINES = 20; // 1枚のQRに入れられる商品の種類
  const MAX_QTY = 20; // 1商品あたりの個数
  const ID_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"; // 読み間違えやすい 0/O・1/I を除く

  const LINE_RE = /^([A-Z0-9_-]{1,32})\*([1-9][0-9]?)$/;
  const HEAD_RE = /^GPOS1\/([A-Z0-9]{4,12})\/T([0-9]{1,6})\/(.+)$/;

  function isPreorder(raw) {
    return typeof raw === "string" && raw.trim().toUpperCase().startsWith(PREFIX);
  }

  // 読み取った文字列を {id, total, lines:[{code, qty}]} にする。形式が違えば null
  function parse(raw) {
    if (typeof raw !== "string") return null;
    const s = raw.trim().toUpperCase();
    if (s.length > MAX_LEN) return null;
    const m = HEAD_RE.exec(s);
    if (!m) return null;
    const parts = m[3].split("+");
    if (parts.length > MAX_LINES) return null;
    const qtyByCode = new Map();
    for (const part of parts) {
      const lm = LINE_RE.exec(part);
      if (!lm) return null;
      const qty = Number(lm[2]);
      if (qty > MAX_QTY) return null;
      // 同じ商品が2回書かれていたらまとめる
      qtyByCode.set(lm[1], Math.min((qtyByCode.get(lm[1]) || 0) + qty, MAX_QTY));
    }
    return {
      id: m[1],
      total: Number(m[2]),
      lines: [...qtyByCode].map(([code, qty]) => ({ code, qty })),
    };
  }

  function newId(rand = Math.random) {
    let id = "";
    for (let i = 0; i < 6; i++) id += ID_CHARS[Math.floor(rand() * ID_CHARS.length)];
    return id;
  }

  // lines: [{code, qty}]（qty 1〜20）, total: ページに出す合計
  function build(id, total, lines) {
    const body = lines.map((l) => `${String(l.code).toUpperCase()}*${l.qty}`).join("+");
    return `${PREFIX}${id}/T${total}/${body}`;
  }

  const api = { PREFIX, MAX_LINES, MAX_QTY, isPreorder, parse, build, newId };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.PreorderCode = api;
})(typeof window !== "undefined" ? window : globalThis);
