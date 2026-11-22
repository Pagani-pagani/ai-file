# -*- coding: utf-8 -*-
"""
AI File 本地端程式 v1
===================
讓 Muse 把檔案直接送到你的 Windows 電腦。
架構：網頁版只能把檔案暫存到私人中繼 repo（ai-file-relay），
本地端程式輪詢中繼 repo，只在你按「同意」後才把檔案寫進允許的資料夾。
電腦才是守門員，網頁永遠碰不到你的檔案。

只用 Python 標準函式庫（urllib / tkinter / json / hashlib / base64），不用 pip。

用法：
    python ai_file_agent.py            # 正常啟動（沒設定過會先開設定精靈）
    python ai_file_agent.py --setup    # 只開第一次設定精靈
    python ai_file_agent.py --once     # 只輪詢一次（測試用）
    python ai_file_agent.py --check    # 不開視窗，跑一次核心檢查
"""

import base64
import hashlib
import json
import os
import secrets
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone

VERSION = "1"
RELAY_OWNER = "Pagani-pagani"
RELAY_REPO = "ai-file-relay"
GITHUB_API = "https://api.github.com"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "ai-file-config.json")
LOG_FILE = os.path.join(BASE_DIR, "ai-file-log.txt")

POLL_INTERVAL = 5        # 即時模式輪詢秒數
HEARTBEAT_INTERVAL = 30  # 心跳秒數
HEARTBEAT_FRESH_SECS = 90


# ---------------------------------------------------------------- 工具函式
def utcnow_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_session_code():
    """產生工作階段代碼，格式如 A1B2-C3D4（不用容易混淆的 0/O/1/I）。"""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    part = lambda: "".join(secrets.choice(alphabet) for _ in range(4))
    return part() + "-" + part()


def valid_session_code(code):
    if not isinstance(code, str):
        return False
    code = code.strip().upper()
    if len(code) != 9 or code[4] != "-":
        return False
    alphabet = set("ABCDEFGHJKLMNPQRSTUVWXYZ23456789")
    return all(c in alphabet for c in code.replace("-", ""))


def code_hash(code):
    return sha256_text(code.strip().upper())


def sanitize_rel_path(rel):
    """檢查並正規化相對路徑。不合法就丟 ValueError。

    拒絕：絕對路徑、磁碟機代號、.. 跳脫、空字串、NUL。
    回傳：以 / 分隔的乾淨相對路徑。
    """
    if not isinstance(rel, str) or not rel.strip():
        raise ValueError("路徑是空的")
    if "\x00" in rel:
        raise ValueError("路徑含有不合法字元")
    rel = rel.replace("\\", "/").strip()
    if rel.startswith("/") or rel.startswith("~"):
        raise ValueError("不接受絕對路徑：%s" % rel)
    if len(rel) >= 2 and rel[1] == ":":
        raise ValueError("不接受磁碟機代號：%s" % rel)
    parts = []
    for seg in rel.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            raise ValueError("路徑企圖跳出資料夾：%s" % rel)
        parts.append(seg)
    if not parts:
        raise ValueError("路徑是空的")
    return "/".join(parts)


def is_path_inside(base, target):
    """target 解析後的實際路徑是否落在 base 之內。"""
    base_real = os.path.realpath(base)
    target_real = os.path.realpath(target)
    try:
        return os.path.commonpath([base_real]) == os.path.commonpath(
            [base_real, target_real]
        )
    except ValueError:
        return False


def safe_join(base, rel):
    """把相對路徑接進允許的資料夾，越界就丟 ValueError。"""
    clean = sanitize_rel_path(rel)
    target = os.path.join(base, *clean.split("/"))
    if not is_path_inside(base, target):
        raise ValueError("路徑跳出允許的資料夾：%s" % rel)
    return target


def pending_filename(rel, index):
    """中繼 repo 裡 pending/ 底下的檔名：不直接用原始路徑，避免特殊字元問題。"""
    clean = sanitize_rel_path(rel)
    digest = sha256_text(clean)[:12]
    base = os.path.basename(clean).replace(" ", "_")
    base = "".join(c for c in base if c.isalnum() or c in ("-", "_", "."))
    if not base:
        base = "file"
    return "%03d_%s_%s" % (index, digest, base)


def log_local(message):
    line = "[%s] %s\n" % (utcnow_iso(), message)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass
    print(line, end="")


# ---------------------------------------------------------------- 中繼 repo 存取
class RelayError(Exception):
    pass


