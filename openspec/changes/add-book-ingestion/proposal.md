## Why

`fetch_book`目前是空專案，尚未有任何功能。需要一個書籍資料匯入流程，能夠從書籍資料來源取得書籍資訊（含分類、封面），並持久保存為可查詢的書籍總目錄，作為後續功能（查詢、分類瀏覽、App 前端等）的資料基礎。

## What Changes

- 使用 Python 建立書籍資料匯入程式（以官方開放資料下載與官方 API 呼叫為主，不做傳統網頁爬蟲）。
- v1 資料來源與角色：
  - **國家圖書館（NCL）開放資料**（主要資料集）— 「臺灣出版新書預告書訊」月度 CSV（`https://isbn.ncl.edu.tw/NEW_ISBNNet/opendata/[年月]_isbn.csv`），含分類號、建議上架分類等欄位，作為每次執行的書籍母集合
  - **Google Books API**（補充來源，僅作 enrichment）— 針對 NCL 資料中每筆 ISBN 查詢，僅用來補齊 NCL 缺漏的封面圖片與分類標籤；不對 Google Books 做主題搜尋或獨立收錄書籍
- **資料以正規化的 PostgreSQL 資料庫持久保存**，作為累積式的書籍總目錄：
  - 每次執行以當月 NCL 資料為輸入，依 ISBN 將書籍資料 upsert 進資料庫（既有 ISBN 更新、新 ISBN 新增），**不清空既有資料**——資料庫會隨每月執行持續累積成完整書目
  - 每次執行記錄為一筆「匯入批次」（月份、執行時間、成功/失敗統計），可追蹤每批資料的來源與結果
  - 已具備非空封面/分類 enrichment 資料的 ISBN，重複執行時 **不再重新查詢 Google Books**（節省配額、確保冪等）
  - 資料庫存取層採用 SQLAlchemy（ORM）+ Alembic（migration 工具）
  - v1 資料庫執行環境為**本機 Docker PostgreSQL**；正式雲端部署（如 AWS RDS）留待未來的部署 change 處理
- 書籍資料以「書籍分類」方式呈現（可查詢）；兩個來源各自的分類體系不同（NCL 為中文分類號/建議上架分類，Google Books 為英文自由標籤），v1 不強求統一為單一分類體系，各自依原生分類保存與呈現。
- 同一 ISBN 若在兩個來源都有資料，以 **ISBN** 作為合併依據（注意：ISBN 識別的是特定版本/載體，同書不同裝訂或電子書版本視為不同紀錄，不強制合併）。
- 書籍資料 SHALL 嘗試取得**封面圖片**；查無封面時仍寫入該筆紀錄，圖片欄位為 null，不因缺封面排除該書或中斷批次。
- **封面圖片儲存政策**（依來源分流，見下方法遵評估）：
  - NCL 提供之圖片（若有）：可下載後存放於自有 S3，並透過 CloudFront 提供服務
  - Google Books 提供之圖片：僅 hotlink 原始網址，不下載、不轉存至自有儲存空間，並依 Google 文件要求顯示 attribution
- **最高原則：資料存取行為不得違反任何來源網站的服務條款、robots.txt 規範或相關法律規定**，優先於效率或涵蓋率考量。以下為本次評估結果（查核日期：2026-08-10，依當時可查得之 robots.txt / 服務條款 / API 條款為準，未來如來源政策變更需重新評估）：
  - **博客來**：robots.txt 明確 Disallow 特定 AI 爬蟲並宣告 `ai-train=no`，無公開書籍資料 API → 排除
  - **讀冊（TAAZE）**：服務條款明文禁止未經書面同意「重製」網站內容資料 → 排除
  - **誠品線上**：具主動反爬蟲機制（Cloudflare 阻擋一般程式化存取），無公開 API → 排除
  - **Google Books API**：條款禁止超過 cache header 時間快取、禁止複製/轉散布內容、要求 attribution → 僅 hotlink、不轉存，且僅作 ISBN 查詢補充，不做內容重製
  - 上述排除的來源除非未來取得書面授權或官方合作管道並重新完成法遵評估，否則不納入

**v1 範例輸入/輸出**（示意，非最終 schema）：
- 輸入：`--month 2026-07`（欲處理的年月）
- 輸出：該月 NCL 新書已 upsert 進資料庫；每筆已嘗試以 ISBN 查詢 Google Books 補齊封面/分類；主控台印出該次執行摘要（成功/失敗筆數、enrichment 筆數）

**預期資料量**：以 NCL 月度新書公告的量級估算，單月約數百至數千筆書目（實際數字待第一次執行後確認）；資料庫會隨每月執行持續累積。

**成功衡量**：
- NCL 該月資料中，所有具有效 ISBN 的紀錄皆存在於資料庫中（新增或更新）
- 單筆資料處理失敗（無論是 NCL 列解析失敗或 Google Books 查詢失敗）不影響其他有效資料的寫入
- 重複執行同一月份為冪等操作：不產生重複紀錄，且不重複呼叫已 enrichment 過的 Google Books 查詢
- 資料庫 schema 符合設計的正規化結構（見 design.md）
- Google Books enrichment 的覆蓋率（補到封面/分類的比例）**不是**本次成功的必要條件——資料來源本身未必每筆都有對應資料

**明確排除於本次 change 範圍之外**（Non-Goals）：
- App 前端（含技術選型，如 React Native）— 另開 change 處理
- AI 問答/推薦功能 — 另開 change 處理
- Google Books 圖片下載存檔（法遵限制，見上）
- 對外提供查詢 API/HTTP 服務（v1 資料庫本身可查詢，但不提供對外服務層）
- 動態設定/切換資料來源（v1 來源寫死於程式碼，非使用者可設定項目）
- 正式雲端資料庫部署（如 AWS RDS、VPC/安全群組設定）— v1 僅本機 Docker，屬於未來部署 change 的範圍
- 作者/分類的模糊比對去重（v1 僅精確字串比對，不處理同名異寫、翻譯差異等）
- 已完成 enrichment 資料的重新驗證/更新機制（v1 一經補值不重查，若需要強制刷新屬於未來需求）

## Capabilities

### New Capabilities
- `book-ingestion`：依據前述原則，以 NCL 開放資料為主要資料集，Google Books API 僅作 ISBN 層級的封面/分類補充，將書籍資料以正規化結構持久保存於 PostgreSQL 資料庫，並依分類方式可查詢。

### Modified Capabilities
（無，本專案第一個 capability）

## Impact

- 新增 Python 專案結構，目前專案內無既有程式碼會受影響。
- 新增本機 PostgreSQL（Docker）、SQLAlchemy、Alembic、資料庫驅動（psycopg）等依賴。
- 國家圖書館來源為官方開放資料 CSV 下載，非網頁爬取，無服務條款疑慮。
- Google Books API 需申請 API 金鑰並留意配額限制、快取與轉散布限制（細節見 design.md）。
- 博客來、讀冊、誠品三個來源明確排除於 v1，相關程式碼/依賴不需要處理這三者的網頁爬取邏輯。
- 若未來需要下載/保存圖片檔（例如 Google Books 來源）或部署正式雲端資料庫，須先取得對應授權/評估，屬於另外的 change 範圍。
