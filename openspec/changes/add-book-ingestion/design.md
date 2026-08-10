## Context

見 proposal.md 的 Why/What Changes。技術面的既有限制：

- 國家圖書館來源是官方**月度 CSV 開放資料**下載（非即時 API、非網頁爬取）。
- Google Books 是官方 **REST API**，僅作 ISBN 層級查詢補充，需要 API 金鑰、有配額與內容使用限制（不得快取超過 cache header 期限、不得複製轉散布、需 attribution）。
- 資料持久化採 **PostgreSQL**，透過 **Django ORM** 定義 models、**Django migrations** 管理 schema 變更。
- 執行方式為 **Celery + Celery Beat** 每日排程觸發，broker 為 **Redis**；另保留 Django management command 供手動觸發。
- 專案目前無既有程式碼。
- 尚未確認 NCL CSV 是否實際包含封面圖片欄位——若沒有，v1 的封面幾乎全部來自 Google Books（僅能 hotlink，見下），S3 儲存路徑實際上很少或不會被使用；此點列為 Open Questions，待第一次下載實際 CSV 後確認，不影響本設計的架構決定。

## Goals / Non-Goals

**Goals:**
- 定義 NCL 為主、Google Books 為 ISBN 層級補充來源的架構
- 定義 Django 專案／app 結構與正規化資料庫 schema（Django models），作為累積式書籍總目錄
- 定義 upsert 邏輯與匯入批次（ingestion run）追蹤方式，含觸發方式（排程／手動）
- 定義 Celery + Celery Beat 的排程設計：每日觸發、當月資料未公告時的處理、task 層級重試policy
- 定義 Enrichment 查詢的冪等性判斷邏輯
- 定義 ISBN 正規化、比對規則（含邊界情況）
- 定義封面圖片依來源分流的儲存政策（NCL 可存 S3、Google 僅 hotlink）
- 定義單次執行失敗時的容錯原則、失敗紀錄方式

**Non-Goals:**
- 跨來源分類體系的統一對照（spec 已定為 v1 不需要）
- 動態來源設定機制（v1 來源寫死於程式碼；若未來需要，屬於另一個 change）
- App 前端、AI 問答/推薦功能、**對外查詢 API（含 DRF）**（已在 proposal 中排除，DRF 留待未來 `add-book-api` change）
- Google Books 來源圖片的下載保存（法遵限制，僅 hotlink）
- 正式雲端部署（AWS RDS、ElastiCache 等）— v1 僅本機 Docker
- 作者/分類的模糊比對去重、已 enrichment 資料的重新驗證機制
- 失敗告警/通知機制（v1 僅記錄於匯入批次紀錄與 log）

## Decisions

### 1. 來源角色與架構
NCL adapter 負責下載並解析月度 CSV，輸出「本次匯入的書籍集合」。Google Books adapter **只提供單一操作：以 ISBN 查詢單筆資料**，不提供搜尋/分頁介面。核心匯入邏輯封裝為一個與框架無關的 **service 函式**（例如 `ingest_month(month: str) -> IngestionResult`），Django management command 與 Celery task 都只是這個 service 函式的觸發入口，不重複實作流程邏輯。

不採用「來源 allowlist/registry + 執行期拒絕未核准來源」的機制：v1 只有兩個來源且寫死在程式碼中，沒有「設定資料來源」這個操作介面。

### 2. Django 專案與 App 結構
單一 Django 專案（`config/`：`settings.py`、`celery.py` 等），一個 `books` app 承載本 capability 的 models、ingestion service、management command、Celery task。未來 `add-book-api` change 直接在同一 Django 專案新增 app（例如 `api/`），重用 `books` app 的 models，不需要重新設計資料層。

### 3. 資料庫 Schema（Django models，正規化設計）

