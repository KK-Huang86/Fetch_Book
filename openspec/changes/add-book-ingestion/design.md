## Context

見 proposal.md 的 Why/What Changes。技術面的既有限制：

- 國家圖書館來源是官方**月度 CSV 開放資料**下載（非即時 API、非網頁爬取）。
- Google Books 是官方 **REST API**，僅作 ISBN 層級查詢補充，需要 API 金鑰、有配額與內容使用限制（不得快取超過 cache header 期限、不得複製轉散布、需 attribution）。
- 資料持久化採 **PostgreSQL**（本機 Docker），存取層為 **SQLAlchemy + Alembic**。
- 專案目前無既有程式碼。
- 尚未確認 NCL CSV 是否實際包含封面圖片欄位——若沒有，v1 的封面幾乎全部來自 Google Books（僅能 hotlink，見下），S3 儲存路徑實際上很少或不會被使用；此點列為 Open Questions，待第一次下載實際 CSV 後確認，不影響本設計的架構決定。

## Goals / Non-Goals

**Goals:**
- 定義 NCL 為主、Google Books 為 ISBN 層級補充來源的架構
- 定義正規化的 PostgreSQL 資料庫 schema，作為累積式書籍總目錄
- 定義 upsert 邏輯與匯入批次（ingestion run）追蹤方式
- 定義 Enrichment 查詢的冪等性判斷邏輯
- 定義 ISBN 正規化、比對規則（含邊界情況）
- 定義封面圖片依來源分流的儲存政策（NCL 可存 S3、Google 僅 hotlink）
- 定義單次執行失敗時的容錯原則、失敗紀錄方式
- 定義 CLI 執行契約與 exit code

**Non-Goals:**
- 跨來源分類體系的統一對照（spec 已定為 v1 不需要）
- 排程/自動化重複執行（v1 為手動觸發的單次執行，累積邏輯由 upsert 保證重跑安全）
- 動態來源設定機制（v1 來源寫死於程式碼；若未來需要，屬於另一個 change）
- App 前端、AI 問答/推薦功能、對外查詢 API（已在 proposal 中排除）
- Google Books 來源圖片的下載保存（法遵限制，僅 hotlink）
- 正式雲端資料庫部署（AWS RDS、VPC 等）— v1 僅本機 Docker
- 作者/分類的模糊比對去重、已 enrichment 資料的重新驗證機制

## Decisions

### 1. 來源角色與架構
NCL adapter 負責下載並解析月度 CSV，輸出「本次匯入的書籍集合」。Google Books adapter **只提供單一操作：以 ISBN 查詢單筆資料**，不提供搜尋/分頁介面。核心流程：NCL 集合 → 逐筆查詢資料庫既有紀錄 → 缺封面或分類且尚未 enrichment 過才呼叫 Google Books adapter → upsert 進資料庫 → 更新匯入批次統計。

不採用「來源 allowlist/registry + 執行期拒絕未核准來源」的機制：v1 只有兩個來源且寫死在程式碼中，沒有「設定資料來源」這個操作介面。若未來要支援更多來源或執行期設定，屬於另一個 change。

### 2. 資料庫 Schema（正規化設計）

```
publishers
  id            serial PK
  name          text UNIQUE NOT NULL

authors
  id            serial PK
  name          text UNIQUE NOT NULL   -- 精確字串比對（trim 後），v1 不做模糊去重

books
  id                    serial PK
  isbn13                char(13) UNIQUE NOT NULL
  isbn10                char(10) NULL
  title                 text NOT NULL
  publisher_id          FK -> publishers.id NULL
  cover_image_url       text NULL
  cover_image_hosting   enum('self','hotlink') NULL
  first_seen_month      char(7) NOT NULL   -- 首次被收錄的 NCL 月份 (YYYY-MM)
  created_at            timestamptz NOT NULL DEFAULT now()
  updated_at            timestamptz NOT NULL DEFAULT now()

book_authors                              -- 多對多
  book_id       FK -> books.id
  author_id     FK -> authors.id
  PRIMARY KEY (book_id, author_id)

categories
  id            serial PK
  source        enum('ncl','google_books') NOT NULL
  type          enum('classification_number','shelf_category','subject_tag') NOT NULL
  code          text NULL
  label         text NOT NULL
  UNIQUE (source, type, code, label)

book_categories                           -- 多對多
  book_id       FK -> books.id
  category_id   FK -> categories.id
  PRIMARY KEY (book_id, category_id)

ingestion_runs
  id                serial PK
  month             char(7) NOT NULL      -- YYYY-MM
  started_at        timestamptz NOT NULL
  finished_at       timestamptz NULL
  status            enum('running','succeeded','partially_failed','failed') NOT NULL
  total             integer NOT NULL DEFAULT 0
  succeeded         integer NOT NULL DEFAULT 0
  failed            integer NOT NULL DEFAULT 0
  google_enriched   integer NOT NULL DEFAULT 0

ingestion_failures
  id            serial PK
  run_id        FK -> ingestion_runs.id
  isbn          text NULL
  stage         enum('ncl_parse','google_lookup') NOT NULL
  error_code    text NOT NULL
  message       text NOT NULL
  created_at    timestamptz NOT NULL DEFAULT now()
```

