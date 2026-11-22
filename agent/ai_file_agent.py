# -*- coding: utf-8 -*-
"""
AI File 本地端程式 v1.4
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
import queue
import random
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone

VERSION_DEFAULT = "1.5"
WRITE_RETRY_MAX = 3  # 寫入遇到 409（檔案剛被另一邊改過）最多重試幾次
RELAY_OWNER = "Pagani-pagani"
RELAY_REPO = "ai-file-relay"
GITHUB_API = "https://api.github.com"
PUBLIC_RAW_VERSION_URL = (
    "https://raw.githubusercontent.com/Pagani-pagani/ai-file/main/VERSION")
PUBLIC_ZIP_URL = (
    "https://codeload.github.com/Pagani-pagani/ai-file/zip/refs/heads/main")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "ai-file-config.json")
LOG_FILE = os.path.join(BASE_DIR, "ai-file-log.txt")
HISTORY_FILE = os.path.join(BASE_DIR, "ai-file-history.json")
BACKUP_DIR = os.path.join(BASE_DIR, "ai-file-backup")
HISTORY_KEEP = 20  # 備份紀錄只留最近幾批
UPDATE_BAT = os.path.join(BASE_DIR, "更新 AI File.bat")

SNAPSHOT_MAX_ENTRIES = 2000  # 快照最多列幾筆，超過就 truncated
CONTENT_MAX_BYTES = 10 * 1024 * 1024  # 網頁索取檔案內容的上限（10 MB）

# 快照裡的檔案種類（副檔名 → 中文名；不在表裡的是「其他」）
KIND_BY_EXT = {}
for _exts, _kind in (
    (("png", "jpg", "jpeg", "gif", "bmp", "webp", "svg", "avif"), "圖片"),
    (("txt", "md", "doc", "docx", "pdf", "csv"), "文件"),
    (("xls", "xlsx"), "試算表"),
    (("ppt", "pptx"), "簡報"),
    (("mp4", "mov", "avi", "mkv"), "影片"),
    (("mp3", "wav", "flac"), "音樂"),
    (("zip", "rar", "7z"), "壓縮檔"),
    (("py", "js", "html", "css", "json", "bat", "ps1", "exe"), "程式"),
):
    for _e in _exts:
        KIND_BY_EXT[_e] = _kind


def file_kind(ext, is_dir):
    """副檔名（小寫、不含點）→ 中文種類；資料夾固定是「資料夾」。"""
    if is_dir:
        return "資料夾"
    return KIND_BY_EXT.get((ext or "").lower(), "其他")


def build_snapshot(allowed_folder, max_entries=SNAPSHOT_MAX_ENTRIES):
    """遞迴列出允許的資料夾裡所有子資料夾與檔案（只列清單，不讀內容、
    不算雜湊）。回傳 snapshot.json 的內容。

    欄位：generated_at、folder_name、total_files、total_dirs、
    truncated、entries（每筆：path、is_dir、size、created、ext、kind）。
    超過上限時只列前 max_entries 筆，truncated=true。
    讀不到的資料夾／檔案就跳過，不讓快照整個失敗。
    """
    entries = []
    total_files = 0
    total_dirs = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(allowed_folder):
        dirnames.sort()
        filenames.sort()
        for name in dirnames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, allowed_folder).replace(os.sep, "/")
            total_dirs += 1
            if len(entries) < max_entries:
                entries.append(_snapshot_entry(rel, True, full))
            else:
                truncated = True
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, allowed_folder).replace(os.sep, "/")
            total_files += 1
            if len(entries) < max_entries:
                entries.append(_snapshot_entry(rel, False, full))
            else:
                truncated = True
    return {
        "generated_at": utcnow_iso(),
        "folder_name": os.path.basename(
            os.path.normpath(allowed_folder)),
        "total_files": total_files,
        "total_dirs": total_dirs,
        "truncated": truncated,
        "entries": entries,
    }


def _snapshot_entry(rel, is_dir, full):
    try:
        created = datetime.fromtimestamp(
            os.path.getctime(full), timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
    except OSError:
        created = None
    try:
        size = 0 if is_dir else os.path.getsize(full)
    except OSError:
        size = 0
    ext = "" if is_dir else os.path.splitext(full)[1][1:].lower()
    return {
        "path": rel,
        "is_dir": is_dir,
        "size": size,
        "created": created,
        "ext": ext,
        "kind": file_kind(ext, is_dir),
    }

# 連線失敗的原因對照表（自我檢查、設定精靈、浮動視窗共用）
CONNECTION_CAUSES = (
    "・401：Token 不對或已過期，請重新產生 Token\n"
    "・403：Token 權限不夠或沒有授權 ai-file-relay，請檢查 Token 設定\n"
    "・404：找不到 repo 或 Token 看不到這個 repo，請檢查 repo 名稱與 Token 授權\n"
    "・連不上：網路或防火牆擋住 api.github.com，請檢查網路")


def _load_version():
    """啟動時讀 repo 根目錄的 VERSION 檔案，讀不到才用內建常數。"""
    try:
        with open(os.path.join(os.path.dirname(BASE_DIR), "VERSION"),
                  "r", encoding="utf-8") as f:
            v = f.read().strip()
            if v:
                return v
    except OSError:
        pass
    return VERSION_DEFAULT


VERSION = _load_version()

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
    """中繼連線錯誤。status 是 HTTP 狀態碼（None 表示連不上伺服器）。"""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def relay_error_reason(exc):
    """把連線錯誤翻成中文原因，方便一眼看出是 Token 問題還是程式問題。"""
    status = getattr(exc, "status", None)
    msg = str(exc)
    if status is None:
        for code in ("401", "403", "404"):
            if code in msg:
                status = int(code)
                break
    if status == 401:
        return "401：Token 不對或已過期，請重新產生 Token"
    if status == 403:
        return "403：Token 權限不夠或沒有授權 ai-file-relay，請檢查 Token 設定"
    if status == 404:
        return "404：找不到 repo 或 Token 看不到這個 repo，請檢查 repo 名稱與 Token 授權"
    if "連不上" in msg:
        return "連不上 GitHub（網路或防火牆可能擋住 api.github.com）"
    return msg


class RelayClient:
    """中繼 repo 的介面。get_json 回傳 (data, sha)，檔案不存在回傳 (None, None)。"""

    def get_json(self, path):
        raise NotImplementedError

    def get_bytes(self, path):
        raise NotImplementedError

    def put_json(self, path, data, message, retry=True, sha=None,
                 create_only=False):
        raise NotImplementedError

    def put_bytes(self, path, data, message, retry=True, sha=None,
                  create_only=False):
        raise NotImplementedError

    def delete(self, path, message):
        raise NotImplementedError


class GitHubRelay(RelayClient):
    """用 GitHub Contents API 讀寫私人中繼 repo。PAT 只從設定檔讀，絕不寫進程式碼。

    api_base 可以注入（環境變數 AI_FILE_API_BASE 或參數），測試時用本機假 API
    伺服器驗證；預設仍是 api.github.com。每次請求的結果會記在 last_error，
    讓浮動視窗的連線狀態行知道最後一次是成功還是失敗。
    """

    def __init__(self, pat, owner=RELAY_OWNER, repo=RELAY_REPO,
                 api_base=None):
        self.pat = pat
        self.owner = owner
        self.repo = repo
        self.api_base = api_base or os.environ.get("AI_FILE_API_BASE") \
            or GITHUB_API
        self.last_error = None

    def _url(self, path):
        parts = "/".join(
            urllib.request.quote(seg, safe="") for seg in path.split("/")
        )
        return "%s/repos/%s/%s/contents/%s" % (
            self.api_base, self.owner, self.repo, parts)

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
                self.last_error = None
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # 檔案不存在是正常情況（例如還沒有心跳），不是連線失敗
                self.last_error = None
                return None
            detail = e.read().decode("utf-8", "replace")[:300]
            err = RelayError("GitHub API %s %s 失敗（%s）：%s" % (
                method, path, e.code, detail), status=e.code)
            self.last_error = err
            raise err
        except urllib.error.URLError as e:
            err = RelayError("連不上 GitHub：%s" % e.reason)
            self.last_error = err
            raise err

    def check_repo(self):
        """檢查 Token 能不能看到中繼 repo。回傳 (ok, 原因說明)。
        用來分辨「檔案還沒建立」和「repo／Token 有問題」。"""
        url = "%s/repos/%s/%s" % (self.api_base, self.owner, self.repo)
        req = urllib.request.Request(url, method="GET")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("Authorization", "Bearer " + self.pat)
        req.add_header("User-Agent", "AI-File-Agent/" + VERSION)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
                return True, "repo 存在，Token 看得到"
        except urllib.error.HTTPError as e:
            return False, relay_error_reason(
                RelayError("HTTP %s" % e.code, status=e.code))
        except urllib.error.URLError as e:
            return False, "連不上 GitHub：%s" % e.reason

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

    def _put(self, path, content_b64, message, retry=True, sha=None,
             create_only=False):
        # Contents API 更新檔案需要目前的 sha。
        # sha=None（沒指定）：先 GET 取一次（單純覆寫用，例如心跳、停止旗標）。
        # sha 有值：直接用這個 sha，不再另外 GET——呼叫端讀到的資料和
        # sha 是同一份才不會寫錯（讀-改-寫一定要用讀到的那個 sha；
        # 另外重取 sha 會讓舊內容配上新 sha，成功蓋掉別人的寫入）。
        # create_only=True：呼叫端讀過了，檔案當時不存在——PUT 不帶 sha、
        # 也不另外 GET；檔案若在那一瞬間被別人建立了，GitHub 會擋下
        # （409 或 422），由呼叫端重讀再寫，絕對不能用新 sha 重寫舊內容。
        # 內部自動重試只在「單純覆寫」（sha=None 且不是 create_only）時做：
        # 有指定 sha 或 create_only 時 409/422 就直接報錯，由呼叫端重讀。
        attempts = 0
        while True:
            attempts += 1
            if create_only:
                body_sha = None
            elif sha is not None:
                body_sha = sha
            else:
                res = self._request("GET", path)
                body_sha = res.get("sha") if res is not None else None
            body = {"message": message, "content": content_b64}
            if body_sha is not None:
                body["sha"] = body_sha
            try:
                return self._request("PUT", path, body)
            except RelayError as e:
                status = getattr(e, "status", None)
                if retry and not create_only and sha is None \
                        and status in (409, 422) \
                        and attempts < WRITE_RETRY_MAX:
                    log_local("寫入 %s 遇到 %s（檔案剛被另一邊改過），"
                              "重試第 %d 次" % (path, status, attempts))
                    # 隨機等一下再重試：兩邊同時寫時才不會一直撞在同一個時間點
                    time.sleep(0.05 + random.random() * 0.15)
                    continue
                raise

    def put_json(self, path, data, message, retry=True, sha=None,
                 create_only=False):
        raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self._put(path, base64.b64encode(raw).decode("ascii"), message,
                  retry=retry, sha=sha, create_only=create_only)

    def put_bytes(self, path, data, message, retry=True, sha=None,
                  create_only=False):
        self._put(path, base64.b64encode(data).decode("ascii"), message,
                  retry=retry, sha=sha, create_only=create_only)

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

    def put_json(self, path, data, message, retry=True, sha=None,
                 create_only=False):
        raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.put_bytes(path, raw, message)

    def put_bytes(self, path, data, message, retry=True, sha=None,
                  create_only=False):
        full = self._full(path)
        if create_only and os.path.exists(full):
            # 本機模擬 GitHub 的擋下行為：只准建立，已存在就報 409
            raise RelayError("檔案 %s 已存在" % path, status=409)
        os.makedirs(os.path.dirname(full), exist_ok=True)
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


# ---------------------------------------------------------------- 備份與返回（同步紀錄）
def new_batch_id():
    """同步批次 id：時間＋短亂數，例如 20261122-190000-a1b2。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return "%s-%s" % (stamp, secrets.token_hex(2))


