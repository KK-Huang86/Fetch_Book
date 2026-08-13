> 本檔案依 CLAUDE.md 的 TDD 規範撰寫：每個 seam 先寫失敗測試（red），確認為紅燈後，才寫最小實作讓它通過（green）。不得整批模組寫完才在最後補測試。
>
> Seam 清單沿用先前確認過的範圍，因改採 Django + Celery 而調整名稱/新增一項：ISBN 正規化、NCL CSV 解析、Google Books 查詢、merge 規則、**Django ORM upsert repository**（原「DB upsert repository」）、**management command**（原「CLI」）、**ingest_month service 整合流程**（原「main.py 整合流程」）、**Celery task（新增，排程觸發與重試語意）**。

## 0. Django + Celery + Redis 專案骨架（prefactor，非 TDD 循環）

- [x] 0.1 建立 Django 專案（`config/`）與 `books` app，設定 `DATABASE_URL` 讀取（`.env`）
- [x] 0.2 更新 `requirements.txt`：移除 SQLAlchemy/Alembic，加入 Django、celery、redis、django-celery-beat（或等效排程套件）、httpx、boto3、python-dotenv、pytest、pytest-django、respx
- [x] 0.3 `docker-compose.yml` 新增 Redis service（host port 6380，避免與其他專案的 Redis 容器衝突），保留既有 PostgreSQL service
- [x] 0.4 設定 Google Books API 金鑰、S3 存取憑證、Celery broker URL 的讀取方式（`.env`），確認 `.gitignore` 涵蓋範圍；並在 `config/settings.py` 以具名 Django settings 暴露（`GOOGLE_BOOKS_API_KEY`/`AWS_*`/`S3_BUCKET_NAME`/`CLOUDFRONT_DOMAIN`/`CELERY_BROKER_URL`），不留給後續程式各自散呼叫 `os.environ`
- [x] 0.5 以 Django models 定義決策 3 的資料表（`Publisher`/`Author`/`Book`/`BookAuthor`/`Category`/`BookCategory`/`IngestionRun`/`IngestionFailure`）；`Category.code` 用 `blank=True, default=""`（非 `null=True`），避免 unique 約束在 NULL 上失效
- [x] 0.6 產生並套用初始 Django migration，於本機 Docker Postgres 驗證建表成功
- [x] 0.7 設定 `config/celery.py`（Celery app 初始化）與 Celery Beat 排程：以 `django-celery-beat` 的 `DatabaseScheduler` + 資料遷移建立 `PeriodicTask`/`CrontabSchedule`（`daily-ingestion-placeholder`，`0 3 * * * Asia/Taipei`，指向 placeholder task `books.tasks.ping`），已驗證 worker 可發現 task、`ModelEntry` 可正確解析排程

## 1. Seam 1 — ISBN 正規化（`books/services/schema.py`）

- [x] 1.1 **(red)** 撰寫 `normalize_isbn` 測試：有效 ISBN-13、有效 ISBN-10（含轉換為 13 碼）、含連字號/空白、checksum 無效、空字串/None、非數字字元、13 碼但非 978/979 開頭的一般 EAN-13 等邊界案例；確認測試先為紅燈
- [x] 1.2 **(green)** 實作 `normalize_isbn`，讓 1.1 全數通過
- [x] 1.3 依 CLAUDE.md 規則檢查：依 code review 補上「978/979 開頭」邊界案例（原實作會誤收一般 EAN-13），先寫紅燈測試再修正實作

## 2. Seam 2 — NCL CSV 下載與解析（`books/sources/ncl.py`）

> 已實際請求 5 個月份（2024-12、2025-01、2025-07、2025-08 成功；2023-12 回應 404，確認回溯邊界）NCL CSV 確認真實契約，記錄於 design.md 決策 8：西元年月、UTF-8 with BOM、標準逗號分隔 CSV、27 欄（以欄位名稱對應，非固定順序；`建議上架分類`/`常用分類` 為同一語意欄位的新舊名稱，已做 fallback）、無封面欄位、每列固定 1 個 ISBN（無多 ISBN 列，故拿掉原「單列多 ISBN 拆解」案例）。

