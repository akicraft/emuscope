"use strict";
// 画面の動き。外部のライブラリは使っていない。
// S3 や DynamoDB から来た文字は、必ず textContent で入れる（innerHTML は使わない）。
// こうしておけば、データに HTML やスクリプトが入っていても実行されない。

const TOKEN = document.querySelector('meta[name="emu-token"]').content;
const $ = (id) => document.getElementById(id);

// ---- 小さな道具 --------------------------------------------------------------

function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  node.append(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
  return node;
}
const button = (label, onclick, cls) => el("button", { type: "button", class: "btn" + (cls ? " " + cls : ""), text: label, onclick });
const pre = (text) => el("pre", { class: "mono", text });

async function request(method, path, params, body, raw) {
  const url = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params || {})) {
    if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, v);
  }
  const opt = { method, headers: {} };
  if (method === "POST") {
    opt.headers["X-Emu-Token"] = TOKEN;  // 書き込みには合言葉が要る
    opt.headers["Content-Type"] = raw ? raw.type || "application/octet-stream" : "application/json";
    opt.body = raw ? raw.body : JSON.stringify(body || {});
  }
  let res;
  try {
    res = await fetch(url, opt);
  } catch {
    throw Object.assign(new Error("画面のサーバーに接続できません（./emu ui は動いていますか）"), { kind: "server" });
  }
  const data = await res.json();
  if (!res.ok) throw Object.assign(new Error(data.message), { kind: data.error });
  return data;
}
const api = (path, params) => request("GET", path, params);
const post = (path, body, params) => request("POST", path, params, body);

function fmtSize(n) {
  if (n === undefined || n === null) return "";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(1) + " MB";
}
function fmtTime(value) {
  if (!value) return "";
  const d = new Date(typeof value === "number" ? value * 1000 : value);
  return isNaN(d) ? String(value) : d.toLocaleString("ja-JP", { dateStyle: "short", timeStyle: "medium" });
}
const fmtClock = (seconds) => new Date(seconds * 1000).toLocaleTimeString("ja-JP");
function cellText(v) {
  if (v === undefined || v === null) return v === null ? "null" : "";
  return typeof v === "object" ? JSON.stringify(v) : String(v);
}
function prettyIfJson(text) {
  if (typeof text !== "string" || !/^\s*[[{]/.test(text)) return text;
  try { return JSON.stringify(JSON.parse(text), null, 2); } catch { return text; }
}
const isTyping = () => ["INPUT", "TEXTAREA"].includes(document.activeElement && document.activeElement.tagName);

let toastTimer = null;
function toast(message, isError, keep) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").className = isError ? "error" : "";
  if (!keep) toastTimer = setTimeout(() => { $("toast").textContent = ""; }, isError ? 8000 : 4000);
}
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast("コピーしました"); }
  catch { toast("コピーできませんでした", true); }
}

// 確認や入力のダイアログ。OK なら入力値（入力欄がなければ true）、やめたら null を返す。
function ask({ title, message, input, textarea, value, okLabel, danger, check }) {
  return new Promise((resolve) => {
    const dlg = $("dlg");
    const field = textarea ? $("dlg-text") : $("dlg-input");
    $("dlg-title").textContent = title;
    $("dlg-message").textContent = message || "";
    $("dlg-message").hidden = !message;
    $("dlg-input").hidden = !input;
    $("dlg-text").hidden = !textarea;
    $("dlg-error").hidden = true;
    field.value = value || "";
    if (input) field.placeholder = typeof input === "string" ? input : "";
    $("dlg-ok").textContent = okLabel || "OK";
    $("dlg-ok").className = "btn " + (danger ? "danger" : "primary");
    const finish = (result) => {
      $("dlg-ok").onclick = $("dlg-cancel").onclick = dlg.oncancel = field.onkeydown = null;
      dlg.close();
      resolve(result);
    };
    const ok = () => {
      if (!input && !textarea) return finish(true);
      const problem = check ? check(field.value) : field.value.trim() ? "" : "入力してください";
      if (problem) { $("dlg-error").textContent = problem; $("dlg-error").hidden = false; return; }
      finish(field.value);
    };
    $("dlg-ok").onclick = ok;
    $("dlg-cancel").onclick = () => finish(null);
    dlg.oncancel = (e) => { e.preventDefault(); finish(null); };
    field.onkeydown = (e) => { if (e.key === "Enter" && (input || e.metaKey || e.ctrlKey)) { e.preventDefault(); ok(); } };
    dlg.showModal();
    (input || textarea ? field : $("dlg-ok")).focus();
  });
}

// 時間のかかる操作。終わるまでメッセージを出し、失敗したら理由を出す。
async function run(label, fn, done) {
  toast(label + "…", false, true);
  try {
    const result = await fn();
    toast(typeof done === "function" ? done(result) : done || "完了しました");
    return result;
  } catch (e) {
    toast(e.message, true);
    return undefined;
  }
}

// ---- 「上に一覧、下に詳細」の枠。どの画面もこれを使う -----------------------------