def load_history():
    """讀本機同步紀錄（新到舊）。檔案壞掉或不存在回傳空清單。"""
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_history(history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def prune_history(history):
    """只留最近 HISTORY_KEEP 批，舊的備份資料夾一起清掉。"""
    removed = history[HISTORY_KEEP:]
    del history[HISTORY_KEEP:]
    for batch in removed:
        try:
            shutil.rmtree(os.path.join(BACKUP_DIR, batch.get("id", "")),
                          ignore_errors=True)
        except OSError:
            pass
    return history


def append_activity(relay, entry):
    """把一筆紀錄 append 到中繼的 activity.json，回傳 True／False。
    activity.json 是讀-改-寫：網頁端也同時寫，兩邊同時寫會互相蓋掉
    （後寫的蓋掉先寫的，紀錄就「存了又不見」）。
    修法：409／422 就重讀最新內容、重新補一筆再寫，最多重試 3 次；
    重試後還是失敗就回傳 False（只寫 log，不假裝存成功）。
    注意：這裡用 retry=False 呼叫 put_json——重試必須由這層重讀
    最新內容再寫，只換 sha 重寫同一份舊內容還是會蓋掉對方那筆；
    讀到檔案不存在時用 create_only（PUT 不帶 sha），檔案若在那一瞬間
    被別人建立了，GitHub 會擋下（409／422），這層重讀再寫。"""
    for attempt in range(1, WRITE_RETRY_MAX + 1):
        try:
            data, sha = relay.get_json("activity.json")
        except Exception as e:
            # 讀不到就不要寫：把 data 當成空清單寫回去會蓋掉整份紀錄。
            # 重試幾次讀取，還是不行就回傳 False（不假裝存成功）。
            log_local("讀取 activity.json 失敗（第 %d 次）：%s" % (attempt, e))
            if attempt < WRITE_RETRY_MAX:
                time.sleep(0.05 + random.random() * 0.15)
                continue
            return False
        if not isinstance(data, list):
            data = []
        entry = dict(entry)
        entry.setdefault("at", utcnow_iso())
        data.append(entry)
        try:
            # 寫入用剛才讀到的那個 sha：資料和 sha 是同一份才不會寫錯。
            # 讀到不存在（sha 是 None）時用 create_only：PUT 不帶 sha，
            # 檔案若在那一瞬間被別人建立了，GitHub 會擋下（409/422），
            # 這層重讀再寫，絕對不用新 sha 重寫舊內容。
            relay.put_json("activity.json", data, "AI File：新增活動紀錄",
                           retry=False, sha=sha, create_only=(sha is None))
            return True
        except RelayError as e:
            if getattr(e, "status", None) in (409, 422) \
                    and attempt < WRITE_RETRY_MAX:
                log_local("寫入 activity.json 遇到 %s，重試第 %d 次"
                          % (getattr(e, "status", "?"), attempt))
                time.sleep(0.05 + random.random() * 0.15)
                continue
            log_local("寫入 activity.json 失敗（已重試 %d 次）：%s"
                      % (WRITE_RETRY_MAX, e))
            return False
        except Exception as e:
            log_local("寫入 activity.json 失敗：%s" % e)
            return False
    return False


def _manifest_entry_key(entry):
    """暫存項目的比對鍵：有 id 用 id，舊資料用路徑＋動作＋AI 名稱＋時間。"""
    if entry.get("id"):
        return ("id", entry["id"])
    return ("k", entry.get("rel_path", ""), entry.get("action", ""),
            entry.get("ai_name", ""), entry.get("time", ""))


def test_connection(relay):
    """測試連線。回傳 (ok, 訊息)，訊息直接講得出原因。
    heartbeat.json 不存在時，再用 check_repo 分辨是「檔案還沒建立」
    還是「repo／Token 有問題」。"""
    try:
        hb, _ = relay.get_json("heartbeat.json")
    except RelayError as e:
        return False, relay_error_reason(e)
    except Exception as e:
        return False, str(e)
    if hb is None:
        if hasattr(relay, "check_repo"):
            ok, reason = relay.check_repo()
            if not ok:
                return False, reason
        return True, "連線成功，但中繼還沒有心跳（本地端還沒啟動過）"
    if isinstance(hb, dict):
        return True, "連線成功，最後心跳：%s" % hb.get("updated_at", "—")
    return True, "連線成功"


def create_desktop_shortcut():
    """在桌面建立 AI File.lnk（不用 pywin32，用 Windows 內建 WScript.Shell）。
    目標是程式資料夾裡的「啟動 AI File.vbs」（隱藏啟動，不跳黑窗）。
    成功回傳 True；非 Windows 或失敗回傳 False（失敗只寫 log）。"""
    if os.name != "nt":
        log_local("建立桌面捷徑：只支援 Windows，本機是 %s，跳過" % os.name)
        return False
    target = os.path.join(BASE_DIR, "啟動 AI File.vbs")
    vbs_text = (
        'Set WshShell = CreateObject("WScript.Shell")\r\n'
        'DesktopPath = WshShell.SpecialFolders("Desktop")\r\n'
        'Set oLink = WshShell.CreateShortcut(DesktopPath & "\\AI File.lnk")\r\n'
        'oLink.TargetPath = "%s"\r\n'
        'oLink.WorkingDirectory = "%s"\r\n'
        'oLink.Description = "AI File：讓 AI 把檔案送到你的電腦"\r\n'
        'oLink.Save\r\n' % (target, BASE_DIR)
    )
    tmp_vbs = None
    try:
        fd, tmp_vbs = tempfile.mkstemp(suffix=".vbs", prefix="aifile_shortcut_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(vbs_text)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(
            ["cscript", "//nologo", tmp_vbs],
            capture_output=True, timeout=30, creationflags=flags)
        if proc.returncode != 0:
            log_local("建立桌面捷徑失敗：%s" % proc.stderr.decode(
                "utf-8", "replace")[:200])
            return False
        log_local("已建立桌面捷徑：AI File.lnk")
        return True
    except Exception as e:
        log_local("建立桌面捷徑失敗：%s" % e)
        return False
    finally:
        if tmp_vbs:
            try:
                os.remove(tmp_vbs)
            except OSError:
                pass


def parse_version(text):
    """版本字串轉 tuple(int)，例如 "1.10" -> (1, 10)，比大小才不會錯
    （字串比會把 1.10 誤判成比 1.2 小）。"""
    parts = []
    for piece in str(text).strip().split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            parts.append(0)
    return tuple(parts) if parts else (0,)


def check_for_update(version_url=None):
    """檢查公開 repo 有沒有新版。回傳 (有新版, 遠端版本字串或 None)。
    任何失敗都回傳 (False, None)，絕對不丟例外、不擋啟動。"""
    url = version_url or PUBLIC_RAW_VERSION_URL
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "AI-File-Agent/" + VERSION})
        with urllib.request.urlopen(req, timeout=15) as resp:
            remote = resp.read().decode("utf-8").strip()
        if not remote:
            return False, None
        return parse_version(remote) > parse_version(VERSION), remote
    except Exception as e:
        log_local("檢查更新失敗（不影響使用）：%s" % e)
        return False, None