class RelayClient:
    """中繼 repo 的介面。get_json 回傳 (data, sha)，檔案不存在回傳 (None, None)。"""

    def get_json(self, path):
        raise NotImplementedError

    def get_bytes(self, path):
        raise NotImplementedError

    def put_json(self, path, data, message):
        raise NotImplementedError

    def put_bytes(self, path, data, message):
        raise NotImplementedError

    def delete(self, path, message):
        raise NotImplementedError


class GitHubRelay(RelayClient):
    """用 GitHub Contents API 讀寫私人中繼 repo。PAT 只從設定檔讀，絕不寫進程式碼。"""

    def __init__(self, pat, owner=RELAY_OWNER, repo=RELAY_REPO):
        self.pat = pat
        self.owner = owner
        self.repo = repo

    def _url(self, path):
        parts = "/".join(
            urllib.request.quote(seg, safe="") for seg in path.split("/")
        )
        return "%s/repos/%s/%s/contents/%s" % (
            GITHUB_API, self.owner, self.repo, parts)

    def _request(self, method, path, body=None):
        req = urllib.request.Request(self._url(path), method=method)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("Authorization", "Bearer " + self.pat)
        req.add_header("User-Agent", "AI-File-Agent/" + VERSION)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, data=data, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            detail = e.read().decode("utf-8", "replace")[:300]
            raise RelayError("GitHub API %s %s 失敗（%s）：%s" % (
                method, path, e.code, detail))
        except urllib.error.URLError as e:
            raise RelayError("連不上 GitHub：%s" % e.reason)

    def get_json(self, path):
        res = self._request("GET", path)
        if res is None:
            return None, None
        raw = base64.b64decode(res["content"])
        return json.loads(raw.decode("utf-8")), res.get("sha")

    def get_bytes(self, path):
        res = self._request("GET", path)
        if res is None:
            return None, None
        return base64.b64decode(res["content"]), res.get("sha")

    def _put(self, path, content_b64, message):
        # Contents API 更新檔案需要目前的 sha；先 GET 取一次（不存在就沒有 sha）
        res = self._request("GET", path)
        body = {"message": message, "content": content_b64}
        if res is not None:
            body["sha"] = res.get("sha")
        return self._request("PUT", path, body)

    def put_json(self, path, data, message):
        raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self._put(path, base64.b64encode(raw).decode("ascii"), message)

    def put_bytes(self, path, data, message):
        self._put(path, base64.b64encode(data).decode("ascii"), message)

    def delete(self, path, message):
        res = self._request("GET", path)
        if res is None:
            return
        self._request("DELETE", path, {"message": message, "sha": res.get("sha")})