```
Publisher
  id            AutoField PK
  name          CharField, unique

Author
  id            AutoField PK
  name          CharField, unique   -- 精確字串比對（trim 後），v1 不做模糊去重

Book
  id                    AutoField PK
  isbn13                CharField(13), unique
  isbn10                CharField(10), null=True
  title                 CharField
  publisher             ForeignKey(Publisher, null=True)
  authors               ManyToManyField(Author, through="BookAuthor")
  categories            ManyToManyField(Category, through="BookCategory")
  cover_image_url        URLField, null=True
  cover_image_hosting    CharField, choices=["self", "hotlink"], null=True
  first_seen_month       CharField(7)   -- 首次被收錄的 NCL 月份 (YYYY-MM)
  created_at              DateTimeField(auto_now_add=True)
  updated_at              DateTimeField(auto_now=True)

BookAuthor（through table，多對多）
  book          ForeignKey(Book)
  author        ForeignKey(Author)
  unique_together = ("book", "author")

Category
  id            AutoField PK
  source        CharField, choices=["ncl", "google_books"]
  type          CharField, choices=["classification_number", "shelf_category", "subject_tag"]
  code          CharField, blank=True, default=""   -- NOT NULL; "no code" is "", not NULL
  label         CharField
  unique_together = ("source", "type", "code", "label")

BookCategory（through table，多對多）
  book          ForeignKey(Book)
  category      ForeignKey(Category)
  unique_together = ("book", "category")

IngestionRun
  id                AutoField PK
  month             CharField(7)                       -- YYYY-MM
  trigger_type      CharField, choices=["scheduled", "manual"]
  started_at        DateTimeField
  finished_at       DateTimeField, null=True
  status            CharField, choices=["running", "succeeded", "partially_failed", "failed", "skipped_not_yet_published"]
  total             IntegerField, default=0
  succeeded         IntegerField, default=0
  failed            IntegerField, default=0
  google_enriched   IntegerField, default=0

IngestionFailure
  id            AutoField PK
  run           ForeignKey(IngestionRun)
  isbn          CharField, null=True
  stage         CharField, choices=["ncl_parse", "google_lookup"]
  error_code    CharField
  message       TextField
  created_at    DateTimeField(auto_now_add=True)
```

- `Book.isbn13` 是累積式 upsert 的 key。
- `Category.code` 刻意不用 `null=True`：PostgreSQL 視多個 `NULL` 彼此不相等，若 code 允許 NULL，`(source, type, code, label)` 的 unique 約束無法擋下重複的「無代碼」分類（例如 Google Books 的 subject tag 都沒有 code），會破壞 upsert 冪等性。統一以空字串 `""` 代表「無代碼」。
- `IngestionRun.status` 新增 `skipped_not_yet_published`，對應「當月 NCL 資料尚未公告」這個非失敗情況（見決策 10）。
- `IngestionRun.trigger_type` 記錄是排程還是手動觸發，滿足 spec 的「手動觸發匯入」需求。
- 以 Django migrations（`manage.py makemigrations`/`migrate`）管理上述 schema，取代原先評估過的 Alembic 方案。

### 4. ISBN 正規化與邊界規則
- 比對前一律移除連字號、空白，正規化為 ISBN-13（ISBN-10 依標準演算法轉換），寫入 `isbn13`／`isbn10` 兩欄。
- 13 碼數字且 checksum 正確，仍須以 `978` 或 `979` 開頭才視為 ISBN；一般 EAN-13（例如商品條碼）checksum 也可能算對，但不是書籍 ISBN，MUST 回傳無效。
- Checksum 無效：該筆資料不寫入 `Book`（無法作為 upsert key），記錄一筆 `IngestionFailure`（`stage=ncl_parse`）。
- NCL 單列包含多個 ISBN（例如套書）：拆解為多筆獨立紀錄。
- 完全無 ISBN 的 NCL 紀錄：不寫入資料庫，記錄一筆 `IngestionFailure`。
- Google Books 回傳多個 `industryIdentifiers` 時，優先取 `ISBN_13`，其次 `ISBN_10`（轉換為 13 碼）。
- 同一來源同一 ISBN 於同一次 CSV 中重複出現：以最後一筆為準，記錄一筆 warning 至 `IngestionFailure`。

### 5. Upsert 與欄位更新規則
- 以 `isbn13` 做為 upsert key：先以 `Book.objects.select_for_update().filter(isbn13=...).first()` 查詢既有紀錄，不存在則新增、存在則更新，整段包在 `transaction.atomic()` 內（Django 的巢狀 `atomic()` 形成 savepoint，對應單筆失敗可 rollback 的需求）。
- `title`／`publisher`：每次以最新 NCL 資料覆蓋（NCL 為書目權威來源）。
- `authors`：以最新 NCL 資料覆蓋 `BookAuthor` 關聯（先清除該書既有關聯、依最新資料重建 M2M），維持與 NCL 一致。
- `categories`：**聯集累加**——新分類透過 `BookCategory` 新增關聯，既有分類不因本次未出現而刪除。
- `cover_image_url` / `cover_image_hosting`：既有值非空時不覆蓋（見決策 6 的冪等性規則）；既有值為空且本次取得新值時才寫入。
- `first_seen_month`：僅在首次新增時設定，之後 upsert 不變更。
- `updated_at`：Django `auto_now=True` 自動更新。
- 單筆書籍寫入失敗時，該筆的 `atomic()` block rollback 並記錄 `IngestionFailure`，不影響同批其餘書籍。