def perform_update(remote_version, zip_url=None):
    """下載新版 zip、驗證、寫好更新批次檔。成功回傳 True（呼叫端接著退出程式），
    失敗回傳 False（繼續用舊版）。更新只覆蓋 agent/ 的檔案，絕對不動
    ai-file-config.json 和 ai-file-log.txt（它們本來就不在 repo 裡）。"""
    url = zip_url or PUBLIC_ZIP_URL
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "AI-File-Agent/" + VERSION})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
        tmp_zip = os.path.join(tempfile.gettempdir(), "aifile_update.zip")
        with open(tmp_zip, "wb") as f:
            f.write(data)
        tmp_dir = tempfile.mkdtemp(prefix="aifile_update_")
        with zipfile.ZipFile(tmp_zip) as z:
            names = z.namelist()
            if "ai-file-main/agent/ai_file_agent.py" not in names:
                raise ValueError("下載的 zip 裡沒有 agent/ai_file_agent.py，格式不對")
            for name in names:
                if name.startswith("ai-file-main/agent/") \
                        and not name.endswith("/"):
                    z.extract(name, tmp_dir)
        src = os.path.join(tmp_dir, "ai-file-main", "agent")
        # 更新批次檔：等 agent 完全退出、覆蓋 agent/、刪暫存、重啟
        bat = (
            "@echo off\r\n"
            "chcp 65001 >nul\r\n"
            "ping -n 4 127.0.0.1 >nul\r\n"
            "xcopy /E /Y /Q \"%s\" \"%s\\\"\r\n"
            "rmdir /S /Q \"%s\"\r\n"
            "del \"%s\"\r\n"
            "start \"\" wscript \"%s\"\r\n"
            "del \"%%~f0\"\r\n" % (
                src, BASE_DIR, tmp_dir, tmp_zip,
                os.path.join(BASE_DIR, "啟動 AI File.vbs"))
        )
        with open(UPDATE_BAT, "w", encoding="utf-8") as f:
            f.write(bat)
        log_local("已下載 v%s，更新批次檔就緒" % remote_version)
        return True
    except Exception as e:
        log_local("自動更新失敗（繼續使用舊版）：%s" % e)
        return False


def run_updater_and_exit():
    """啟動更新批次檔（讓它在背景跑），呼叫端接著退出程式。"""
    try:
        if os.name == "nt":
            os.startfile(UPDATE_BAT)
        else:
            subprocess.Popen(["cmd", "/c", UPDATE_BAT])
    except Exception as e:
        log_local("啟動更新批次檔失敗：%s" % e)


