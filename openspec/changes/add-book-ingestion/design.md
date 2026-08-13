## Context

見 proposal.md 的 Why/What Changes。技術面的既有限制：

- 國家圖書館來源是官方**月度 CSV 開放資料**下載（非即時 API、非網頁爬取）。
- Google Books 是官方 **REST API**，僅作 ISBN 層級查詢補充，需要 API 金鑰、有配額與內容使用限制（不得快取超過 cache header 期限、不得複製轉散布、需 attribution）。
- 資料持久化採 **PostgreSQL**，透過 **Django ORM** 定義 models、**Django migrations** 管理 schema 變更。
- 執行方式為 **Celery + Celery Beat** 每日排程觸發，broker 為 **Redis**；另保留 Django management command 供手動觸發。
- 專案目前無既有程式碼。
- 已下載實際 CSV（2024-12、2025-01、2025-07、2025-08 共 4 個月份）確認：NCL CSV **不包含封面圖片欄位**，v1 的封面因此幾乎全部來自 Google Books（僅能 hotlink，見下），S3 儲存路徑實務上很少或不會被使用；架構決策不受影響，維持原設計（保留 S3 路徑供 NCL 未來若提供封面欄位時使用）。

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
  run           ForeignKey(IngestionRun, related_name="failures")
  isbn          CharField, null=True
  stage         CharField, choices=["ncl_download", "ncl_parse", "google_lookup", "book_upsert", "cover_storage", "ingestion"]
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
- NCL 單列固定對應一個 ISBN（已實測 4 個月份、14000+ 筆資料驗證，無單列多 ISBN 情況，見決策 8），故不設計拆解邏輯。
- 完全無 ISBN 或 ISBN 無效（含 checksum 錯誤、非 978/979 開頭）的 NCL 紀錄：不寫入資料庫，記錄一筆 `IngestionFailure`（`stage=ncl_parse`）。
- Google Books 回傳多個 `industryIdentifiers` 時，優先取 `ISBN_13`，其次 `ISBN_10`（轉換為 13 碼）。
- 同一來源同一 ISBN 於同一次 CSV 中重複出現：以最後一筆為準，記錄一筆 warning 至 `IngestionFailure`。