### 6. Enrichment 查詢之冪等性
呼叫 Google Books 前，先查詢資料庫該 ISBN 現有的 `cover_image_url` 與是否已有 `source='google_books'` 的 `Category` 關聯：兩者皆非空則跳過查詢；否則才呼叫 Google Books API。

### 7. 封面圖片儲存政策
- NCL 提供封面網址時：下載圖片，上傳至自有 S3 bucket，`cover_image_hosting = 'self'`，`cover_image_url` 為 CloudFront 網域網址。
- 僅 Google Books 提供封面時：不下載，`cover_image_hosting = 'hotlink'`，`cover_image_url` 為 Google Books 原始圖片網址，並於系統文件/App 端顯示 attribution。
- S3 + CloudFront（含 OAC）為一次性基礎設施佈建，不在本次程式碼 tasks 範圍內（見 Open Questions）。

### 8. NCL CSV 契約
- 網址：`https://isbn.ncl.edu.tw/NEW_ISBNNet/opendata/[YYYYMM]_isbn.csv`（民國年或西元年待第一次下載時確認實際格式，記錄於實作註記）。
- 編碼：下載後先偵測 BOM／編碼（常見為 UTF-8 或 Big5），統一轉為 UTF-8 處理。
- 分隔符號：以實際下載檔案確認，解析邏輯集中於 `books/sources/ncl.py`。
- 欄位對應表於實作時依實際欄位名稱建立，集中管理於同一模組。
- 404（該月份尚未 release）：在排程情境下視為正常情況（決策 10）；在手動觸發情境下視為明確錯誤回報給使用者。
- 下載中斷 → 交由 Celery task 層級重試（決策 10）；空檔案 → 視為 0 筆資料，`IngestionRun.status='succeeded'` 但 `total=0`。
- 測試固定使用保存於專案內的 CSV fixture，不依賴即時網路請求。

### 9. Google Books Client 與重試策略
- 逾時設定：連線 5 秒、讀取 10 秒。
- 重試條件：僅對逾時、429、502/503/504 重試；4xx（除 429）不重試，直接記錄失敗。
- 最大重試次數：3 次，採指數退避＋隨機 jitter，總等待時間上限 30 秒。
- 尊重 `Retry-After` header。
- 併發限制：v1 以循序（單一併發）呼叫。
- 此層重試是「單一 Google Books 請求」的重試，與決策 10 的「Celery task 整體」重試是不同層級，兩者不互相取代。

### 10. 排程與執行方式（Celery + Celery Beat）
- **Broker**：Redis，v1 於本機獨立 Docker 容器運行（host port 6380，避免與機器上其他專案的 Redis 容器衝突），不與其他專案共用。
- **排程**：Celery Beat 每日固定時間（預設 03:00 Asia/Taipei，避開 Google Books/NCL 尖峰時段，可調整）觸發 `ingest_current_month` task，目標月份固定為「觸發當下所屬年月」。
- **404（當月資料尚未公告）**：service 函式回傳明確的「尚未公告」結果，task 將 `IngestionRun.status` 設為 `skipped_not_yet_published`，**不觸發 Celery 重試**，視為正常結束。
- **非預期錯誤**（例如資料庫連線中斷、未預期例外）：使用 Celery 的 `autoretry_for` + `retry_backoff=True` + `max_retries=3`，重試次數與間隔皆有上限；重試全部失敗後，`IngestionRun.status` 設為 `failed`，錯誤記錄於 `IngestionFailure`／log，目前無告警/通知整合，需自行查表或查 log。
- **手動觸發**（management command）：呼叫同一個 `ingest_month(month)` service 函式，不透過 Celery（同步執行），但套用相同的規則（含 404 時的行為——手動觸發下 404 視為明確錯誤訊息回報給執行者，而非靜默略過，因為使用者是主動指定月份，理應被告知該月尚無資料）。

### 11. 管理指令（Django management command）
```
python manage.py ingest_books --month YYYY-MM
```
- `--month`：必填，格式驗證。
- 內部呼叫 `ingest_month(month)` service 函式，`trigger_type='manual'`。
- Exit code：`0` 成功／部分成功；`1` 致命錯誤（含手動觸發下的 404）；`2` 參數錯誤。

