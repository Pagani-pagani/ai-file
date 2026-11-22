# -*- coding: utf-8 -*-
"""AI File 自我檢查 v1.5
檢查設定檔、路徑守門、雜湊驗證、停止旗標、manifest 解析、版本比對、連線、
備份與返回、快照、重新命名、資料夾刪除、activity 寫入重試、manifest 併入、
系統匣 no-op、背景輪詢。
用法：python 自我檢查.py
全部 [OK] 才算通過。第 10 步連線是正式檢查：失敗會印完整錯誤＋原因對照，
沒有 PAT 也會明確講要先完成設定精靈，不是一句警告就帶過。
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import ai_file_agent as A

PASS = 0
FAIL = 0


def check(name, ok, note=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("[OK] %s" % name)
    else:
        FAIL += 1
        print("[失敗] %s %s" % (name, ("（" + note + "）") if note else ""))
    return ok


def warn(name, note=""):
    print("[警告] %s %s" % (name, ("（" + note + "）") if note else ""))


def _reset_check(relay, code):
    """啟動重設：session 是 approved 時，啟動後要變回 waiting。"""
    relay.put_json("session.json", {
        "status": "approved", "ai_name": "Muse",
        "code_hash": A.code_hash(code)}, "test")
    A.reset_approved_session_on_start(relay)
    session, _ = relay.get_json("session.json")
    return isinstance(session, dict) and session.get("status") == "waiting"


def main():
    print("AI File 自我檢查 v%s" % A.VERSION)
    print("=" * 40)

    # 同步紀錄與備份改寫到暫存目錄，自我檢查不污染程式資料夾的真實紀錄
    _hist_tmp = tempfile.mkdtemp(prefix="aifile_check_hist_")
    _bak_tmp = tempfile.mkdtemp(prefix="aifile_check_bak_")
    A.HISTORY_FILE = os.path.join(_hist_tmp, "ai-file-history.json")
    A.BACKUP_DIR = os.path.join(_bak_tmp, "ai-file-backup")

    # 1. Python 與 tkinter
    check("Python 3.8 以上", sys.version_info >= (3, 8),
          "目前是 %s" % sys.version.split()[0])
    try:
        import tkinter  # noqa: F401
        check("tkinter 可用", True)
    except ImportError:
        check("tkinter 可用", False, "介面需要 tkinter")

    # 2. 設定檔
    cfg = A.load_config()
    if cfg is None:
        warn("尚未設定", "第一次請雙擊「啟動 AI File.bat」開設定精靈")
    else:
        check("設定檔存在", True)
        check("設定檔有 PAT", bool(cfg.get("pat")))
        check("設定檔有允許的資料夾",
              bool(cfg.get("allowed_folder"))
              and os.path.isdir(cfg.get("allowed_folder", "")),
              "資料夾不存在或未設定")
        check("設定檔有工作階段代碼",
              A.valid_session_code(cfg.get("session_code", "")))

    # 3. 路徑守門
    base = os.path.join(tempfile.gettempdir(), "aifile_check_base")
    os.makedirs(base, exist_ok=True)
    try:
        check("路徑守門：資料夾內放行",
              A.safe_join(base, "子資料夾/檔案.txt").startswith(
                  os.path.realpath(base)))
    except ValueError as e:
        check("路徑守門：資料夾內放行", False, str(e))
    for bad, label in [
            ("../escape.txt", ".. 跳脫"),
            ("a/../../escape.txt", "多層 .. 跳脫"),
            ("/etc/passwd", "絕對路徑"),
            ("C:/Windows/system32/x.txt", "磁碟機代號"),
            ("", "空路徑"),
    ]:
        try:
            A.safe_join(base, bad)
            check("路徑守門：拒絕「%s」" % label, False, "竟然放行了！")
        except ValueError:
            check("路徑守門：拒絕「%s」" % label, True)

    # 4. 雜湊驗證
    data = b"AI File test"
    check("SHA-256 計算正確",
          A.sha256_bytes(b"hello") ==
          "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e730"
          "43362938b9824")
    check("SHA-256 驗證：相符通過",
          A.sha256_bytes(data) == A.sha256_bytes(data))
    check("SHA-256 驗證：竄改拒絕",
          A.sha256_bytes(data) != A.sha256_bytes(b"AI File test!"))

    # 5. 工作階段代碼
    code = A.new_session_code()
    check("工作階段代碼格式正確", A.valid_session_code(code), code)
    check("代碼雜湊可驗證",
          A.code_hash(code) == A.sha256_text(code.strip().upper()))
    check("錯誤代碼會被拒絕",
          not A.valid_session_code("A1B2-C3D")
          and not A.valid_session_code("A1B2_C3D4")
          and not A.valid_session_code("A1B2-C3D0")
          and not A.valid_session_code("A1B2-C3DO"))

    # 6. manifest 解析（用稍後第 8 步的真實 payload 算雜湊）
    _payload = "你好，AI File！".encode("utf-8")
    _payload_hash = A.sha256_bytes(_payload)
    manifest = {"files": [
        {"rel_path": "test.txt", "file": "000_abc_test.txt",
         "action": "add", "sha256": _payload_hash, "ai_name": "Muse",
         "time": "2026-11-22T00:00:00Z", "session_code": code},
        {"rel_path": "old.txt", "action": "delete",
         "ai_name": "Muse", "time": "2026-11-22T00:00:00Z",
         "session_code": code},
    ]}
    ok_manifest = (
        isinstance(manifest.get("files"), list)
        and all("rel_path" in f and "session_code" in f
                for f in manifest["files"]))
    check("manifest 格式正確", ok_manifest)

    # 7. 停止旗標（用本機假中繼）
    tmp = tempfile.mkdtemp(prefix="aifile_check_")
    relay = A.LocalRelay(tmp)
    relay.put_json("stop.json", {"stopped": True, "by": "web"},
                   "test")
    stop, _ = relay.get_json("stop.json")
    check("停止旗標讀取", isinstance(stop, dict) and stop.get("stopped"))

    # 8. 完整流程（本機假中繼＋自動同意的假 UI）
    allowed = os.path.join(tmp, "allowed")
    os.makedirs(allowed, exist_ok=True)
    cfg2 = {"pat": "fake", "allowed_folder": allowed,
            "session_code": code}
    agent = A.Agent(cfg2, relay, A.HeadlessUI(approve=True))
    # 先清除第 7 步設的停止旗標，否則 poll_once 會直接停手
    relay.put_json("stop.json", {"stopped": False}, "test")
    relay.put_json("session.json", {
        "status": "approved", "ai_name": "Muse",
        "code_hash": A.code_hash(code)}, "test")
    payload = "你好，AI File！".encode("utf-8")
    relay.put_bytes("pending/000_abc_test.txt", payload, "test")
    relay.put_json("pending/manifest.json", manifest, "test")
    agent.poll_once()
    got = os.path.join(allowed, "test.txt")
    check("同步流程：檔案寫入允許的資料夾",
          os.path.exists(got)
          and open(got, "rb").read() == payload)
    act, _ = relay.get_json("activity.json")
    check("同步流程：活動紀錄有寫入",
          isinstance(act, list) and len(act) > 0)

    # 9. 越界檔案會被拒絕（本機假中繼）
    relay.put_json("pending/manifest.json", {"files": [
        {"rel_path": "../escape.txt", "file": "000_abc_escape.txt",
         "action": "add", "sha256": "x" * 64, "ai_name": "Muse",
         "time": "2026-11-22T00:00:00Z", "session_code": code},
    ]}, "test")
    agent._last_manifest_sig = None
    agent.poll_once()
    check("同步流程：越界檔案被拒絕，沒有寫出去",
          not os.path.exists(os.path.join(tmp, "escape.txt")))

    # 10. 連線（正式檢查，不是警告：失敗要印完整錯誤＋原因對照）
    if cfg and cfg.get("pat"):
        try:
            r = A.GitHubRelay(cfg["pat"])
            ok, msg = A.test_connection(r)
            check("中繼 repo 連線", ok, msg if not ok else "")
            if ok:
                print("    " + msg)
            else:
                print("    原因對照：")
                for line in A.CONNECTION_CAUSES.split("\n"):
                    print("    " + line)
        except Exception as e:
            check("中繼 repo 連線", False, "完整錯誤：%s" % e)
    else:
        check("中繼 repo 連線", False,
              "還沒設定 PAT，請先完成設定精靈"
              "（需要先去 GitHub 產生細粒度 Token）")

    # 11. 版本比對
    check("版本比對：1.5 > 1.4",
          A.parse_version("1.5") > A.parse_version("1.4"))
    check("版本比對：1.10 > 1.2",
          A.parse_version("1.10") > A.parse_version("1.2"))
    check("版本比對：1.5 == 1.5",
          A.parse_version("1.5") == A.parse_version("1.5"))
    check("VERSION 是 1.5", A.VERSION == "1.5")

    # 12. 備份與返回（本機假中繼＋自動同意的假 UI）
    btmp = tempfile.mkdtemp(prefix="aifile_check_undo_")
    brelay = A.LocalRelay(btmp)
    ballowed = os.path.join(btmp, "allowed")
    os.makedirs(ballowed, exist_ok=True)
    bcode = A.new_session_code()
    bagent = A.Agent({"pat": "fake", "allowed_folder": ballowed,
                      "session_code": bcode},
                     brelay, A.HeadlessUI(approve=True))
    # 改一支舊檔、刪一支舊檔、新增一支檔案，再全部返回
    with open(os.path.join(ballowed, "old.txt"), "w",
              encoding="utf-8") as f:
        f.write("原本的內容")
    with open(os.path.join(ballowed, "todel.txt"), "w",
              encoding="utf-8") as f:
        f.write("等一下會被刪掉")
    brelay.put_json("stop.json", {"stopped": False}, "test")
    brelay.put_json("session.json", {
        "status": "approved", "ai_name": "Muse",
        "code_hash": A.code_hash(bcode)}, "test")
    new_payload = "新的內容".encode("utf-8")
    del_payload = b""
    brelay.put_bytes("pending/001_aaa_old.txt", new_payload, "test")
    bmanifest = {"files": [
        {"rel_path": "old.txt", "file": "001_aaa_old.txt",
         "action": "add", "sha256": A.sha256_bytes(new_payload),
         "ai_name": "Muse", "time": "2026-11-22T00:00:00Z",
         "session_code": bcode},
        {"rel_path": "todel.txt", "action": "delete",
         "ai_name": "Muse", "time": "2026-11-22T00:00:00Z",
         "session_code": bcode},
        {"rel_path": "brandnew.txt", "file": "002_bbb_new.txt",
         "action": "add", "sha256": A.sha256_bytes(del_payload),
         "ai_name": "Muse", "time": "2026-11-22T00:00:00Z",
         "session_code": bcode},
    ]}
    brelay.put_bytes("pending/002_bbb_new.txt", del_payload, "test")
    brelay.put_json("pending/manifest.json", bmanifest, "test")
    bagent.poll_once()
    check("備份與返回：修改有寫入",
          open(os.path.join(ballowed, "old.txt"),
               encoding="utf-8").read() == "新的內容")
    check("備份與返回：刪除有執行",
          not os.path.exists(os.path.join(ballowed, "todel.txt")))
    check("備份與返回：新增有寫入",
          os.path.exists(os.path.join(ballowed, "brandnew.txt")))
    hist = A.load_history()
    check("備份與返回：同步紀錄有寫入",
          len(hist) >= 1 and len(hist[0]["items"]) == 3
          and hist[0]["items"][0]["rel_path"] == "old.txt")
    bid = hist[0]["id"]
    undone = bagent.undo_batch(bid)
    check("備份與返回：返回成功",
          undone
          and open(os.path.join(ballowed, "old.txt"),
                   encoding="utf-8").read() == "原本的內容"
          and os.path.exists(os.path.join(ballowed, "todel.txt"))
          and not os.path.exists(os.path.join(ballowed, "brandnew.txt")))
    check("備份與返回：一批只能返回一次",
          not bagent.undo_batch(bid))
    check("備份與返回：啟動重設已允許的工作階段",
          _reset_check(brelay, bcode))

    # 13. 快照、重新命名、資料夾刪除（本機假中繼＋自動同意的假 UI）
    ctmp = tempfile.mkdtemp(prefix="aifile_check_snap2_")
    crelay = A.LocalRelay(ctmp)
    callowed = os.path.join(ctmp, "allowed")
    os.makedirs(os.path.join(callowed, "資料夾A", "子"), exist_ok=True)
    os.makedirs(os.path.join(callowed, "刪我"), exist_ok=True)
    with open(os.path.join(callowed, "頂層.txt"), "w",
              encoding="utf-8") as f:
        f.write("頂層內容")
    with open(os.path.join(callowed, "圖.png"), "wb") as f:
        f.write(b"\x89PNG1234")
    with open(os.path.join(callowed, "資料夾A", "內檔.txt"), "w",
              encoding="utf-8") as f:
        f.write("內檔")
    with open(os.path.join(callowed, "資料夾A", "子", "深.txt"), "w",
              encoding="utf-8") as f:
        f.write("深層")
    with open(os.path.join(callowed, "刪我", "一.txt"), "w",
              encoding="utf-8") as f:
        f.write("一")
    with open(os.path.join(callowed, "刪我", "二.txt"), "w",
              encoding="utf-8") as f:
        f.write("二")
    ccode = A.new_session_code()
    cagent = A.Agent({"pat": "fake", "allowed_folder": callowed,
                      "session_code": ccode},
                     crelay, A.HeadlessUI(approve=True))
    crelay.put_json("stop.json", {"stopped": False}, "test")
    crelay.put_json("session.json", {
        "status": "approved", "ai_name": "Muse",
        "code_hash": A.code_hash(ccode)}, "test")
    snap = A.build_snapshot(callowed)
    check("快照：檔案與資料夾數正確",
          snap["total_files"] == 6 and snap["total_dirs"] == 3
          and len(snap["entries"]) == 9)
    check("快照：種類、副檔名、大小、時間欄位正確",
          any(e["path"] == "圖.png" and e["kind"] == "圖片"
              and e["ext"] == "png" and e["size"] == 8
              and e["created"] for e in snap["entries"])
          and any(e["path"] == "頂層.txt" and e["kind"] == "文件"
                  and e["size"] == len("頂層內容".encode("utf-8"))
                  for e in snap["entries"])
          and any(e["path"] == "資料夾A" and e["is_dir"]
                  and e["kind"] == "資料夾" and e["size"] == 0
                  for e in snap["entries"])
          and any(e["path"] == "資料夾A/子/深.txt"
                  for e in snap["entries"]))
    check("快照：上限截斷",
          A.build_snapshot(callowed, max_entries=2)["truncated"] is True)
    check("快照：發佈到中繼",
          cagent.publish_snapshot()
          and (lambda s: isinstance(s, dict)
               and s["total_files"] == 6)(
              crelay.get_json("snapshot.json")[0]))
    # list_request：網頁要求重新整理，本地端重新產生快照並刪掉要求
    crelay.put_json("list_request.json",
                    {"request_id": "lr-test-1",
                     "requested_at": "2026-11-22T00:00:00Z",
                     "by": "admin"}, "test")
    cagent.poll_once()
    snap2, _ = crelay.get_json("snapshot.json")
    check("快照：收到重新整理要求會重新產生",
          isinstance(snap2, dict) and snap2["total_files"] == 6)
    check("快照：重新整理要求用完會刪掉",
          crelay.get_json("list_request.json")[0] is None)
    # content_request：索取檔案內容
    crelay.put_json("content_request.json",
                    {"request_id": "cr-test-1", "path": "頂層.txt",
                     "by": "admin", "at": "2026-11-22T00:00:00Z"}, "test")
    cagent.poll_once()
    resp, _ = crelay.get_json("content_response.json")
    import base64 as _b64
    check("索取內容：文字檔回傳正確",
          isinstance(resp, dict) and resp.get("ok") is True
          and _b64.b64decode(resp["content_b64"]).decode("utf-8")
          == "頂層內容")
    check("索取內容：要求用完會刪掉",
          crelay.get_json("content_request.json")[0] is None)
    crelay.put_json("content_request.json",
                    {"request_id": "cr-test-2", "path": "沒有這個.txt",
                     "by": "admin", "at": "2026-11-22T00:00:00Z"}, "test")
    cagent.poll_once()
    resp2, _ = crelay.get_json("content_response.json")
    check("索取內容：找不到檔案回 error",
          isinstance(resp2, dict) and resp2.get("ok") is False
          and "找不到" in (resp2.get("error") or ""))
    crelay.put_json("content_request.json",
                    {"request_id": "cr-test-3", "path": "../逃.txt",
                     "by": "admin", "at": "2026-11-22T00:00:00Z"}, "test")
    cagent.poll_once()
    resp3, _ = crelay.get_json("content_response.json")
    check("索取內容：越界路徑被拒絕",
          isinstance(resp3, dict) and resp3.get("ok") is False)
    # 重新命名（管理員項目，不用工作階段核准）
    crelay.put_json("pending/manifest.json", {"files": [
        {"rel_path": "頂層.txt", "new_rel": "頂層改名.txt",
         "action": "rename", "ai_name": "管理員", "by": "admin",
         "time": "2026-11-22T00:00:00Z", "session_code": ""},
    ]}, "test")
    cagent._last_manifest_sig = None
    cagent.poll_once()
    check("重新命名：檔案改名成功",
          os.path.exists(os.path.join(callowed, "頂層改名.txt"))
          and not os.path.exists(os.path.join(callowed, "頂層.txt")))
    hist = A.load_history()
    check("重新命名：同步紀錄有寫 new_rel",
          len(hist) >= 1 and hist[0]["items"][0].get("action") == "rename"
          and hist[0]["items"][0].get("new_rel") == "頂層改名.txt")
    rid = hist[0]["id"]
    check("重新命名：返回改回原名",
          cagent.undo_batch(rid)
          and os.path.exists(os.path.join(callowed, "頂層.txt"))
          and not os.path.exists(os.path.join(callowed, "頂層改名.txt")))
    # 重新命名：新路徑跳脫、目標已存在，都要拒絕、不誤改
    crelay.put_json("pending/manifest.json", {"files": [
        {"rel_path": "頂層.txt", "new_rel": "../逃.txt",
         "action": "rename", "ai_name": "管理員", "by": "admin",
         "time": "2026-11-22T00:00:00Z", "session_code": ""},
    ]}, "test")
    cagent._last_manifest_sig = None
    cagent.poll_once()
    check("重新命名：新路徑跳脫被拒絕",
          os.path.exists(os.path.join(callowed, "頂層.txt"))
          and not os.path.exists(os.path.join(ctmp, "逃.txt")))
    crelay.put_json("pending/manifest.json", {"files": [
        {"rel_path": "頂層.txt", "new_rel": "圖.png",
         "action": "rename", "ai_name": "管理員", "by": "admin",
         "time": "2026-11-22T00:00:00Z", "session_code": ""},
    ]}, "test")
    cagent._last_manifest_sig = None
    cagent.poll_once()
    check("重新命名：目標已存在不覆蓋",
          os.path.exists(os.path.join(callowed, "頂層.txt"))
          and open(os.path.join(callowed, "圖.png"), "rb").read()
          == b"\x89PNG1234")
    # 資料夾刪除：整棵備份、刪掉、返回整棵回來
    crelay.put_json("pending/manifest.json", {"files": [
        {"rel_path": "刪我", "action": "delete", "is_dir": True,
         "ai_name": "管理員", "by": "admin",
         "time": "2026-11-22T00:00:00Z", "session_code": ""},
    ]}, "test")
    cagent._last_manifest_sig = None
    cagent.poll_once()
    check("資料夾刪除：整棵刪掉",
          not os.path.exists(os.path.join(callowed, "刪我")))
    hist = A.load_history()
    check("資料夾刪除：同步紀錄標 is_dir",
          len(hist) >= 1 and hist[0]["items"][0].get("is_dir") is True)
    did = hist[0]["id"]
    check("資料夾刪除：返回整棵回來",
          cagent.undo_batch(did)
          and os.path.exists(os.path.join(callowed, "刪我", "一.txt"))
          and os.path.exists(os.path.join(callowed, "刪我", "二.txt")))

    # 14. v1.5：activity 寫入重試、manifest 併入、系統匣 no-op、背景輪詢
    # 假 GitHub API：用 dict 模擬 repo，PUT 時檢查 sha，不符就回 409
    import base64 as _b64b

    class FakeGitHub:
        def __init__(self):
            self.files = {}  # path -> (content_str, sha)
            self.counter = 0
            self.fail_put_409_times = 0

        def _new_sha(self):
            self.counter += 1
            return "sha-%d" % self.counter

        def request(self, method, path, body=None):
            if method == "GET":
                if path not in self.files:
                    return None
                content, sha = self.files[path]
                return {"content": _b64b.b64encode(
                    content.encode("utf-8")).decode("ascii"), "sha": sha}
            if method == "PUT":
                if self.fail_put_409_times > 0:
                    self.fail_put_409_times -= 1
                    raise A.RelayError(
                        "GitHub API PUT %s 失敗（409）" % path, status=409)
                cur = self.files.get(path)
                if body.get("sha") is None and cur is not None:
                    # 跟真的 GitHub 一樣：沒帶 sha 但檔案已存在 → 422
                    raise A.RelayError(
                        "GitHub API PUT %s 失敗（422）" % path, status=422)
                if body.get("sha") is not None and (
                        cur is None or body["sha"] != cur[1]):
                    raise A.RelayError(
                        "GitHub API PUT %s 失敗（409）" % path, status=409)
                content = _b64b.b64decode(body["content"]).decode("utf-8")
                self.files[path] = (content, self._new_sha())
                return {"content": body["content"],
                        "sha": self.files[path][1]}
            if method == "DELETE":
                self.files.pop(path, None)
                return {}
            raise AssertionError("不支援的方法 %s" % method)

    def _fake_relay(fake):
        r = A.GitHubRelay("fake")
        r._request = fake.request
        return r

    # 14a. _put 遇到 409 會自動重試並成功
    fake = FakeGitHub()
    frelay = _fake_relay(fake)
    fake.files["a.txt"] = ("舊內容", fake._new_sha())
    fake.fail_put_409_times = 1
    frelay.put_json("a.txt", {"x": 1}, "test")
    check("寫入重試：409 後重試成功",
          frelay.get_json("a.txt")[0] == {"x": 1})
    # 14b. 409 一直失敗會明確報錯，不假裝成功
    fake.fail_put_409_times = 99
    try:
        frelay.put_json("a.txt", {"x": 2}, "test")
        check("寫入重試：一直 409 會報錯", False, "竟然沒報錯！")
    except A.RelayError as e:
        check("寫入重試：一直 409 會報錯",
              getattr(e, "status", None) == 409)
    # 14c. 兩邊同時寫 activity.json 不會丟紀錄
    fake2 = FakeGitHub()
    relay_a = _fake_relay(fake2)
    relay_b = _fake_relay(fake2)
    check("兩邊同時寫：B 先寫一筆",
          A.append_activity(relay_b, {"action": "synced",
                                      "ai_name": "B",
                                      "rel_path": "b.txt"}) is True)
    # A 寫的時候第一次 PUT 遇到 409（B 剛寫過），重讀再補
    fake2.fail_put_409_times = 1
    check("兩邊同時寫：A 遇到 409 後重試成功",
          A.append_activity(relay_a, {"action": "synced",
                                      "ai_name": "A",
                                      "rel_path": "a.txt"}) is True)
    act2, _ = relay_a.get_json("activity.json")
    check("兩邊同時寫：兩筆都在，沒丟",
          isinstance(act2, list) and len(act2) == 2
          and {e.get("ai_name") for e in act2} == {"A", "B"})
    # 14d. 409 一直失敗時 append_activity 回傳 False（不假裝存成功）
    fake2.fail_put_409_times = 99
    check("兩邊同時寫：重試耗盡回傳 False",
          A.append_activity(relay_a, {"action": "synced",
                                      "ai_name": "A",
                                      "rel_path": "a2.txt"}) is False)
    # 14e. manifest 更新不會蓋掉網頁端同時暫存的新項目
    fake3 = FakeGitHub()
    mrelay = _fake_relay(fake3)
    mrelay.put_json("pending/manifest.json", {"files": [
        {"id": "st-old", "rel_path": "舊的.txt"},
        {"id": "st-new", "rel_path": "新的.txt"},
    ]}, "test")
    magent = A.Agent({"pat": "fake", "allowed_folder": allowed,
                      "session_code": code},
                     mrelay, A.HeadlessUI(approve=True))
    check("manifest 併入：只扣掉已處理的項目，新項目留著",
          magent._update_manifest_remove(
              [{"id": "st-old", "rel_path": "舊的.txt"}]) is True
          and [f["id"] for f in
               mrelay.get_json("pending/manifest.json")[0]["files"]]
          == ["st-new"])
    fake3.fail_put_409_times = 1
    check("manifest 併入：409 後重試成功",
          magent._update_manifest_remove(
              [{"id": "st-new", "rel_path": "新的.txt"}]) is True
          and mrelay.get_json("pending/manifest.json")[0]["files"] == [])
    # 14f. 系統匣：非 Windows 上是 no-op，不報錯
    tray = A.TrayIcon(on_show=lambda: None, on_quit=lambda: None)
    check("系統匣：非 Windows 上 start() 回傳 False",
          tray.start() is False and tray.available is False)
    try:
        tray.poll()
        tray.stop()
        check("系統匣：非 Windows 上 poll／stop 不報錯", True)
    except Exception as e:
        check("系統匣：非 Windows 上 poll／stop 不報錯", False, str(e))
    called = []
    tray2 = A.TrayIcon(on_show=lambda: called.append("show"),
                       on_quit=lambda: called.append("quit"))
    tray2.commands.put("show")
    tray2.commands.put("quit")
    tray2.poll()
    check("系統匣：queue 命令會交給主執行緒的回呼",
          called == ["show", "quit"])
    # 14g. 背景輪詢：poll_in_thread 立刻回來，跑完旗標歸零
    stmp = tempfile.mkdtemp(prefix="aifile_check_slow_")

    class SlowRelay(A.LocalRelay):
        def get_json(self, path):
            time.sleep(0.3)
            return super().get_json(path)

    srelay = SlowRelay(stmp)
    srelay.put_json("stop.json", {"stopped": False}, "test")
    srelay.put_json("session.json", {
        "status": "approved", "ai_name": "Muse",
        "code_hash": A.code_hash(code)}, "test")
    srelay.put_json("pending/manifest.json", {"files": []}, "test")
    sagent = A.Agent({"pat": "fake", "allowed_folder": allowed,
                      "session_code": code},
                     srelay, A.HeadlessUI(approve=True))
    sagent.poll_in_thread()
    check("背景輪詢：poll_in_thread 立刻回來（不卡住）",
          sagent._poll_running is True)
    sagent.poll_in_thread()  # 上一輪還沒跑完，直接跳過
    time.sleep(4)
    check("背景輪詢：跑完後旗標歸零",
          sagent._poll_running is False)

    print("=" * 40)
    print("結果：%d 通過，%d 失敗" % (PASS, FAIL))
    if FAIL:
        print("請先修好 [失敗] 的項目再使用。")
        return 1
    print("自我檢查通過。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
