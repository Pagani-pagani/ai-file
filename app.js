/* AI File v1.3 — 網頁版
 * 靜態 SPA，跑在 GitHub Pages 上，沒有後端伺服器。
 * GitHub 金鑰只由管理員設定一次，存在瀏覽器 localStorage（aifile_pat）；
 * AI 登入只要工作階段代碼，不用再貼金鑰。
 * 絕對不要把任何 Token、密碼寫死在程式碼裡。
 * 網頁只能把檔案「暫存」到私人中繼 repo，真正寫進電腦的動作
 * 永遠由本地端程式在使用者按「同意」後才做。
 *
 * v1.4 改動：
 * - 管理員介面新增「檔案總管」：看快照裡的資料夾與檔案（副檔名、
 *   建立時間、大小、種類），電腦開著可要求即時重新整理；上傳、
 *   編輯、刪除、重新命名都走 manifest 暫存，電腦按「同意」才套用。
 * - 管理員暫存的項目（by="admin"）不用等工作階段核准，但一樣要
 *   按「同意」才套用；重新命名走新的 action:"rename"。
 */
"use strict";

var RELAY_OWNER = "Pagani-pagani";
var RELAY_REPO = "ai-file-relay";
var API_BASE = "https://api.github.com/repos/" + RELAY_OWNER + "/" + RELAY_REPO + "/contents";
var HEARTBEAT_FRESH_SECS = 90;

// 檔案總管：文字編輯白名單、編輯上限 200KB、索取檔案內容上限 10MB、等電腦回應最多 30 秒
var TEXT_EDIT_EXTS = ["txt", "md", "json", "js", "py", "html", "css", "csv", "log", "xml", "ini", "bat", "ps1"];
var TEXT_EDIT_MAX_BYTES = 200 * 1024;
var CONTENT_MAX_BYTES = 10 * 1024 * 1024;
var EXPLORER_WAIT_MS = 30000;

var $ = function (id) { return document.getElementById(id); };
function show(el) { el.classList.remove("hidden"); }
function hide(el) { el.classList.add("hidden"); }

function notice(msg, type) {
  var n = $("notice");
  n.textContent = msg;
  n.className = "notice " + (type || "info");
  show(n);
  window.scrollTo({ top: 0, behavior: "smooth" });
}
function clearNotice() { hide($("notice")); }

/* ---------- 編碼工具 ---------- */
function utf8ToB64(str) {
  var bytes = new TextEncoder().encode(str);
  var bin = "";
  var chunk = 0x8000;
  for (var i = 0; i < bytes.length; i += chunk) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(bin);
}
function b64ToUtf8(b64) {
  var bin = atob(b64.replace(/\s/g, ""));
  var bytes = new Uint8Array(bin.length);
  for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new TextDecoder().decode(bytes);
}
function bytesToB64(bytes) {
  var bin = "";
  var chunk = 0x8000;
  for (var i = 0; i < bytes.length; i += chunk) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(bin);
}
function sha256Hex(input) {
  // input: string 或 Uint8Array，回傳 hex 字串
  if (!window.crypto || !crypto.subtle) {
    return Promise.reject(new Error("這個瀏覽器不支援加密功能（需要 HTTPS），請用 Chrome 或 Edge 開啟。"));
  }
  var data = (typeof input === "string") ? new TextEncoder().encode(input) : input;
  return crypto.subtle.digest("SHA-256", data).then(function (buf) {
    return Array.from(new Uint8Array(buf)).map(function (b) {
      return b.toString(16).padStart(2, "0");
    }).join("");
  });
}
function utcnow() { return new Date().toISOString().replace(/\.\d+Z$/, "Z"); }

/* ---------- GitHub API ---------- */
function ghHeaders(pat) {
  return {
    "Accept": "application/vnd.github+json",
    "Authorization": "Bearer " + pat,
    "Content-Type": "application/json"
  };
}
function ghPath(path) {
  return path.split("/").map(encodeURIComponent).join("/");
}
// 把 HTTP 狀態翻成中文原因，連線失敗時一眼就知道是 Token 問題還是程式問題
function apiErrorReason(status) {
  if (status === 401) return "401：Token 不對或已過期，請重新產生 Token";
  if (status === 403) return "403：Token 權限不夠或沒有授權 ai-file-relay，請檢查 Token 設定";
  if (status === 404) return "404：找不到 repo 或 Token 看不到這個 repo，請檢查 repo 名稱與 Token 授權";
  return "HTTP " + status;
}
// 讀一個檔案：回傳 {data, sha}；不存在回傳 {data: null, sha: null}
function ghGetFile(pat, path) {
  return fetch(API_BASE + "/" + ghPath(path), { headers: ghHeaders(pat) })
    .then(function (res) {
      if (res.status === 404) return { data: null, sha: null };
      if (!res.ok) throw new Error("讀取 " + path + " 失敗（" + apiErrorReason(res.status) + "）");
      return res.json().then(function (j) {
        return { data: b64ToUtf8(j.content || ""), sha: j.sha };
      });
    })
    .catch(function (e) {
      // 我們自己丟的 HTTP 錯誤原樣往上丟；其他（例如連不上）才翻成中文
      if (e && e.message && e.message.indexOf("讀取 ") === 0) throw e;
      throw new Error("連不上 api.github.com，請檢查網路或防火牆");
    });
}
function ghGetJson(pat, path) {
  return ghGetFile(pat, path).then(function (r) {
    if (r.data === null) return { data: null, sha: null };
    try { return { data: JSON.parse(r.data), sha: r.sha }; }
    catch (e) { throw new Error("檔案 " + path + " 不是正確的 JSON"); }
  });
}
// 寫一個檔案（文字或 base64 字串）：自動帶上現有 sha（更新用）
function ghPutFile(pat, path, contentB64, message) {
  return ghGetFile(pat, path).then(function (cur) {
    var body = { message: message, content: contentB64 };
    if (cur.sha) body.sha = cur.sha;
    return fetch(API_BASE + "/" + ghPath(path), {
      method: "PUT", headers: ghHeaders(pat), body: JSON.stringify(body)
    }).then(function (res) {
      if (!res.ok) {
        return res.json().catch(function () { return {}; }).then(function (j) {
          throw new Error("寫入 " + path + " 失敗（" + apiErrorReason(res.status) + "）" +
            (j.message ? "：" + j.message : ""));
        });
      }
      return res.json();
    });
  });
}
function ghPutJson(pat, path, obj, message) {
  return ghPutFile(pat, path, utf8ToB64(JSON.stringify(obj, null, 2)), message);
}
function ghDeleteFile(pat, path, message) {
  return ghGetFile(pat, path).then(function (cur) {
    if (!cur.sha) return;
    return fetch(API_BASE + "/" + ghPath(path), {
      method: "DELETE", headers: ghHeaders(pat),
      body: JSON.stringify({ message: message, sha: cur.sha })
    });
  });
}
function appendActivity(pat, entry) {
  return ghGetJson(pat, "activity.json").then(function (r) {
    var list = Array.isArray(r.data) ? r.data : [];
    entry.at = entry.at || utcnow();
    list.push(entry);
    return ghPutJson(pat, "activity.json", list, "AI File：新增活動紀錄");
  }).catch(function (e) {
    console.warn("寫入活動紀錄失敗（不影響暫存本身）：", e);
  });
}

/* ---------- localStorage 安全讀寫 ---------- */
// 金鑰消失的真正原因（v1.3 查核）：程式碼從來沒有刪過 aifile_pat，登出也只清
// aifile_ai。金鑰是瀏覽器本身沒留住：換瀏覽器、無痕視窗、關閉時清除網站資料、
// 或是不保留資料的雲端瀏覽器。另外 v1.2 有個小缺口：setItem 失敗會靜默丟例外，
// 使用者看不到「其實沒存下來」。下面全部用 try/catch 包起來，失敗就明講。
function storageAvailable() {
  try {
    localStorage.setItem("aifile_storage_test", "1");
    var ok = localStorage.getItem("aifile_storage_test") === "1";
    localStorage.removeItem("aifile_storage_test");
    return ok;
  } catch (e) { return false; }
}
function loadStoredPat() {
  try { return localStorage.getItem("aifile_pat") || ""; }
  catch (e) { return ""; }
}