### 12. 執行紀錄與失敗紀錄
執行開始時建立一筆 `IngestionRun`（`status='running'`，記錄 `trigger_type`），過程中的失敗逐筆寫入 `IngestionFailure`，執行結束時更新 `IngestionRun` 的統計欄位與最終 `status`（`succeeded`／`partially_failed`／`failed`／`skipped_not_yet_published`）。同時將摘要印至 stdout/log。`IngestionFailure.message` 不得包含 API 金鑰或帶金鑰參數的完整請求網址。

### 13. Migration 管理
使用 Django 內建 migrations（`manage.py makemigrations`/`migrate`）管理 schema 變更，取代先前評估過的 Alembic 方案。初始 migration 建立決策 3 列出的全部 models。

### 14. 本機開發環境
`docker-compose.yml` 啟動本機 PostgreSQL（host port 5434）與 Redis（host port 6380，Celery broker），兩者皆與機器上其他專案的容器分開、不共用。連線資訊透過 `.env`（不進版控）提供給 Django 與 Celery。

### 15. 專案結構
```
fetch_book/
  config/
    settings.py
    celery.py              # Celery app 設定、Celery Beat 排程定義
    urls.py                 # v1 為空／預留，不對外提供 API
  books/
    models.py                # 決策 3 的 Django models
    migrations/
    services/
      ingest.py               # ingest_month(month) 核心 service 函式
      merge.py                 # 欄位覆蓋/聯集規則（決策 5）
      schema.py                 # ISBN 正規化、中介資料結構
    sources/
      ncl.py                    # NCL 月度 CSV 下載與解析
      google_books.py           # Google Books ISBN 查詢（僅單筆查詢，含 retry）
    storage/
      s3.py                      # NCL 來源圖片上傳（hotlink 情況不使用此模組）
    tasks.py                    # Celery task（決策 10）
    management/
      commands/
        ingest_books.py           # 決策 11 的 management command
    tests/
      fixtures/                   # 固定的 NCL CSV 樣本、Google Books mock 回應
  docker-compose.yml
  manage.py
  requirements.txt
```

## Risks / Trade-offs

- [Risk] Google Books API 配額可能不足以逐筆查詢整月新書 → Mitigation：僅在缺封面/缺分類且尚未 enrichment 過時才查詢；序列呼叫控制速率
- [Risk] NCL CSV 欄位格式或網址規則（年月格式）未來若調整，解析邏輯可能失效 → Mitigation：解析與網址組成邏輯集中在 `books/sources/ncl.py`，並以 fixture 測試鎖定目前已知行為
- [Risk] ISBN 格式不一致或缺漏導致無法寫入資料庫 → Mitigation：合併前一律正規化，無效或缺漏 ISBN 記錄失敗但不中斷整批
- [Risk] 誤將 Google Books 圖片下載保存，違反其 API 條款 → Mitigation：`cover_image_hosting` 欄位與程式邏輯明確區分來源，僅 NCL 圖片進入下載/上傳流程
- [Risk] 每日排程在資料尚未公告的日子持續執行，可能產生大量無意義的 log/執行紀錄 → Mitigation：`skipped_not_yet_published` 狀態不計入失敗、log 層級降低（info 而非 error），必要時未來可加監控但 v1 不做
- [Risk] Celery task 重試與 upsert 冪等性設計搭配不當可能造成重複副作用 → Mitigation：upsert 本身冪等，task 重試整段 `ingest_month` 是安全的，不會因重試而產生重複資料
- [Risk] `categories` 聯集累加、`authors` 覆蓋重建的不對稱規則可能造成混淆 → Mitigation：規則已於決策 5 明確定義並附原因，之後如需調整需先修訂本文件

## Open Questions

- NCL CSV 是否實際包含封面圖片欄位／網址：待第一次下載實際 CSV 後確認。若沒有，S3 儲存路徑在 v1 實務上不會被使用，但架構決策不受影響。
- NCL CSV 網址中的年月格式（西元或民國年）：待第一次實際下載時確認，記錄於 `books/sources/ncl.py` 的實作註記。
- Google Books API 實際配額數字與費用門檻：待申請金鑰後確認（不影響目前的架構與 tasks 拆分）。
- S3 + CloudFront（含 OAC）基礎設施的實際佈建（bucket 政策、CloudFront 設定）：屬於部署前置工作，非本次程式碼 tasks 範圍，待實作 NCL 圖片上傳功能前另行處理。
- 正式雲端資料庫（AWS RDS）與 Redis（ElastiCache）的部署時機與方式：留待未來的部署 change，v1 僅本機 Docker。
- 每日排程的確切觸發時間（目前預設 03:00 Asia/Taipei）：若之後觀察到 NCL 實際公告時間規律，可再調整，不影響架構。