- `books.isbn13` 是累積式 upsert 的 key。
- `categories` 的 unique 約束避免同一分類被重複建立；`book_categories` 只增不減（見決策 4）。
- `ingestion_runs`/`ingestion_failures` 取代原本 JSON 方案裡的「執行摘要／failures 陣列」，成為可查詢的執行歷史。

### 3. ISBN 正規化與邊界規則
- 比對前一律移除連字號、空白，正規化為 ISBN-13（ISBN-10 依標準演算法轉換），寫入 `isbn13`／`isbn10` 兩欄。
- Checksum 無效：該筆資料不寫入 `books`（無法作為 upsert key），記錄一筆 `ingestion_failures`（`stage=ncl_parse`）。
- NCL 單列包含多個 ISBN（例如套書）：拆解為多筆獨立紀錄。
- 完全無 ISBN 的 NCL 紀錄：不寫入資料庫（`books.isbn13` 為 NOT NULL），記錄一筆 `ingestion_failures`。
- Google Books 回傳多個 `industryIdentifiers` 時，優先取 `ISBN_13`，其次 `ISBN_10`（轉換為 13 碼）。
- 同一來源同一 ISBN 於同一次 CSV 中重複出現：以最後一筆為準，記錄一筆 warning 至 `ingestion_failures`。

### 4. Upsert 與欄位更新規則
- 以 `isbn13` 做 `INSERT ... ON CONFLICT (isbn13) DO UPDATE`。
- `title`／`publisher_id`：每次以最新 NCL 資料覆蓋（NCL 為書目權威來源）。
- `authors`：以最新 NCL 資料覆蓋 `book_authors` 關聯（先刪除該書既有關聯、依最新資料重建），維持與 NCL 一致。
- `categories`：**聯集累加**——新分類透過 `book_categories` 新增關聯，既有分類不因本次未出現而刪除（避免因單次資料缺漏而丟失先前已知的分類資訊）。
- `cover_image_url` / `cover_image_hosting`：既有值非空時不覆蓋（見決策 5 的冪等性規則）；既有值為空且本次取得新值時才寫入。
- `first_seen_month`：僅在首次新增時設定，之後 upsert 不變更。
- `updated_at`：每次 upsert 更新。
- 整批操作包在單一 DB transaction 中；單筆書籍寫入失敗時，該筆 rollback 並記錄 `ingestion_failures`，不影響同批其餘書籍（採逐筆 savepoint，而非整批 all-or-nothing）。

### 5. Enrichment 查詢之冪等性
呼叫 Google Books 前，先查詢資料庫該 ISBN 現有的 `cover_image_url` 與是否已有 `source='google_books'` 的 `categories` 關聯：兩者皆非空則跳過查詢；否則才呼叫 Google Books API。此規則同時降低配額消耗、確保重跑同一批次不會產生不必要的外部請求。

### 6. 封面圖片儲存政策
- NCL 提供封面網址時：下載圖片，上傳至自有 S3 bucket，`cover_image_hosting = 'self'`，`cover_image_url` 為 CloudFront 網域網址。
- 僅 Google Books 提供封面時：不下載，`cover_image_hosting = 'hotlink'`，`cover_image_url` 為 Google Books 原始圖片網址，並於系統文件/App 端顯示 attribution。
- S3 + CloudFront（含 OAC）為一次性基礎設施佈建，不在本次程式碼 tasks 範圍內（見 Open Questions）。

### 7. NCL CSV 契約
- 網址：`https://isbn.ncl.edu.tw/NEW_ISBNNet/opendata/[YYYYMM]_isbn.csv`（民國年或西元年待第一次下載時確認實際格式，記錄於實作註記）。
- 編碼：下載後先偵測 BOM／編碼（常見為 UTF-8 或 Big5），統一轉為 UTF-8 處理。
- 分隔符號：以實際下載檔案確認，解析邏輯集中於 `sources/ncl.py`。
- 欄位對應表於實作時依實際欄位名稱建立，集中管理於同一模組。
- 錯誤處理：HTTP 404（該月份尚未 release）→ 該次執行失敗並回報明確錯誤訊息、`ingestion_runs.status='failed'`；下載中斷 → 重試後仍失敗則整批失敗；空檔案 → 視為 0 筆資料，`ingestion_runs.status='succeeded'` 但 `total=0`。
- 測試固定使用保存於專案內的 CSV fixture，不依賴即時網路請求。

### 8. Google Books Client 與重試策略
- 逾時設定：連線 timeout 5 秒、讀取 timeout 10 秒。
- 重試條件：僅對逾時、429、502/503/504 重試；4xx（除 429）不重試，直接記錄失敗。
- 最大重試次數：3 次，採指數退避＋隨機 jitter，總等待時間上限 30 秒。
- 尊重 `Retry-After` header（若提供，優先於退避運算結果）。
- 併發限制：v1 以循序（單一併發）呼叫。

