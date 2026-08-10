## Context

見 proposal.md 的 Why/What Changes。技術面的既有限制：

- 國家圖書館來源是官方**月度 CSV 開放資料**下載（非即時 API、非網頁爬取）。
- Google Books 是官方 **REST API**，需要 API 金鑰、有配額限制。
- 專案目前無既有程式碼。

## Goals / Non-Goals

**Goals:**
- 定義可擴充的「來源」架構，未來新增已核准來源時不需重寫核心邏輯
- 定義跨來源共用的書籍紀錄格式（schema），供合併、輸出、未來其他系統（如 App）消費
- 定義 ISBN 比對合併與封面圖片補齊的具體規則
- 定義單次執行失敗時的容錯原則（單筆失敗不中斷整批）

**Non-Goals:**
- 跨來源分類體系的統一對照（spec 已定為 v1 不需要）
- 排程/自動化重複執行（v1 為手動觸發的單次執行）
- 資料庫或跨批次的歷史資料管理（v1 僅輸出單次執行結果的 JSON 檔）
- App 前端、AI 問答/推薦功能（已在 proposal 中排除）

## Decisions

**1. 來源採 adapter 架構**
每個已核准來源（NCL、Google Books）實作相同介面，各自負責「取得原始資料 → 轉換為共用書籍紀錄格式」；核心流程（合併、輸出）不需知道各來源的實作細節。未來新增來源只需新增一個 adapter。

**2. 共用書籍紀錄格式（schema）**
每本書輸出為一筆紀錄，至少包含：`isbn`、`title`、`authors`、`publisher`、`categories`（陣列，元素含 `source` 與 `label`，保留各來源原生分類）、`cover_image_url`、`sources`（貢獻此筆資料的來源清單）。
- 理由：不論哪個來源，輸出格式一致，方便後續解析與未來延伸使用（例如將來要做 App 時，資料結構已經是乾淨的）。

**3. NCL 存取方式**
下載月度 CSV（`https://isbn.ncl.edu.tw/NEW_ISBNNet/opendata/[年月]_isbn.csv`），以標準 CSV 解析，欄位對應：分類號／建議上架分類 → `categories`。屬於檔案下載，非爬蟲。

**4. Google Books 存取方式**
呼叫官方 REST API，須設定 API 金鑰（以環境變數提供，不寫入版本控制），並遵守其配額限制，加入重試與退避機制。
- 考慮過不帶金鑰的匿名配額，但額度更低、更容易被限流，故採用申請金鑰的方式。

**5. 跨來源資料衝突處理**
以 ISBN（正規化為 ISBN-13）比對合併。欄位衝突時：書名、作者、出版社等書目欄位以 **NCL 為主**（權威書目來源）；封面圖片、英文分類標籤等 NCL 未提供的欄位以 **Google Books 補足**。

**6. 封面圖片補齊策略**
優先使用該筆資料原生附帶的封面連結；若目前來源缺封面，以相同 ISBN 向另一已核准來源查詢補齊。

**7. 專案結構（初版）**
```
fetch_book/
  crawler/
    sources/
      ncl.py            # NCL 月度 CSV 下載與解析
      google_books.py   # Google Books API 存取
    schema.py            # 共用書籍紀錄格式
    merge.py             # ISBN 比對合併邏輯
    output.py            # JSON 輸出
    main.py              # 進入點
  output/                 # 執行結果 JSON 存放處
  requirements.txt
```

**8. 容錯原則**
單一書籍或單一來源請求失敗（例如 Google Books 查無資料、逾時）不得中斷整批次執行，需記錄失敗項目並繼續處理其餘資料。

## Risks / Trade-offs

- [Risk] Google Books API 配額可能不足以逐筆查詢整月新書 → Mitigation：僅在 NCL 資料缺封面/缺分類時才查詢 Google Books，不對每本書重複查詢兩次
- [Risk] NCL CSV 欄位格式未來若調整，解析邏輯可能失效 → Mitigation：解析邏輯集中在 `sources/ncl.py`，欄位對應獨立管理，方便調整
- [Risk] ISBN 格式不一致（10 碼／13 碼）導致比對失敗 → Mitigation：合併前一律正規化為 ISBN-13

## Open Questions

- Google Books API 實際配額數字與費用門檻，待申請金鑰後確認（不影響目前的架構與 tasks 拆分）
