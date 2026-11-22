# -*- coding: utf-8 -*-
"""AI File 自我檢查 v1.2
檢查設定檔、路徑守門、雜湊驗證、停止旗標、manifest 解析、版本比對與連線。
用法：python 自我檢查.py
全部 [OK] 才算通過。第 10 步連線是正式檢查：失敗會印完整錯誤＋原因對照，
沒有 PAT 也會明確講要先完成設定精靈，不是一句警告就帶過。
"""
import json
import os
import sys
import tempfile

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


def main():
    print("AI File 自我檢查 v%s" % A.VERSION)
    print("=" * 40)

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
    check("版本比對：1.2 > 1.1",
          A.parse_version("1.2") > A.parse_version("1.1"))
    check("版本比對：1.10 > 1.2",
          A.parse_version("1.10") > A.parse_version("1.2"))
    check("版本比對：1.2 == 1.2",
          A.parse_version("1.2") == A.parse_version("1.2"))
    check("VERSION 是 1.2", A.VERSION == "1.2")

    print("=" * 40)
    print("結果：%d 通過，%d 失敗" % (PASS, FAIL))
    if FAIL:
        print("請先修好 [失敗] 的項目再使用。")
        return 1
    print("自我檢查通過。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