function makePane(root) {
  const p = { openKey: null, listJson: null, onClose: null };
  p.title = el("h1", { class: "mono" });
  p.summary = el("div", { class: "summary" });
  p.toolbar = el("div", { class: "toolbar" });
  p.head = el("div", { class: "pane-head" }, p.title, p.summary, p.toolbar);
  p.notice = el("div", { class: "notice", hidden: true });
  p.list = el("div", { class: "pane-list" });
  p.dTitle = el("div", { class: "detail-title mono" });
  p.dMeta = el("div", { class: "detail-meta" });
  p.dActions = el("div", { class: "detail-actions" });
  p.dNotice = el("div", { class: "notice", hidden: true });
  p.dBody = el("div", { class: "detail-body" });
  p.detail = el("section", { class: "pane-detail", hidden: true },
    el("div", { class: "detail-head" }, p.dTitle, p.dMeta, p.dActions, button("閉じる", () => p.close())),
    p.dNotice, p.dBody);
  root.replaceChildren(p.head, p.notice, p.list, p.detail);

  p.setHead = (title, summary, toolbar, notice) => {
    p.head.hidden = title === null;
    p.title.textContent = title || "";
    p.summary.replaceChildren(...[summary || ""].flat());
    p.toolbar.replaceChildren(...(toolbar || []).filter(Boolean));
    p.setNotice(notice);
  };
  p.setNotice = (notice) => { p.notice.hidden = !notice; p.notice.textContent = notice || ""; };
  p.setList = (...nodes) => p.list.replaceChildren(...nodes.flat().filter(Boolean));
  p.message = (...children) => p.setList(el("div", { class: "message" }, ...children));
  p.markRows = () => {
    for (const tr of p.list.querySelectorAll("tr[data-key]")) {
      if (tr.dataset.key === p.openKey) tr.setAttribute("aria-selected", "true"); else tr.removeAttribute("aria-selected");
    }
  };
  p.open = (key, title, meta, actions, notice, ...body) => {
    p.onClose = null;  // 別のものを開いたら、前の「閉じたとき」の処理は捨てる
    p.openKey = key;
    p.dTitle.textContent = title;
    p.dMeta.textContent = meta || "";
    p.dActions.replaceChildren(...(actions || []).filter(Boolean));
    p.dNotice.hidden = !notice;
    p.dNotice.textContent = notice || "";
    p.setBody(...body);
    p.dBody.scrollTop = 0;
    p.detail.hidden = false;
    p.markRows();
  };
  p.setBody = (...body) => p.dBody.replaceChildren(...body.flat().filter(Boolean));
  p.close = () => {
    const handler = p.onClose;
    p.onClose = null;
    p.openKey = null;
    p.detail.hidden = true;
    p.markRows();
    if (handler) handler();
  };
  return p;
}

// 行を作る。key を付けておくと、詳細を開いている行に色が付く。
function row(pane, key, onclick, ...cells) {
  return el("tr", { class: onclick ? "click" : "", "data-key": key, "aria-selected": pane.openKey === key ? "true" : null, onclick }, ...cells);
}
function table(headers, rows) {
  return el("table", null,
    el("thead", null, el("tr", null, ...headers.map((h) => (typeof h === "string" ? el("th", { text: h }) : h)))),
    el("tbody", null, ...rows));
}
function kv(obj) {
  const entries = Object.entries(obj || {});
  if (!entries.length) return null;
  return el("dl", { class: "kv mono" }, ...entries.flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: cellText(v) })]));
}

// ---- 全体の状態 ----------------------------------------------------------------

const S = { view: "resources", readOnly: false, emu: { running: false }, proxy: {}, kinds: [], showEmpty: false };
let sel = null;                              // 選んでいるもの {kind, id}。id が null なら種類の一覧
const s3 = { prefix: "", flat: false };
const ddb = { pk: "", pages: 1 };
let sqsLoaded = null;                        // 中身を読み込み済みのキュー {url, data}
let ticket = 0;                              // 古い応答で画面を上書きしないための通し番号
const kindOf = (id) => S.kinds.find((k) => k.id === id);

const R = makePane($("pane-resources"));
const C = makePane($("view-calls"));
const L = makePane($("view-logs"));
const P = makePane($("view-snapshots"));

// ---- 画面の切り替えとURL -------------------------------------------------------