class LocalRelay(RelayClient):
    """本機目錄模擬的中繼 repo，給測試與離線演練用，不連 GitHub。"""

    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _full(self, path):
        return os.path.join(self.root, *path.split("/"))

    def get_json(self, path):
        full = self._full(path)
        if not os.path.exists(full):
            return None, None
        with open(full, "rb") as f:
            raw = f.read()
        return json.loads(raw.decode("utf-8")), sha256_bytes(raw)

    def get_bytes(self, path):
        full = self._full(path)
        if not os.path.exists(full):
            return None, None
        with open(full, "rb") as f:
            raw = f.read()
        return raw, sha256_bytes(raw)

    def put_json(self, path, data, message):
        raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.put_bytes(path, raw, message)

    def put_bytes(self, path, data, message):
        full = self._full(path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(data)

    def delete(self, path, message):
        full = self._full(path)
        if os.path.exists(full):
            os.remove(full)


# ---------------------------------------------------------------- 設定檔
def load_config():
    if not os.path.exists(CONFIG_FILE):
        return None
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def append_activity(relay, entry):
    """把一筆紀錄 append 到中繼的 activity.json。"""
    try:
        data, _ = relay.get_json("activity.json")
    except Exception:
        data = None
    if not isinstance(data, list):
        data = []
    entry = dict(entry)
    entry.setdefault("at", utcnow_iso())
    data.append(entry)
    try:
        relay.put_json("activity.json", data, "AI File：新增活動紀錄")
    except Exception as e:
        log_local("寫入 activity.json 失敗：%s" % e)


# ---------------------------------------------------------------- UI（tkinter）
class TkUI:
    """桌面通知、確認對話框、浮動視窗。全部 tkinter，Windows 與 Linux 共用。"""

    def __init__(self, root, agent):
        self.root = root
        self.agent = agent
        self.float_win = None
        self.float_list = None
        self.float_status = None
        self.float_code = None

    # -- 通知與確認 ------------------------------------------------
    def notify(self, title, message):
        import tkinter.messagebox as mb
        try:
            self.root.attributes("-topmost", True)
            mb.showinfo(title, message, parent=self.root)
        except Exception:
            print("[通知] %s：%s" % (title, message))

    def ask_yes_no(self, title, message):
        import tkinter.messagebox as mb
        try:
            self.root.attributes("-topmost", True)
            return bool(mb.askyesno(title, message, parent=self.root))
        except Exception:
            return False

    def ask_sync(self, changes):
        """立即同步確認窗：列出變更清單，使用者按「同意」才回傳 True。"""
        import tkinter as tk
        from tkinter import ttk
        result = {"ok": False}
        win = tk.Toplevel(self.root)
        win.title("AI File：立即同步")
        win.attributes("-topmost", True)
        win.geometry("560x420")
        ttk.Label(win, text="Muse 想同步以下變更到你的電腦：",
                  font=("Microsoft JhengHei", 12, "bold")).pack(
                      anchor="w", padx=16, pady=(14, 6))
        ttk.Label(win, text="請先看過清單，確認沒問題再按「同意同步」。",
                  font=("Microsoft JhengHei", 10)).pack(
                      anchor="w", padx=16, pady=(0, 8))
        frame = ttk.Frame(win)
        frame.pack(fill="both", expand=True, padx=16)
        lb = tk.Listbox(frame, font=("Microsoft JhengHei", 11))
        lb.pack(fill="both", expand=True)
        for c in changes:
            action = {"add": "新增", "modify": "修改",
                      "delete": "刪除"}.get(c.get("action", "add"), "新增")
            lb.insert("end", "[%s] %s（%s，%s）" % (
                action, c.get("rel_path", "?"),
                c.get("ai_name", "?"), c.get("time", "")[:19]))
        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=16, pady=14)
        ttk.Button(btns, text="同意同步",
                   command=lambda: (result.update(ok=True), win.destroy())
                   ).pack(side="left", padx=(0, 10))
        ttk.Button(btns, text="稍後再說",
                   command=win.destroy).pack(side="left")
        self.root.wait_window(win)
        return result["ok"]

    # -- 浮動視窗 --------------------------------------------------
    def open_floating(self):
        import tkinter as tk
        from tkinter import ttk
        if self.float_win is not None:
            try:
                self.float_win.deiconify()
                self.float_win.lift()
                return
            except tk.TclError:
                self.float_win = None
        win = tk.Toplevel(self.root)
        win.title("AI File")
        win.attributes("-topmost", True)
        win.geometry("420x360+40+40")
        win.protocol("WM_DELETE_WINDOW", win.withdraw)
        ttk.Label(win, text="AI File — 正在監控",
                  font=("Microsoft JhengHei", 12, "bold")).pack(
                      anchor="w", padx=12, pady=(10, 2))
        self.float_status = ttk.Label(win, text="狀態：啟動中…",
                                      font=("Microsoft JhengHei", 10))
        self.float_status.pack(anchor="w", padx=12)
        self.float_code = ttk.Label(win, text="工作階段代碼：—",
                                    font=("Microsoft JhengHei", 10))
        self.float_code.pack(anchor="w", padx=12, pady=(0, 6))
        ttk.Label(win, text="最近的異動：",
                  font=("Microsoft JhengHei", 10, "bold")).pack(
                      anchor="w", padx=12)
        frame = ttk.Frame(win)
        frame.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        self.float_list = tk.Listbox(frame, font=("Microsoft JhengHei", 10))
        self.float_list.pack(fill="both", expand=True)
        stop_btn = tk.Button(
            win, text="■ 停止", font=("Microsoft JhengHei", 14, "bold"),
            bg="#c0392b", fg="white", height=2,
            command=self.agent.stop_from_local)
        stop_btn.pack(fill="x", padx=12, pady=(0, 12))
        self.float_win = win

    def add_activity(self, entry):
        if self.float_list is None:
            return
        try:
            action = {"add": "新增", "modify": "修改", "delete": "刪除",
                      "session-approved": "已允許連線",
                      "session-rejected": "已拒絕連線",
                      "rejected-path": "已拒絕越界",
                      "stopped": "已停止",
                      "synced": "已同步"}.get(entry.get("action", ""),
                                                entry.get("action", ""))
            text = "%s [%s] %s %s" % (
                entry.get("at", "")[11:19], entry.get("ai_name", ""),
                action, entry.get("rel_path", ""))
            self.float_list.insert(0, text.strip())
        except Exception:
            pass

    def set_status(self, text):
        if self.float_status is not None:
            try:
                self.float_status.config(text="狀態：" + text)
            except Exception:
                pass

    def set_code(self, code):
        if self.float_code is not None:
            try:
                self.float_code.config(text="工作階段代碼：" + code)
            except Exception:
                pass