### 5. Upsert 與欄位更新規則
- 以 `isbn13` 做為 upsert key：先以 `Book.objects.select_for_update().filter(isbn13=...).first()` 查詢既有紀錄，不存在則新增、存在則更新，整段包在 `transaction.atomic()` 內（Django 的巢狀 `atomic()` 形成 savepoint，對應單筆失敗可 rollback 的需求）。
- `title`／`publisher`：每次以最新 NCL 資料覆蓋（NCL 為書目權威來源）。
- `authors`：以最新 NCL 資料覆蓋 `BookAuthor` 關聯（先清除該書既有關聯、依最新資料重建 M2M），維持與 NCL 一致。
- `categories`：**聯集累加**——新分類透過 `BookCategory` 新增關聯，既有分類不因本次未出現而刪除。
- `cover_image_url` / `cover_image_hosting`：既有值非空時不覆蓋（見決策 6 的冪等性規則，且不回頭處理既有值，即使既有值是 `http://`）；既有值為空且本次取得新值時才寫入——寫入前若新值為 `http://` 開頭，一律轉換為 `https://`（v1 唯一封面來源 Google Books 的圖片網址常見 `http://`，其 CDN 同網域支援 `https://`，避免未來前端出現 mixed content 問題；NCL 目前無封面欄位，此規則實務上只作用於 Google Books 來源）。
- `categories` 的「聯集累加」在跨來源合併時需要保留每筆分類的來源（`source`），才能正確對應到 `Category` model 的 `(source, type, code, label)` unique key；`books/services/schema.py::CategoryInput` 本身不含 `source`（由各 adapter 依語境隱含），故合併層（`books/services/merge.py`）另定義帶 `source` 的 `ResolvedCategory` 作為輸出/既有狀態的型別，去重以 `(source, type, code, label)` 為準。
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
- 網址：`https://isbn.ncl.edu.tw/NEW_ISBNNet/opendata/[YYYYMM]_isbn.csv`，**年月為西元年**（已實測驗證：`202508` 回 200，民國年 `11508` 回 404）。
- 編碼：**UTF-8 with BOM**（已實測確認，開頭 `EF BB BF`）；解析時以 `utf-8-sig` 讀取去除 BOM。
- 分隔符號：**標準逗號分隔 CSV，RFC4180 quoting**（含逗號的欄位以雙引號包住），Python 內建 `csv` 模組可直接處理，不需自訂分隔符號偵測。
- 欄位對應**以欄位名稱對應（`csv.DictReader`），不得用固定欄位順序（index）**——已實測發現欄位名稱曾經改版：2025-01 為「常用分類」「是否為翻譯書」，2025-07 起變成「建議上架分類」「是否為引進版權著作」，證實了本文件「Risks」一節原本預期的欄位調整風險確實發生過。
- 已知欄位（27 欄，2025-07/08 版本）：申請書名、作者、出版機構、版次、預訂出版日、標題、0-3歲嬰幼兒圖書分齡主題詞、3-6歲幼兒圖書分齡主題詞、適讀對象、分級註記、分類號、ISBN、開數、頁數、得獎紀錄、資料類型、建議上架分類、作品語文、作品語文(其他)、圖書主題、是否為引進版權著作、定價、裝訂方式、其他裝訂方式、出版形式、關鍵字、出版機構類型。無封面圖片相關欄位。
- ISBN 欄位：**每列固定一個 ISBN**（13 碼數字），已實測 4 個月份、共 14000+ 筆資料，無一列多個 ISBN 的情況——每列對應一次出版品登記申請，資料模型上不會有多值 ISBN，故不設計「單列多 ISBN 拆解」邏輯（原 tasks.md 2.1/2.2 的此案例已移除）。仍須處理 ISBN 欄位缺漏／格式無效的邊界情況（實測樣本中未出現，但不保證未來不會發生，需防禦性處理）。
- 分類號欄位：實測約 67% 為空值，是正常情況，非例外。
- 已實測請求 5 個月份確認上述行為：2024-12、2025-01、2025-07、2025-08 成功取得資料（共 14000+ 筆列），2023-12 回應 404（確認開放資料回溯邊界，往前不會抓到更早的資料）。
- 404（該月份尚未 release）：在排程情境下視為正常情況（決策 10）；在手動觸發情境下視為明確錯誤回報給使用者。
- 下載中斷 → 交由 Celery task 層級重試（決策 10）；空檔案 → 視為 0 筆資料，`IngestionRun.status='succeeded'` 但 `total=0`。
- 測試固定使用保存於專案內的 CSV fixture（依實測欄位結構手造，含一份 2025-01 舊欄位版 fixture 驗證「常用分類」別名 fallback），不依賴即時網路請求。
- **下載 client**（`books/sources/ncl.py::download_ncl_csv`，issue #2 範圍內，非留待後續）：`build_ncl_csv_url(month)` 組出網址；`download_ncl_csv(month, client)` 以 `httpx.Client` 執行 GET，timeout 採連線 5 秒／讀取 30 秒（無官方 SLA 可查，讀取時間比照決策 9 但拉長，因單次月度 CSV（實測約 1-1.5MB）比單次 API 回應大）；回傳結構化例外供上層判斷：`NclNotFoundError`（404）、`NclDownloadTimeoutError`（連線/讀取逾時）、`NclNetworkError`（其他網路層錯誤）、`NclDownloadError`（其他非預期 HTTP 狀態，為前三者共同基底類別）。單元測試以 `respx` mock HTTP，不依賴即時網路請求；下載/解析整合行為由 7.x/8.x 與人工 smoke test（10.x）涵蓋。
- **欄位別名 fallback**：`建議上架分類`/`常用分類` 視為同一語意欄位，解析時依序嘗試取值（新欄位優先），確保 backfill 舊月份資料時不會靜默漏值。