# ---------------------------------------------------------------- UI（tkinter）
class TkUI:
    """桌面通知、確認對話框、浮動視窗。全部 tkinter，Windows 與 Linux 共用。"""

    def __init__(self, root, agent):
        self.root = root
        self.agent = agent
        self.float_win = None
        self.float_list = None
        self.float_status = None
        self.float_conn = None
        self.float_code = None
        self.tray_available = False  # 系統匣圖示有沒有成功建立（main 啟動時設定）
        # 主執行緒工作佇列：tkinter 只能在主執行緒動，背景執行緒想動介面
        # 就把工作放進這個 queue，由主執行緒的 root.after 迴圈消化。
        # （root.after 不能在背景執行緒呼叫，會報 main thread is not
        # in main loop；所以這裡不用 after 跨執行緒，只用 queue 傳遞。）
        self._main_queue = queue.Queue()
        self._drain_scheduled = False
        self._ensure_drain()

    def _ensure_drain(self):
        """確保主執行緒定期消化 queue（root.after 迴圈，只在主執行緒排）。"""
        if self._drain_scheduled:
            return
        self._drain_scheduled = True

        def drain():
            # 程式正在關閉就不要再排了；任何例外都吞掉，
            # 不然 Tcl 會把 after 回呼的錯誤印到 stderr
            try:
                if not self.root.winfo_exists():
                    self._drain_scheduled = False
                    return
            except Exception:
                self._drain_scheduled = False
                return
            try:
                self.drain_main_queue()
            except Exception:
                pass
            try:
                self.root.after(50, drain)
            except Exception:
                self._drain_scheduled = False
        try:
            self.root.after(50, drain)
        except Exception:
            self._drain_scheduled = False

    def drain_main_queue(self):
        """主執行緒呼叫：把 queue 裡排隊的工作全部跑完。"""
        while True:
            try:
                fn, done, result = self._main_queue.get_nowait()
            except queue.Empty:
                return
            try:
                result["value"] = fn()
            except Exception as e:
                result["error"] = e
            finally:
                if done is not None:
                    done.set()

    def call_on_main(self, fn):
        """背景執行緒想動介面時用這個：放進 queue，保證在主執行緒跑。"""
        self._ensure_drain()
        self._main_queue.put((fn, None, {}))

    def call_on_main_sync(self, fn):
        """背景執行緒想動介面、又要等結果時用這個：在主執行緒跑完才回傳。
        已經在主執行緒就直接跑（不會自己等自己）。"""
        if threading.current_thread() is threading.main_thread():
            return fn()
        self._ensure_drain()
        done = threading.Event()
        result = {}
        self._main_queue.put((fn, done, result))
        done.wait()
        if "error" in result:
            raise result["error"]
        return result.get("value")

    def _on_main(self, fn):
        """動介面的方法共用：在主執行緒跑，背景執行緒呼叫就等它跑完。
        輪詢改成背景執行緒後，notify／確認窗／狀態列都走這裡，
        才不會在背景執行緒裡直接碰 tkinter。"""
        return self.call_on_main_sync(fn)

    def quit_app(self):
        """1.5 秒後關掉主視窗（更新流程用，讓「正在更新」先顯示一下）。"""
        try:
            self.root.after(1500, self.root.destroy)
        except Exception:
            pass

    # -- 通知與確認 ------------------------------------------------
    def notify(self, title, message):
        def run():
            import tkinter.messagebox as mb
            try:
                self.root.attributes("-topmost", True)
                mb.showinfo(title, message, parent=self.root)
            except Exception:
                print("[通知] %s：%s" % (title, message))
        self._on_main(run)

    def ask_yes_no(self, title, message):
        def run():
            import tkinter.messagebox as mb
            try:
                self.root.attributes("-topmost", True)
                return bool(mb.askyesno(title, message, parent=self.root))
            except Exception:
                return False
        return self._on_main(run)

    def ask_sync(self, changes):
        """立即同步確認窗：列出變更清單，使用者按「同意」才回傳 True。"""
        def run():
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
                          "delete": "刪除", "rename": "重新命名"}.get(
                              c.get("action", "add"), "新增")
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
        return self._on_main(run)

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
        win.geometry("420x440+40+40")
        win.protocol("WM_DELETE_WINDOW", win.withdraw)
        ttk.Label(win, text="AI File — 正在監控",
                  font=("Microsoft JhengHei", 12, "bold")).pack(
                      anchor="w", padx=12, pady=(10, 2))
        self.float_status = ttk.Label(win, text="狀態：啟動中…",
                                      font=("Microsoft JhengHei", 10))
        self.float_status.pack(anchor="w", padx=12)
        self.float_conn = ttk.Label(win, text="連線狀態：—",
                                    font=("Microsoft JhengHei", 10))
        self.float_conn.pack(anchor="w", padx=12)
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
        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=12, pady=(0, 8))
        ttk.Button(btns, text="建立桌面捷徑",
                   command=self.agent.create_shortcut_manual
                   ).pack(side="left", padx=(0, 4))
        ttk.Button(btns, text="檢查更新",
                   command=self.agent.check_update_manual
                   ).pack(side="left", padx=(0, 4))
        ttk.Button(btns, text="測試連線",
                   command=self.agent.test_connection_manual
                   ).pack(side="left", padx=(0, 4))
        ttk.Button(btns, text="重設資料",
                   command=self.agent.reset_data
                   ).pack(side="left")
        btns2 = ttk.Frame(win)
        btns2.pack(fill="x", padx=12, pady=(0, 4))
        ttk.Button(btns2, text="紀錄",
                   command=self.agent.open_history
                   ).pack(side="left", padx=(0, 4))
        ttk.Button(btns2, text="結束程式",
                   command=self.agent.quit_program
                   ).pack(side="left")
        ttk.Label(win, text=("關掉視窗只會隱藏，程式還在跑；"
                  "對系統匣圖示按右鍵可以顯示視窗或結束程式。"
                  if self.tray_available else
                  "關掉視窗只會隱藏，程式還在跑；"
                  "要真正結束請按「結束程式」。"),
                  font=("Microsoft JhengHei", 9)).pack(
                      anchor="w", padx=12, pady=(0, 4))
        stop_btn = tk.Button(
            win, text="■ 停止", font=("Microsoft JhengHei", 14, "bold"),
            bg="#c0392b", fg="white", height=2,
            command=self.agent.stop_from_local)
        stop_btn.pack(fill="x", padx=12, pady=(0, 12))
        self.float_win = win

    def open_history(self, agent):
        """同步紀錄視窗：由新到舊列出每一批，每批有「返回」按鈕。"""
        import tkinter as tk
        from tkinter import ttk
        history = load_history()
        win = tk.Toplevel(self.root)
        win.title("AI File：同步紀錄")
        win.attributes("-topmost", True)
        win.geometry("640x480")
        ttk.Label(win, text="同步紀錄（只留最近 20 批，舊的自動清掉）",
                  font=("Microsoft JhengHei", 12, "bold")).pack(
                      anchor="w", padx=16, pady=(12, 4))
        ttk.Label(win, text="按「返回」可以回到該批同步前的狀態："
                  "被改過／刪掉的檔案從備份還原，被新增的檔案刪掉。",
                  font=("Microsoft JhengHei", 10)).pack(
                      anchor="w", padx=16, pady=(0, 8))
        frame = ttk.Frame(win)
        frame.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        if not history:
            ttk.Label(frame, text="還沒有同步紀錄。").pack(anchor="w")
            return
        canvas = tk.Canvas(frame)
        scroll = ttk.Scrollbar(frame, orient="vertical",
                               command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(
            scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for batch in history:
            row = ttk.Frame(inner, relief="solid", borderwidth=1)
            row.pack(fill="x", pady=4)
            head = "%s · %s · %d 個變更" % (
                batch.get("at", "")[:19].replace("T", " "),
                batch.get("ai_name", ""), len(batch.get("items", [])))
            ttk.Label(row, text=head,
                      font=("Microsoft JhengHei", 11, "bold")).pack(
                          anchor="w", padx=10, pady=(6, 2))
            for item in batch.get("items", []):
                verb = {"add": "新增", "modify": "修改",
                        "delete": "刪除", "rename": "重新命名"}.get(
                            item.get("action", "add"), "新增")
                label = "  [%s] %s" % (verb, item.get("rel_path", ""))
                if item.get("action") == "rename" and item.get("new_rel"):
                    label += " → %s" % item["new_rel"]
                ttk.Label(row, text=label,
                    font=("Microsoft JhengHei", 10)).pack(
                        anchor="w", padx=10)
            if batch.get("undone"):
                ttk.Label(row, text="已返回（%s）" % batch.get(
                    "undone_at", "")[:19],
                    font=("Microsoft JhengHei", 10)).pack(
                        anchor="w", padx=10, pady=(2, 6))
            else:
                ttk.Button(row, text="返回",
                           command=lambda bid=batch["id"], w=win: (
                               self._do_undo(agent, bid, w))
                           ).pack(anchor="w", padx=10, pady=(2, 6))

    def _do_undo(self, agent, batch_id, win):
        import tkinter.messagebox as mb
        if not mb.askyesno(
                "AI File：返回",
                "確定要把這批同步返回到同步前的狀態嗎？\n"
                "被改過／刪掉的檔案會從備份還原，被新增的檔案會刪掉。",
                parent=win):
            return
        # 還原要讀寫磁碟和網路，在背景跑，視窗不卡住；做完再回主執行緒刷新
        def work():
            agent.undo_batch(batch_id)

            def refresh():
                try:
                    win.destroy()
                except Exception:
                    pass
                self.open_history(agent)
            self.call_on_main(refresh)
        threading.Thread(target=work, daemon=True).start()

    def quit_program(self):
        """真的結束整個程式（關掉浮動視窗只會隱藏，程式還在跑）。
        先移除系統匣圖示再關視窗，圖示才不會留在系統匣裡。"""
        def run():
            try:
                tray = getattr(self.agent, "tray", None)
                if tray is not None:
                    tray.stop()
            except Exception:
                pass
            try:
                self.root.destroy()
            except Exception:
                pass
        self._on_main(run)

    def add_activity(self, entry):
        def run():
            if self.float_list is None:
                return
            try:
                action = {"add": "新增", "modify": "修改", "delete": "刪除",
                          "rename": "重新命名",
                          "session-approved": "已允許連線",
                          "session-rejected": "已拒絕連線",
                          "rejected-path": "已拒絕越界",
                          "stopped": "已停止",
                          "undone": "已返回",
                          "undo-requested": "要求返回",
                          "undo-declined": "已拒絕返回",
                          "synced": "已同步"}.get(entry.get("action", ""),
                                                    entry.get("action", ""))
                text = "%s [%s] %s %s" % (
                    entry.get("at", "")[11:19], entry.get("ai_name", ""),
                    action, entry.get("rel_path", ""))
                self.float_list.insert(0, text.strip())
            except Exception:
                pass
        self._on_main(run)

    def set_status(self, text):
        def run():
            if self.float_status is not None:
                try:
                    self.float_status.config(text="狀態：" + text)
                except Exception:
                    pass
        self._on_main(run)

    def set_conn(self, text):
        def run():
            if self.float_conn is not None:
                try:
                    self.float_conn.config(text="連線狀態：" + text)
                except Exception:
                    pass
        self._on_main(run)

    def set_code(self, code):
        def run():
            if self.float_code is not None:
                try:
                    self.float_code.config(text="工作階段代碼：" + code)
                except Exception:
                    pass
        self._on_main(run)


class HeadlessUI:
    """沒有視窗時的 UI（測試用）：通知印出來，確認一律回傳預設值。"""

    def __init__(self, approve=False):
        self.approve = approve
        self.notices = []
        self.activities = []
        self.quit_requested = False

    def call_on_main(self, fn):
        fn()

    def call_on_main_sync(self, fn):
        return fn()

    def quit_app(self):
        self.quit_requested = True

    def quit_program(self):
        self.quit_requested = True

    def open_history(self, agent):
        pass

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

    def set_conn(self, text):
        print("[連線狀態] " + text)

    def set_code(self, code):
        print("[代碼] " + code)


# ---------------------------------------------------------------- 系統匣（Windows）
class TrayIcon:
    """Windows 系統匣圖示。只用 Python 標準函式庫（ctypes），不依賴 pywin32，
    也不需要 pip。非 Windows 上 start() 直接回傳 False，什麼都不做。
    右鍵選單只有兩項：「顯示視窗」「結束程式」；左鍵雙擊顯示視窗。
    選單命令用 queue 傳回主執行緒（main 裡的 root.after 定期取用），
    訊息執行緒絕對不直接動 tkinter。"""

    WM_USER_TRAY = 0x0400 + 1
    WM_RBUTTONUP = 0x0205
    WM_LBUTTONDBLCLK = 0x0203
    WM_COMMAND = 0x0111
    WM_QUIT = 0x0012
    NIM_ADD = 0x00000000
    NIM_DELETE = 0x00000002
    NIF_MESSAGE = 0x00000001
    NIF_ICON = 0x00000002
    NIF_TIP = 0x00000004
    TPM_RETURNCMD = 0x0100
    TPM_RIGHTBUTTON = 0x0002
    ID_SHOW = 1001
    ID_QUIT = 1002

    def __init__(self, on_show, on_quit):
        self.on_show = on_show
        self.on_quit = on_quit
        self.commands = queue.Queue()
        self._hwnd = None
        self._nid = None
        self._user32 = None
        self._shell32 = None
        self._wndproc = None
        self._running = False
        self._thread = None
        self._available = False

    @property
    def available(self):
        return self._available

    def start(self):
        """建立系統匣圖示。非 Windows 回傳 False；失敗只記 log，回傳 False，
        程式照常跑（沒有系統匣也能用）。"""
        if sys.platform != "win32":
            return False
        try:
            import ctypes
            import ctypes.wintypes as wt

            user32 = ctypes.windll.user32
            shell32 = ctypes.windll.shell32
            kernel32 = ctypes.windll.kernel32

            WNDPROC = ctypes.WINFUNCTYPE(
                wt.LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

            def wndproc(hwnd, msg, wparam, lparam):
                if msg == self.WM_USER_TRAY:
                    if lparam == self.WM_RBUTTONUP:
                        self._show_menu(hwnd)
                    elif lparam == self.WM_LBUTTONDBLCLK:
                        self.commands.put("show")
                    return 0
                if msg == self.WM_COMMAND:
                    cmd = wparam & 0xFFFF
                    if cmd == self.ID_SHOW:
                        self.commands.put("show")
                    elif cmd == self.ID_QUIT:
                        self.commands.put("quit")
                    return 0
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

            # WndProc 的參考一定要留著，否則會被 GC 收掉、訊息一來就當掉
            self._wndproc = WNDPROC(wndproc)

            class WNDCLASSW(ctypes.Structure):
                _fields_ = [
                    ("style", wt.UINT),
                    ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wt.HINSTANCE),
                    ("hIcon", wt.HICON),
                    ("hCursor", wt.HCURSOR),
                    ("hbrBackground", wt.HBRUSH),
                    ("lpszMenuName", wt.LPCWSTR),
                    ("lpszClassName", wt.LPCWSTR),
                ]

            hinstance = kernel32.GetModuleHandleW(None)
            classname = "AIFileTrayWindow"
            wc = WNDCLASSW()
            wc.style = 0
            wc.lpfnWndProc = self._wndproc
            wc.cbClsExtra = 0
            wc.cbWndExtra = 0
            wc.hInstance = hinstance
            wc.hIcon = None
            wc.hCursor = None
            wc.hbrBackground = None
            wc.lpszMenuName = None
            wc.lpszClassName = classname
            if not user32.RegisterClassW(ctypes.byref(wc)):
                raise RuntimeError("RegisterClassW 失敗")
            hwnd = user32.CreateWindowExW(
                0, classname, "AI File", 0,
                0, 0, 0, 0, None, None, hinstance, None)
            if not hwnd:
                raise RuntimeError("CreateWindowExW 失敗")
            self._hwnd = hwnd

            # 圖示：優先從 pythonw.exe 抓，抓不到就用系統預設圖示
            hicon = None
            try:
                pythonw = os.path.join(
                    os.path.dirname(sys.executable), "pythonw.exe")
                hicon = shell32.ExtractIconW(hinstance, pythonw, 0)
            except Exception:
                hicon = None
            if not hicon:
                hicon = user32.LoadIconW(None, 32512)  # IDI_APPLICATION

            class NOTIFYICONDATAW(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wt.DWORD),
                    ("hWnd", wt.HWND),
                    ("uID", wt.UINT),
                    ("uFlags", wt.UINT),
                    ("uCallbackMessage", wt.UINT),
                    ("hIcon", wt.HICON),
                    ("szTip", wt.WCHAR * 128),
                    ("dwState", wt.DWORD),
                    ("dwStateMask", wt.DWORD),
                    ("szInfo", wt.WCHAR * 256),
                    ("uVersion", wt.UINT),
                    ("szInfoTitle", wt.WCHAR * 64),
                    ("dwInfoFlags", wt.DWORD),
                    ("guidItem", wt.BYTE * 16),
                    ("hBalloonIcon", wt.HICON),
                ]

            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = hwnd
            nid.uID = 1
            nid.uFlags = self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP
            nid.uCallbackMessage = self.WM_USER_TRAY
            nid.hIcon = hicon
            nid.szTip = "AI File"
            if not shell32.Shell_NotifyIconW(self.NIM_ADD,
                                             ctypes.byref(nid)):
                raise RuntimeError("Shell_NotifyIconW 失敗")
            self._nid = nid
            self._user32 = user32
            self._shell32 = shell32
            self._available = True
            self._running = True
            self._thread = threading.Thread(target=self._message_loop,
                                            daemon=True)
            self._thread.start()
            log_local("系統匣圖示已建立（右鍵：顯示視窗／結束程式）")
            return True
        except Exception as e:
            log_local("系統匣啟用失敗（繼續使用，沒有系統匣也能用）：%s" % e)
            return False

    def _message_loop(self):
        """獨立訊息迴圈（daemon thread）：收 Windows 訊息、轉給 WndProc。"""
        import ctypes
        import ctypes.wintypes as wt

        class MSG(ctypes.Structure):
            _fields_ = [
                ("hwnd", wt.HWND),
                ("message", wt.UINT),
                ("wParam", wt.WPARAM),
                ("lParam", wt.LPARAM),
                ("time", wt.DWORD),
                ("pt", wt.POINT),
            ]

        msg = MSG()
        user32 = self._user32
        while self._running:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _show_menu(self, hwnd):
        """右鍵彈出選單（在訊息執行緒裡跑，只把選到的命令放進 queue）。"""
        try:
            import ctypes
            import ctypes.wintypes as wt

            user32 = self._user32
            menu = user32.CreatePopupMenu()
            if not menu:
                return
            user32.AppendMenuW(menu, 0, self.ID_SHOW, "顯示視窗")
            user32.AppendMenuW(menu, 0, self.ID_QUIT, "結束程式")
            pt = wt.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            user32.SetForegroundWindow(hwnd)
            cmd = user32.TrackPopupMenu(
                menu, self.TPM_RETURNCMD | self.TPM_RIGHTBUTTON,
                pt.x, pt.y, 0, hwnd, None)
            if cmd == self.ID_SHOW:
                self.commands.put("show")
            elif cmd == self.ID_QUIT:
                self.commands.put("quit")
            user32.DestroyMenu(menu)
        except Exception as e:
            log_local("系統匣選單失敗：%s" % e)

    def poll(self):
        """主執行緒定期呼叫：把選單命令交給 tkinter 主執行緒處理。"""
        try:
            while True:
                cmd = self.commands.get_nowait()
                if cmd == "show":
                    try:
                        self.on_show()
                    except Exception:
                        pass
                elif cmd == "quit":
                    try:
                        self.on_quit()
                    except Exception:
                        pass
        except queue.Empty:
            pass

    def stop(self):
        """移除系統匣圖示、結束訊息迴圈（真正結束程式時呼叫）。"""
        if not self._available:
            return
        self._running = False
        try:
            self._shell32.Shell_NotifyIconW(self.NIM_DELETE,
                                            ctypes.byref(self._nid))
        except Exception:
            pass
        try:
            self._user32.PostMessageW(self._hwnd, self.WM_QUIT, 0, 0)
        except Exception:
            pass
        try:
            self._user32.DestroyWindow(self._hwnd)
        except Exception:
            pass
        self._available = False
        log_local("系統匣圖示已移除")


# ---------------------------------------------------------------- Agent 主邏輯
class Agent:
    def __init__(self, config, relay, ui):
        self.config = config
        self.relay = relay
        self.ui = ui
        self.allowed_folder = config.get("allowed_folder", "")
        self.session_code = config.get("session_code", "")
        self.stopped = False
        self._resetting = False
        self._last_manifest_sig = None
        self._seen_waiting = set()
        self._seen_undo = set()
        self._seen_list_requests = set()
        self._seen_content_requests = set()
        self._last_heartbeat = 0
        self._pending_notice_sig = None
        self._last_poll_ok_at = 0
        self._last_poll_error = ""
        self.tray = None          # 系統匣圖示（main 啟動時設定）
        self._poll_running = False  # 背景輪詢同時只跑一輪

    # -- 連線狀態 --------------------------------------------------
    def _note_conn_ok(self):
        self._last_poll_ok_at = time.time()
        self._last_poll_error = ""
        self.ui.set_conn("連線正常，最後成功：%s" % time.strftime(
            "%H:%M:%S", time.localtime(self._last_poll_ok_at)))

    def _note_conn_error(self, exc):
        self._last_poll_error = relay_error_reason(exc)
        self.ui.set_conn("連線失敗：" + self._last_poll_error)

    def _note_conn_error_text(self, text):
        self._last_poll_error = text
        self.ui.set_conn("連線失敗：" + text)

    def _refresh_conn_status(self):
        """每輪結束後看 relay 的 last_error，更新連線狀態行。
        輪詢失敗不再只寫 log，浮動視窗直接看得到原因。"""
        err = getattr(self.relay, "last_error", None)
        if err is not None:
            self._note_conn_error(err)
        else:
            self._note_conn_ok()

    # -- 桌面捷徑／更新／測試連線／重設 ---------------------------
    def create_shortcut_manual(self):
        def work():
            ok = create_desktop_shortcut()
            self.ui.call_on_main(lambda: self.ui.notify(
                "AI File",
                "已建立桌面捷徑：AI File.lnk" if ok
                else "建立桌面捷徑失敗，請看 ai-file-log.txt"))
        threading.Thread(target=work, daemon=True).start()

    def check_update_manual(self):
        """手動檢查更新：只檢查，有新版才問要不要更新。"""
        def work():
            has_new, remote = check_for_update()

            def done():
                if not has_new:
                    self.ui.notify("AI File",
                                   "已經是最新版（v%s）。" % VERSION)
                    return
                if self.ui.ask_yes_no(
                        "AI File：有新版 v%s" % remote,
                        "目前是 v%s，有新版 v%s，要現在更新嗎？\n"
                        "更新完會自動重開。" % (VERSION, remote)):
                    if perform_update(remote):
                        self.ui.set_status("正在更新到 v%s…" % remote)
                        run_updater_and_exit()
                        self.ui.quit_app()
                    else:
                        self.ui.notify(
                            "AI File",
                            "更新失敗，請看 ai-file-log.txt，繼續用舊版。")
            self.ui.call_on_main(done)
        threading.Thread(target=work, daemon=True).start()

    def test_connection_manual(self):
        def work():
            ok, msg = test_connection(self.relay)

            def done():
                if ok:
                    self._note_conn_ok()
                else:
                    self._note_conn_error_text(msg)
                self.ui.notify("AI File：測試連線", msg)
            self.ui.call_on_main(done)
        threading.Thread(target=work, daemon=True).start()

    def open_history(self):
        """打開同步紀錄視窗。"""
        self.ui.open_history(self)

    def quit_program(self):
        """真的結束整個程式（關掉浮動視窗只會隱藏，程式還在跑）。"""
        try:
            self.ui.quit_program()
        except Exception:
            pass

    # -- 備份與返回 ------------------------------------------------
    def undo_batch(self, batch_id):
        """把一批同步返回到同步前的狀態：被改過／刪掉的檔案從備份還原，
        被新增的檔案刪掉。一批只能返回一次。"""
        history = load_history()
        batch = next((b for b in history if b.get("id") == batch_id), None)
        if batch is None:
            append_activity(self.relay, {
                "action": "undo-failed", "ai_name": "",
                "batch_id": batch_id,
                "detail": "找不到這批同步的備份（可能已超過 20 批被清掉）"})
            self.ui.notify("AI File：找不到備份",
                           "找不到這批同步的備份（可能已經超過 20 批被清掉）。")
            log_local("返回失敗：找不到批次 %s" % batch_id)
            return False
        if batch.get("undone"):
            self.ui.notify("AI File", "這批已經返回過了。")
            return False
        ok_count = 0
        fail_count = 0
        batch_dir = os.path.join(BACKUP_DIR, batch_id)
        for item in reversed(batch.get("items", [])):
            rel = item.get("rel_path", "")
            try:
                target = safe_join(self.allowed_folder, rel)
            except ValueError:
                fail_count += 1
                continue
            try:
                if item.get("action") == "rename":
                    # 重新命名：把檔案從 new_rel 改回 rel。
                    # new_rel 不存在或 rel 已被佔用就跳過，絕對不誤刪。
                    new_rel = item.get("new_rel", "")
                    try:
                        new_target = safe_join(self.allowed_folder,
                                               new_rel)
                    except ValueError:
                        fail_count += 1
                        log_local("返回失敗：重新命名目標路徑不合法 %s"
                                  % new_rel)
                        continue
                    if not os.path.exists(new_target):
                        fail_count += 1
                        log_local("返回失敗：%s 已經不在了，無法改回 %s"
                                  % (new_rel, rel))
                    elif os.path.exists(target):
                        fail_count += 1
                        log_local("返回失敗：%s 已被佔用，不覆蓋" % rel)
                    else:
                        parent = os.path.dirname(target)
                        if parent:
                            os.makedirs(parent, exist_ok=True)
                        os.rename(new_target, target)
                        ok_count += 1
                    continue
                if item.get("existed"):
                    src = os.path.join(
                        batch_dir, *item["backup"].split("/"))
                    if not os.path.exists(src):
                        fail_count += 1
                        log_local("返回失敗：備份遺失 %s" % rel)
                        continue
                    if item.get("is_dir"):
                        # 被刪掉的整棵資料夾：目標已存在就不覆蓋
                        if os.path.exists(target):
                            fail_count += 1
                            log_local("返回失敗：%s 已存在，不覆蓋" % rel)
                            continue
                        parent = os.path.dirname(target)
                        if parent:
                            os.makedirs(parent, exist_ok=True)
                        shutil.copytree(src, target)
                    else:
                        parent = os.path.dirname(target)
                        if parent:
                            os.makedirs(parent, exist_ok=True)
                        shutil.copyfile(src, target)
                else:
                    if os.path.exists(target) \
                            and os.path.isfile(target):
                        os.remove(target)
                ok_count += 1
            except OSError as e:
                fail_count += 1
                log_local("返回失敗：%s（%s）" % (rel, e))
        batch["undone"] = True
        batch["undone_at"] = utcnow_iso()
        try:
            save_history(history)
        except OSError as e:
            log_local("儲存同步紀錄失敗：%s" % e)
        append_activity(self.relay, {
            "action": "undone", "ai_name": batch.get("ai_name", ""),
            "batch_id": batch_id,
            "detail": "已返回到同步前的狀態（%d 個檔案還原，%d 個失敗）"
                      % (ok_count, fail_count)})
        self.ui.add_activity({
            "action": "undone", "ai_name": batch.get("ai_name", ""),
            "rel_path": "", "at": utcnow_iso()})
        self.ui.notify("AI File：已返回",
                       "已回到同步前的狀態：%d 個檔案還原，%d 個失敗。"
                       % (ok_count, fail_count))
        log_local("已返回批次 %s：%d 還原，%d 失敗"
                  % (batch_id, ok_count, fail_count))
        # 返回完更新一次快照，檔案總管才看得到還原後的樣子
        self.publish_snapshot()
        return fail_count == 0

    def _check_undo(self):
        """輪詢 undo.json：管理員在網頁版按了「返回」，跳確認對話框，
        使用者按確定才執行還原，完成後刪掉 undo.json。"""
        try:
            undo, _ = self.relay.get_json("undo.json")
        except Exception:
            return
        if not isinstance(undo, dict) or not undo.get("batch_id"):
            return
        batch_id = undo["batch_id"]
        if batch_id in self._seen_undo:
            return
        self._seen_undo.add(batch_id)
        ok = self.ui.ask_yes_no(
            "AI File：管理員要求返回",
            "管理員在網頁版要求把一批同步返回到同步前的狀態：\n"
            "批次 %s\n\n要執行還原嗎？\n"
            "（被改過／刪掉的檔案從備份還原，被新增的檔案刪掉。）"
            % batch_id)
        if ok:
            self.undo_batch(batch_id)
        else:
            append_activity(self.relay, {
                "action": "undo-declined", "ai_name": "",
                "batch_id": batch_id,
                "detail": "使用者拒絕了網頁版的返回要求"})
            log_local("使用者拒絕返回批次 %s" % batch_id)
        try:
            self.relay.delete("undo.json", "AI File：已處理返回要求")
        except Exception:
            pass

    # -- 快照（檔案總管用） ----------------------------------------
    def publish_snapshot(self):
        """對允許的資料夾做一次快照，寫到中繼的 snapshot.json。
        快照是顯示用的，不依賴工作階段狀態；失敗只寫 log，不擋主流程。"""
        try:
            snap = build_snapshot(self.allowed_folder)
            self.relay.put_json("snapshot.json", snap, "AI File：更新快照")
            log_local("已更新快照：%d 個檔案、%d 個資料夾%s" % (
                snap["total_files"], snap["total_dirs"],
                "（已截斷，只列前 2000 筆）" if snap["truncated"] else ""))
            return True
        except Exception as e:
            log_local("更新快照失敗：%s" % e)
            return False

    def _check_list_request(self):
        """輪詢 list_request.json：網頁版要求重新整理檔案總管時，
        重新產生快照、寫 snapshot.json、刪掉 request 檔。"""
        try:
            req, _ = self.relay.get_json("list_request.json")
        except Exception:
            return
        if not isinstance(req, dict) or not req.get("request_id"):
            return
        request_id = req["request_id"]
        if request_id in self._seen_list_requests:
            return
        self._seen_list_requests.add(request_id)
        log_local("收到檔案總管重新整理要求：%s" % request_id)
        self.publish_snapshot()
        try:
            self.relay.delete("list_request.json",
                              "AI File：已處理檔案總管重新整理要求")
        except Exception:
            pass

    def _check_content_request(self):
        """輪詢 content_request.json：網頁版要讀一個檔案的內容
        （編輯／下載用）。讀到就把 base64 寫進 content_response.json，
        再刪掉 request 檔。超過 10 MB 或讀不到就回 error。
        路徑一定通過守門；這是管理員（使用者本人）的要求，不依賴
        工作階段狀態，但停止旗標開啟時也不讀（停了就是停了）。"""
        if self.stopped:
            return
        try:
            req, _ = self.relay.get_json("content_request.json")
        except Exception:
            return
        if not isinstance(req, dict) or not req.get("request_id") \
                or not req.get("path"):
            return
        request_id = req["request_id"]
        if request_id in self._seen_content_requests:
            return
        self._seen_content_requests.add(request_id)
        rel = req["path"]
        response = {"request_id": request_id, "path": rel, "ok": False}
        try:
            target = safe_join(self.allowed_folder, rel)
            if os.path.isdir(target):
                response["error"] = "這是資料夾，無法讀取內容"
            elif not os.path.exists(target):
                response["error"] = "找不到檔案"
            elif os.path.getsize(target) > CONTENT_MAX_BYTES:
                response["error"] = "檔案太大（超過 10 MB）"
            else:
                with open(target, "rb") as f:
                    data = f.read()
                response["ok"] = True
                response["content_b64"] = base64.b64encode(
                    data).decode("ascii")
                response["size"] = len(data)
        except ValueError as e:
            response["error"] = "路徑不合法：%s" % e
            log_local("拒絕讀取檔案內容（越界）：%s" % rel)
        except OSError as e:
            response["error"] = "讀取失敗：%s" % e
        try:
            self.relay.put_json("content_response.json", response,
                                "AI File：回傳檔案內容")
        except Exception as e:
            log_local("寫入 content_response.json 失敗：%s" % e)
        try:
            self.relay.delete("content_request.json",
                              "AI File：已處理檔案內容要求")
        except Exception:
            pass

    def reset_data(self, wizard_fn=None):
        """重設資料：確認後停止輪詢、刪除設定與紀錄檔、重新開設定精靈。
        本機資料只有程式資料夾裡的 ai-file-config.json 和 ai-file-log.txt，
        沒有登錄檔、沒有 AppData。"""
        ok = self.ui.ask_yes_no(
            "AI File：重設資料",
            "確定要清除全部本機資料嗎？\n\n會刪除：\n"
            "・GitHub Token\n"
            "・允許的資料夾\n"
            "・工作階段代碼\n"
            "・本機紀錄（ai-file-log.txt）\n"
            "・同步紀錄與備份（ai-file-history.json、ai-file-backup/）\n\n"
            "刪除後會重新打開第一次設定精靈。")
        if not ok:
            return False
        self._resetting = True
        self.stopped = True
        for path in (CONFIG_FILE, LOG_FILE, HISTORY_FILE):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError as e:
                log_local("刪除 %s 失敗：%s" % (path, e))
        try:
            if os.path.isdir(BACKUP_DIR):
                shutil.rmtree(BACKUP_DIR, ignore_errors=True)
        except OSError as e:
            log_local("刪除備份資料夾失敗：%s" % e)
        # 注意：這裡不能再呼叫 log_local，否則會把剛刪的 log 檔又建回來
        print("AI File：本機資料已清除，重新開啟設定精靈")
        fn = wizard_fn or run_setup_wizard
        root = getattr(self.ui, "root", None)
        res = fn(root)
        self._resetting = False
        if res.get("saved"):
            new_cfg = res["config"]
            self.config = new_cfg
            self.allowed_folder = new_cfg.get("allowed_folder", "")
            self.session_code = new_cfg.get("session_code", "")
            self.relay = GitHubRelay(
                new_cfg["pat"], new_cfg.get("relay_owner", RELAY_OWNER),
                new_cfg.get("relay_repo", RELAY_REPO))
            self.stopped = False
            self._last_manifest_sig = None
            self._seen_waiting = set()
            self._pending_notice_sig = None
            self.ui.set_code(self.session_code)
            self.ui.set_status("監控中 — %s" % os.path.basename(
                os.path.normpath(self.allowed_folder)))
            # 心跳和快照是網路操作，在背景跑，不卡設定精靈關掉後的畫面
            threading.Thread(target=lambda: (
                self.send_heartbeat(force=True),
                self.publish_snapshot()), daemon=True).start()
            log_local("重設完成，已用新設定繼續監控")
            return True
        # 使用者關掉精靈沒存：沒有設定了，停在停止狀態
        self.ui.set_status("已重設 — 尚未設定，請重新啟動 AI File")
        self.ui.notify("AI File",
                       "已清除資料，但沒有完成新的設定。\n"
                       "請重新啟動 AI File 再設定一次。")
        return False

    # -- 停止 ------------------------------------------------------
    def stop_from_local(self):
        self.stopped = True
        self.ui.set_status("已停止 — Muse 動不到任何檔案")

        def work():
            try:
                self.relay.put_json("stop.json", {
                    "stopped": True, "by": "local", "at": utcnow_iso()},
                    "AI File：本地端按下停止")
                append_activity(self.relay, {
                    "action": "stopped", "ai_name": "",
                    "detail": "使用者在本地端按下停止"})
            except Exception as e:
                log_local("寫入停止旗標失敗：%s" % e)

            def done():
                self.ui.notify("AI File 已停止",
                               "已經停止，Muse 現在動不到你的檔案。\n"
                               "要恢復請到網頁版管理員介面按「恢復」，"
                               "或重新啟動 AI File。")
            self.ui.call_on_main(done)
            log_local("使用者在本地端按下停止")
        # 寫中繼是網路操作，在背景跑，浮動視窗按下去立刻有反應
        threading.Thread(target=work, daemon=True).start()

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
            session, session_sha = self.relay.get_json("session.json")
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
            try:
                self.relay.put_json("session.json", session,
                                    "AI File：允許工作階段", sha=session_sha)
            except RelayError as e:
                if getattr(e, "status", None) == 409:
                    # 寫入的當下 session.json 剛被另一邊改過：下輪重讀再問一次
                    self._seen_waiting.discard(key)
                    log_local("允許工作階段時遇到 409，下輪重讀再問一次")
                    return
                raise
            append_activity(self.relay, {
                "action": "session-approved", "ai_name": ai_name})
            self.ui.notify("AI File", "已允許「%s」連線。" % ai_name)
            self.ui.add_activity({"action": "session-approved",
                                  "ai_name": ai_name, "at": utcnow_iso()})
            log_local("已允許工作階段：%s" % ai_name)
        else:
            session["status"] = "stopped"
            try:
                self.relay.put_json("session.json", session,
                                    "AI File：拒絕工作階段", sha=session_sha)
            except RelayError as e:
                if getattr(e, "status", None) == 409:
                    self._seen_waiting.discard(key)
                    log_local("拒絕工作階段時遇到 409，下輪重讀再問一次")
                    return
                raise
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
        # 工作階段必須是已允許，才會跳出同步確認；但管理員（使用者本人）
        # 在檔案總管暫存的項目（by="admin"）不用等工作階段——使用者在
        # 同步確認窗按「同意」本身就是授權。AI 的項目仍要先按「是」。
        admin_only = all(f.get("by") == "admin" for f in files)
        if not admin_only:
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
        成功寫入後才從中繼清掉該筆；全部完成後更新 manifest。
        寫入任何檔案之前，先把所有受影響檔案的目前內容備份到
        ai-file-backup/<批次 id>/，才能事後「返回」。"""
        batch_id = new_batch_id()
        batch_dir = os.path.join(BACKUP_DIR, batch_id)
        history_items = []
        results = []
        remaining = []
        # 0. 先備份：還沒寫入任何東西之前，備份所有受影響檔案的目前內容
        # backups[idx] = (existed, backup_rel, error, is_dir)
        backups = {}
        for idx, entry in enumerate(files):
            rel = entry.get("rel_path", "")
            action = entry.get("action", "add")
            try:
                target = safe_join(self.allowed_folder, rel)
            except ValueError:
                backups[idx] = (False, None, "path", False)
                continue
            if os.path.isdir(target) and action in ("delete", "rename"):
                # 刪除／重新命名資料夾：整棵樹先備份起來，才能事後返回
                try:
                    clean = sanitize_rel_path(rel)
                    dest = os.path.join(batch_dir, *clean.split("/"))
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.copytree(target, dest)
                    backups[idx] = (True, clean, None, True)
                except OSError as e:
                    backups[idx] = (False, None, "備份失敗：%s" % e, True)
            elif os.path.isfile(target):
                try:
                    clean = sanitize_rel_path(rel)
                    dest = os.path.join(batch_dir, *clean.split("/"))
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.copyfile(target, dest)
                    backups[idx] = (True, clean, None, False)
                except OSError as e:
                    backups[idx] = (False, None, "備份失敗：%s" % e, False)
            elif os.path.exists(target):
                backups[idx] = (False, None, "是資料夾，無法處理", False)
            else:
                backups[idx] = (False, None, None, False)
        for idx, entry in enumerate(files):
            rel = entry.get("rel_path", "")
            action = entry.get("action", "add")
            ai_name = entry.get("ai_name", "")
            result = {"rel_path": rel, "action": action,
                      "ai_name": ai_name, "ok": False}
            # 1. 工作階段代碼必須是當下有效的；但管理員（使用者本人）
            # 在檔案總管暫存的項目（by="admin"）不用檢查——那是他用
            # 自己的金鑰寫的，真正的授權是他稍後在同步確認窗按「同意」。
            if entry.get("by") != "admin" \
                    and entry.get("session_code") != self.session_code:
                result["error"] = "工作階段代碼不符，已拒收"
                self._record_reject(entry, "工作階段代碼不符", batch_id)
                results.append(result)
                remaining.append(entry)
                continue
            # 2. 路徑守門
            try:
                target = safe_join(self.allowed_folder, rel)
            except ValueError as e:
                result["error"] = str(e)
                self._record_reject(entry, str(e), batch_id)
                results.append(result)
                continue  # 越界的直接丟掉，不留在待同步
            # 2b. 備份失敗的檔案不寫入，留在待同步等下次
            backup_error = backups.get(idx, (False, None, None, False))[2]
            if backup_error and backup_error != "path":
                result["error"] = backup_error
                remaining.append(entry)
                results.append(result)
                continue
            # 3. 刪除（檔案或整棵資料夾）
            if action == "delete":
                try:
                    if os.path.isdir(target):
                        shutil.rmtree(target)
                    elif os.path.exists(target):
                        os.remove(target)
                    result["ok"] = True
                except OSError as e:
                    result["error"] = "刪除失敗：%s" % e
                    remaining.append(entry)
                self._record_applied(entry, result, batch_id)
                results.append(result)
                if result["ok"]:
                    history_items.append({
                        "rel_path": rel, "action": action,
                        "existed": backups[idx][0],
                        "backup": backups[idx][1],
                        "is_dir": backups[idx][3]})
                continue
            # 3b. 重新命名（沒有暫存檔，直接改名）
            if action == "rename":
                new_rel = entry.get("new_rel", "")
                try:
                    new_target = safe_join(self.allowed_folder, new_rel)
                except ValueError as e:
                    result["error"] = "新路徑不合法：%s" % e
                    remaining.append(entry)
                    results.append(result)
                    continue
                try:
                    if not os.path.exists(target):
                        result["error"] = "要改名的檔案不存在"
                        remaining.append(entry)
                    elif os.path.exists(new_target):
                        result["error"] = "目標名稱已經存在，不覆蓋"
                        remaining.append(entry)
                    else:
                        parent = os.path.dirname(new_target)
                        if parent:
                            os.makedirs(parent, exist_ok=True)
                        os.rename(target, new_target)
                        result["ok"] = True
                except OSError as e:
                    result["error"] = "重新命名失敗：%s" % e
                    remaining.append(entry)
                self._record_applied(entry, result, batch_id)
                results.append(result)
                if result["ok"]:
                    history_items.append({
                        "rel_path": rel, "action": action,
                        "new_rel": sanitize_rel_path(new_rel),
                        "existed": backups[idx][0],
                        "backup": backups[idx][1],
                        "is_dir": backups[idx][3]})
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
                self._record_reject(entry, "雜湊驗證失敗", batch_id)
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
            self._record_applied(entry, result, batch_id)
            results.append(result)
            if result["ok"]:
                history_items.append({
                    "rel_path": rel, "action": action,
                    "existed": backups[idx][0],
                    "backup": backups[idx][1],
                    "is_dir": backups[idx][3]})
            # 6. 成功就從中繼刪掉暫存檔
            if result["ok"]:
                try:
                    self.relay.delete("pending/" + pending_name,
                                      "AI File：已同步，清除暫存")
                except Exception:
                    pass
        # 寫入同步紀錄（只記真的動到磁碟的檔案）；沒動到就不留備份
        if history_items:
            history = load_history()
            history.insert(0, {
                "id": batch_id, "at": utcnow_iso(),
                "ai_name": files[0].get("ai_name", "") if files else "",
                "undone": False, "items": history_items})
            prune_history(history)
            try:
                save_history(history)
            except OSError as e:
                log_local("儲存同步紀錄失敗：%s" % e)
        elif os.path.isdir(batch_dir):
            shutil.rmtree(batch_dir, ignore_errors=True)
        # 更新 manifest：從最新清單扣掉這批已處理的項目。
        # 不能直接用本批算出來的 remaining 蓋掉整份清單：同步進行時，
        # 網頁端可能同時暫存了新項目，直接蓋掉會把新項目刪掉。
        remove_entries = [e for e in files
                          if _manifest_entry_key(e) not in
                          {_manifest_entry_key(r) for r in remaining}]
        self._update_manifest_remove(remove_entries)
        # 同步完更新一次快照，檔案總管才看得到最新的樣子
        self.publish_snapshot()
        return results

    def _update_manifest_remove(self, remove_entries):
        """從中繼的待同步清單扣掉指定項目。先重讀最新清單再扣、再寫，
        409 就重讀一次再扣（最多 3 次），不會蓋掉網頁端同時暫存的新項目。
        回傳 True（寫好了）或 False（重試後還是失敗，只寫 log）。"""
        remove_keys = {_manifest_entry_key(e) for e in remove_entries}
        for attempt in range(1, WRITE_RETRY_MAX + 1):
            try:
                data, sha = self.relay.get_json("pending/manifest.json")
            except Exception as e:
                # 讀不到就不要寫：當成空清單寫回去會把別人的暫存項目清掉。
                log_local("讀取待同步清單失敗（第 %d 次）：%s" % (attempt, e))
                if attempt < WRITE_RETRY_MAX:
                    time.sleep(0.05 + random.random() * 0.15)
                    continue
                return False
            files = (data.get("files") if isinstance(data, dict)
                     else None) or []
            remaining = [f for f in files
                         if _manifest_entry_key(f) not in remove_keys]
            try:
                self.relay.put_json("pending/manifest.json",
                                    {"files": remaining},
                                    "AI File：更新待同步清單", retry=False,
                                    sha=sha, create_only=(sha is None))
                return True
            except RelayError as e:
                if getattr(e, "status", None) in (409, 422) \
                        and attempt < WRITE_RETRY_MAX:
                    log_local("更新待同步清單遇到 %s，重試第 %d 次"
                              % (getattr(e, "status", "?"), attempt))
                    time.sleep(0.05 + random.random() * 0.15)
                    continue
                log_local("更新待同步清單失敗（已重試 %d 次）：%s"
                          % (WRITE_RETRY_MAX, e))
                return False
            except Exception as e:
                log_local("更新待同步清單失敗：%s" % e)
                return False
        return False

    def _record_applied(self, entry, result, batch_id=None):
        verb = {"add": "新增", "modify": "修改",
                "delete": "刪除", "rename": "重新命名"}.get(
                    entry.get("action", "add"), "新增")
        if result.get("ok"):
            act = {
                "action": entry.get("action", "add"),
                "ai_name": entry.get("ai_name", ""),
                "rel_path": entry.get("rel_path", ""),
                "detail": "已同步"}
            if batch_id:
                act["batch_id"] = batch_id
            append_activity(self.relay, act)
            self.ui.add_activity({
                "action": entry.get("action", "add"),
                "ai_name": entry.get("ai_name", ""),
                "rel_path": entry.get("rel_path", ""),
                "at": utcnow_iso()})
            log_local("%s：%s" % (verb, entry.get("rel_path", "")))
        else:
            act = {
                "action": "sync-failed",
                "ai_name": entry.get("ai_name", ""),
                "rel_path": entry.get("rel_path", ""),
                "detail": result.get("error", "")}
            if batch_id:
                act["batch_id"] = batch_id
            append_activity(self.relay, act)
            log_local("%s失敗：%s（%s）" % (
                verb, entry.get("rel_path", ""), result.get("error", "")))

    def _record_reject(self, entry, reason, batch_id=None):
        act = {
            "action": "rejected-path",
            "ai_name": entry.get("ai_name", ""),
            "rel_path": entry.get("rel_path", ""),
            "detail": reason}
        if batch_id:
            act["batch_id"] = batch_id
        append_activity(self.relay, act)
        self.ui.add_activity({
            "action": "rejected-path",
            "ai_name": entry.get("ai_name", ""),
            "rel_path": entry.get("rel_path", ""),
            "at": utcnow_iso()})
        self.ui.notify("AI File：已拒絕一個要求",
                       "原因：%s\n路徑：%s" % (reason, entry.get("rel_path", "")))
        log_local("拒絕：%s（%s）" % (entry.get("rel_path", ""), reason))

    # -- 輪詢 ------------------------------------------------------
    def poll_in_thread(self):
        """在背景執行緒跑一輪輪詢，浮動視窗不會被網路卡住。
        同時只跑一輪：上一輪還沒跑完（例如 GitHub 很慢）就跳過這一輪，
        不會越積越多。對話框與狀態列由 TkUI 轉回主執行緒顯示。"""
        if self._poll_running:
            return
        self._poll_running = True

        def work():
            try:
                self.poll_once()
            except Exception:
                traceback.print_exc()
                log_local("輪詢出錯（已略過，下一輪繼續）")
            finally:
                self._poll_running = False

        threading.Thread(target=work, daemon=True).start()

    def poll_once(self):
        """跑一輪：停止旗標 → 心跳 → 工作階段 → 重新生成 → 待同步。"""
        if self._resetting:
            return
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
            # 停止只擋寫入與讀取檔案內容：快照重新整理是唯讀的清單，
            # 使用者本人在看自己的檔案，照常回答；但下載／編輯要先恢復。
            self._check_list_request()
            self._refresh_conn_status()
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
        self._check_undo()
        self._check_list_request()
        self._check_content_request()
        self._check_pending()
        self._refresh_conn_status()
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
    ttk.Label(win, text="這組金鑰只設定一次，存在你的電腦，"
              "絕對不要把這組給 AI；給 AI 的只有第 3 步的工作階段代碼。",
              font=("Microsoft JhengHei", 9, "bold")).pack(
                  anchor="w", padx=18)
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
        # 存檔後立刻測一次連線，失敗就跳清楚的錯誤，不要只寫警告就繼續跑
        r = relay or GitHubRelay(pat)
        ok, msg = test_connection(r)
        if not ok:
            mb.showerror("AI File：連線失敗",
                         "設定已存在本機，但連不到中繼 repo：\n%s\n\n"
                         "原因對照：\n%s" % (msg, CONNECTION_CAUSES),
                         parent=win)
            return
        # 把代碼雜湊寫進中繼，網頁版才能驗證 Muse 貼的代碼
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


def reset_approved_session_on_start(relay):
    """每次啟動時：如果中繼的 session 還是上次的 approved，重設回 waiting，
    AI 要重新取得一次「是」才行（否則重開程式就自動同意了）。"""
    try:
        session, session_sha = relay.get_json("session.json")
    except Exception as e:
        log_local("啟動時讀取 session.json 失敗：%s" % e)
        return
    if isinstance(session, dict) and session.get("status") == "approved":
        session["status"] = "waiting"
        session["reset_at"] = utcnow_iso()
        try:
            relay.put_json("session.json", session,
                           "AI File：啟動時重設工作階段為等待",
                           sha=session_sha)
            log_local("啟動時把上次已允許的工作階段重設回等待"
                      "（AI 要重新取得一次「是」）")
        except Exception as e:
            log_local("啟動時重設工作階段失敗：%s" % e)


def _make_shortcut_once(cfg):
    """第一次啟動時在背景建立桌面捷徑，成功才記下來，下次不再試。"""
    if create_desktop_shortcut():
        cfg["shortcut_created"] = True
        try:
            save_config(cfg)
        except OSError:
            pass


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
    ui._ensure_drain()  # 背景執行緒的介面工作由主執行緒的 queue 迴圈消化
    # 系統匣：Windows 才會真的建立圖示，其他平台安靜跳過
    agent.tray = TrayIcon(on_show=ui.open_floating,
                          on_quit=ui.quit_program)
    ui.tray_available = agent.tray.start()
    ui.open_floating()
    ui.set_code(agent.session_code)
    ui.set_status("監控中 — %s" % os.path.basename(
        os.path.normpath(agent.allowed_folder)))

    # 啟動時的網路工作（重設工作階段、檢查更新、心跳、快照）全部在背景跑：
    # 浮動視窗先出來，不等網路，GitHub 很慢也不會卡在啟動畫面
    def startup_work():
        # 啟動時把上次已允許的工作階段重設回等待：重開程式不等於自動同意
        try:
            reset_approved_session_on_start(relay)
        except Exception:
            pass
        # 啟動時自動更新：開始輪詢之前先檢查，有新版就更新、重啟
        has_new, remote = check_for_update()
        if has_new and perform_update(remote):
            def begin_update():
                ui.set_status("正在更新到 v%s…" % remote)
                log_local("正在更新到 v%s，完成後自動重開" % remote)
                run_updater_and_exit()
                root.after(2000, root.destroy)
            ui.call_on_main(begin_update)
            return
        agent.send_heartbeat(force=True)
        # 啟動完成就做一次快照，網頁版一打開就有最新的清單
        agent.publish_snapshot()

    threading.Thread(target=startup_work, daemon=True).start()

    # 第一次啟動：在背景建立桌面捷徑（失敗就靜默，只寫 log）
    if not cfg.get("shortcut_created"):
        threading.Thread(target=_make_shortcut_once, args=(cfg,),
                         daemon=True).start()

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
            if not agent._resetting:
                agent.poll_in_thread()
        except Exception:
            traceback.print_exc()
            log_local("輪詢出錯（已略過，下一輪繼續）")
        root.after(POLL_INTERVAL * 1000, tick)

    # 系統匣選單命令每 0.5 秒取一次，在主執行緒處理
    def drain_tray():
        if agent.tray is not None:
            agent.tray.poll()
        root.after(500, drain_tray)

    root.after(POLL_INTERVAL * 1000, tick)
    root.after(500, drain_tray)
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
        checks.append(("版本比對：1.2 > 1.1",
                       parse_version("1.2") > parse_version("1.1")))
        checks.append(("版本比對：1.10 > 1.2",
                       parse_version("1.10") > parse_version("1.2")))
        checks.append(("版本比對：1.2 == 1.2",
                       parse_version("1.2") == parse_version("1.2")))
        bid = new_batch_id()
        checks.append(("備份批次 id 格式",
                       len(bid) == 20 and bid[8] == "-"
                       and bid[15] == "-"))
        checks.append(("檔案種類：圖片", file_kind("jpg", False) == "圖片"))
        checks.append(("檔案種類：試算表",
                       file_kind("XLSX", False) == "試算表"))
        checks.append(("檔案種類：其他",
                       file_kind("xyz", False) == "其他"))
        checks.append(("檔案種類：資料夾",
                       file_kind("", True) == "資料夾"))
        _snap_dir = tempfile.mkdtemp(prefix="aifile_check_snap_")
        os.makedirs(os.path.join(_snap_dir, "子資料夾"), exist_ok=True)
        with open(os.path.join(_snap_dir, "a.txt"), "w",
                  encoding="utf-8") as f:
            f.write("hello")
        with open(os.path.join(_snap_dir, "子資料夾", "b.png"), "wb") as f:
            f.write(b"\x89PNG")
        _snap = build_snapshot(_snap_dir)
        checks.append(("快照：檔案與資料夾數正確",
                       _snap["total_files"] == 2
                       and _snap["total_dirs"] == 1
                       and len(_snap["entries"]) == 3))
        checks.append(("快照：種類與副檔名正確",
                       any(e["path"] == "a.txt" and e["kind"] == "文件"
                           and e["ext"] == "txt" and e["size"] == 5
                           for e in _snap["entries"])
                       and any(e["path"] == "子資料夾/b.png"
                               and e["kind"] == "圖片"
                               for e in _snap["entries"])
                       and any(e["path"] == "子資料夾"
                               and e["is_dir"] and e["kind"] == "資料夾"
                               for e in _snap["entries"])))
        checks.append(("快照：上限截斷",
                       build_snapshot(_snap_dir, max_entries=2)
                       ["truncated"] is True))
        checks.append(("VERSION 是 1.5", VERSION == "1.5"))
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