class HeadlessUI:
    """沒有視窗時的 UI（測試用）：通知印出來，確認一律回傳預設值。"""

    def __init__(self, approve=False):
        self.approve = approve
        self.notices = []
        self.activities = []

    def notify(self, title, message):
        self.notices.append((title, message))
        print("[通知] %s：%s" % (title, message))

    def ask_yes_no(self, title, message):
        self.notices.append(("ask:" + title, message))
        return self.approve

    def ask_sync(self, changes):
        self.notices.append(("sync", [c.get("rel_path") for c in changes]))
        return self.approve

    def open_floating(self):
        pass

    def add_activity(self, entry):
        self.activities.append(entry)

    def set_status(self, text):
        print("[狀態] " + text)

    def set_code(self, code):
        print("[代碼] " + code)


# ---------------------------------------------------------------- Agent 主邏輯
class Agent:
    def __init__(self, config, relay, ui):
        self.config = config
        self.relay = relay
        self.ui = ui
        self.allowed_folder = config.get("allowed_folder", "")
        self.session_code = config.get("session_code", "")
        self.stopped = False
        self._last_manifest_sig = None
        self._seen_waiting = set()
        self._last_heartbeat = 0
        self._pending_notice_sig = None

    # -- 停止 ------------------------------------------------------
    def stop_from_local(self):
        self.stopped = True
        try:
            self.relay.put_json("stop.json", {
                "stopped": True, "by": "local", "at": utcnow_iso()},
                "AI File：本地端按下停止")
            append_activity(self.relay, {
                "action": "stopped", "ai_name": "",
                "detail": "使用者在本地端按下停止"})
        except Exception as e:
            log_local("寫入停止旗標失敗：%s" % e)
        self.ui.set_status("已停止 — Muse 動不到任何檔案")
        self.ui.notify("AI File 已停止",
                       "已經停止，Muse 現在動不到你的檔案。\n"
                       "要恢復請到網頁版管理員介面按「恢復」，或重新啟動 AI File。")
        log_local("使用者在本地端按下停止")

    # -- 心跳 ------------------------------------------------------
    def send_heartbeat(self, force=False):
        now = time.time()
        if not force and now - self._last_heartbeat < HEARTBEAT_INTERVAL:
            return
        self._last_heartbeat = now
        try:
            self.relay.put_json("heartbeat.json", {
                "status": "online" if not self.stopped else "stopped",
                "folder_name": os.path.basename(
                    os.path.normpath(self.allowed_folder)),
                "allowed_folder": self.allowed_folder,
                "session_code": self.session_code,
                "version": VERSION,
                "updated_at": utcnow_iso(),
            }, "AI File：心跳")
        except Exception as e:
            log_local("心跳寫入失敗：%s" % e)

    # -- 工作階段 --------------------------------------------------
    def _check_session(self):
        try:
            session, _ = self.relay.get_json("session.json")
        except Exception as e:
            log_local("讀取 session.json 失敗：%s" % e)
            return
        if not isinstance(session, dict):
            return
        status = session.get("status")
        if status != "waiting":
            return
        key = "%s|%s" % (session.get("ai_name", ""),
                         session.get("created_at", ""))
        if key in self._seen_waiting:
            return
        self._seen_waiting.add(key)
        ai_name = session.get("ai_name", "AI")
        ok = self.ui.ask_yes_no(
            "AI File：新的連線請求",
            "「%s」想連線到你的電腦，\n只能用你允許的資料夾：\n%s\n\n要允許嗎？" % (
                ai_name, self.allowed_folder))
        if ok:
            session["status"] = "approved"
            session["approved_at"] = utcnow_iso()
            self.relay.put_json("session.json", session,
                                "AI File：允許工作階段")
            append_activity(self.relay, {
                "action": "session-approved", "ai_name": ai_name})
            self.ui.notify("AI File", "已允許「%s」連線。" % ai_name)
            self.ui.add_activity({"action": "session-approved",
                                  "ai_name": ai_name, "at": utcnow_iso()})
            log_local("已允許工作階段：%s" % ai_name)
        else:
            session["status"] = "stopped"
            self.relay.put_json("session.json", session,
                                "AI File：拒絕工作階段")
            append_activity(self.relay, {
                "action": "session-rejected", "ai_name": ai_name})
            self.ui.notify("AI File", "已拒絕「%s」的連線請求。" % ai_name)
            log_local("已拒絕工作階段：%s" % ai_name)

    def _check_regen(self):
        try:
            regen, _ = self.relay.get_json("regen.json")
        except Exception:
            return
        if not isinstance(regen, dict) or not regen.get("requested"):
            return
        new_code = new_session_code()
        self.session_code = new_code
        self.config["session_code"] = new_code
        try:
            save_config(self.config)
        except OSError as e:
            log_local("儲存設定檔失敗：%s" % e)
        self.relay.put_json("session.json", {
            "status": "none", "code_hash": code_hash(new_code),
            "created_at": utcnow_iso()},
            "AI File：重新生成工作階段代碼")
        try:
            self.relay.delete("regen.json", "AI File：已處理重新生成請求")
        except Exception:
            pass
        append_activity(self.relay, {
            "action": "code-regenerated", "ai_name": "",
            "detail": "工作階段代碼已重新生成，舊代碼失效"})
        self.ui.set_code(new_code)
        self.ui.notify("AI File：代碼已重新生成",
                       "新的工作階段代碼是：\n%s\n\n舊代碼已經失效，請把新代碼給 Muse。" % new_code)
        log_local("工作階段代碼已重新生成：%s" % new_code)

    # -- 待同步 ----------------------------------------------------
    def _check_pending(self):
        try:
            manifest, _ = self.relay.get_json("pending/manifest.json")
        except Exception as e:
            log_local("讀取待同步清單失敗：%s" % e)
            return
        if not isinstance(manifest, dict):
            return
        files = manifest.get("files") or []
        if not files:
            self._last_manifest_sig = None
            return
        sig = sha256_text(json.dumps(files, ensure_ascii=False, sort_keys=True))
        if sig == self._last_manifest_sig:
            return  # 已經處理過同一份清單
        # 工作階段必須是已允許，才會跳出同步確認
        try:
            session, _ = self.relay.get_json("session.json")
        except Exception:
            session = None
        if not (isinstance(session, dict)
                and session.get("status") == "approved"):
            if sig != self._pending_notice_sig:
                self._pending_notice_sig = sig
                self.ui.notify("AI File：有待同步檔案",
                               "中繼有 %d 個檔案等著同步，"
                               "但工作階段尚未允許。\n請先在電腦上按「是」允許連線。"
                               % len(files))
            return
        # 跳「立即同步」確認窗，使用者看過清單、按同意才寫入
        if not self.ui.ask_sync(files):
            self._last_manifest_sig = sig  # 稍後再說：等清單變了再問
            log_local("使用者選擇稍後同步（%d 個檔案）" % len(files))
            return
        results = self.apply_pending(files)
        ok_count = sum(1 for r in results if r.get("ok"))
        self._last_manifest_sig = sig
        self.ui.notify("AI File：同步完成",
                       "已同步 %d / %d 個檔案到：\n%s" % (
                           ok_count, len(files), self.allowed_folder))
        log_local("同步完成：%d / %d" % (ok_count, len(files)))

    def apply_pending(self, files):
        """下載、驗證、寫入。每個檔案獨立處理，失敗的不影響其他檔案。
        成功寫入後才從中繼清掉該筆；全部完成後更新 manifest。"""
        results = []
        remaining = []
        for entry in files:
            rel = entry.get("rel_path", "")
            action = entry.get("action", "add")
            ai_name = entry.get("ai_name", "")
            result = {"rel_path": rel, "action": action,
                      "ai_name": ai_name, "ok": False}
            # 1. 工作階段代碼必須是當下有效的
            if entry.get("session_code") != self.session_code:
                result["error"] = "工作階段代碼不符，已拒收"
                self._record_reject(entry, "工作階段代碼不符")
                results.append(result)
                remaining.append(entry)
                continue
            # 2. 路徑守門
            try:
                target = safe_join(self.allowed_folder, rel)
            except ValueError as e:
                result["error"] = str(e)
                self._record_reject(entry, str(e))
                results.append(result)
                continue  # 越界的直接丟掉，不留在待同步
            # 3. 刪除
            if action == "delete":
                try:
                    if os.path.exists(target):
                        os.remove(target)
                    result["ok"] = True
                except OSError as e:
                    result["error"] = "刪除失敗：%s" % e
                    remaining.append(entry)
                self._record_applied(entry, result)
                results.append(result)
                continue
            # 4. 下載並驗證雜湊
            pending_name = entry.get("file", "")
            if not pending_name:
                result["error"] = "待同步檔案名稱缺失"
                remaining.append(entry)
                results.append(result)
                continue
            try:
                data, _ = self.relay.get_bytes("pending/" + pending_name)
            except Exception as e:
                result["error"] = "下載失敗：%s" % e
                remaining.append(entry)
                results.append(result)
                continue
            if data is None:
                result["error"] = "中繼找不到檔案"
                remaining.append(entry)
                results.append(result)
                continue
            if sha256_bytes(data) != entry.get("sha256", ""):
                result["error"] = "雜湊驗證失敗，檔案可能被竄改，已拒收"
                self._record_reject(entry, "雜湊驗證失敗")
                results.append(result)
                continue
            # 5. 寫入允許的資料夾
            try:
                parent = os.path.dirname(target)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(target, "wb") as f:
                    f.write(data)
                result["ok"] = True
            except OSError as e:
                result["error"] = "寫入失敗：%s" % e
                remaining.append(entry)
            self._record_applied(entry, result)
            results.append(result)
            # 6. 成功就從中繼刪掉暫存檔
            if result["ok"]:
                try:
                    self.relay.delete("pending/" + pending_name,
                                      "AI File：已同步，清除暫存")
                except Exception:
                    pass
        # 更新 manifest：只留還沒成功的
        try:
            self.relay.put_json("pending/manifest.json",
                                {"files": remaining},
                                "AI File：更新待同步清單")
        except Exception as e:
            log_local("更新待同步清單失敗：%s" % e)
        return results

    def _record_applied(self, entry, result):
        verb = {"add": "新增", "modify": "修改",
                "delete": "刪除"}.get(entry.get("action", "add"), "新增")
        if result.get("ok"):
            append_activity(self.relay, {
                "action": entry.get("action", "add"),
                "ai_name": entry.get("ai_name", ""),
                "rel_path": entry.get("rel_path", ""),
                "detail": "已同步"})
            self.ui.add_activity({
                "action": entry.get("action", "add"),
                "ai_name": entry.get("ai_name", ""),
                "rel_path": entry.get("rel_path", ""),
                "at": utcnow_iso()})
            log_local("%s：%s" % (verb, entry.get("rel_path", "")))
        else:
            append_activity(self.relay, {
                "action": "sync-failed",
                "ai_name": entry.get("ai_name", ""),
                "rel_path": entry.get("rel_path", ""),
                "detail": result.get("error", "")})
            log_local("%s失敗：%s（%s）" % (
                verb, entry.get("rel_path", ""), result.get("error", "")))

    def _record_reject(self, entry, reason):
        append_activity(self.relay, {
            "action": "rejected-path",
            "ai_name": entry.get("ai_name", ""),
            "rel_path": entry.get("rel_path", ""),
            "detail": reason})
        self.ui.add_activity({
            "action": "rejected-path",
            "ai_name": entry.get("ai_name", ""),
            "rel_path": entry.get("rel_path", ""),
            "at": utcnow_iso()})
        self.ui.notify("AI File：已拒絕一個要求",
                       "原因：%s\n路徑：%s" % (reason, entry.get("rel_path", "")))
        log_local("拒絕：%s（%s）" % (entry.get("rel_path", ""), reason))

    # -- 輪詢 ------------------------------------------------------
    def poll_once(self):
        """跑一輪：停止旗標 → 心跳 → 工作階段 → 重新生成 → 待同步。"""
        try:
            stop, _ = self.relay.get_json("stop.json")
        except Exception as e:
            log_local("讀取停止旗標失敗：%s" % e)
            stop = None
        if isinstance(stop, dict) and stop.get("stopped"):
            if not self.stopped:
                self.stopped = True
                self.ui.set_status("已停止（%s）" % stop.get("by", ""))
                self.ui.notify("AI File 已停止",
                               "停止旗標已開啟（由 %s 停止），"
                               "Muse 現在動不到你的檔案。" % stop.get("by", ""))
                log_local("偵測到停止旗標（by=%s），停手" % stop.get("by", ""))
            self.send_heartbeat()
            return
        if self.stopped and not (isinstance(stop, dict)
                                 and stop.get("stopped")):
            # 網頁版按了恢復
            self.stopped = False
            self.ui.set_status("已恢復，監控中")
            log_local("停止旗標已清除，恢復監控")
        self.send_heartbeat()
        self._check_session()
        self._check_regen()
        self._check_pending()
        if not self.stopped:
            self.ui.set_status("監控中 — %s" % os.path.basename(
                os.path.normpath(self.allowed_folder)))