/* ---------- 狀態 ---------- */
var state = {
  pat: loadStoredPat(),
  adminAuthed: false,
  ai: null,          // {name, code}：只在這次頁面登入後才有；重開頁面不自動還原
  sessionCode: "",   // 目前的工作階段代碼（從心跳讀來，管理員暫存時帶上）
  sessionPoll: null,
  statusTimer: null
};

// 金鑰只從管理員設定讀（localStorage aifile_pat），AI 登入不再帶金鑰
function pat() { return state.pat || ""; }
function savePat(p) {
  try {
    localStorage.setItem("aifile_pat", p);
    state.pat = p;
    return true;
  } catch (e) {
    return false;
  }
}

/* ---------- 連線狀態（登入後才開始每 30 秒讀 heartbeat.json） ---------- */
function refreshStatus() {
  // 還沒登入就不顯示也不讀：登入前看不到連線狀態
  if (!state.adminAuthed && !state.ai) return Promise.resolve();
  var p = pat();
  if (!p) {
    setStatus("grey", "尚未設定金鑰", "請管理員先在管理員介面設定 GitHub 金鑰（只需設定一次）。");
    return Promise.resolve();
  }
  return ghGetJson(p, "heartbeat.json").then(function (r) {
    var hb = r.data;
    if (!hb || hb.status === "never" || !hb.updated_at) {
      setStatus("grey", "未連線", "中繼還沒有心跳，請確認 Windows 電腦上的 AI File 已經啟動。");
      $("kv-folder").textContent = "—";
      $("kv-session").textContent = "—";
      $("kv-version").textContent = "—";
      $("kv-heartbeat").textContent = "—";
      return;
    }
    var age = (Date.now() - new Date(hb.updated_at).getTime()) / 1000;
    if (hb.status === "stopped") {
      setStatus("red", "已停止", "本地端或網頁版已按下停止，AI 現在動不到任何檔案。");
    } else if (age <= HEARTBEAT_FRESH_SECS) {
      setStatus("green", "已連線", "本地端正常運作中（" + Math.round(age) + " 秒前回報）。");
    } else {
      setStatus("red", "未連線（心跳過期）", "最後心跳是 " + Math.round(age) + " 秒前，請確認 Windows 電腦上的 AI File 還在跑。");
    }
    $("kv-folder").textContent = hb.folder_name || hb.allowed_folder || "—";
    $("kv-session").textContent = hb.session_code ? ("代碼 " + hb.session_code) : "—";
    $("kv-version").textContent = hb.version ? ("v" + hb.version) : "—";
    $("kv-heartbeat").textContent = hb.updated_at.replace("T", " ").replace("Z", " UTC");
    $("ai-folder").textContent = hb.folder_name || hb.allowed_folder || "—";
    if (hb.session_code) {
      $("session-code").textContent = hb.session_code;
      state.sessionCode = hb.session_code;
    }
  }).catch(function (e) {
    setStatus("red", "連線失敗", String(e.message || e));
  });
}
function setStatus(color, text, hint) {
  $("status-dot").className = "dot dot-" + color;
  $("status-text").textContent = text;
  $("status-hint").textContent = hint || "";
}
function startStatusTimer() {
  if (state.statusTimer) return;
  state.statusTimer = setInterval(function () {
    refreshStatus();
    if (state.adminAuthed) { refreshPending(); refreshActivity(); refreshStopState(); }
  }, 30000);
}
function stopStatusTimer() {
  if (state.statusTimer) { clearInterval(state.statusTimer); state.statusTimer = null; }
}
function showStatusCard() { show($("status-card")); }

/* ---------- 進入畫面 ---------- */
function showLanding() {
  show($("landing"));
  hide($("admin-panel")); hide($("ai-panel"));
  hide($("admin-auth")); hide($("ai-auth")); hide($("status-card"));
}
function enterAi() {
  hide($("landing")); hide($("admin-auth")); hide($("admin-panel"));
  clearNotice();
  if (state.ai) {
    show($("ai-panel"));
    $("ai-session-state").textContent = "工作階段狀態：已登入（" + state.ai.name + "）";
    refreshMyStaged(); pollSession();
  } else {
    show($("ai-auth"));
  }
}
function enterAdmin() {
  hide($("landing")); hide($("ai-auth")); hide($("ai-panel"));
  clearNotice();
  if (state.adminAuthed) {
    show($("admin-panel")); loadAdminData();
  } else {
    openAdminAuth();
  }
}
function openHelp() { show($("help-dialog")); }
function closeHelp() { hide($("help-dialog")); }

/* ---------- 管理員：帳號密碼（只鎖這個瀏覽器的介面） ---------- */
function adminHashExists() {
  try { return !!localStorage.getItem("aifile_admin_hash"); }
  catch (e) { return false; }
}
function openAdminAuth() {
  hide($("ai-auth")); hide($("ai-panel")); hide($("landing"));
  show($("admin-auth"));
  if (adminHashExists()) {
    hide($("admin-setup")); show($("admin-login"));
    $("admin-auth-title").textContent = "管理員登入";
  } else {
    show($("admin-setup")); hide($("admin-login"));
    $("admin-auth-title").textContent = "第一次設定管理員帳號";
  }
}
function setupAdmin() {
  var user = $("setup-user").value.trim();
  var p1 = $("setup-pass").value, p2 = $("setup-pass2").value;
  if (!user) return notice("請輸入帳號。", "err");
  if (!p1) return notice("請輸入密碼。", "err");
  if (p1 !== p2) return notice("兩次輸入的密碼不一樣。", "err");
  sha256Hex(p1).then(function (h) {
    try {
      localStorage.setItem("aifile_admin_user", user);
      localStorage.setItem("aifile_admin_hash", h);
    } catch (e) {
      return notice("帳號存不下來：這個瀏覽器拒絕寫入網站資料，請改用一般視窗再試一次。", "err");
    }
    state.adminAuthed = true;
    $("admin-user").textContent = user;
    hide($("admin-auth")); show($("admin-panel"));
    notice("管理員帳號已設定。提醒：這只是這個瀏覽器的介面鎖。", "ok");
    loadAdminData();
  }).catch(function (e) { notice(String(e.message || e), "err"); });
}
function loginAdmin() {
  var user = $("login-user").value.trim();
  var pass = $("login-pass").value;
  var savedUser, savedHash;
  try {
    savedUser = localStorage.getItem("aifile_admin_user");
    savedHash = localStorage.getItem("aifile_admin_hash");
  } catch (e) {
    return notice("讀不到管理員帳號：這個瀏覽器不保留網站資料，請改用一般視窗。", "err");
  }
  sha256Hex(pass).then(function (h) {
    if (user === savedUser && h === savedHash) {
      state.adminAuthed = true;
      $("admin-user").textContent = user;
      hide($("admin-auth")); show($("admin-panel"));
      clearNotice();
      loadAdminData();
    } else {
      notice("帳號或密碼不對。", "err");
    }
  }).catch(function (e) { notice(String(e.message || e), "err"); });
}

/* ---------- 管理員：GitHub 金鑰設定（只設定一次） ---------- */
function updatePatState() {
  var text = state.pat ? "已設定" : "未設定";
  var el = $("pat-state");
  if (el) el.textContent = text;
  var landing = $("landing-pat-state");
  if (landing) landing.textContent = text;
}
function savePatKey() {
  var v = $("admin-pat-setup").value.trim();
  if (!v) return notice("請先貼上 GitHub 細粒度 Token。", "err");
  if (!savePat(v)) {
    return notice("金鑰沒有存下來：這個瀏覽器拒絕寫入網站資料（可能是無痕模式或儲存空間已滿）。請改用一般視窗再試一次。", "err");
  }
  $("admin-pat-setup").value = "";
  updatePatState();
  notice("GitHub 金鑰已設定（只存在這個瀏覽器）。之後 AI 登入只要工作階段代碼，不用再貼金鑰。", "ok");
  refreshStatus();
}
function clearPatKey() {
  if (!state.pat && !loadStoredPat()) return notice("本來就沒有設定金鑰。", "info");
  if (!confirm("確定要清除這個瀏覽器裡的 GitHub 金鑰嗎？清除後 AI 登入和連線狀態都會失效，要重新設定。")) return;
  try { localStorage.removeItem("aifile_pat"); } catch (e) {}
  state.pat = "";
  updatePatState();
  notice("已清除金鑰。", "info");
  refreshStatus();
}
function testConnection() {
  var p = pat();
  if (!p) {
    $("conn-test-result").textContent = "還沒設定 GitHub 金鑰，請管理員先在上方設定（只需設定一次）。";
    return;
  }
  $("conn-test-result").textContent = "測試中…";
  ghGetJson(p, "heartbeat.json").then(function (r) {
    if (!r.data || !r.data.updated_at) {
      $("conn-test-result").textContent = "連線成功，但中繼還沒有心跳（本地端還沒啟動過）。";
    } else {
      $("conn-test-result").textContent = "已連線，最後心跳：" + r.data.updated_at.replace("T", " ").replace("Z", " UTC");
    }
    refreshStatus();
  }).catch(function (e) {
    $("conn-test-result").textContent = "連線失敗：" + String(e.message || e);
    refreshStatus();
  });
}

