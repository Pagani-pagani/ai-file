# AI File — agent.md

## 這是什麼
AI File 解決一個麻煩：每次 Muse 做完程式，使用者都要自己 git clone 或下載、解壓縮。
AI File 讓 Muse 把檔案直接送到使用者的 Windows 電腦，使用者全程看得到、隨時能停。

## 架構（誠實版）
GitHub Pages 是靜態網站，沒有伺服器，網頁本身碰不到使用者的電腦。
所以用 GitHub 當中繼，電腦才是守門員：
- **公開 repo `Pagani-pagani/ai-file`**：GitHub Pages 網站（網頁版管理介面＋AI 登入＋檔案暫存介面）與 Windows 本地端程式碼。裡面不放任何機密資料。
- **私人 repo `Pagani-pagani/ai-file-relay`**：中繼資料，只有本人能存取。放待同步檔案、工作階段、停止旗標、活動紀錄、心跳。
- **網頁版（靜態 SPA）**：用瀏覽器 localStorage 裡的金鑰呼叫 GitHub REST API 讀寫中繼 repo。沒有後端伺服器。
- **本地端（Python，標準函式庫 only，不用 pip）**：`啟動 AI File.bat` 用 pythonw.exe 啟動，定時去中繼 repo 輪詢，跳桌面通知、顯示浮動視窗，只在使用者同意後才把檔案寫進允許的資料夾。

## 安全模型（重要，照實說）
- 電腦是守門員：網頁永遠不能直接碰電腦檔案；所有檔案都先暫存在中繼 repo，本地端程式檢查通過才寫入。
- 配對代碼 = 一組細粒度 GitHub token（fine-grained PAT），只授權 `ai-file-relay` 這一個私人 repo 的內容讀寫，其他 repo 完全碰不到；使用者隨時可以在 GitHub 設定裡撤銷。
- 工作階段代碼 = 本地端產生的短代碼（例如 `A1B2-C3D4`），暫存的檔案一定要帶著當下有效的代碼，本地端才會收；重新生成代碼後，舊代碼的暫存一律拒收。
- 管理員密碼只是瀏覽器本機的介面鎖（存在該瀏覽器 localStorage 的雜湊值），不是伺服器驗證；真正的保護是上面三點。
- 路徑守門：每一個檔案路徑解析後必須落在允許的資料夾內，否則拒絕並立刻通知使用者。
- 停止：本地端浮動視窗的停止鈕或網頁版的停止，都會寫入停止旗標；本地端看到旗標就停手，網頁版也拒絕新的暫存。

## 流程
1. **配對**：本地端第一次設定時貼上 PAT、選好允許的資料夾，產生工作階段代碼顯示在畫面上。使用者把代碼給 Muse。
2. **AI 登入**：Muse 到網頁版點「AI 登入」，輸入名稱（例如 Muse）＋貼上代碼。網頁版用該代碼存取中繼 repo，並寫入 `session.json`（AI 名稱、登入時間、代碼雜湊、狀態）。
3. **使用者按「是」**：本地端收到新工作階段，跳通知；使用者按「是」後才開始接受該工作階段的檔案。
4. **暫存檔案**：Muse 在網頁版上傳或貼上檔案內容、指定允許資料夾內的相對路徑，按暫存 → 寫入中繼 repo 的 `pending/`（base64）＋`pending/manifest.json`（檔名、路徑、雜湊、AI 名稱、時間、工作階段代碼）。
5. **離線同步**：電腦關機時，暫存就等在中繼 repo；Muse 可以繼續在網頁上工作。電腦一開，本地端看到待同步就跳「立即同步」通知，使用者看過變更清單、按同意後，本地端才下載、驗證雜湊、寫入允許的資料夾，並把結果寫回 `activity.json`。
6. **即時模式**：工作階段有效且使用者已按「是」時，本地端頻繁輪詢，暫存的檔案會盡快套用；浮動視窗逐條列出動了哪些檔案（時間、AI 名稱、新增／修改／刪除、路徑），新增或刪除檔案時另外跳即時通知。
7. **越界嘗試**：任何想讀取或寫入允許資料夾以外的要求，本地端直接拒絕、立刻通知使用者、寫入紀錄。
8. **隨時停止**：本地端浮動視窗按停止，或網頁版管理員按停止／重新生成代碼，之後 Muse 就動不到任何檔案。

## 中繼 repo 內容（`ai-file-relay`，私人）
- `session.json`：目前工作階段（AI 名稱、代碼雜湊、到期時間、狀態：等待允許／已允許／已停止）
- `stop.json`：停止旗標（`{"stopped": true/false, "by": "local|web", "at": "..."}`）
- `pending/manifest.json`：待同步清單
- `pending/<檔名>`：待同步檔案（base64 文字檔，經 Contents API 寫入）
- `activity.json`：活動紀錄（append-only，含每次新增／修改／刪除／拒絕／停止）
- `heartbeat.json`：本地端每 30 秒更新（最後上線時間、允許的資料夾名稱、版本）