# ---------------------------------------------------------------- 設定精靈
def run_setup_wizard(root, relay=None):
    """第一次設定精靈：貼上 PAT、選允許的資料夾、產生工作階段代碼。"""
    import tkinter as tk
    from tkinter import filedialog, ttk
    import tkinter.messagebox as mb

    cfg = load_config() or {}
    win = tk.Toplevel(root)
    win.title("AI File：第一次設定")
    win.geometry("560x470")
    win.attributes("-topmost", True)

    ttk.Label(win, text="AI File 第一次設定",
              font=("Microsoft JhengHei", 14, "bold")).pack(
                  anchor="w", padx=18, pady=(16, 4))
    ttk.Label(win, text="設定一次就好。代碼只存在你的電腦，不會上傳。",
              font=("Microsoft JhengHei", 10)).pack(
                  anchor="w", padx=18, pady=(0, 12))

    ttk.Label(win, text="1. GitHub 細粒度 Token（PAT）",
              font=("Microsoft JhengHei", 11, "bold")).pack(
                  anchor="w", padx=18)
    ttk.Label(win, text="只授權私人 repo「ai-file-relay」的內容讀寫，"
              "在 GitHub 設定裡產生。",
              font=("Microsoft JhengHei", 9)).pack(anchor="w", padx=18)
    pat_var = tk.StringVar(value=cfg.get("pat", ""))
    ttk.Entry(win, textvariable=pat_var, show="*", width=60).pack(
        padx=18, pady=(2, 10), fill="x")

    ttk.Label(win, text="2. 允許 Muse 使用的資料夾",
              font=("Microsoft JhengHei", 11, "bold")).pack(
                  anchor="w", padx=18)
    ttk.Label(win, text="Muse 只能動這個資料夾裡的檔案，其他地方一律拒絕。",
              font=("Microsoft JhengHei", 9)).pack(anchor="w", padx=18)
    folder_var = tk.StringVar(value=cfg.get("allowed_folder", ""))
    row = ttk.Frame(win)
    row.pack(fill="x", padx=18, pady=(2, 10))
    ttk.Entry(row, textvariable=folder_var).pack(side="left", fill="x",
                                                 expand=True)
    ttk.Button(row, text="瀏覽…",
               command=lambda: folder_var.set(
                   filedialog.askdirectory() or folder_var.get())
               ).pack(side="left", padx=(8, 0))

    ttk.Label(win, text="3. 工作階段代碼（把這組給 Muse）",
              font=("Microsoft JhengHei", 11, "bold")).pack(
                  anchor="w", padx=18)
    code_var = tk.StringVar(
        value=cfg.get("session_code") or new_session_code())
    code_row = ttk.Frame(win)
    code_row.pack(fill="x", padx=18, pady=(2, 4))
    code_label = ttk.Label(code_row, textvariable=code_var,
                           font=("Microsoft JhengHei", 16, "bold"))
    code_label.pack(side="left")
    ttk.Button(code_row, text="重新生成",
               command=lambda: code_var.set(new_session_code())
               ).pack(side="left", padx=(10, 0))
    ttk.Label(win, text="Muse 到網頁版貼上這組代碼，你在電腦上按「是」才會連線。",
              font=("Microsoft JhengHei", 9)).pack(anchor="w", padx=18)

    result = {"saved": False}

    def save():
        pat = pat_var.get().strip()
        folder = folder_var.get().strip()
        code = code_var.get().strip().upper()
        if not pat:
            mb.showerror("AI File", "請先貼上 GitHub Token（PAT）。",
                         parent=win)
            return
        if not folder or not os.path.isdir(folder):
            mb.showerror("AI File", "請選擇一個存在的資料夾。",
                         parent=win)
            return
        if not valid_session_code(code):
            mb.showerror("AI File", "工作階段代碼格式不對。",
                         parent=win)
            return
        new_cfg = {"pat": pat, "allowed_folder": folder,
                   "session_code": code,
                   "relay_owner": RELAY_OWNER, "relay_repo": RELAY_REPO}
        save_config(new_cfg)
        # 把代碼雜湊寫進中繼，網頁版才能驗證 Muse 貼的代碼
        r = relay or GitHubRelay(pat)
        try:
            r.put_json("session.json", {
                "status": "none", "code_hash": code_hash(code),
                "created_at": utcnow_iso()},
                "AI File：第一次設定，寫入工作階段代碼雜湊")
            r.put_json("stop.json", {"stopped": False},
                       "AI File：第一次設定，清除停止旗標")
        except Exception as e:
            mb.showwarning("AI File",
                           "設定已存在本機，但寫入中繼 repo 失敗：\n%s\n"
                           "請檢查網路與 Token 後再試一次。" % e,
                           parent=win)
        result["saved"] = True
        result["config"] = new_cfg
        mb.showinfo("AI File", "設定完成！\n工作階段代碼：%s\n\n"
                    "請把這組代碼給 Muse，Muse 到網頁版登入後，"
                    "你再按「是」就能開始用。" % code, parent=win)
        win.destroy()

    ttk.Button(win, text="儲存並開始", command=save).pack(pady=16)
    root.wait_window(win)
    return result