### 9. Google Books Client 與重試策略
- 逾時設定：連線 5 秒、讀取 10 秒。
- 重試條件：僅對逾時、429、502/503/504 重試；4xx（除 429）不重試，直接記錄失敗。
- 最大重試次數：3 次，採指數退避＋隨機 jitter，總等待時間上限 30 秒。
- 尊重 `Retry-After` header：**v1 僅支援 delta-seconds（數字秒數）格式**，不解析 HTTP-date 格式（例如 `Wed, 21 Oct 2026 07:28:00 GMT`）；遇到無法解析為數字的值時，退回自算的指數退避，不會因此中斷或報錯。
- 併發限制：v1 以循序（單一併發）呼叫。
- 此層重試是「單一 Google Books 請求」的重試，與決策 10 的「Celery task 整體」重試是不同層級，兩者不互相取代。
- **回傳結果比對**：Google Books 以 ISBN 搜尋不保證第一筆 `items` 就是精確匹配，須逐筆比對該筆 `industryIdentifiers`（依上述優先序解析出的 ISBN-13）是否等於查詢的 ISBN，相符才採用其封面/分類；全部不符時視為 `not_found`（而非誤用不相符書籍的資料）。
- **錯誤訊息不得含 API 金鑰**：HTTP 逾時/連線錯誤的 `GoogleBooksResult.error_message` 僅記錄例外類型名稱（例如 `network error: ConnectError`），不得對 httpx 例外直接 `str()`——exception 字串可能內嵌完整帶金鑰的 request URL，違反本文件決策 12「`IngestionFailure.message` 不得包含 API 金鑰」的要求。
- **回應內容防禦性解析**：200 回應仍可能是非預期格式（非法 JSON、`items` 非 list、單筆項目缺 `volumeInfo`/型別錯誤等）——視為外部邊界輸入，解析失敗時回傳 `status='failed'`，不拋出未處理例外；單筆項目格式錯誤只跳過該筆，不影響其他項目的比對。

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
- `--month`：必填，格式與語意驗證（`books/services/schema.py::is_valid_month`，與 `build_ncl_csv_url` 共用同一份規則，拒絕 `2025-13` 這類形狀正確但月份無效的輸入，避免組出不存在的 NCL 網址、被誤判成「尚未公告」）。
- 內部呼叫 `ingest_month(month, trigger_type='manual')` service 函式。
- 依 Django 慣例實作：成功時 `handle()` 直接 `return`（不主動 `sys.exit`）；失敗時 `raise CommandError(msg, returncode=N)`，由 Django 在透過 `manage.py` 實際執行時轉換為對應的 process exit code；`call_command()`（測試或其他程式碼呼叫）則直接以 `CommandError`/正常回傳表達結果，不強制 `SystemExit`。
- Exit code（`CommandError.returncode`）：`0` 成功／部分成功（無例外，正常 `return`）；`1` 致命錯誤（缺 `GOOGLE_BOOKS_API_KEY`、`ingest_month` 拋出未預期例外、`IngestionRun.status=='failed'`，含手動觸發下的 404）；`2` 參數錯誤（`--month` 缺漏或格式/語意無效）。

### 12. 執行紀錄與失敗紀錄
執行開始時建立一筆 `IngestionRun`（`status='running'`，記錄 `trigger_type`），過程中的失敗逐筆寫入 `IngestionFailure`，執行結束時更新 `IngestionRun` 的統計欄位與最終 `status`（`succeeded`／`partially_failed`／`failed`／`skipped_not_yet_published`）。同時將摘要印至 stdout/log。`IngestionFailure.message` 不得包含 API 金鑰或帶金鑰參數的完整請求網址。

**`status` 判定規則**（`total`／`succeeded`／`failed` 皆為本次 NCL 列處理結果，不含 `skipped_not_yet_published` 這種完全沒有列可處理的情況，那由決策 10 的 404 分支單獨決定）：
- `total == 0`（空檔案）或 `failed == 0`（全部成功）→ `succeeded`
- `succeeded == 0` 且 `failed > 0`（全部失敗，含空檔案以外的情況）→ `failed`
- 其餘（部分成功部分失敗）→ `partially_failed`

**同一 CSV 內重複 ISBN**（決策 4）：以最後一筆為準——因為每筆都各自呼叫 upsert，後面的自然覆蓋前面的，不需要額外邏輯；但仍在 `ingest_month` 逐一統計後，對每個重複出現的 ISBN 記錄一筆 `IngestionFailure`（`stage=ncl_parse`, `error_code=duplicate_isbn_in_csv`）作為 warning，方便事後追查來源資料品質。

