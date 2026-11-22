/* AI File v1.2 — 網頁版
 * 靜態 SPA，跑在 GitHub Pages 上，沒有後端伺服器。
 * GitHub 金鑰只由管理員設定一次，存在瀏覽器 localStorage（aifile_pat）；
 * AI 登入只要工作階段代碼，不用再貼金鑰。
 * 絕對不要把任何 Token、密碼寫死在程式碼裡。
 * 網頁只能把檔案「暫存」到私人中繼 repo，真正寫進電腦的動作
 * 永遠由本地端程式在使用者按「同意」後才做。
 */
"use strict";

var RELAY_OWNER = "Pagani-pagani";
var RELAY_REPO = "ai-file-relay";
var API_BASE = "https://api.github.com/repos/" + RELAY_OWNER + "/" + RELAY_REPO + "/contents";
var HEARTBEAT_FRESH_SECS = 90;

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

/* ---------- 狀態 ---------- */
var state = {
  pat: localStorage.getItem("aifile_pat") || "",
  adminAuthed: false,
  ai: null,          // {name, code}：金鑰不再存在 AI 登入裡
  sessionPoll: null,
  statusTimer: null
};
try {
  var savedAi = JSON.parse(localStorage.getItem("aifile_ai") || "null");
  if (savedAi && savedAi.name && savedAi.code) {
    // v1.2：舊版存過 pat 的瀏覽器自動清掉，只留名稱和代碼
    if (savedAi.pat) {
      delete savedAi.pat;
      localStorage.setItem("aifile_ai", JSON.stringify(savedAi));
    }
    state.ai = { name: savedAi.name, code: savedAi.code };
  }
} catch (e) { /* 忽略損壞的暫存 */ }

// 金鑰只從管理員設定讀（localStorage aifile_pat），AI 登入不再帶金鑰
function pat() { return state.pat || ""; }
function savePat(p) {
  state.pat = p;
  localStorage.setItem("aifile_pat", p);
}

/* ---------- 連線狀態（每 30 秒讀 heartbeat.json） ---------- */
function refreshStatus() {
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
    if (hb.session_code) $("session-code").textContent = hb.session_code;
  }).catch(function (e) {
    setStatus("red", "連線失敗", String(e.message || e));
  });
}
function setStatus(color, text, hint) {
  $("status-dot").className = "dot dot-" + color;
  $("status-text").textContent = text;
  $("status-hint").textContent = hint || "";
}

/* ---------- 管理員：帳號密碼（只鎖這個瀏覽器的介面） ---------- */
function adminHashExists() { return !!localStorage.getItem("aifile_admin_hash"); }
function openAdminAuth() {
  hide($("ai-auth")); hide($("ai-panel"));
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
    localStorage.setItem("aifile_admin_user", user);
    localStorage.setItem("aifile_admin_hash", h);
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
  var savedUser = localStorage.getItem("aifile_admin_user");
  var savedHash = localStorage.getItem("aifile_admin_hash");
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
  var el = $("pat-state");
  if (el) el.textContent = state.pat ? "已設定" : "未設定";
}
function savePatKey() {
  var v = $("admin-pat-setup").value.trim();
  if (!v) return notice("請先貼上 GitHub 細粒度 Token。", "err");
  savePat(v);
  $("admin-pat-setup").value = "";
  updatePatState();
  notice("GitHub 金鑰已設定（只存在這個瀏覽器）。之後 AI 登入只要工作階段代碼，不用再貼金鑰。", "ok");
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
  updatePatState();
  refreshStatus();
  refreshPending();
  refreshActivity();
  refreshStopState();
  if (!state.statusTimer) {
    state.statusTimer = setInterval(function () {
      refreshStatus(); refreshPending(); refreshActivity(); refreshStopState();
    }, 30000);
  }
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
      ul.appendChild(li);
    });
  }).catch(function () { /* 忽略，狀態列會顯示 */ });
}
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
    list.slice(-50).reverse().forEach(function (e) {
      var li = document.createElement("li");
      var action = {
        add: "新增", modify: "修改", delete: "刪除",
        "session-approved": "已允許連線", "session-rejected": "已拒絕連線",
        "rejected-path": "已拒絕越界", stopped: "已停止",
        "code-regenerated": "代碼已重新生成", synced: "已同步",
        "sync-failed": "同步失敗"
      }[e.action] || e.action;
      li.textContent = "[" + action + "] " + (e.rel_path || e.detail || "");
      var meta = document.createElement("span");
      meta.className = "meta";
      meta.textContent = (e.ai_name || "") + " · " + (e.at || "");
      li.appendChild(meta);
      ul.appendChild(li);
    });
  }).catch(function () { /* 忽略 */ });
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
    localStorage.setItem("aifile_ai", JSON.stringify(state.ai));
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
function stageEntry(entry, fileB64) {
  // entry: {rel_path, action, sha256, size, file(pending 名稱或 null)}
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
      ul.appendChild(li);
    });
  }).catch(function () { /* 忽略 */ });
}

/* ---------- 事件綁定 ---------- */
$("btn-admin").addEventListener("click", function () {
  if (state.adminAuthed) {
    hide($("ai-auth")); hide($("ai-panel")); hide($("admin-auth"));
    show($("admin-panel")); loadAdminData();
  } else {
    openAdminAuth();
  }
});
$("btn-ai").addEventListener("click", function () {
  hide($("admin-auth")); hide($("admin-panel"));
  if (state.ai) {
    show($("ai-panel"));
    $("ai-name").value = state.ai.name;
    $("ai-code").value = state.ai.code;
    $("ai-session-state").textContent = "工作階段狀態：已登入（" + state.ai.name + "）";
    refreshMyStaged(); pollSession();
  } else {
    show($("ai-auth"));
  }
});
$("btn-logout").addEventListener("click", function () {
  state.adminAuthed = false; state.ai = null;
  localStorage.removeItem("aifile_ai");
  if (state.sessionPoll) clearInterval(state.sessionPoll);
  hide($("admin-panel")); hide($("ai-panel")); hide($("admin-auth")); hide($("ai-auth"));
  notice("已登出。GitHub 金鑰仍留在瀏覽器裡（管理員設定），下次不用重設。", "info");
});
$("btn-setup-admin").addEventListener("click", setupAdmin);
$("btn-login-admin").addEventListener("click", loginAdmin);
$("btn-save-pat").addEventListener("click", savePatKey);
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

/* ---------- 啟動 ---------- */
if (state.ai) {
  show($("btn-logout"));
  show($("ai-panel"));
  $("ai-name").value = state.ai.name;
  $("ai-code").value = state.ai.code;
  $("ai-session-state").textContent = "工作階段狀態：已登入（" + state.ai.name + "）";
  refreshMyStaged();
  pollSession();
}
if (adminHashExists() && state.pat) { /* 管理員已設定過，點右上角登入即可 */ }
updatePatState();
refreshStatus();
state.statusTimer = state.statusTimer || setInterval(refreshStatus, 30000);
