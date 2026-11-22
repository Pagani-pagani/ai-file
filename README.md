# AI File v1

讓 AI 把檔案直接送到你的 Windows 電腦，不用再自己 git clone 或下載解壓縮。
你全程看得到、隨時能停。

## 架構（誠實版）

GitHub Pages 是靜態網站，沒有伺服器，網頁本身碰不到你的電腦。
所以用 GitHub 當中繼，電腦才是守門員：

- **公開 repo `Pagani-pagani/ai-file`**：這個專案。放網頁版（repo 根目錄的 `index.html`、`app.js`、`style.css`）和 Windows 本地端程式（`agent/`）。裡面不放任何機密資料。
- **私人 repo `Pagani-pagani/ai-file-relay`**：中繼資料，只有本人能存取。放待同步檔案、工作階段、停止旗標、活動紀錄、心跳。
- **網頁版**：`https://pagani-pagani.github.io/ai-file/`（啟用 GitHub Pages 後）。用瀏覽器 localStorage 裡的 Token 呼叫 GitHub API 讀寫中繼 repo。
- **本地端**：Python 程式，只用標準函式庫，不用 pip。`啟動 AI File.bat` 啟動後定時輪詢中繼 repo，只在你按「同意」後才把檔案寫進允許的資料夾。

## 安全模型

- 電腦是守門員：網頁永遠不能直接碰電腦檔案；所有檔案先暫存在中繼 repo，本地端檢查通過、你按同意後才寫入。
- 配對代碼 = 一組細粒度 GitHub Token（fine-grained PAT），只授權 `ai-file-relay` 這一個私人 repo 的內容讀寫，其他 repo 完全碰不到；隨時可以在 GitHub 設定裡撤銷。
- 工作階段代碼 = 本地端產生的短代碼（例如 `A1B2-C3D4`）。暫存的檔案一定要帶著當下有效的代碼，本地端才會收；重新生成代碼後，舊代碼的暫存一律拒收。
- 管理員密碼只是瀏覽器本機的介面鎖（存在該瀏覽器 localStorage 的雜湊值），不是伺服器驗證；真正的保護是上面三點。
- 路徑守門：每一個檔案路徑解析後必須落在允許的資料夾內，否則拒絕並立刻通知你。
- 停止：本地端浮動視窗的停止鈕或網頁版的停止，都會寫入停止旗標；本地端看到旗標就停手，網頁版也拒絕新的暫存。

## 安裝（Windows 電腦）

1. 下載整包 ZIP：https://codeload.github.com/Pagani-pagani/ai-file/zip/refs/heads/main
   （或到 https://github.com/Pagani-pagani/ai-file 開 `agent/` 資料夾下載）
2. 解壓縮，雙擊 `agent/啟動 AI File.bat`（用已簽署的 pythonw.exe 啟動，不跳黑視窗）。
   備援：雙擊 `agent/啟動 AI File.vbs`。
3. 第一次會開設定精靈：
   - 貼上 GitHub 細粒度 Token（產生方式見下一節）
   - 選擇「允許 Muse 使用的資料夾」（Muse 只能動這個資料夾裡的檔案）
   - 記下工作階段代碼（例如 `A1B2-C3D4`），把這組給 Muse。
4. 之後每次開機只要雙擊 `啟動 AI File.bat` 就好。

## 產生 GitHub 細粒度 Token（PAT）

1. 到 GitHub → 右上角頭像 → Settings → 左邊最下面 Developer settings → Personal access tokens → Fine-grained tokens → Generate new token。
2. Token name：`AI File`；Expiration：照你習慣選（建議 90 天或 1 年）。
3. Repository access：選 **Only select repositories**，只勾 `ai-file-relay`。
4. Repository permissions：把 **Contents** 設為 **Read and write**，其他全部不動。
5. 點 Generate token，複製起來（只會顯示一次）。把這組 Token 貼進本地端設定精靈，也給 Muse 貼到網頁版 AI 登入。
6. 不想用了就到同一個頁面按 Revoke，Token 立刻失效。

## 使用流程

1. 電腦：啟動 AI File，拿到工作階段代碼，把代碼和 Token 給 Muse。
2. Muse：到網頁版點「AI 登入」，輸入名稱、貼上 Token 和代碼。
3. 你的電腦跳通知問要不要允許連線，按「是」。
4. Muse 在網頁版上傳或貼上檔案內容、填好相對路徑，按「暫存」。
5. 你的電腦跳「立即同步」通知，看過變更清單，按「同意」才會寫進電腦。
   電腦關機時，暫存就等在中繼 repo；Muse 可以繼續在網頁上工作，等你開電腦再同步。
6. 浮動視窗會逐條列出 Muse 動了哪些檔案。隨時按「停止」，Muse 就動不到了。

## 網頁版管理員介面

右上角「管理員登入」：第一次設定帳號密碼（只鎖這個瀏覽器）。登入後可以看到：

- 連線狀態（讀心跳，每 30 秒更新；過期就是未連線）
- 允許的資料夾、工作階段代碼、重新生成代碼
- 停止 / 恢復
- 待同步清單、過去所有異動紀錄

## 已知限制（照實說）

- GitHub Pages 沒有伺服器，「即時」其實是輪詢（即時模式每 5 秒、心跳每 30 秒），不是真正的即時推播。
- 檔案要經過 GitHub 中繼 repo：單檔建議 1 MB 以下（Contents API 限制），不適合傳幾百 MB 的大檔。
- Token 要你在 GitHub 設定裡親手產生一次，Muse 不能代勞。
- Windows 的 .bat 啟動器與桌面通知無法在 Linux 上實測，只能測核心邏輯（輪詢、路徑守門、雜湊驗證、停止旗標）與 tkinter 介面。
- 管理員密碼只鎖瀏覽器介面；真正的保護是 Token 範圍＋工作階段代碼＋本地端守門。
- 同一時間如果兩邊同時寫 activity.json，後寫的可能蓋掉先寫的（v1 已知限制，檔案小、頻率低，實務上很少發生）。

## 檔案清單

- `index.html`、`app.js`、`style.css`（GitHub Pages 網站，放在 repo 根目錄，Pages 只認根目錄）
- `agent/ai_file_agent.py`、`agent/啟動 AI File.bat`、`agent/啟動 AI File.vbs`、`agent/自我檢查.py`、`agent/檔案清單.txt`
- `agent.md`、`README.md`、`VERSION`、`.gitignore`

詳細設計見 `agent.md`。