function writeHash() {
  let hash = "#/" + S.view;
  if (S.view === "resources" && sel) {
    hash += "/" + sel.kind + (sel.id ? "/" + encodeURIComponent(sel.id) : "");
    if (sel.kind === "s3" && sel.id && s3.prefix) hash += "?p=" + encodeURIComponent(s3.prefix);
  }
  if (location.hash !== hash) history.replaceState(null, "", hash);
}
function readHash() {
  const [path, query] = location.hash.replace(/^#\/?/, "").split("?");
  const [view, kind, id] = path.split("/");
  if (["resources", "calls", "logs", "snapshots", "connect"].includes(view)) S.view = view;
  if (S.view === "resources" && kind) {
    sel = { kind, id: id ? decodeURIComponent(id) : null };
    s3.prefix = new URLSearchParams(query || "").get("p") || "";
  }
}
function showView() {
  for (const b of $("tabs").querySelectorAll("button")) {
    if (b.dataset.view === S.view) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
  }
  for (const v of ["resources", "calls", "logs", "snapshots", "connect"]) $("view-" + v).hidden = v !== S.view;
  writeHash();
}
function setView(view) {
  S.view = view;
  showView();
  if (view === "connect") renderConnect();
  if (view === "calls") renderCalls();
  tick(true);
}
for (const b of $("tabs").querySelectorAll("button")) b.addEventListener("click", () => setView(b.dataset.view));

// ---- 上の帯: エミュレーターの状態と操作 -----------------------------------------

async function pollState() {
  const s = await api("/api/state");
  S.readOnly = s.readOnly;
  S.emu = s.emulator;
  S.proxy = s.proxy;
  const state = $("emu-state");
  state.className = "state " + (S.emu.running ? "up" : "down");
  state.textContent = S.emu.running
    ? `稼働中 ・ v${S.emu.version || "?"} ・ ${S.emu.endpoint.replace("https://", "")}` + (S.readOnly ? " ・ 読み取り専用" : "")
    : "エミュレーターは停止中";
  $("emu-restart").hidden = S.readOnly || !S.emu.running;
  $("emu-toggle").hidden = S.readOnly;
  $("emu-toggle").textContent = S.emu.running ? "停止" : "起動";
}
async function control(action, label, done) {
  for (const id of ["emu-restart", "emu-toggle"]) $(id).disabled = true;
  await run(label, () => post("/api/emulator/" + action), done);
  for (const id of ["emu-restart", "emu-toggle"]) $(id).disabled = false;
  sqsLoaded = null;
  R.listJson = null;
  tick(true);
}
$("emu-restart").addEventListener("click", async () => {
  if (await ask({ title: "再起動して空にする", message: "エミュレーターを再起動します。作ったバケットやテーブルなどは、すべて消えます。\n残したいものは、先に「スナップショット」で保存してください。", okLabel: "再起動する", danger: true })) {
    control("restart", "再起動しています", "再起動しました（中身は空です）");
  }
});
$("emu-toggle").addEventListener("click", async () => {
  if (!S.emu.running) return control("start", "起動しています", "起動しました");
  if (await ask({ title: "エミュレーターを停止", message: "停止すると、作ったリソースはすべて消えます。", okLabel: "停止する", danger: true })) {
    control("stop", "停止しています", "停止しました");
  }
});

// ---- リソース: 左の一覧 --------------------------------------------------------

const SIDE_MAX = 12;  // 1種類につき、左に並べる件数。超えた分は種類の一覧で見る

function select(kind, id) {
  R.close();
  sel = { kind, id: id === undefined ? null : id };
  s3.prefix = "";
  ddb.pk = "";
  ddb.pages = 1;
  R.listJson = null;
  writeHash();
  tick(true);
}

let sideJson = null;
function renderSide() {
  const json = JSON.stringify([S.kinds.map((k) => [k.id, k.error, k.rows.map((r) => [r.id, r.name])]), sel, S.showEmpty]);
  if (json === sideJson) return;
  sideJson = json;
  const nodes = [];
  const empty = [];
  for (const kind of S.kinds) {
    if (!kind.rows.length) { empty.push(kind); continue; }
    nodes.push(el("button", {
        type: "button", class: "kind", title: kind.label + " の一覧",
        "aria-current": sel && sel.kind === kind.id && !sel.id ? "true" : null,
        onclick: () => select(kind.id),
      }, el("span", { text: kind.label }), el("span", { class: "n", text: String(kind.rows.length) })));
    for (const r of kind.rows.slice(0, SIDE_MAX)) {
      nodes.push(el("button", {
        type: "button", class: "mono", text: r.name, title: r.name,
        "aria-current": sel && sel.kind === kind.id && sel.id === r.id ? "true" : null,
        onclick: () => select(kind.id, r.id),
      }));
    }
    if (kind.rows.length > SIDE_MAX) {
      nodes.push(el("button", { type: "button", class: "more", text: `ほか ${kind.rows.length - SIDE_MAX} 件…`, onclick: () => select(kind.id) }));
    }
  }
  if (empty.length) {
    nodes.push(el("button", {
      type: "button", class: "toggle",
      text: S.showEmpty ? "空のサービスを隠す" : `空のサービスも表示（${empty.length}）`,
      onclick: () => { S.showEmpty = !S.showEmpty; renderSide(); },
    }));
    if (S.showEmpty) {
      for (const kind of empty) {
        nodes.push(kind.error
          ? el("div", { class: "empty-kind" }, el("span", { text: kind.label }), el("span", { class: "warn", text: kind.error }))
          : el("button", { type: "button", class: "more", text: kind.label, "aria-current": sel && sel.kind === kind.id ? "true" : null, onclick: () => select(kind.id) }));
      }
    }
  }
  $("side").replaceChildren(...nodes);
}

// ---- リソース: 右側 ------------------------------------------------------------

async function refreshResources() {
  const mine = ++ticket;
  try {
    const inv = await api("/api/inventory");
    if (mine !== ticket) return;
    S.kinds = inv.kinds;
    if (sel) {  // 選んでいたものが消えていたら（再起動など）、一つ上に戻る
      const kind = kindOf(sel.kind);
      if (!kind) sel = null;
      else if (sel.id && !kind.rows.some((r) => r.id === sel.id)) { sel = { kind: sel.kind, id: null }; R.close(); }
      writeHash();
    }
    renderSide();
    await renderMain(mine);
    if (mine === ticket) $("status-text").textContent = "最終更新 " + new Date().toLocaleTimeString("ja-JP");
  } catch (e) {
    if (mine !== ticket || e.kind === "gone") return;  // 一覧と中身の取得の間に消えた。次の更新で直る
    if (e.kind === "down") return renderDown();
    $("status-text").textContent = e.message;
  }
}

function renderDown() {
  if (R.listJson === "down") return;  // すでに出している。描き直すとボタンが押しにくくなる
  sideJson = null;
  $("side").replaceChildren();
  R.close();
  R.listJson = "down";
  R.setHead(null);
  R.message(
    el("p", { text: "エミュレーターは停止しています。" }),
    S.readOnly ? null : el("p", null, button("起動する", () => control("start", "起動しています", "起動しました"), "primary")),
    el("p", null, "ターミナルからは ", el("code", { text: "./emu up" }), " で起動できます。起動すると、この画面は自動でつながります。"));
}

async function renderMain(mine) {
  if (!sel) {
    const total = S.kinds.reduce((n, k) => n + k.rows.length, 0);
    const json = "none:" + (total ? "some" : "empty");
    if (json === R.listJson) return;
    R.listJson = json;
    R.close();
    R.setHead(null);
    R.message(...(total
      ? [el("p", { text: "左の一覧から、見たいものを選んでください。" })]
      : [el("p", { text: "エミュレーターは空です。まだ何も作られていません。" }),
         el("p", null, "サンプルデータを入れるには ", el("code", { text: "./emu seed" }), "、保存した状態に戻すには「スナップショット」を使います。"),
         el("p", null, "自分のプログラムからつなぐ方法は「つなぎ方」にあります。")]));
    return;
  }
  const kind = kindOf(sel.kind);
  R.title.className = !sel.id || kind.view === "generic" ? "" : "mono";  // 名前（バケット名など）だけ等幅にする
  if (!sel.id || kind.view === "generic") return renderKind(kind);
  if (kind.view === "s3") return renderS3(mine);
  if (kind.view === "dynamodb") return renderDdb(mine);
  if (kind.view === "sqs") return renderSqs(mine, false);
}

const TIME_COLUMNS = new Set(["created", "modified", "changed"]);

// ある種類の一覧（S3 バケットの一覧、Lambda 関数の一覧など）
function renderKind(kind) {
  const json = JSON.stringify(["kind", kind, S.readOnly]);
  if (json === R.listJson) return;
  R.listJson = json;
  const tools = [];
  if (kind.id === "s3" && !S.readOnly) {
    tools.push(button("バケットを作成", async () => {
      const name = await ask({ title: "バケットを作成", input: "バケット名（例: my-test-bucket）", okLabel: "作成" });
      if (name) { await run("作成しています", () => post("/api/s3/create-bucket", { bucket: name.trim() }), "作成しました"); tick(true); }
    }));
  }
  R.setHead(kind.label, `${kind.rows.length} 件`, tools, kind.note);
  if (kind.error) return R.message(el("p", { text: `このエミュレーターでは、一覧を取得できませんでした（${kind.error}）。` }));
  if (!kind.rows.length) return R.message(el("p", { text: "まだ1つもありません。" }));
  const rows = kind.rows.map((r) => row(R, r.id, () => (kind.view === "generic" ? openGeneric(kind, r) : select(kind.id, r.id)),
    ...kind.columns.map(([key]) => {
      const v = r[key];
      const text = TIME_COLUMNS.has(key) ? fmtTime(v) : key === "bytes" ? fmtSize(v) : cellText(v);
      return el("td", { class: (typeof v === "number" ? "num " : "") + "mono", text, title: text });
    })));
  R.setList(table(kind.columns.map(([key, label]) => el("th", { class: typeof (kind.rows[0] || {})[key] === "number" ? "num" : "", text: label })), rows));
  if (sel.id && R.openKey !== sel.id) {  // URL で直接開いたとき
    const r = kind.rows.find((x) => x.id === sel.id);
    if (r) openGeneric(kind, r);
  }
}

async function openGeneric(kind, r) {
  sel = { kind: kind.id, id: r.id };
  writeHash();
  sideJson = null;
  renderSide();
  R.open(r.id, r.name, kind.label, [button("読み直す", () => openGeneric(kind, r))], "", el("p", { text: "読み込み中…" }));
  R.onClose = () => {  // 詳細を閉じたら、選択も「種類の一覧」に戻す
    if (sel && sel.kind === kind.id) { sel = { kind: kind.id, id: null }; writeHash(); renderSide(); }
  };
  try {
    const d = await api("/api/kind/detail", { kind: kind.id, id: r.id });
    if (R.openKey !== r.id) return;
    R.setBody(d.text ? pre(d.text) : null, d.text ? el("h3", { text: "情報" }) : null, pre(JSON.stringify(d.data, null, 2)));
  } catch (e) {
    if (R.openKey === r.id) R.setBody(el("p", { text: "取得できませんでした（" + e.message + "）" }));
  }
}

// ---- S3 ------------------------------------------------------------------------

async function renderS3(mine) {
  const bucket = sel.id;
  const data = await api("/api/s3/list", { bucket, prefix: s3.prefix, flat: s3.flat ? "1" : "" });
  if (mine !== ticket) return;
  const json = JSON.stringify(["s3", bucket, s3, data, S.readOnly]);
  if (json === R.listJson) return;
  R.listJson = json;

  const go = (prefix) => { s3.prefix = prefix; R.listJson = null; R.close(); writeHash(); tick(true); };
  const crumbs = ["S3 バケット ・ ", el("button", { type: "button", text: bucket, onclick: () => go("") })];
  let walked = "";
  for (const part of s3.prefix.split("/").filter(Boolean)) {
    walked += part + "/";
    const target = walked;
    crumbs.push(" / ", el("button", { type: "button", text: part, onclick: () => go(target) }));
  }
  crumbs.push(` ・ ${data.folders.length ? data.folders.length + " フォルダ、" : ""}${data.objects.length} ファイル`);

  R.setHead(bucket, crumbs, [
    !S.readOnly && button("アップロード", () => $("file-input").click()),
    button(s3.flat ? "フォルダごとに表示" : "すべて並べて表示", () => { s3.flat = !s3.flat; go(s3.flat ? "" : s3.prefix); }),
    el("span", { class: "spacer" }),
    !S.readOnly && button("バケットを削除", async () => {
      if (await ask({ title: "バケットを削除", message: `${bucket} を、中のファイルごと削除します。`, okLabel: "削除する", danger: true })) {
        await run("削除しています", () => post("/api/s3/delete-bucket", { bucket }), "削除しました");
        select("s3");
      }
    }, "danger"),
  ], data.next ? `先頭の ${data.limit} 件だけ表示しています。ほかにもファイルがあります。` : "");

  if (!data.folders.length && !data.objects.length) {
    return R.message(el("p", { text: s3.prefix ? "このフォルダは空です。" : "このバケットは空です。" }),
      S.readOnly ? null : el("p", { text: "ファイルをここにドラッグするか、「アップロード」で追加できます。" }));
  }
  const strip = (key) => (s3.flat ? key : key.slice(s3.prefix.length));
  const rows = [
    ...data.folders.map((f) => row(R, "dir:" + f, () => go(f),
      el("td", { class: "mono", text: strip(f) }), el("td", { class: "num dim", text: "フォルダ" }), el("td"))),
    ...data.objects.map((o) => row(R, o.key, () => showObject(bucket, o.key),
      el("td", { class: "mono", text: strip(o.key), title: o.key }),
      el("td", { class: "num", text: fmtSize(o.size) }),
      el("td", { text: fmtTime(o.modified) }))),
  ];
  R.setList(table(["名前", el("th", { class: "num", text: "サイズ" }), "更新日時"], rows));
}

async function showObject(bucket, key) {
  R.open(key, key, "", [], "", el("p", { text: "読み込み中…" }));
  let info;
  try {
    info = await api("/api/s3/object", { bucket, key });
  } catch (e) {
    if (R.openKey === key) R.setBody(el("p", { text: "中身を取得できませんでした（" + e.message + "）" }));
    return;
  }
  if (R.openKey !== key) return;
  const link = (path) => { const u = new URL(path, location.origin); u.searchParams.set("bucket", bucket); u.searchParams.set("key", key); return u.href; };
  let body;
  if (info.kind === "image") body = el("img", { src: link("/api/s3/image"), alt: key });
  else if (info.kind === "text") body = info.text === "" ? el("p", { text: "中身は空です。" }) : pre(info.text);
  else body = el("p", { text: "テキストでも画像でもないため、中身は表示しません。ダウンロードはできます。" });
  const extra = { ...Object.fromEntries(Object.entries(info.metadata).map(([k, v]) => ["メタデータ " + k, v])),
                  ...Object.fromEntries(Object.entries(info.tags).map(([k, v]) => ["タグ " + k, v])) };
  R.open(key, key, `${fmtSize(info.size)} ・ ${info.contentType || "種類不明"}`, [
    el("a", { class: "btn", href: link("/api/s3/download"), download: key.split("/").pop(), text: "ダウンロード" }),
    button("S3 URI をコピー", () => copyText(info.uri)),
    !S.readOnly && button("削除", async () => {
      if (await ask({ title: "ファイルを削除", message: info.uri, okLabel: "削除する", danger: true })) {
        await run("削除しています", () => post("/api/s3/delete", { bucket, key }), "削除しました");
        R.close();
        tick(true);
      }
    }, "danger"),
  ], info.truncated ? "大きなファイルのため、先頭の 1 MB だけ表示しています。" : "", body, kv(extra));
}

async function uploadFiles(files) {
  if (!sel || sel.kind !== "s3" || !sel.id || S.readOnly || !files.length) return;
  const bucket = sel.id;
  const prefix = s3.flat ? "" : s3.prefix;
  let done = 0;
  for (const file of files) {
    toast(`アップロードしています（${done + 1} / ${files.length}）…`, false, true);
    try {
      await request("POST", "/api/s3/upload", { bucket, key: prefix + file.name }, null, { body: file, type: file.type });
      done++;
    } catch (e) {
      toast(`${file.name}: ${e.message}`, true);
      break;
    }
  }
  if (done === files.length) toast(`${done} 個アップロードしました`);
  tick(true);
}
$("file-input").addEventListener("change", (e) => { uploadFiles([...e.target.files]); e.target.value = ""; });
R.list.addEventListener("dragover", (e) => {
  if (sel && sel.kind === "s3" && sel.id && !S.readOnly) { e.preventDefault(); R.list.classList.add("dragover"); }
});
R.list.addEventListener("dragleave", () => R.list.classList.remove("dragover"));
R.list.addEventListener("drop", (e) => {
  e.preventDefault();
  R.list.classList.remove("dragover");
  uploadFiles([...e.dataTransfer.files]);
});

// ---- DynamoDB ------------------------------------------------------------------

async function renderDdb(mine) {
  const tableName = sel.id;
  let first = null, next = null;
  const items = [];
  for (let i = 0; i < ddb.pages; i++) {  // 「さらに読み込む」を押した分だけ、続きを取りにいく
    const page = await api("/api/ddb/items", { table: tableName, pk: ddb.pk, start: next ? JSON.stringify(next) : "" });
    if (mine !== ticket) return;
    first = first || page;
    items.push(...page.items);
    next = page.next;
    if (!next) break;
  }
  const json = JSON.stringify(["ddb", tableName, ddb, items, next, S.readOnly]);
  if (json === R.listJson) return;
  R.listJson = json;

  const keys = first.keys;
  const columns = [...keys, ...[...new Set(items.flatMap((i) => Object.keys(i.plain)))].filter((c) => !keys.includes(c)).sort()];
  const filter = el("input", { type: "text", placeholder: `${keys[0]} で絞り込み（完全一致）`, value: ddb.pk,
    onkeydown: (e) => { if (e.key === "Enter") applyFilter(); } });
  const applyFilter = () => { ddb.pk = filter.value.trim(); ddb.pages = 1; R.listJson = null; R.close(); tick(true); };
  const template = Object.fromEntries(keys.map((k) => [k, { [first.keyTypes[k]]: "" }]));

  R.setHead(tableName,
    `DynamoDB テーブル ・ ${items.length} 件${next ? "（続きあり）" : ""} ・ キー: ${keys.map((k) => `${k} (${first.keyTypes[k]})`).join(" / ")}` +
      (first.indexes.length ? ` ・ インデックス: ${first.indexes.join(", ")}` : ""),
    [
      filter, button("絞り込む", applyFilter),
      ddb.pk && button("解除", () => { filter.value = ""; applyFilter(); }),
      !S.readOnly && button("項目を追加", () => editItem(tableName, template, true)),
      el("span", { class: "spacer" }),
      !S.readOnly && button("テーブルを削除", async () => {
        if (await ask({ title: "テーブルを削除", message: `${tableName} を、中の項目ごと削除します。`, okLabel: "削除する", danger: true })) {
          await run("削除しています", () => post("/api/ddb/delete-table", { table: tableName }), "削除しました");
          select("dynamodb");
        }
      }, "danger"),
    ]);
  if (!items.length) return R.message(el("p", { text: ddb.pk ? "条件に合う項目はありません。" : "このテーブルは空です。" }));

  const rows = items.map((item) => {
    const id = JSON.stringify(keys.map((k) => item.plain[k]));
    return row(R, id, () => showItem(tableName, keys, columns, item, id),
      ...columns.map((c) => el("td", { class: "mono", text: cellText(item.plain[c]), title: cellText(item.plain[c]) })));
  });
  if (next) {
    rows.push(el("tr", { class: "click more", onclick: () => { ddb.pages++; R.listJson = null; tick(true); } },
      el("td", { colspan: String(columns.length), text: `さらに読み込む（いま ${items.length} 件）` })));
  }
  R.setList(table(columns.map((c) => el("th", { class: "mono" }, c, keys.includes(c) ? el("span", { class: "key", text: "（キー）" }) : null)), rows));
}

const ordered = (obj, columns) => Object.fromEntries(columns.filter((c) => c in obj).map((c) => [c, obj[c]]));

function showItem(tableName, keys, columns, item, id) {
  const title = keys.map((k) => `${k} = ${cellText(item.plain[k])}`).join(", ");
  R.open(id, title, "", [
    !S.readOnly && button("編集", () => editItem(tableName, ordered(item.typed, columns), false, id, title)),
    !S.readOnly && button("削除", async () => {
      if (await ask({ title: "項目を削除", message: title, okLabel: "削除する", danger: true })) {
        const key = Object.fromEntries(keys.map((k) => [k, item.typed[k]]));
        await run("削除しています", () => post("/api/ddb/delete", { table: tableName, key }), "削除しました");
        R.close();
        tick(true);
      }
    }, "danger"),
  ], "", pre(JSON.stringify(ordered(item.plain, columns), null, 2)));
}

// 項目の編集。DynamoDB の「型付きJSON」（{"S": "文字"} の形）で書く。型を取り違えないため。
function editItem(tableName, typed, isNew, id, title) {
  const area = el("textarea", { class: "mono", spellcheck: "false" });
  area.value = JSON.stringify(typed, null, 2);
  const save = async () => {
    let item;
    try { item = JSON.parse(area.value); } catch (e) { return toast("JSON として読めません: " + e.message, true); }
    const ok = await run("保存しています", async () => { await post("/api/ddb/put", { table: tableName, item }); return true; }, "保存しました");
    if (ok) { R.close(); R.listJson = null; tick(true); }
  };
  R.open(id || "new", isNew ? "新しい項目" : title, "", [button("保存", save, "primary"), button("やめる", () => R.close())],
    'DynamoDB の型付きJSONで書きます。例: {"S": "文字"} {"N": "123"} {"BOOL": true}。キーの値を変えると、別の項目として追加されます。', area);
  area.focus();
  area.setSelectionRange(0, 0);  // 先頭から読めるようにする
  area.scrollTop = 0;
}

// ---- SQS -----------------------------------------------------------------------

async function renderSqs(mine, force) {
  const url = sel.id;
  if (force || !sqsLoaded || sqsLoaded.url !== url) {
    const data = await api("/api/sqs/detail", { url });
    if (mine !== ticket) return;
    sqsLoaded = { url, data };
  }
  const data = sqsLoaded.data;
  const counts = kindOf("sqs").rows.find((r) => r.id === url) || {};
  const json = JSON.stringify(["sqs", url, data, counts, S.readOnly]);
  if (json === R.listJson) return;
  R.listJson = json;
  const reload = () => { R.close(); ticket++; renderSqs(ticket, true).catch((e) => toast(e.message, true)); };
  const name = url.split("/").pop();

  R.setHead(name, `SQS キュー ・ 待機中 ${counts.visible ?? "?"} ・ 処理中 ${counts.inflight ?? "?"} ・ 読み込み済み ${data.messages.length} 通`, [
    !S.readOnly && button("メッセージを送る", async () => {
      const body = await ask({ title: "メッセージを送る", message: name, textarea: true, value: '{\n  "hello": "world"\n}', okLabel: "送る" });
      if (body) { await run("送っています", () => post("/api/sqs/send", { url, body }), "送りました"); reload(); }
    }),
    button("中身を読み直す", reload),
    button("属性を見る", () => R.open("attrs", name, "キューの属性", [button("URL をコピー", () => copyText(url))], "", pre(JSON.stringify(data.attributes, null, 2)))),
    el("span", { class: "spacer" }),
    !S.readOnly && button("空にする", async () => {
      if (await ask({ title: "キューを空にする", message: `${name} のメッセージをすべて消します。`, okLabel: "空にする", danger: true })) {
        await run("空にしています", () => post("/api/sqs/purge", { url }), "空にしました");
        reload();
      }
    }, "danger"),
    !S.readOnly && button("キューを削除", async () => {
      if (await ask({ title: "キューを削除", message: name, okLabel: "削除する", danger: true })) {
        await run("削除しています", () => post("/api/sqs/delete-queue", { url }), "削除しました");
        sqsLoaded = null;
        select("sqs");
      }
    }, "danger"),
  ], "中身は自動では更新しません。SQS には「のぞくだけ」の方法がないため、読み直すたびに各メッセージの受信回数が1増えます。");

  if (!data.messages.length) return R.message(el("p", { text: "待機中のメッセージはありません。" }));
  const rows = data.messages.map((m) => row(R, m.id, () => R.open(m.id, m.id, `受信 ${m.attributes.ApproximateReceiveCount || "?"} 回`,
      [button("本文をコピー", () => copyText(m.body))], "", pre(prettyIfJson(m.body)),
      el("h3", { text: "属性" }), kv({ ...m.attributes, ...Object.fromEntries(Object.entries(m.messageAttributes).map(([k, v]) => ["メッセージ属性 " + k, v.StringValue ?? v])) })),
    el("td", { class: "mono wide", text: m.body.replace(/\s+/g, " ").slice(0, 200), title: m.body.slice(0, 500) }),
    el("td", { class: "num", text: m.attributes.ApproximateReceiveCount || "" }),
    el("td", { text: m.attributes.SentTimestamp ? fmtTime(Number(m.attributes.SentTimestamp) / 1000) : "" }),
    el("td", { class: "mono dim", text: m.id.slice(0, 8) })));
  R.setList(table(["本文", el("th", { class: "num", text: "受信回数" }), "送信日時", "ID"], rows));
}

// ---- APIログ -------------------------------------------------------------------

let calls = [];
let callsLast = 0;
const callFilter = { text: "", errors: false, missing: false };

async function pollCalls() {
  let d;
  try { d = await api("/api/calls", { after: callsLast }); } catch { return; }
  if (d.lastId < callsLast) { calls = []; callsLast = 0; return pollCalls(); }  // サーバーが再起動して番号が戻った
  if (!d.calls.length) return;
  calls.push(...d.calls);
  calls = calls.slice(-2000);
  callsLast = d.lastId;
  renderCallCount();
  if (S.view === "calls") renderCalls();
}
function renderCallCount() {
  const missing = calls.filter((c) => c.status === 501).length;
  $("calls-count").textContent = calls.length ? `（${calls.length}${missing ? "・未実装 " + missing : ""}）` : "";
  $("calls-count").className = missing ? "bad" : "";
}

function setupCalls() {
  const text = el("input", { type: "text", placeholder: "サービス名や操作名で絞り込み", oninput: (e) => { callFilter.text = e.target.value.toLowerCase(); renderCalls(); } });
  const check = (label, key) => el("label", { class: "check" },
    el("input", { type: "checkbox", onchange: (e) => { callFilter[key] = e.target.checked; renderCalls(); } }), label);
  C.setHead("APIログ", "", [
    text, check("エラーだけ", "errors"), check("未実装（501）だけ", "missing"), el("span", { class: "spacer" }),
    button("消去", async () => { await post("/api/calls/clear"); calls = []; C.close(); renderCallCount(); renderCalls(); }),
  ]);
  C.title.className = "";
}

function renderCalls() {
  if (!S.proxy.enabled) {
    C.summary.textContent = "";
    return C.message(el("p", { text: "APIログ用の中継を起動していません（--no-proxy）。" }));
  }
  const errors = calls.filter((c) => c.status >= 400);
  const missing = [...new Set(calls.filter((c) => c.status === 501).map((c) => `${c.service}:${c.operation}`))];
  C.summary.replaceChildren(
    `${calls.length} 件 ・ エラー ${errors.length} 件`,
    missing.length ? el("span", { class: "bad", text: ` ・ 未実装の操作 ${missing.length} 種類: ${missing.join(", ")}` }) : " ・ 未実装の操作なし");
  if (!calls.length) {
    C.listJson = null;
    return C.message(
      el("p", { text: "まだ呼び出しが記録されていません。" }),
      el("p", { text: "自分のプログラムを、次のように中継を通して動かすと、ここに呼び出しが1件ずつ出ます。エミュレーターが未実装（501）と答えた操作は赤で表示されます。" }),
      pre("./emu run --log -- python my_script.py\n./emu run --log -- aws s3 ls"),
      el("p", null, "シェルごと切り替える方法などは「つなぎ方」にあります。中継の宛先は ", el("code", { text: S.proxy.endpoint }), " です。"));
  }
  const shown = calls.filter((c) =>
    (!callFilter.errors || c.status >= 400) && (!callFilter.missing || c.status === 501) &&
    (!callFilter.text || `${c.service} ${c.operation} ${c.error} ${c.client}`.toLowerCase().includes(callFilter.text)));
  const json = JSON.stringify([shown.length, shown.length ? shown[shown.length - 1].id : 0, shown.length ? shown[0].id : 0, callFilter]);
  if (json === C.listJson) return;
  C.listJson = json;
  if (!shown.length) return C.message(el("p", { text: "条件に合う呼び出しはありません。" }));
  const rows = shown.slice().reverse().map((c) => row(C, String(c.id), () => showCall(c),
    el("td", { class: "mono", text: fmtClock(c.time) }),
    el("td", { class: "mono", text: c.service }),
    el("td", { class: "mono", text: c.operation, title: `${c.method} ${c.path}` }),
    el("td", { class: "mono" + (c.status >= 400 ? " bad" : ""), text: c.status === 501 ? "501 未実装" : `${c.status}${c.error ? " " + c.error : ""}` }),
    el("td", { class: "num", text: c.ms.toFixed(1) + " ms" }),
    el("td", { class: "num", text: fmtSize(c.reqBytes) }),
    el("td", { class: "num", text: fmtSize(c.resBytes) }),
    el("td", { class: "dim", text: c.client })));
  C.setList(table(["時刻", "サービス", "操作", "結果", el("th", { class: "num", text: "時間" }),
    el("th", { class: "num", text: "送信" }), el("th", { class: "num", text: "受信" }), "呼び出し元"], rows));
}

async function showCall(c) {
  const key = String(c.id);
  C.open(key, `${c.service}:${c.operation}`, `${c.status} ・ ${c.ms.toFixed(1)} ms`, [], "", el("p", { text: "読み込み中…" }));
  let d;
  try { d = await api("/api/calls/detail", { id: c.id }); }
  catch { if (C.openKey === key) C.setBody(el("p", { text: "この呼び出しの詳細は、もう残っていません。" })); return; }
  if (C.openKey !== key) return;
  const body = (b) => b.kind === "empty" ? el("p", { text: "本文なし" })
    : b.kind === "binary" ? el("p", { text: `バイナリ（${fmtSize(b.bytes)}）` })
    : [pre(b.text), b.truncated ? el("p", { text: `先頭だけ表示しています（全体は ${fmtSize(b.bytes)}）` }) : null];
  C.dActions.replaceChildren(button("要求の本文をコピー", () => copyText(d.reqBody.text || "")));
  C.setBody(
    el("h3", { text: "要求" }), pre(`${d.method} ${d.path}${d.query ? "?" + d.query : ""}`), body(d.reqBody), kv(d.reqHeaders),
    el("h3", { text: `応答 ${d.status}${d.error ? " " + d.error : ""}` }), body(d.resBody), kv(d.resHeaders));
}

// ---- エミュレーターのログ --------------------------------------------------------

let showNoise = false;
function setupLogs() {
  L.setHead("エミュレーターのログ",
    "エミュレーター自身が出しているログです。設定の不足で配信が止められた、といった警告はここに出ます。状態を変える操作の記録（監査ログ）も含みます。", [
      el("label", { class: "check" }, el("input", { type: "checkbox", onchange: (e) => { showNoise = e.target.checked; L.listJson = null; refreshLogs(); } }), "害のない警告も表示"),
    ]);
  L.title.className = "";
}
async function refreshLogs() {
  let d;
  try { d = await api("/api/logs", { tail: 2000 }); } catch (e) { return L.message(el("p", { text: e.message })); }
  const entries = d.entries.filter((e) => showNoise || !e.noise);
  const json = JSON.stringify([entries.length, entries.length ? entries[entries.length - 1] : null, showNoise]);
  if (json === L.listJson) return;
  L.listJson = json;
  if (!entries.length) return L.message(el("p", { text: S.emu.running ? "ログはまだありません。" : "エミュレーターは停止しています。" }));
  L.setList(entries.slice().reverse().map((e) => el("div", { class: "log-line mono" },
    el("span", { class: "dim", text: e.time ? new Date(e.time).toLocaleTimeString("ja-JP") : "" }),
    el("span", { class: e.level === "WARN" ? "warn" : e.level === "ERROR" ? "bad" : "dim", text: e.level }),
    el("span", { class: "msg", text: e.message }),
    Object.keys(e.fields).length ? el("span", { class: "fields", text: Object.entries(e.fields).map(([k, v]) => `${k}=${cellText(v)}`).join("  ") }) : null)));
}

// ---- スナップショット ------------------------------------------------------------

function setupSnapshots() {
  const name = el("input", { type: "text", placeholder: "名前（例: base）", onkeydown: (e) => { if (e.key === "Enter") save(); } });
  const save = async () => {
    const value = name.value.trim();
    if (!value) return toast("名前を入力してください", true);
    const r = await run("保存しています", () => post("/api/snapshots/save", { name: value }), "保存しました");
    if (r) { name.value = ""; P.listJson = null; refreshSnapshots(); }
  };
  P.setHead("スナップショット", "", [name, button("今の状態を保存", save, "primary")]);
  P.title.className = "";
}
const countText = (counts) => Object.entries(counts || {}).filter(([, n]) => n).map(([k, n]) => `${k} ${n}`).join("、") || "空";

async function refreshSnapshots() {
  let d;
  try { d = await api("/api/snapshots"); } catch (e) { return P.message(el("p", { text: e.message })); }
  P.summary.textContent = `今の中身をフォルダに保存し、あとで戻せます。保存するのは S3・DynamoDB・SQS・SNS・SSM パラメーター・Secrets Manager です（Lambda などは対象外）。保存先: ${d.folder}`;
  P.toolbar.hidden = S.readOnly;
  const json = JSON.stringify([d.snapshots, S.readOnly]);
  if (json === P.listJson) return;
  P.listJson = json;
  if (!d.snapshots.length) {
    return P.message(el("p", { text: "スナップショットはまだありません。" }),
      el("p", { text: "エミュレーターは止めると中身がすべて消えます。開発に使うデータを入れた状態で保存しておくと、いつでもその状態に戻せます。" }));
  }
  const load = async (s, reset) => {
    const message = reset
      ? `エミュレーターを再起動して空にしてから、${s.name} を読み込みます。今ある中身は消えます。`
      : `今の中身を残したまま、${s.name} を重ねます。同じ名前のファイルや項目は上書きされます。`;
    if (!(await ask({ title: "スナップショットを読み込む", message, okLabel: "読み込む", danger: reset }))) return;
    await run("読み込んでいます", () => post("/api/snapshots/load", { name: s.name, reset }), (r) => "読み込みました: " + countText(r.counts));
    sqsLoaded = null;
    tick(true);
  };
  const rows = d.snapshots.map((s) => el("tr", null,
    el("td", { class: "mono", text: s.name }),
    el("td", { text: fmtTime(s.created) }),
    el("td", { class: "wrap", text: countText(s.counts) }),
    el("td", { class: "num", text: fmtSize(s.bytes) }),
    el("td", { class: "actions" }, ...(S.readOnly ? [] : [
      button("空にしてから読み込む", () => load(s, true)),
      button("今の状態に重ねる", () => load(s, false)),
      button("削除", async () => {
        if (await ask({ title: "スナップショットを削除", message: s.name, okLabel: "削除する", danger: true })) {
          await run("削除しています", () => post("/api/snapshots/delete", { name: s.name }), "削除しました");
          P.listJson = null;
          refreshSnapshots();
        }
      }, "danger"),
    ]))));
  P.setList(table(["名前", "保存日時", "中身", el("th", { class: "num", text: "サイズ" }), ""], rows));
}

// ---- つなぎ方 ------------------------------------------------------------------

let connectLoaded = false;
async function renderConnect() {
  if (connectLoaded) return;
  let d;
  try { d = await api("/api/connect"); } catch (e) { return $("view-connect").replaceChildren(el("div", { class: "message", text: e.message })); }
  connectLoaded = true;
  $("view-connect").replaceChildren(el("div", { class: "doc" },
    el("section", null, el("h2", { text: "自分のプログラムをエミュレーターにつなぐ" }),
      el("p", null, "コマンドは ", el("code", { class: "mono", text: d.root }), " にある emu を使います。別のフォルダからは、フルパスで呼んでください。")),
    ...d.sections.map((s) => el("section", null, el("h2", { text: s.title }), el("p", { text: s.body }),
      el("div", { class: "code" }, pre(s.code), button("コピー", () => copyText(s.code)))))));
}

// ---- 定期的な更新 ---------------------------------------------------------------

async function tick(force) {
  // 見ていないとき、入力中、ダイアログ表示中は、勝手に画面を変えない
  if (!force && (document.visibilityState !== "visible" || $("dlg").open || isTyping())) return;
  try {
    await pollState();
  } catch (e) {
    $("emu-state").className = "state down";
    $("emu-state").textContent = "画面のサーバーに接続できません";
    $("status-text").textContent = e.message;
    return;
  }
  pollCalls();
  if (S.view === "resources") { if (S.emu.running) await refreshResources(); else renderDown(); }
  if (S.view === "logs") refreshLogs();
  if (S.view === "snapshots") refreshSnapshots();
}

setupCalls();
setupLogs();
setupSnapshots();
readHash();
showView();
if (S.view === "connect") renderConnect();
setInterval(tick, 3000);
setInterval(() => { if (S.view === "calls" && document.visibilityState === "visible") pollCalls(); }, 1000);
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") tick(); });
window.addEventListener("hashchange", () => {  // URL を直接書き換えたとき、戻る・進むを押したとき
  readHash();
  showView();
  R.listJson = null;
  if (S.view === "connect") renderConnect();
  if (S.view === "calls") renderCalls();
  tick(true);
});
tick(true);