- [x] 2.1 依實測欄位結構手造最小 CSV 測試樣本（正常列、缺 ISBN 列、ISBN 格式無效列、缺分類號欄位列、空檔案、舊欄位版本、好壞列交錯批次），存至 `books/tests/fixtures/`（4 個月份真實資料衍生的正常列，涵蓋單一作者+角色字尾、句點黏連角色字尾、逗號多作者+引號欄位、分號+逗號混合作者群組、2025-01 舊欄位「常用分類」等真實樣態）
- [x] 2.2 **(red)** 針對 CSV 解析函式（輸入 CSV bytes、輸出中介資料結構，不含網路下載）撰寫測試，涵蓋 2.1 的各種樣本；確認測試先為紅燈（`ModuleNotFoundError: books.sources.ncl`）
- [x] 2.3 **(green)** 實作解析邏輯（`utf-8-sig` 解碼、`csv.DictReader` 依欄位名稱對應，含新舊欄位別名 fallback；另於 `books/services/schema.py` 新增 `split_authors`——依真實資料切分並去除角色字尾/著/編著/譯/主編等，見 `books/tests/test_schema.py::TestSplitAuthors`——與 `CategoryInput`/`ParsedBookRecord`/`ParseFailure` 中介資料結構），讓 2.2 全數通過
- [x] 2.4 **(red→green)** 實作下載 client（`build_ncl_csv_url`/`download_ncl_csv`，依月份組網址、`httpx` GET、連線 5 秒／讀取 30 秒逾時、404/逾時/網路錯誤對應結構化例外 `NclNotFoundError`/`NclDownloadTimeoutError`/`NclNetworkError`/`NclDownloadError`），單元測試以 `respx` mock HTTP、不依賴即時網路請求；404 該如何解讀（排程視為正常/手動視為錯誤）留給 #5/#8 決定，不在本 seam 處理
- [x] 2.5 補強「壞列不中斷整批解析」測試：同一份 CSV 內好壞列交錯（好→缺ISBN→好→無效ISBN），驗證中間壞列不中止迴圈、後續正常列仍正確解析

## 3. Seam 3 — Google Books 查詢（`books/sources/google_books.py`）

- [x] 3.1 建立 mock HTTP 回應 fixture：成功（含多個 `industryIdentifiers`）、查無資料、429、5xx、timeout（`books/tests/test_google_books.py` 內以 respx 建構，非另存檔案，因為是 API JSON 回應而非下載樣本）
- [x] 3.2 **(red)** 撰寫查詢函式測試（HTTP 以 mock 取代），涵蓋 3.1 各情境、`industryIdentifiers` 優先序、逾時與重試（僅 429/502/503/504/timeout 重試、其他 4xx 與 500 不重試、最多 3 次、尊重 `Retry-After`、總等待上限 30 秒）；確認測試先為紅燈（`ModuleNotFoundError: books.sources.google_books`）
- [x] 3.3 **(green)** 實作 `query_google_books_by_isbn`（`books/sources/google_books.py`）：逾時（連線 5s／讀取 10s）、重試與退避＋jitter＋`Retry-After` 優先、總等待時間以剩餘預算裁切；回傳 `GoogleBooksResult`（`found`/`not_found`/`failed`，見 `books/services/schema.py`），不拋出未處理例外，讓 3.2 全數通過
- [x] 3.4 依 CLAUDE.md 規則檢查（PR review 3 項修正，先紅燈後修正）：(a) 首版只解析、未實際比對回傳項目的 ISBN 是否等於查詢的 ISBN 就採用其資料，改為逐筆比對相符才採用，全不符時回傳 `not_found`；(b) 逾時/連線錯誤的 `error_message` 直接 `str()` httpx 例外，可能內嵌帶金鑰的 request URL，改為只記錄例外類型名稱；(c) 200 回應為非法 JSON 或欄位型別錯誤時會拋出未處理例外，改為防禦性解析＋外層 try/except，回傳 `status='failed'`；順便將 `GoogleBooksResult.status` 改為 `Literal` 型別、`categories` 增加 strip/去重/濾除非字串與空值
## 4. Seam 4 — 合併/欄位覆蓋規則（`books/services/merge.py`）