**非預期例外的收尾**：`ingest_month` 內部已明確處理的分支（NCL 404、NCL 下載錯誤、單筆 upsert 失敗）都會正常走完並回傳 `IngestionRun`；其餘任何未預期例外（CSV 解碼失敗、`should_query_google_books` 的資料庫錯誤、adapter 本身的程式錯誤等）由最外層的 orchestration guard 統一接住：寫入一筆 `IngestionFailure`（`stage=ingestion`，對應 `IngestionFailure.Stage` 中「未分類」的選項）、將 `IngestionRun.status` 標記為 `failed`，**再重新拋出例外**——不吞掉、不僅記錄——讓手動 management command 轉換為 `CommandError(returncode=1)`，未來 Celery task（issue #8）可以依 `autoretry_for` 機制重試。`total` 在 NCL CSV 解析完成當下就設定（不是等整個書籍迴圈跑完才設），fatal error 時的 `failed` 採 v1 最簡單的算法：`max(total - succeeded, 1)`——不新增 skipped/aborted 欄位，未成功確認的一律算失敗，確保 `total == succeeded + failed` 恆成立，即使是中途 fatal abort。寫入失敗紀錄本身（`record_ingestion_failure`）也可能失敗，此時仍會以 `try/finally` 盡力嘗試 `finish_ingestion_run` 收尾——**這是盡力而為，不是資料庫完全無法寫入時的硬保證**；若整個資料庫層級都不可用，`finish_ingestion_run` 本身也會失敗，run 仍可能停在 `running`。

**Google Books 查詢失敗（非 not_found）時該筆書籍的處理**：依 spec「Google Books 查無對應資料」情境，NCL 資料仍照常 upsert（該筆計入 `succeeded`，缺漏欄位維持空值），並額外記錄一筆 `IngestionFailure`（`stage=google_lookup`）；`google_enriched` 只在 `status='found'` 時累加。

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
- [Risk] NCL CSV 欄位格式調整（已實測證實發生過，見決策 8）→ Mitigation：解析邏輯以欄位名稱對應（非固定順序），並以 fixture 測試鎖定目前已知欄位結構；若未來再次改版，只需更新欄位對應表與 fixture，不影響其餘架構
- [Risk] ISBN 格式不一致或缺漏導致無法寫入資料庫 → Mitigation：合併前一律正規化，無效或缺漏 ISBN 記錄失敗但不中斷整批
- [Risk] 誤將 Google Books 圖片下載保存，違反其 API 條款 → Mitigation：`cover_image_hosting` 欄位與程式邏輯明確區分來源，僅 NCL 圖片進入下載/上傳流程
- [Risk] 每日排程在資料尚未公告的日子持續執行，可能產生大量無意義的 log/執行紀錄 → Mitigation：`skipped_not_yet_published` 狀態不計入失敗、log 層級降低（info 而非 error），必要時未來可加監控但 v1 不做
- [Risk] Celery task 重試與 upsert 冪等性設計搭配不當可能造成重複副作用 → Mitigation：upsert 本身冪等，task 重試整段 `ingest_month` 是安全的，不會因重試而產生重複資料
- [Risk] `categories` 聯集累加、`authors` 覆蓋重建的不對稱規則可能造成混淆 → Mitigation：規則已於決策 5 明確定義並附原因，之後如需調整需先修訂本文件

## Open Questions

- Google Books API 實際配額數字與費用門檻：待申請金鑰後確認（不影響目前的架構與 tasks 拆分）。
- S3 + CloudFront（含 OAC）基礎設施的實際佈建（bucket 政策、CloudFront 設定）：屬於部署前置工作，非本次程式碼 tasks 範圍，待實作 NCL 圖片上傳功能前另行處理。
- 正式雲端資料庫（AWS RDS）與 Redis（ElastiCache）的部署時機與方式：留待未來的部署 change，v1 僅本機 Docker。
- 每日排程的確切觸發時間（目前預設 03:00 Asia/Taipei）：若之後觀察到 NCL 實際公告時間規律，可再調整，不影響架構。