/* ---------- 管理員介面 ---------- */
function loadAdminData() {
  showStatusCard();
  updatePatState();
  refreshStatus();
  refreshPending();
  refreshActivity();
  refreshStopState();
  refreshExplorer(true);
  startStatusTimer();
}
function refreshStopState() {
  var p = pat();
  if (!p) return;
  ghGetJson(p, "stop.json").then(function (r) {
    var stopped = r.data && r.data.stopped;
    $("stop-state").textContent = stopped ? "目前：已停止" : "目前：監控中";
    if (stopped) { hide($("btn-stop")); show($("btn-resume")); }
    else { show($("btn-stop")); hide($("btn-resume")); }
  }).catch(function () { /* 狀態列已顯示錯誤 */ });
}
// 暫存項目的比對鍵：有 id 用 id，舊資料用 rel_path＋時間＋AI 名稱
function entryKey(f) {
  if (f.id) return "id:" + f.id;
  return "k:" + (f.rel_path || "") + "|" + (f.time || "") + "|" + (f.ai_name || "");
}
function cancelButton(f, isAdmin) {
  var btn = document.createElement("button");
  btn.className = "btn btn-outline btn-small";
  btn.textContent = "取消暫存";
  btn.addEventListener("click", function () { cancelStaged(entryKey(f), isAdmin); });
  return btn;
}
// 取消一筆暫存：從 manifest 移除，檔案型的再刪 pending/ 裡的 blob，並寫活動紀錄
function cancelStaged(key, isAdmin) {
  var p = pat();
  if (!p) return notice("請先設定 GitHub 金鑰。", "err");
  if (!confirm("確定要取消這筆暫存嗎？本地端還沒同步的檔案不會寫進電腦。")) return;
  ghGetJson(p, "pending/manifest.json").then(function (r) {
    var files = (r.data && r.data.files) || [];
    var target = null;
    var rest = files.filter(function (f) {
      if (entryKey(f) === key) { target = f; return false; }
      return true;
    });
    if (!target) throw new Error("這筆暫存已經不在清單裡（可能已經同步或被取消了）。");
    if (!isAdmin && (!state.ai || target.ai_name !== state.ai.name)) {
      throw new Error("只能取消自己的暫存項目。");
    }
    var delBlob = target.file
      ? ghDeleteFile(p, "pending/" + target.file, "AI File：取消暫存，刪除暫存檔")
      : Promise.resolve();
    return delBlob.then(function () {
      return ghPutJson(p, "pending/manifest.json", { files: rest }, "AI File：取消一筆暫存");
    }).then(function () {
      return appendActivity(p, {
        action: "cancelled-staged",
        ai_name: state.ai ? state.ai.name : "",
        rel_path: target.rel_path,
        detail: "已取消暫存"
      });
    });
  }).then(function () {
    notice("已取消暫存。", "ok");
    refreshMyStaged();
    if (state.adminAuthed) refreshPending();
  }).catch(function (e) { notice(String(e.message || e), "err"); });
}
function refreshPending() {
  var p = pat();
  if (!p) return;
  ghGetJson(p, "pending/manifest.json").then(function (r) {
    var files = (r.data && r.data.files) || [];
    var ul = $("pending-list");
    ul.innerHTML = "";
    if (!files.length) {
      ul.innerHTML = "<li class='hint'>目前沒有待同步的檔案。</li>";
      return;
    }
    files.forEach(function (f) {
      var li = document.createElement("li");
      var action = { add: "新增", modify: "修改", delete: "刪除" }[f.action || "add"] || "新增";
      li.textContent = "[" + action + "] " + f.rel_path;
      var meta = document.createElement("span");
      meta.className = "meta";
      meta.textContent = (f.ai_name || "") + " · " + (f.time || "") + (f.size ? " · " + f.size + " bytes" : "");
      li.appendChild(meta);
      var actions = document.createElement("span");
      actions.className = "actions";
      actions.appendChild(cancelButton(f, true));
      li.appendChild(actions);
      ul.appendChild(li);
    });
  }).catch(function () { /* 忽略，狀態列會顯示 */ });
}
var ACTION_NAMES = {
  add: "新增", modify: "修改", delete: "刪除", rename: "重新命名",
  "session-approved": "已允許連線", "session-rejected": "已拒絕連線",
  "rejected-path": "已拒絕越界", stopped: "已停止", resumed: "已恢復",
  "code-regenerated": "代碼已重新生成", synced: "已同步",
  "sync-failed": "同步失敗", "cancelled-staged": "已取消暫存",
  undone: "已返回", "undo-requested": "要求返回"
};
// 管理員異動紀錄：同一批同步（同 batch_id）整理成一組，每組可以「返回」
function refreshActivity() {
  var p = pat();
  if (!p) return;
  ghGetJson(p, "activity.json").then(function (r) {
    var list = Array.isArray(r.data) ? r.data : [];
    var ul = $("activity-list");
    ul.innerHTML = "";
    if (!list.length) {
      ul.innerHTML = "<li class='hint'>還沒有紀錄。</li>";
      return;
    }
    var renderedBatches = {};
    // 由新到舊：遇到某批的第一筆就把整批一起畫出來
    list.slice().reverse().forEach(function (e) {
      if (e.batch_id) {
        if (renderedBatches[e.batch_id]) return;
        renderedBatches[e.batch_id] = true;
        var batchEntries = list.filter(function (x) { return x.batch_id === e.batch_id; });
        var undone = batchEntries.some(function (x) { return x.action === "undone"; });
        var undoRequested = batchEntries.some(function (x) { return x.action === "undo-requested"; });
        var items = batchEntries.filter(function (x) {
          return x.action !== "undone" && x.action !== "undo-requested";
        });
        var first = items[0] || batchEntries[0];
        var li = document.createElement("li");
        var head = document.createElement("div");
        head.className = "batch-head";
        head.textContent = "同步批次 " + e.batch_id + " · " + (first.ai_name || "") + " · " + (first.at || "");
        li.appendChild(head);
        items.forEach(function (x) {
          var div = document.createElement("div");
          div.textContent = "[" + (ACTION_NAMES[x.action] || x.action) + "] " + (x.rel_path || x.detail || "");
          li.appendChild(div);
        });
        var foot = document.createElement("div");
        if (undone) {
          foot.textContent = "已返回";
          foot.className = "batch-undone";
        } else if (undoRequested) {
          foot.textContent = "已送出返回，等本地端確認";
          foot.className = "batch-undone";
        } else {
          var btn = document.createElement("button");
          btn.className = "btn btn-outline btn-small";
          btn.textContent = "返回";
          btn.addEventListener("click", function () { requestUndo(e.batch_id); });
          foot.appendChild(btn);
        }
        li.appendChild(foot);
        ul.appendChild(li);
      } else {
        var li2 = document.createElement("li");
        li2.textContent = "[" + (ACTION_NAMES[e.action] || e.action) + "] " + (e.rel_path || e.detail || "");
        var meta = document.createElement("span");
        meta.className = "meta";
        meta.textContent = (e.ai_name || "") + " · " + (e.at || "");
        li2.appendChild(meta);
        ul.appendChild(li2);
      }
    });
  }).catch(function () { /* 忽略 */ });
}
// 管理員要求返回一批同步：寫 undo.json，本地端看到會跳確認，使用者按確定才還原
function requestUndo(batchId) {
  var p = pat();
  if (!p) return notice("請先設定 GitHub 金鑰。", "err");
  if (!confirm("確定要把這批同步返回到同步前的狀態嗎？\n本地端會跳出確認，你按確定後才會真的還原。")) return;
  ghPutJson(p, "undo.json", { batch_id: batchId, by: "web", at: utcnow() }, "AI File：管理員要求返回一批同步")
    .then(function () {
      return appendActivity(p, {
        action: "undo-requested", ai_name: "",
        batch_id: batchId, detail: "管理員在網頁版要求返回這批同步"
      });
    })
    .then(function () {
      notice("已送出返回要求，等本地端確認。", "ok");
      refreshActivity();
    })
    .catch(function (e) { notice(String(e.message || e), "err"); });
}
function regenCode() {
  var p = pat();
  if (!p) return notice("請先在管理員介面設定 GitHub 金鑰（只需設定一次）。", "err");
  if (!confirm("確定要重新生成工作階段代碼嗎？舊代碼會立刻失效，尚未同步的暫存也會被拒收。")) return;
  ghPutJson(p, "regen.json", { requested: true, at: utcnow(), by: "web" }, "AI File：請求重新生成代碼")
    .then(function () {
      notice("已送出重新生成請求。新的代碼會顯示在你電腦的 AI File 視窗上，網頁這裡也會在下次心跳時更新。", "ok");
      appendActivity(p, { action: "code-regenerated", ai_name: "", detail: "管理員在網頁版請求重新生成代碼" });
    })
    .catch(function (e) { notice(String(e.message || e), "err"); });
}
function setStopped(stopped) {
  var p = pat();
  if (!p) return notice("請先在管理員介面設定 GitHub 金鑰（只需設定一次）。", "err");
  ghPutJson(p, "stop.json", { stopped: stopped, by: "web", at: utcnow() }, stopped ? "AI File：網頁版按下停止" : "AI File：網頁版按下恢復")
    .then(function () {
      appendActivity(p, { action: stopped ? "stopped" : "resumed", ai_name: "", detail: stopped ? "管理員在網頁版按下停止" : "管理員在網頁版按下恢復" });
      notice(stopped ? "已停止。AI 現在動不到你的檔案。" : "已恢復監控。", "ok");
      refreshStopState(); refreshStatus();
    })
    .catch(function (e) { notice(String(e.message || e), "err"); });
}