- [x] 4.1 **(red)** 撰寫測試：書目欄位（title/publisher/authors）以 NCL 覆蓋、categories 聯集累加不刪除既有、cover_image 既有非空值不覆蓋、URL 升級 https 等 design.md 決策 5 的規則；確認測試先為紅燈（`ModuleNotFoundError: books.services.merge`）
- [x] 4.2 **(green)** 實作 `merge.py`（`merge_book_fields`，新增 `ResolvedCategory`/`ExistingBookState`/`MergedBookFields` 型別——`CategoryInput` 本身不含來源，合併層才需要區分 NCL/Google Books 來源以對應 `Category` 的 unique key），讓 4.1 全數通過
- [x] 4.3 依 CLAUDE.md 規則檢查（PR review）：`merge_book_fields` 原本未驗證 `google_result.isbn13` 是否等於 `ncl_record.isbn13` 就合併其封面/分類——正常流程不會出錯（呼叫端固定用 NCL 記錄自己的 ISBN 去查 Google Books），但這是兩來源資料真正結合的邊界，未來若接線寫錯會靜默把錯的書籍資料黏到另一本書上；先寫紅燈測試（不符時應 `raise ValueError`）確認會 `DID NOT RAISE`，再補上驗證邏輯

## 5. Seam 5 — Django ORM Upsert Repository（`books/services/ingest.py` 內的寫入邏輯）

- [x] 5.1 **(red)**（使用 `pytest-django` 的資料庫測試，transaction rollback 隔離）撰寫測試：首次新增新書籍、同 ISBN 重複執行更新既有紀錄（非新增重複列）、categories 累加、authors 覆蓋重建、`cover_image` 既有值保留規則、單筆失敗 rollback 不留下部分寫入；確認測試先為紅燈（`ModuleNotFoundError: books.services.ingest`）
- [x] 5.2 **(green)** 實作 `upsert_book`（`select_for_update` + `transaction.atomic()` 包裹單筆寫入、失敗 rollback 該筆不影響同批其餘；`Publisher`/`Author`/`Category` 皆以 `get_or_create` 建立，`BookAuthor` 先清除重建、`BookCategory` 只新增不刪除），讓 5.1 全數通過
- [x] 5.2.1 依 CLAUDE.md 規則檢查（PR review）：`select_for_update()` 無法鎖住尚不存在的資料列，兩個 worker 同時新增同一個新 ISBN 時，其中一個會在 `Book.objects.create()` 撞到 unique constraint 而整個 ingestion 被記成失敗（資料不會重複，但少算一筆成功）。先寫紅燈測試（模擬第一次查詢查無資料、但實際 `create()` 因真實 unique constraint 撞到 `IntegrityError`），確認會直接往外拋出未處理例外；再補上復原邏輯：`create()` 包一層 nested `atomic()`（savepoint），`IntegrityError` 時改為 `select_for_update().get()` 鎖住並對贏家的實際狀態重新跑一次 `merge_book_fields` 後更新，而非讓例外中斷該筆 ingestion
- [x] 5.3 **(red)** 撰寫測試：enrichment 冪等性判斷（已有非空封面/分類的 ISBN 應跳過 Google Books；尚未補齊的則應查詢；ISBN 尚未存在於資料庫也應查詢；僅 NCL 來源分類不算數，須為 `source=google_books`）；確認測試先為紅燈
- [x] 5.4 **(green)** 實作 `should_query_google_books`，讓 5.3 全數通過
- [x] 5.5 **(red)** 撰寫測試：`IngestionRun` 建立（`start_ingestion_run`，狀態/`trigger_type`）與更新（`finish_ingestion_run`，統計欄位、`finished_at`）、`IngestionFailure` 寫入（`record_ingestion_failure`）；確認測試先為紅燈
- [x] 5.6 **(green)** 實作對應寫入邏輯，讓 5.5 全數通過
- [x] 5.6.1 依 CLAUDE.md 規則檢查（PR review，商業邏輯調整並同步修改測試）：`record_ingestion_failure` 原本對含 `key=` 字樣的訊息直接 `raise ValueError` 拒絕寫入，但這個函式通常從例外處理路徑呼叫，若自己又拋例外，會讓「記錄這一筆失敗」變成「整批中斷」，違反「單筆失敗不中斷整批」的核心規則；改為 redact（`key=[REDACTED]`）後正常寫入，永不拋出。原本斷言「拒絕寫入」的測試已同步改寫為斷言「redact 後寫入成功」，並補上 `api_key=`、`key = ` 等變體案例