# ---------------------------------------------------------------- 主程式
def main(argv):
    if "--check" in argv:
        return run_core_check()

    import tkinter as tk

    cfg = load_config()
    root = tk.Tk()
    root.withdraw()

    if cfg is None or "--setup" in argv:
        res = run_setup_wizard(root)
        if not res.get("saved"):
            if cfg is None:
                root.destroy()
                return 1
            cfg = load_config()
        else:
            cfg = res["config"]
        if "--setup" in argv and not res.get("saved"):
            root.destroy()
            return 0

    relay = GitHubRelay(cfg["pat"], cfg.get("relay_owner", RELAY_OWNER),
                        cfg.get("relay_repo", RELAY_REPO))
    ui = TkUI(root, None)
    agent = Agent(cfg, relay, ui)
    ui.agent = agent
    ui.open_floating()
    ui.set_code(agent.session_code)
    ui.set_status("監控中 — %s" % os.path.basename(
        os.path.normpath(agent.allowed_folder)))
    agent.send_heartbeat(force=True)

    if "--once" in argv:
        try:
            agent.poll_once()
        except Exception:
            traceback.print_exc()
            return 1
        root.destroy()
        return 0

    def tick():
        try:
            agent.poll_once()
        except Exception:
            traceback.print_exc()
            log_local("輪詢出錯（已略過，下一輪繼續）")
        root.after(POLL_INTERVAL * 1000, tick)

    root.after(POLL_INTERVAL * 1000, tick)
    log_local("AI File 已啟動，監控：%s" % agent.allowed_folder)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    return 0