/* ---------- 檔案總管（管理員介面） ----------
 * 內容來自中繼的 snapshot.json（本地端每次啟動、每次更新完都會產生）。
 * 電腦開著時，「重新整理」與打開檔案總管會寫 list_request.json，
 * 本地端看到就重新產生快照；電腦沒開就顯示上次的快照。
 * 這裡的所有操作（上傳、編輯、刪除、重新命名）都只是「暫存」進
 * manifest，電腦上按「同意」才會真的套用，絕對不繞過。
 * 管理員暫存的項目帶 by="admin"：不用等工作階段核准（使用者本人
 * 在同步確認窗按「同意」本身就是授權），但停止旗標開啟時一樣拒絕。
 */
var explorer = {
  snapshot: null,    // snapshot.json 的內容
  online: false,     // 心跳新鮮（90 秒內）
  currentPath: "",   // 目前資料夾（相對路徑，根目錄是 ""）
  search: "",
  multiselect: false,
  selected: {}       // rel_path -> true
};

/* 純函式（方便單元測試）：大小、時間、篩選、排序 */
function formatSize(bytes) {
  if (bytes >= 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + " MB";
  return (bytes / 1024).toFixed(1) + " KB";
}
function formatDateTime(iso) {
  if (!iso) return "—";
  var d = new Date(iso);
  if (isNaN(d.getTime())) return "—";
  function p(n) { return String(n).padStart(2, "0"); }
  return d.getFullYear() + "/" + p(d.getMonth() + 1) + "/" + p(d.getDate()) +
    " " + p(d.getHours()) + ":" + p(d.getMinutes());
}
function explorerChildren(snapshot, path) {
  var prefix = path ? path + "/" : "";
  var out = [];
  (snapshot.entries || []).forEach(function (e) {
    if (e.path.indexOf(prefix) !== 0) return;
    var rest = e.path.slice(prefix.length);
    if (!rest || rest.indexOf("/") !== -1) return;
    out.push(e);
  });
  return out;
}
function explorerSearch(snapshot, query) {
  var q = String(query || "").toLowerCase();
  return (snapshot.entries || []).filter(function (e) {
    var name = e.path.split("/").pop().toLowerCase();
    return name.indexOf(q) !== -1;
  });
}
function explorerSort(entries) {
  return entries.slice().sort(function (a, b) {
    if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
    var an = a.path.split("/").pop(), bn = b.path.split("/").pop();
    return an.localeCompare(bn, "zh-Hant");
  });
}
function countInside(snapshot, dirPath) {
  var prefix = dirPath + "/";
  var n = 0;
  (snapshot.entries || []).forEach(function (e) {
    if (e.path.indexOf(prefix) === 0 && !e.is_dir) n++;
  });
  return n;
}
function explorerBadgeText() {
  if (!explorer.snapshot) return "尚未載入";
  var gen = explorer.snapshot.generated_at;
  if (explorer.online && gen) {
    var age = (Date.now() - new Date(gen).getTime()) / 1000;
    if (age <= 120) return "即時";
  }
  return "快照：" + formatDateTime(gen);
}
function isHeartbeatFresh(hb) {
  if (!hb || hb.status === "never" || !hb.updated_at) return false;
  return (Date.now() - new Date(hb.updated_at).getTime()) / 1000 <= HEARTBEAT_FRESH_SECS;
}

/* 載入快照：電腦在線且要求即時時，先寫 list_request.json 等新快照 */
function refreshExplorer(requestFresh) {
  var p = pat();
  if (!p || !state.adminAuthed) return Promise.resolve();
  return ghGetJson(p, "heartbeat.json").then(function (r) {
    explorer.online = isHeartbeatFresh(r.data);
    if (r.data && r.data.session_code) state.sessionCode = r.data.session_code;
    if (explorer.online && requestFresh) {
      return requestFreshSnapshot().then(function () { return loadSnapshot(); });
    }
    return loadSnapshot();
  }).catch(function () {
    explorer.online = false;
    return loadSnapshot();
  });
}
function loadSnapshot() {
  var p = pat();
  return ghGetJson(p, "snapshot.json").then(function (r) {
    explorer.snapshot = r.data || null;
    renderExplorer();
  }).catch(function () {
    explorer.snapshot = null;
    renderExplorer();
  });
}
function requestFreshSnapshot() {
  var p = pat();
  var requestId = "lr-" + Date.now().toString(36) + "-" +
    Math.random().toString(36).slice(2, 8);
  var requestedAt = utcnow();
  return ghPutJson(p, "list_request.json",
    { request_id: requestId, requested_at: requestedAt, by: "admin" },
    "AI File：要求重新整理快照").then(function () {
      // 等 snapshot.json 的 generated_at 比要求時間新，最多等 30 秒
      var start = Date.now();
      return new Promise(function (resolve) {
        function poll() {
          ghGetJson(p, "snapshot.json").then(function (r) {
            if (r.data && r.data.generated_at &&
                r.data.generated_at >= requestedAt) return resolve(true);
            if (Date.now() - start > EXPLORER_WAIT_MS) return resolve(false);
            setTimeout(poll, 2000);
          }).catch(function () {
            if (Date.now() - start > EXPLORER_WAIT_MS) return resolve(false);
            setTimeout(poll, 2000);
          });
        }
        poll();
      });
    }).then(function (ok) {
      if (!ok) notice("電腦沒回應，可能沒開。先顯示上次的快照。", "err");
      return ok;
    });
}

/* 渲染：麵包屑、列表、搜尋、狀態徽章 */
function renderExplorer() {
  var badge = $("explorer-status");
  var count = $("explorer-count");
  var crumbs = $("explorer-crumbs");
  var tbody = $("explorer-list");
  if (!explorer.snapshot) {
    badge.textContent = "尚未載入";
    count.textContent = "";
    crumbs.innerHTML = "";
    tbody.innerHTML = "<tr><td colspan='7' class='hint'>還沒有快照。請確認電腦上的 AI File 已經啟動過一次。</td></tr>";
    return;
  }
  badge.textContent = explorerBadgeText();
  if (explorer.snapshot.truncated) {
    count.textContent = "（檔案太多，只顯示前 2000 筆）";
  } else {
    count.textContent = "共 " + explorer.snapshot.total_files + " 個檔案、" +
      explorer.snapshot.total_dirs + " 個資料夾";
  }
  // 麵包屑
  crumbs.innerHTML = "";
  var root = document.createElement("a");
  root.href = "#";
  root.textContent = explorer.snapshot.folder_name || "全部檔案";
  root.addEventListener("click", function (e) {
    e.preventDefault();
    explorer.currentPath = ""; explorer.search = "";
    $("explorer-search").value = "";
    renderExplorer();
  });
  crumbs.appendChild(root);
  if (explorer.currentPath) {
    var parts = explorer.currentPath.split("/");
    var acc = "";
    parts.forEach(function (seg) {
      crumbs.appendChild(document.createTextNode(" / "));
      acc = acc ? acc + "/" + seg : seg;
      (function (p) {
        var a = document.createElement("a");
        a.href = "#"; a.textContent = seg;
        a.addEventListener("click", function (e) {
          e.preventDefault();
          explorer.currentPath = p;
          renderExplorer();
        });
        crumbs.appendChild(a);
      })(acc);
    });
  }
  // 列表
  tbody.innerHTML = "";
  var rows;
  var searching = !!explorer.search.trim();
  if (searching) {
    rows = explorerSort(explorerSearch(explorer.snapshot, explorer.search.trim()));
    if (!rows.length) {
      tbody.innerHTML = "<tr><td colspan='7' class='hint'>沒有符合的檔案</td></tr>";
      return;
    }
    count.textContent = "找到 " + rows.length + " 個符合的檔案";
  } else {
    rows = explorerSort(explorerChildren(explorer.snapshot, explorer.currentPath));
    if (!rows.length) {
      tbody.innerHTML = "<tr><td colspan='7' class='hint'>這個資料夾是空的</td></tr>";
      return;
    }
  }
  rows.forEach(function (e) { tbody.appendChild(explorerRow(e, searching)); });
  updateSelectionBar();
}
function explorerRow(e, searching) {
  var tr = document.createElement("tr");
  tr.className = "explorer-row";
  var tdCheck = document.createElement("td");
  if (explorer.multiselect) {
    var cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !!explorer.selected[e.path];
    cb.addEventListener("change", function () {
      if (cb.checked) explorer.selected[e.path] = true;
      else delete explorer.selected[e.path];
      updateSelectionBar();
    });
    tdCheck.appendChild(cb);
  }
  tr.appendChild(tdCheck);
  var name = e.path.split("/").pop();
  var tdName = document.createElement("td");
  tdName.className = "explorer-name";
  tdName.textContent = (e.is_dir ? "📁 " : "📄 ") + name;
  if (searching) {
    tdName.appendChild(document.createElement("br"));
    var meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = e.path;
    tdName.appendChild(meta);
  }
  tdName.addEventListener("click", function () {
    if (explorer.multiselect) {
      toggleSelect(e.path);
      renderExplorer();
      return;
    }
    if (e.is_dir) {
      explorer.currentPath = e.path;
      explorer.search = "";
      $("explorer-search").value = "";
      renderExplorer();
    }
  });
  tdName.addEventListener("dblclick", function () {
    if (!explorer.multiselect) openExplorerAction(e);
  });
  tr.appendChild(tdName);
  var tdExt = document.createElement("td");
  tdExt.textContent = e.is_dir ? "—" : (e.ext || "—");
  tr.appendChild(tdExt);
  var tdCreated = document.createElement("td");
  tdCreated.textContent = formatDateTime(e.created);
  tr.appendChild(tdCreated);
  var tdSize = document.createElement("td");
  tdSize.textContent = e.is_dir ? "—" : formatSize(e.size || 0);
  tr.appendChild(tdSize);
  var tdKind = document.createElement("td");
  tdKind.textContent = e.kind || "—";
  tr.appendChild(tdKind);
  var tdMore = document.createElement("td");
  var btn = document.createElement("button");
  btn.className = "btn btn-outline btn-small";
  btn.textContent = "⋯";
  btn.addEventListener("click", function () { openExplorerAction(e); });
  tdMore.appendChild(btn);
  tr.appendChild(tdMore);
  return tr;
}

/* 多選 */
function toggleSelect(path) {
  if (explorer.selected[path]) delete explorer.selected[path];
  else explorer.selected[path] = true;
  updateSelectionBar();
}
function selectedPaths() { return Object.keys(explorer.selected); }
function updateSelectionBar() {
  var bar = $("explorer-selection");
  var n = selectedPaths().length;
  if (explorer.multiselect && n > 0) {
    show(bar);
    $("explorer-selection-count").textContent = "已選 " + n + " 個檔案";
  } else {
    hide(bar);
  }
}
function setMultiselect(on) {
  explorer.multiselect = on;
  explorer.selected = {};
  $("btn-explorer-multiselect").textContent = on ? "取消多選" : "多選";
  renderExplorer();
}
function confirmMultiselect() {
  var paths = selectedPaths();
  if (!paths.length) return;
  var entries = paths.map(function (p) {
    return (explorer.snapshot.entries || []).find(function (e) { return e.path === p; });
  }).filter(Boolean);
  openExplorerModal("已選 " + entries.length + " 個項目", function (body) {
    var p = document.createElement("p");
    p.textContent = "要對這 " + entries.length + " 個項目做什麼？";
    body.appendChild(p);
    var ul = document.createElement("ul");
    entries.forEach(function (e) {
      var li = document.createElement("li");
      li.textContent = e.path;
      ul.appendChild(li);
    });
    body.appendChild(ul);
  }, [
    { text: "刪除", cls: "btn-danger", onClick: function () { closeExplorerModal(); confirmDeleteEntries(entries); } },
    { text: "重新命名（變更）", cls: "btn-primary", onClick: function () { closeExplorerModal(); openRenameDialog(entries); } },
    { text: "取消", onClick: closeExplorerModal }
  ]);
}

/* 通用對話框 */
function openExplorerModal(title, bodyBuilder, buttons) {
  $("explorer-modal-title").textContent = title;
  var body = $("explorer-modal-body");
  body.innerHTML = "";
  if (bodyBuilder) bodyBuilder(body);
  var btns = $("explorer-modal-buttons");
  btns.innerHTML = "";
  (buttons || []).forEach(function (b) {
    var btn = document.createElement("button");
    btn.className = "btn " + (b.cls || "btn-outline");
    btn.textContent = b.text;
    btn.addEventListener("click", b.onClick);
    btns.appendChild(btn);
  });
  show($("explorer-modal"));
}
function closeExplorerModal() { hide($("explorer-modal")); }

/* 操作選單：檔案（下載／編輯／刪除／多選）、資料夾（重新命名／刪除） */
function openExplorerAction(e) {
  var name = e.path.split("/").pop();
  if (e.is_dir) {
    openExplorerModal("資料夾：" + name, function (body) {
      var p = document.createElement("p");
      p.className = "hint";
      p.textContent = "路徑：" + e.path;
      body.appendChild(p);
    }, [
      { text: "重新命名", cls: "btn-primary", onClick: function () { closeExplorerModal(); openRenameDialog([e]); } },
      { text: "刪除", cls: "btn-danger", onClick: function () { closeExplorerModal(); confirmDeleteEntries([e]); } },
      { text: "取消", onClick: closeExplorerModal }
    ]);
  } else {
    openExplorerModal("檔案：" + name, function (body) {
      var p = document.createElement("p");
      p.className = "hint";
      p.textContent = "路徑：" + e.path + " · " + formatSize(e.size || 0) + " · " + (e.kind || "");
      body.appendChild(p);
    }, [
      { text: "下載", cls: "btn-primary", onClick: function () { closeExplorerModal(); downloadExplorerFile(e); } },
      { text: "編輯", onClick: function () { closeExplorerModal(); editExplorerFile(e); } },
      { text: "刪除", cls: "btn-danger", onClick: function () { closeExplorerModal(); confirmDeleteEntries([e]); } },
      { text: "多選", onClick: function () { closeExplorerModal(); setMultiselect(true); toggleSelect(e.path); renderExplorer(); } },
      { text: "取消", onClick: closeExplorerModal }
    ]);
  }
}

/* 管理員暫存（不走 AI 的工作階段檢查，但一樣要過停止旗標、
 * 一樣進 manifest、一樣等電腦按「同意」才套用） */
function checkAdminReady() {
  var p = pat();
  return ghGetJson(p, "stop.json").then(function (r) {
    if (r.data && r.data.stopped) throw new Error("目前是停止狀態，無法暫存。請先按「恢復」。");
    return true;
  });
}
function stageAdminEntry(entry, fileB64) {
  return queueStage(function () { return stageAdminEntryNow(entry, fileB64); });
}
function stageAdminEntryNow(entry, fileB64) {
  var p = pat();
  var putFile = entry.file
    ? ghPutFile(p, "pending/" + entry.file, fileB64, "AI File：管理員暫存 " + entry.rel_path)
    : Promise.resolve();
  return putFile.then(function () {
    return ghGetJson(p, "pending/manifest.json");
  }).then(function (r) {
    var manifest = (r.data && Array.isArray(r.data.files)) ? r.data : { files: [] };
    manifest.files.push(entry);
    return ghPutJson(p, "pending/manifest.json", manifest, "AI File：更新待同步清單");
  }).then(function () {
    return appendActivity(p, {
      action: entry.action, ai_name: "管理員",
      rel_path: entry.rel_path, detail: "管理員已暫存，等待電腦同意同步"
    });
  });
}
function makeAdminEntry(action, rel, extra) {
  var entry = {
    id: newStageId(),
    rel_path: rel,
    file: null,
    action: action,
    sha256: "",
    size: 0,
    ai_name: "管理員",
    by: "admin",
    time: utcnow(),
    session_code: state.sessionCode || ""
  };
  if (extra) for (var k in extra) entry[k] = extra[k];
  return entry;
}
function stageAdminFiles(fileList, dirPath) {
  if (!fileList || !fileList.length) return;
  checkAdminReady().then(function () {
    var jobs = [];
    Array.from(fileList).forEach(function (file) {
      jobs.push(new Promise(function (resolve, reject) {
        var rel;
        try {
          rel = sanitizeRelPath((dirPath ? dirPath + "/" : "") + file.name);
        } catch (e) { return reject(e); }
        var reader = new FileReader();
        reader.onload = function () {
          var bytes = new Uint8Array(reader.result);
          sha256Hex(bytes).then(function (hash) {
            var entry = makeAdminEntry("add", rel, {
              file: pendingFileName(rel, Date.now() % 1000, hash),
              sha256: hash, size: bytes.length
            });
            stageAdminEntry(entry, bytesToB64(bytes)).then(resolve).catch(reject);
          }).catch(reject);
        };
        reader.onerror = function () { reject(new Error("讀取檔案失敗：" + file.name)); };
        reader.readAsArrayBuffer(file);
      }));
    });
    return Promise.all(jobs);
  }).then(function () {
    notice("已暫存 " + fileList.length + " 個檔案到「" + (dirPath || "根目錄") + "」。電腦開著會跳出確認，等你按「同意」才會寫入；電腦沒開就先等著。", "ok");
    $("explorer-file-input").value = "";
    refreshPending();
  }).catch(function (e) { notice(String(e.message || e), "err"); });
}

/* 下載／編輯：向電腦索取檔案內容（content_request / content_response） */
function requestFileContent(rel) {
  var p = pat();
  var requestId = "cr-" + Date.now().toString(36) + "-" +
    Math.random().toString(36).slice(2, 8);
  return ghPutJson(p, "content_request.json",
    { request_id: requestId, path: rel, by: "admin", at: utcnow() },
    "AI File：要求讀取檔案內容").then(function () {
      var start = Date.now();
      return new Promise(function (resolve) {
        function poll() {
          ghGetJson(p, "content_response.json").then(function (r) {
            if (r.data && r.data.request_id === requestId) return resolve(r.data);
            if (Date.now() - start > EXPLORER_WAIT_MS) return resolve(null);
            setTimeout(poll, 2000);
          }).catch(function () {
            if (Date.now() - start > EXPLORER_WAIT_MS) return resolve(null);
            setTimeout(poll, 2000);
          });
        }
        poll();
      });
    });
}
function downloadExplorerFile(e) {
  if ((e.size || 0) > CONTENT_MAX_BYTES) {
    return notice("這個檔案超過 10 MB，無法透過網頁下載。", "err");
  }
  notice("正在向電腦索取檔案內容…", "info");
  requestFileContent(e.path).then(function (res) {
    if (!res) return notice("電腦沒開，無法讀取檔案內容。", "err");
    if (!res.ok) return notice("讀取失敗：" + (res.error || "未知錯誤"), "err");
    var bin = atob((res.content_b64 || "").replace(/\s/g, ""));
    var bytes = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    var blob = new Blob([bytes]);
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = e.path.split("/").pop();
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
    notice("已開始下載：" + e.path.split("/").pop(), "ok");
  }).catch(function (err) { notice(String(err.message || err), "err"); });
}
function editExplorerFile(e) {
  if (TEXT_EDIT_EXTS.indexOf((e.ext || "").toLowerCase()) === -1) {
    return notice("這個檔案不能在網頁上編輯，請下載後修改再上傳覆蓋。", "err");
  }
  if ((e.size || 0) > TEXT_EDIT_MAX_BYTES) {
    return notice("這個檔案超過 200 KB，不能在網頁上編輯，請下載後修改再上傳覆蓋。", "err");
  }
  notice("正在向電腦索取檔案內容…", "info");
  requestFileContent(e.path).then(function (res) {
    if (!res) return notice("電腦沒開，無法讀取檔案內容。", "err");
    if (!res.ok) return notice("讀取失敗：" + (res.error || "未知錯誤"), "err");
    var text;
    try { text = b64ToUtf8(res.content_b64 || ""); }
    catch (err) { return notice("這個檔案不是 UTF-8 文字，無法在網頁上編輯。", "err"); }
    openExplorerModal("編輯：" + e.path.split("/").pop(), function (body) {
      var ta = document.createElement("textarea");
      ta.id = "explorer-edit-text";
      ta.rows = 14;
      ta.value = text;
      body.appendChild(ta);
      var p = document.createElement("p");
      p.className = "hint";
      p.textContent = "儲存後會暫存覆蓋同一路徑，電腦上按「同意」才會真的寫入。";
      body.appendChild(p);
    }, [
      { text: "儲存", cls: "btn-primary", onClick: function () {
          var newText = $("explorer-edit-text").value;
          closeExplorerModal();
          saveExplorerEdit(e.path, newText);
        } },
      { text: "取消", onClick: closeExplorerModal }
    ]);
  }).catch(function (err) { notice(String(err.message || err), "err"); });
}
function saveExplorerEdit(rel, text) {
  checkAdminReady().then(function () {
    var bytes = new TextEncoder().encode(text);
    return sha256Hex(bytes).then(function (hash) {
      var entry = makeAdminEntry("add", rel, {
        file: pendingFileName(rel, Date.now() % 1000, hash),
        sha256: hash, size: bytes.length
      });
      return stageAdminEntry(entry, bytesToB64(bytes));
    });
  }).then(function () {
    notice("已暫存修改：" + rel + "。電腦上按「同意」後才會覆蓋。", "ok");
    refreshPending();
  }).catch(function (e) { notice(String(e.message || e), "err"); });
}

/* 刪除：資料夾要明確顯示裡面有幾個檔案 */
function confirmDeleteEntries(entries) {
  var lines = entries.map(function (e) {
    if (e.is_dir) {
      var n = explorer.snapshot ? countInside(explorer.snapshot, e.path) : 0;
      return "資料夾「" + e.path + "」（裡面有 " + n + " 個檔案，整個刪掉）";
    }
    return "「" + e.path + "」";
  });
  if (!confirm("確定要刪除以下項目嗎？\n" + lines.join("\n") +
      "\n\n這只是「暫存刪除要求」，電腦上按「同意」後才會真的刪除。")) return;
  checkAdminReady().then(function () {
    var jobs = entries.map(function (e) {
      var entry = makeAdminEntry("delete", e.path, { is_dir: !!e.is_dir });
      return stageAdminEntry(entry, null);
    });
    return Promise.all(jobs);
  }).then(function () {
    notice("已暫存 " + entries.length + " 筆刪除要求，電腦上按「同意」後才會真的刪除。", "ok");
    refreshPending();
    setMultiselect(false);
  }).catch(function (e) { notice(String(e.message || e), "err"); });
}

/* 重新命名：每個選到的項目各一個輸入框，預填目前檔名 */
function openRenameDialog(entries) {
  var inputs = [];
  openExplorerModal(entries.length === 1 ? "重新命名" : "重新命名（" + entries.length + " 個）", function (body) {
    entries.forEach(function (e) {
      var name = e.path.split("/").pop();
      var parent = e.path.indexOf("/") === -1 ? "" : e.path.slice(0, e.path.lastIndexOf("/"));
      var label = document.createElement("label");
      label.textContent = e.path;
      body.appendChild(label);
      var input = document.createElement("input");
      input.type = "text";
      input.value = name;
      input.setAttribute("data-parent", parent);
      input.setAttribute("data-old", e.path);
      body.appendChild(input);
      inputs.push(input);
    });
    var p = document.createElement("p");
    p.className = "hint";
    p.textContent = "檔名不能有 / 或 \\，也不能是 ..。確定後會暫存重新命名要求，電腦上按「同意」才會真的改名。";
    body.appendChild(p);
  }, [
    { text: "確定重新命名", cls: "btn-primary", onClick: function () {
        var renames = [];
        for (var i = 0; i < inputs.length; i++) {
          var input = inputs[i];
          var newName = input.value.trim();
          if (!newName || newName.indexOf("/") !== -1 ||
              newName.indexOf("\\") !== -1 || newName === ".." || newName === ".") {
            notice("檔名不合法：" + (input.value || "（空白）"), "err");
            return;
          }
          var parent = input.getAttribute("data-parent");
          var oldPath = input.getAttribute("data-old");
          var newRel = parent ? parent + "/" + newName : newName;
          if (newRel === oldPath) continue;
          renames.push({ oldPath: oldPath, newRel: newRel });
        }
        if (!renames.length) { closeExplorerModal(); return; }
        closeExplorerModal();
        stageRenames(renames);
      } },
    { text: "取消", onClick: closeExplorerModal }
  ]);
}
function stageRenames(renames) {
  checkAdminReady().then(function () {
    var jobs = renames.map(function (r) {
      var entry = makeAdminEntry("rename", r.oldPath, { new_rel: r.newRel });
      return stageAdminEntry(entry, null);
    });
    return Promise.all(jobs);
  }).then(function () {
    notice("已暫存 " + renames.length + " 筆重新命名，電腦上按「同意」後才會真的改名。", "ok");
    refreshPending();
    setMultiselect(false);
  }).catch(function (e) { notice(String(e.message || e), "err"); });
}

/* ---------- AI 登入 ---------- */
function aiLogin() {
  var name = $("ai-name").value.trim();
  var code = $("ai-code").value.trim().toUpperCase();
  if (!name) return notice("請輸入 AI 名稱。", "err");
  if (!code) return notice("請輸入工作階段代碼。", "err");
  var p = pat();
  if (!p) return notice("這個瀏覽器還沒設定 GitHub 金鑰，請管理員先在上方設定（只需設定一次）；或直接把工作階段代碼在對話裡給 Muse，Muse 會用自己的連線處理。", "err");
  ghGetJson(p, "session.json").then(function (r) {
    if (!r.data || !r.data.code_hash) {
      throw new Error("本地端尚未產生工作階段代碼，請先在 Windows 電腦上完成 AI File 的第一次設定。");
    }
    return sha256Hex(code).then(function (h) {
      if (h !== r.data.code_hash) throw new Error("工作階段代碼不對，請跟使用者確認最新的一組代碼。");
      return ghPutJson(p, "session.json", {
        status: "waiting",
        ai_name: name,
        code_hash: r.data.code_hash,
        created_at: utcnow()
      }, "AI File：" + name + " 請求連線");
    });
  }).then(function () {
    state.ai = { name: name, code: code };
    try {
      // 只存名稱和代碼，下次開頁面只預填欄位，絕對不自動還原登入
      localStorage.setItem("aifile_ai", JSON.stringify(state.ai));
    } catch (e) { /* 存不下來也沒關係，下次再填一次就好 */ }
    notice("已送出連線請求，請等使用者在他的電腦上按「是」。", "info");
    $("ai-session-state").textContent = "工作階段狀態：等待使用者按「是」…";
    pollSession();
  }).catch(function (e) { notice(String(e.message || e), "err"); });
}
function pollSession() {
  if (state.sessionPoll) clearInterval(state.sessionPoll);
  state.sessionPoll = setInterval(function () {
    if (!state.ai) { clearInterval(state.sessionPoll); return; }
    ghGetJson(pat(), "session.json").then(function (r) {
      var s = r.data || {};
      if (s.status === "approved") {
        clearInterval(state.sessionPoll);
        $("ai-session-state").textContent = "工作階段狀態：已允許，可以開始暫存檔案。";
        show($("ai-panel"));
        showStatusCard();
        startStatusTimer();
        refreshStatus();
        notice("使用者已按「是」，可以開始暫存檔案了。", "ok");
      } else if (s.status === "stopped") {
        clearInterval(state.sessionPoll);
        $("ai-session-state").textContent = "工作階段狀態：已被拒絕或停止。";
        notice("連線請求被拒絕或已停止。", "err");
      } else {
        $("ai-session-state").textContent = "工作階段狀態：等待使用者按「是」…";
      }
    }).catch(function () { /* 下一輪再試 */ });
  }, 5000);
}

/* ---------- 暫存檔案 ---------- */
function checkStagingReady() {
  var p = pat();
  return ghGetJson(p, "stop.json").then(function (r) {
    if (r.data && r.data.stopped) throw new Error("目前是停止狀態，無法暫存。請使用者先恢復。");
    return ghGetJson(p, "session.json");
  }).then(function (r) {
    if (!r.data || r.data.status !== "approved") {
      throw new Error("工作階段尚未被允許，請等使用者在電腦上按「是」。");
    }
    return true;
  });
}
function sanitizeRelPath(rel) {
  rel = String(rel || "").replace(/\\/g, "/").trim();
  if (!rel) throw new Error("請填寫相對路徑。");
  if (rel.charAt(0) === "/" || rel.charAt(0) === "~") throw new Error("請用相對路徑，不要用絕對路徑。");
  if (rel.length >= 2 && rel.charAt(1) === ":") throw new Error("相對路徑不能有磁碟機代號。");
  var parts = [];
  rel.split("/").forEach(function (seg) {
    if (seg === "" || seg === ".") return;
    if (seg === "..") throw new Error("相對路徑不能包含 ..（會跳出允許的資料夾）。");
    parts.push(seg);
  });
  if (!parts.length) throw new Error("請填寫相對路徑。");
  return parts.join("/");
}
function pendingFileName(rel, index, sha) {
  var base = rel.split("/").pop().replace(/ /g, "_").replace(/[^A-Za-z0-9._-]/g, "") || "file";
  return String(index).padStart(3, "0") + "_" + sha.slice(0, 12) + "_" + base;
}
function newStageId() {
  return "st-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
}
// 暫存寫入要排隊：manifest 和 activity.json 都是讀出來、改好、寫回去，
// 一次上傳多個檔案時如果並行寫，後寫的會蓋掉先寫的（少一筆）。
var stageQueue = Promise.resolve();
function queueStage(fn) {
  var run = function () { return fn(); };
  stageQueue = stageQueue.then(run, run);
  return stageQueue;
}
function stageEntry(entry, fileB64) {
  // entry: {id, rel_path, action, sha256, size, file(pending 名稱或 null)}
  return queueStage(function () { return stageEntryNow(entry, fileB64); });
}
function stageEntryNow(entry, fileB64) {
  var p = pat();
  var putFile = entry.file
    ? ghPutFile(p, "pending/" + entry.file, fileB64, "AI File：" + state.ai.name + " 暫存 " + entry.rel_path)
    : Promise.resolve();
  return putFile.then(function () {
    return ghGetJson(p, "pending/manifest.json");
  }).then(function (r) {
    var manifest = (r.data && Array.isArray(r.data.files)) ? r.data : { files: [] };
    manifest.files.push(entry);
    return ghPutJson(p, "pending/manifest.json", manifest, "AI File：更新待同步清單");
  }).then(function () {
    return appendActivity(p, {
      action: entry.action, ai_name: state.ai.name,
      rel_path: entry.rel_path, detail: "已暫存，等待使用者同意同步"
    });
  });
}
function stageFiles(fileList, relPathHint) {
  if (!state.ai) return notice("請先完成 AI 登入。", "err");
  if (!fileList || !fileList.length) return notice("請先選擇要上傳的檔案。", "err");
  checkStagingReady().then(function () {
    var jobs = [];
    Array.from(fileList).forEach(function (file, i) {
      jobs.push(new Promise(function (resolve, reject) {
        var rel;
        try {
          var baseName = relPathHint ? sanitizeRelPath(relPathHint) : null;
          // 如果只選一個檔案且有填路徑，就用那個路徑；多檔時路徑是資料夾
          rel = (fileList.length === 1 && baseName) ? baseName
            : (baseName ? baseName + "/" + file.name : file.name);
          rel = sanitizeRelPath(rel);
        } catch (e) { return reject(e); }
        var reader = new FileReader();
        reader.onload = function () {
          var bytes = new Uint8Array(reader.result);
          sha256Hex(bytes).then(function (hash) {
            var entry = {
              id: newStageId(),
              rel_path: rel,
              file: pendingFileName(rel, Date.now() % 1000, hash),
              action: "add",
              sha256: hash,
              size: bytes.length,
              ai_name: state.ai.name,
              time: utcnow(),
              session_code: state.ai.code
            };
            stageEntry(entry, bytesToB64(bytes)).then(resolve).catch(reject);
          }).catch(reject);
        };
        reader.onerror = function () { reject(new Error("讀取檔案失敗：" + file.name)); };
        reader.readAsArrayBuffer(file);
      }));
    });
    return Promise.all(jobs);
  }).then(function () {
    notice("已暫存 " + fileList.length + " 個檔案，使用者在電腦上按「同意」後才會寫入。", "ok");
    $("file-input").value = "";
    refreshMyStaged();
    if (state.adminAuthed) refreshPending();
  }).catch(function (e) { notice(String(e.message || e), "err"); });
}
function stagePaste() {
  if (!state.ai) return notice("請先完成 AI 登入。", "err");
  var name = $("paste-name").value.trim();
  var content = $("paste-content").value;
  var rel;
  try { rel = sanitizeRelPath(name); }
  catch (e) { return notice(String(e.message || e), "err"); }
  if (!content) return notice("請先貼上檔案內容。", "err");
  checkStagingReady().then(function () {
    var bytes = new TextEncoder().encode(content);
    return sha256Hex(bytes).then(function (hash) {
      var entry = {
        id: newStageId(),
        rel_path: rel,
        file: pendingFileName(rel, Date.now() % 1000, hash),
        action: "add",
        sha256: hash,
        size: bytes.length,
        ai_name: state.ai.name,
        time: utcnow(),
        session_code: state.ai.code
      };
      return stageEntry(entry, bytesToB64(bytes));
    });
  }).then(function () {
    notice("已暫存 " + rel + "，使用者在電腦上按「同意」後才會寫入。", "ok");
    $("paste-name").value = ""; $("paste-content").value = "";
    refreshMyStaged();
    if (state.adminAuthed) refreshPending();
  }).catch(function (e) { notice(String(e.message || e), "err"); });
}
function stageDelete() {
  if (!state.ai) return notice("請先完成 AI 登入。", "err");
  var rel;
  try { rel = sanitizeRelPath($("delete-path").value.trim()); }
  catch (e) { return notice(String(e.message || e), "err"); }
  if (!confirm("確定要請使用者刪除「" + rel + "」嗎？使用者在電腦上按「同意」後才會真的刪除。")) return;
  checkStagingReady().then(function () {
    var entry = {
      id: newStageId(),
      rel_path: rel, file: null, action: "delete", sha256: "", size: 0,
      ai_name: state.ai.name, time: utcnow(), session_code: state.ai.code
    };
    return stageEntry(entry, null);
  }).then(function () {
    notice("已暫存刪除要求：" + rel + "。", "ok");
    $("delete-path").value = "";
    refreshMyStaged();
    if (state.adminAuthed) refreshPending();
  }).catch(function (e) { notice(String(e.message || e), "err"); });
}
function refreshMyStaged() {
  if (!state.ai) return;
  ghGetJson(pat(), "pending/manifest.json").then(function (r) {
    var files = ((r.data && r.data.files) || []).filter(function (f) {
      return f.ai_name === state.ai.name;
    });
    var ul = $("my-staged-list");
    ul.innerHTML = "";
    if (!files.length) {
      ul.innerHTML = "<li class='hint'>還沒有暫存任何檔案。</li>";
      return;
    }
    files.forEach(function (f) {
      var li = document.createElement("li");
      var action = { add: "新增", modify: "修改", delete: "刪除" }[f.action || "add"] || "新增";
      li.textContent = "[" + action + "] " + f.rel_path;
      var meta = document.createElement("span");
      meta.className = "meta";
      meta.textContent = (f.time || "") + " · 等待使用者同意同步";
      li.appendChild(meta);
      var actions = document.createElement("span");
      actions.className = "actions";
      actions.appendChild(cancelButton(f, false));
      li.appendChild(actions);
      ul.appendChild(li);
    });
  }).catch(function () { /* 忽略 */ });
}

/* ---------- 事件綁定 ---------- */
$("btn-enter-ai").addEventListener("click", enterAi);
$("btn-enter-admin").addEventListener("click", enterAdmin);
$("btn-help").addEventListener("click", openHelp);
$("help-close").addEventListener("click", closeHelp);
$("help-backdrop").addEventListener("click", closeHelp);
document.addEventListener("keydown", function (e) {
  if (e.key === "Escape") { closeHelp(); closeExplorerModal(); }
});
$("btn-logout").addEventListener("click", function () {
  state.adminAuthed = false; state.ai = null;
  try { localStorage.removeItem("aifile_ai"); } catch (e) {}
  if (state.sessionPoll) { clearInterval(state.sessionPoll); state.sessionPoll = null; }
  stopStatusTimer();
  explorer.snapshot = null; explorer.currentPath = ""; explorer.search = "";
  explorer.multiselect = false; explorer.selected = {}; explorer.online = false;
  showLanding();
  $("ai-name").value = "";
  $("ai-code").value = "";
  updatePatState();
  notice("已登出。GitHub 金鑰仍留在瀏覽器裡（管理員設定），下次不用重設。", "info");
});
$("btn-setup-admin").addEventListener("click", setupAdmin);
$("btn-login-admin").addEventListener("click", loginAdmin);
$("btn-save-pat").addEventListener("click", savePatKey);
$("btn-clear-pat").addEventListener("click", clearPatKey);
$("btn-test-conn").addEventListener("click", testConnection);
$("btn-regen").addEventListener("click", regenCode);
$("btn-stop").addEventListener("click", function () { setStopped(true); });
$("btn-resume").addEventListener("click", function () { setStopped(false); });
$("btn-refresh-activity").addEventListener("click", refreshActivity);
$("btn-login-ai").addEventListener("click", aiLogin);
$("btn-stage-file").addEventListener("click", function () {
  stageFiles($("file-input").files, $("rel-path").value.trim());
});
$("btn-stage-paste").addEventListener("click", stagePaste);
$("btn-stage-delete").addEventListener("click", stageDelete);
$("explorer-search").addEventListener("input", function () {
  explorer.search = $("explorer-search").value;
  renderExplorer();
});
$("btn-explorer-refresh").addEventListener("click", function () { refreshExplorer(true); });
$("btn-explorer-upload").addEventListener("click", function () { $("explorer-file-input").click(); });
$("explorer-file-input").addEventListener("change", function () {
  stageAdminFiles($("explorer-file-input").files, explorer.currentPath);
});
$("btn-explorer-multiselect").addEventListener("click", function () { setMultiselect(!explorer.multiselect); });
$("btn-explorer-confirm").addEventListener("click", confirmMultiselect);
$("btn-explorer-cancel").addEventListener("click", function () { setMultiselect(false); });
$("explorer-modal-close").addEventListener("click", closeExplorerModal);
$("explorer-modal-backdrop").addEventListener("click", closeExplorerModal);

/* ---------- 啟動 ---------- */
// 只把名稱和代碼預填進欄位，絕對不自動還原已允許的工作階段：
// 使用者每次都要再按一次「AI 登入」，重新寫 status "waiting" 等電腦按「是」。
try {
  var savedAi = JSON.parse(localStorage.getItem("aifile_ai") || "null");
  if (savedAi && savedAi.name && savedAi.code) {
    $("ai-name").value = savedAi.name;
    $("ai-code").value = savedAi.code;
  }
} catch (e) { /* 忽略損壞的暫存 */ }
updatePatState();
if (!storageAvailable()) {
  var lp = $("landing-pat-state");
  if (lp) lp.textContent = "無法儲存（這個瀏覽器不保留網站資料）";
  notice("這個瀏覽器無法儲存 GitHub 金鑰（可能是無痕模式或已停用網站資料儲存），關掉後要重新設定。請改用一般視窗的 Chrome 或 Edge。", "err");
}
// 注意：這裡不呼叫 refreshStatus、也不啟動定時刷新；連線狀態要登入後才出現。