## 6. Seam 6 — Management Command（手動觸發，`books/management/commands/ingest_books.py`）

- [x] 6.1 **(red)** 撰寫測試：`--month` 格式驗證（缺參數、含合法/不合法格式）、缺 Google Books API 金鑰情境（且應在呼叫 `ingest_month` 前就攔下）、exit code（0 成功/部分成功、1 致命錯誤、2 參數錯誤）、呼叫 `ingest_month` 時固定帶 `trigger_type='manual'`、summary 輸出至 stdout；確認測試先為紅燈（`ModuleNotFoundError: books.management.commands.ingest_books`）
- [x] 6.2 **(green)** 實作 management command，讓 6.1 全數通過
- [x] 6.3 依 CLAUDE.md 規則檢查（PR review，商業邏輯調整並同步修改測試）：
  - 月份驗證改用 `books/services/schema.py::is_valid_month` 共用函式（拒絕 `2025-13`/`2025-00` 這類形狀正確但語意無效的月份，避免組出不存在的 NCL 網址、被誤判成「尚未公告」），`books/sources/ncl.py::build_ncl_csv_url` 同步改用同一份驗證，不再各自維護一份寬鬆 regex
  - Command 改為 Django 慣例：成功路徑 `return`（不再無條件 `sys.exit(0)`），失敗路徑一律 `raise CommandError(msg, returncode=N)`（2=參數錯誤、1=致命錯誤/API金鑰缺失/`ingest_month` 拋出未預期例外/`IngestionRun.status=='failed'`）；原本用 `sys.exit()` 的測試已同步改寫為斷言 `CommandError`/正常 `return`，因為 `sys.exit(0)` 會讓 `call_command()` 在任何呼叫情境（含其他程式碼重用、測試組合）都無條件拋出 `SystemExit`，不是 Django 慣例

## 7. Seam 7 — `ingest_month` service 整合流程（端到端，框架無關）