def run_core_check():
    """不開視窗的核心檢查（給 --check 與自我檢查用）。"""
    checks = []
    try:
        c = new_session_code()
        checks.append(("工作階段代碼格式", valid_session_code(c)))
        checks.append(("代碼雜湊一致",
                       code_hash(c) == sha256_text(c.strip().upper())))
        checks.append(("路徑守門：資料夾內放行",
                       safe_join("/tmp/ok", "a/b.txt") ==
                       os.path.join("/tmp/ok", "a", "b.txt")))
        try:
            safe_join("/tmp/ok", "../evil.txt")
            checks.append(("路徑守門：.. 跳脫拒絕", False))
        except ValueError:
            checks.append(("路徑守門：.. 跳脫拒絕", True))
        try:
            safe_join("/tmp/ok", "C:/Windows/x.txt")
            checks.append(("路徑守門：絕對路徑拒絕", False))
        except ValueError:
            checks.append(("路徑守門：絕對路徑拒絕", True))
        checks.append(("SHA-256 驗證",
                       sha256_bytes(b"hello") ==
                       "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e730"
                       "43362938b9824"))
    except Exception as e:
        print("檢查出錯：%s" % e)
        return 1
    ok = True
    for name, passed in checks:
        print("[%s] %s" % ("OK" if passed else "失敗", name))
        ok = ok and passed
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