## 網頁版功能（公開 repo `ai-file` 的根目錄）
- 右上角「管理員登入」：第一次設定帳號密碼（只鎖該瀏覽器的介面）；登入後是管理員介面。
- 管理員介面：連線狀態（讀 `heartbeat.json`，新鮮＝已連線，過期＝未連線，顯示「用代碼連線」提示）、允許的資料夾、工作階段代碼顯示＋重新生成、停止／恢復、待同步清單、過去所有異動紀錄（讀 `activity.json`）。
- 「AI 登入」：輸入 AI 名稱＋貼上代碼，登入後是暫存介面：上傳檔案或貼上檔案內容、指定相對路徑、暫存；預設跳到正在工作的那個資料夾（讀 `heartbeat.json` 的資料夾名稱顯示）。
- 連線狀態自動偵測：每 30 秒讀一次 `heartbeat.json`；未連線時顯示代碼連線欄位。

## 本地端功能（公開 repo `ai-file` 的 `agent/`）
- `ai_file_agent.py`：Python 標準函式庫 only（urllib、tkinter、json、hashlib、base64），不用 pip。
- 第一次設定精靈：貼上 PAT、選擇允許的資料夾、產生工作階段代碼、寫入本機 `ai-file-config.json`（只存本機，絕不推送）。
- 輪詢中繼 repo：心跳、工作階段、停止旗標、待同步。
- 桌面通知（ctypes MessageBox／tkinter 置頂彈窗）：新工作階段、新增／刪除檔案、越界嘗試、立即同步。
- 浮動視窗（tkinter 置頂小視窗）：逐條列出 Muse 動了哪些檔案，附大大的停止鈕。
- 套用同步：只寫入允許的資料夾內、驗證雜湊、寫入本機 `ai-file-log.txt` 與中繼 `activity.json`。
- `啟動 AI File.bat`：用 pythonw.exe 啟動（已簽署，避開 Smart App Control），另附 `啟動 AI File.vbs` 備援。
- `自我檢查.py`、`檔案清單.txt`、`VERSION`（根目錄，內容只有版本號，v1 = `1`）。

## 檔案清單（公開 repo）
- `index.html`、`app.js`、`style.css`（GitHub Pages 網站，放在 repo 根目錄；v1.1 之前在 `web/`，Pages 只認根目錄所以首頁顯示成 README，已移出並刪除 `web/`）
- `agent/ai_file_agent.py`、`agent/啟動 AI File.bat`、`agent/啟動 AI File.vbs`、`agent/自我檢查.py`、`agent/檔案清單.txt`
- `agent.md`、`README.md`、`VERSION`、`.gitignore`（`ai-file-config.json`、`ai-file-log.txt` 絕不進版控）
- `圖片翻譯程式`、`GitHub 版本管理器` 的做法沿用：`VERSION` 檔案慣例、自我檢查、xvfb 測試。

## 已知限制（照實說）
- GitHub Pages 沒有伺服器，所以「即時」其實是輪詢（預設每 5 秒即時模式、每 30 秒心跳），不是真正的即時推播。
- 檔案要經過 GitHub 中繼 repo：單檔建議 1 MB 以下（Contents API 限制），不適合傳幾百 MB 的大檔。
- PAT 要使用者在 GitHub 設定裡親手產生一次（細粒度、只給 `ai-file-relay`），Muse 不能代勞。
- Windows 的 .bat 啟動器與桌面通知無法在 Linux VM 上實測，只能測核心邏輯（輪詢、路徑守門、雜湊驗證、停止旗標）與 tkinter 介面（xvfb）。
- 管理員密碼只鎖瀏覽器介面；真正的保護是 PAT 範圍＋工作階段代碼＋本地端守門。

## 版本歷史
- v1（2026-11-22）：第一版。網頁版（管理員登入、AI 登入、待同步、異動紀錄、停止／重新生成代碼）＋ Windows 本地端（.bat 啟動、通知、浮動視窗、立即同步、路徑守門）＋ 私人中繼 repo。部署到 GitHub Pages（公開 `ai-file`）。
- v1.1（2026-11-22）：修首頁問題。v1 把網頁放在 `web/`，但 GitHub Pages 只認 repo 根目錄，所以首頁顯示成 README.md。把 `index.html`、`app.js`、`style.css` 移到根目錄，刪除 `web/`。VERSION 1 → 1.1。
  - 測試紀錄（2026-11-22，Linux VM）：`py_compile` 全部通過；`node --check web/app.js` 通過；HTML 解析通過；app.js 用到的 52 個元素 ID 全部在 index.html 裡；程式碼裡沒有任何真實 token／密碼。自我檢查 19/19 通過（路徑守門 5 項、SHA-256 3 項、代碼格式 3 項、manifest、停止旗標、同步流程 2 項；2 個[警告]是預期的：尚未設定、沒有真 PAT 所以未測中繼連線）。獨立單元測試 15/15 通過（錯誤代碼拒收、雜湊不符拒收、正常寫入含子目錄、刪除、停止旗標停手＋恢復、重新生成代碼＋舊代碼失效、工作階段按「是」／按「否」、越界拒絕、缺欄位不崩潰）。xvfb GUI 測試 10/10 通過（設定精靈開、填、存＋代碼雜湊寫進中繼、浮動視窗開＋列出異動、工作階段詢問按「是」、立即同步對話框按「同意」後檔案寫入＋清單清空）。測試中抓到兩個測試本身的 bug（停止旗標沒清除、假雜湊）並修掉，程式邏輯本身沒改。未測：Windows .bat／.vbs 實機啟動、真實 PAT 連線、GitHub Pages 實際上線、瀏覽器裡的完整 AI 登入→暫存→同步流程。