- [x] 7.1 **(red)** 撰寫整合測試：mock `download_ncl_csv`/`query_google_books_by_isbn`（真實 `parse_ncl_csv` 對已知 fixture 解析）＋測試用資料庫，涵蓋成功案例、單筆解析失敗不中斷整批且正確寫入 `IngestionFailure`、單筆 upsert 例外不中斷整批、enrichment 冪等性在完整流程中生效（第二次執行不重複查詢已補齊的書）、Google 查詢失敗時該書仍以 NCL 資料寫入且不中斷、同一 CSV 內重複 ISBN 以最後一筆為準並記錄一筆 warning、當月 404 時排程觸發回傳 `skipped_not_yet_published`／手動觸發回傳明確 `failed`、下載逾時等其他錯誤整批標記失敗且不留部分資料；確認測試先為紅燈（`ImportError: cannot import name 'ingest_month'`）
- [x] 7.2 **(green)** 實作 `ingest_month(month, trigger_type)`（NCL 下載解析 → 重複 ISBN warning → 逐筆冪等性檢查 → 視需要呼叫 Google Books → upsert → 統計與 `IngestionRun.status` 判定：`total==0` 或 `failed==0` 為 succeeded、`succeeded==0` 為 failed、其餘為 partially_failed），讓 7.1 全數通過
- [x] 7.3 依 CLAUDE.md 規則檢查（PR review High finding）：原本只處理 `NclNotFoundError`/`NclDownloadError`/單筆 `upsert_book` 例外，CSV 解碼失敗、`should_query_google_books` 的資料庫錯誤、Google adapter 未預期錯誤等任何其他例外都會直接往外拋，讓 `IngestionRun` 永久停在 `status='running'`、`finished_at=NULL`，牴觸 spec 的「非預期錯誤 MUST 標記為失敗並保留可查詢的失敗紀錄」。先寫紅燈測試（確認 run 卡在 running），再加上外層 try/except：捕捉後寫入一筆 `IngestionFailure`（`stage=ingestion`）、將 run 標記為 `failed`，再重新拋出例外（讓手動指令回報 exit 1、未來 Celery task 仍可重試）
- [x] 7.4 依 CLAUDE.md 規則檢查（PR review 第二輪，2 項收尾）：(a) `total` 原本要等書籍迴圈整個跑完才設定，fatal error 中途發生時會保留初始值 0，造成 `total`／`succeeded`／`failed` 互相矛盾——先寫紅燈測試（4 筆資料、第 2 筆 Google adapter 拋未預期例外，驗證 `run.total==4`），改為 NCL 解析完成當下就設定 `total`，fatal 分支的 `failed` 改採 `max(total - succeeded, 1)`（v1 不新增 skipped 欄位，未確認成功的一律算失敗，確保 `total==succeeded+failed` 恆成立）；(b) fatal guard 內的 `record_ingestion_failure` 本身若失敗，原本會讓 `finish_ingestion_run` 完全不執行、run 仍卡在 running——先寫紅燈測試（mock `record_ingestion_failure` 拋例外），改用 `try/finally` 確保 `finish_ingestion_run` 仍會被嘗試（無法保證資料庫完全不可寫時仍成功，design.md 已同步弱化措辭）。另外 management command 的未預期例外訊息原本會包含 `str(exc)`，可能洩漏潛在敏感內容，改為只保留例外類型名稱

## 8. Seam 8 — Celery Task（排程觸發，`books/tasks.py`）

- [ ] 8.1 **(red)** 撰寫測試：task 呼叫 `ingest_month` 並正確處理三種結果——成功/部分成功（`IngestionRun.status` 對應設定）、當月尚未公告（`status='skipped_not_yet_published'`，**不觸發 Celery 重試**）、非預期例外（觸發 `autoretry_for`，最多重試 3 次＋指數退避，重試上限後標記為 `failed`）；確認測試先為紅燈
- [ ] 8.2 **(green)** 實作 Celery task 與 Celery Beat 每日排程設定（決策 10），讓 8.1 全數通過

## 9. 封面圖片儲存（`books/storage/s3.py`，未列為正式 TDD seam，維持一般任務拆分）

- [ ] 9.1 實作 NCL 圖片下載並上傳至 S3、標記 `cover_image_hosting = 'self'`
- [ ] 9.2 Google Books 提供封面時僅保留原始網址、標記 `hotlink`，不呼叫上傳邏輯
- [ ] 9.3 兩來源皆無封面時，欄位維持明確空值，不中斷批次
- [ ] 9.4（部署前置，非程式碼）確認 S3 bucket 與 CloudFront + OAC 設定就緒（可等 2.5 確認 NCL 是否真的提供封面欄位後再排優先序）

## 10. 人工 Smoke Test（不屬於自動化測試）

- [ ] 10.1 以實際一份月度 CSV 與真實 Google Books API，對本機 Docker Postgres + Redis 執行一次 management command 與一次 Celery task 觸發，人工查詢資料庫驗證結果（含重跑一次驗證冪等性與累積行為）
- [ ] 10.2 人工確認 Celery Beat 排程設定實際生效（本機觀察至少一次自動觸發，或手動調整排程時間驗證後改回正式排程）