### 9. CLI 契約
```
python -m crawler.main --month YYYY-MM
```
- `--month`：必填，格式 `YYYY-MM`，格式錯誤直接報錯並以非 0 exit code 結束。
- 資料庫連線字串由環境變數提供（例如 `DATABASE_URL`），不作為 CLI 參數。
- 不再有 `--output`/`--force`：輸出改為資料庫 upsert，重跑本身即為冪等操作，不存在「檔案已存在」的覆寫問題。
- 缺少 Google Books API 金鑰：不中止整批，僅略過 enrichment 步驟（所有書籍僅有 NCL 資料），並在執行摘要與 `ingestion_failures` 中明確註記「enrichment skipped: missing API key」。
- Exit code：`0` 全部成功（含「部分書籍缺封面/分類」這種非致命情況）；`1` 有致命錯誤導致無法完成匯入（例如 NCL 下載失敗）；`2` 參數錯誤。單筆 Google Books 查詢失敗屬於預期內的部分成功，不影響 exit code 為 0。

### 10. 執行紀錄與失敗紀錄
執行開始時建立一筆 `ingestion_runs`（`status='running'`），過程中的失敗逐筆寫入 `ingestion_failures`，執行結束時更新 `ingestion_runs` 的統計欄位與最終 `status`。同時將摘要（成功/失敗筆數、enrichment 筆數）印至 stdout/stderr，方便執行時觀察。`ingestion_failures.message` 不得包含 API 金鑰或帶金鑰參數的完整請求網址。

### 11. Migration 管理
使用 Alembic 管理 schema 變更；初始 migration 建立決策 2 列出的全部資料表。之後任何 schema 調整都透過新的 migration 檔案追蹤，不手動改資料庫。

### 12. 本機開發環境
提供 `docker-compose.yml` 啟動本機 PostgreSQL，開發與測試皆連此實例；連線資訊透過 `.env`（不進版控）提供給應用程式與 Alembic。

### 13. 專案結構
```
fetch_book/
  crawler/
    sources/
      ncl.py              # NCL 月度 CSV 下載與解析
      google_books.py     # Google Books ISBN 查詢（僅單筆查詢，含 retry）
    storage/
      s3.py                # NCL 來源圖片上傳（hotlink 情況不使用此模組）
    db/
      models.py            # SQLAlchemy models
      session.py           # DB session/連線管理
      repository.py        # upsert、冪等性查詢等資料存取邏輯
    schema.py               # 匯入流程中介資料結構、ISBN 正規化
    merge.py                 # 欄位覆蓋/聯集規則（決策 4）
    cli.py                    # 參數解析、exit code
    main.py                    # 進入點，orchestrate 全流程
  alembic/                     # migration scripts
  docker-compose.yml
  tests/
    fixtures/                  # 固定的 NCL CSV 樣本、Google Books mock 回應
  requirements.txt
```

## Risks / Trade-offs

- [Risk] Google Books API 配額可能不足以逐筆查詢整月新書 → Mitigation：僅在缺封面/缺分類且尚未 enrichment 過時才查詢；序列呼叫控制速率
- [Risk] NCL CSV 欄位格式或網址規則（年月格式）未來若調整，解析邏輯可能失效 → Mitigation：解析與網址組成邏輯集中在 `sources/ncl.py`，並以 fixture 測試鎖定目前已知行為
- [Risk] ISBN 格式不一致或缺漏導致無法寫入資料庫 → Mitigation：合併前一律正規化，無效或缺漏 ISBN 記錄失敗但不中斷整批
- [Risk] 誤將 Google Books 圖片下載保存，違反其 API 條款 → Mitigation：`cover_image_hosting` 欄位與程式邏輯明確區分來源，僅 NCL 圖片進入下載/上傳流程
- [Risk] SQLAlchemy/Alembic 增加初始設置成本 → Mitigation：採標準 declarative pattern，一次性建立必要的表，不過度設計
- [Risk] `categories` 聯集累加、`authors` 覆蓋重建的不對稱規則可能造成混淆 → Mitigation：規則已於決策 4 明確定義並附原因，之後如需調整需先修訂本文件

## Open Questions

- NCL CSV 是否實際包含封面圖片欄位／網址：待第一次下載實際 CSV 後確認。若沒有，S3 儲存路徑在 v1 實務上不會被使用，但架構決策不受影響。
- NCL CSV 網址中的年月格式（西元或民國年）：待第一次實際下載時確認，記錄於 `sources/ncl.py` 的實作註記。
- Google Books API 實際配額數字與費用門檻：待申請金鑰後確認（不影響目前的架構與 tasks 拆分）。
- S3 + CloudFront（含 OAC）基礎設施的實際佈建（bucket 政策、CloudFront 設定）：屬於部署前置工作，非本次程式碼 tasks 範圍，待實作 NCL 圖片上傳功能前另行處理。
- 正式雲端資料庫（AWS RDS）的部署時機與方式：留待未來的部署 change，v1 僅本機 Docker。
