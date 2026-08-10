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

## 2. Seam 2 — NCL CSV 解析（`books/sources/ncl.py` 的純解析函式）

> 已實際下載 4 個月份（2024-12、2025-01、2025-07、2025-08）NCL CSV 確認真實契約，記錄於 design.md 決策 8：西元年月、UTF-8 with BOM、標準逗號分隔 CSV、27 欄（以欄位名稱對應，非固定順序）、無封面欄位、每列固定 1 個 ISBN（無多 ISBN 列，故拿掉原「單列多 ISBN 拆解」案例）。2.5（回頭確認契約）已提前完成，不再需要事後補齊。

- [x] 2.1 依實測欄位結構手造最小 CSV 測試樣本（正常列、缺 ISBN 列、ISBN 格式無效列、缺分類號欄位列、空檔案），存至 `books/tests/fixtures/`（4 個月份真實資料衍生的 4 筆正常列，涵蓋單一作者+角色字尾、句點黏連角色字尾、逗號多作者+引號欄位、分號+逗號混合作者群組等真實樣態）
- [x] 2.2 **(red)** 針對 CSV 解析函式（輸入 CSV bytes、輸出中介資料結構，不含網路下載）撰寫測試，涵蓋 2.1 的各種樣本；確認測試先為紅燈（`ModuleNotFoundError: books.sources.ncl`）
- [x] 2.3 **(green)** 實作解析邏輯（`utf-8-sig` 解碼、`csv.DictReader` 依欄位名稱對應；另於 `books/services/schema.py` 新增 `split_authors`——依真實資料切分並去除角色字尾/著/編著/譯/主編等，見 `books/tests/test_schema.py::TestSplitAuthors`——與 `CategoryInput`/`ParsedBookRecord`/`ParseFailure` 中介資料結構），讓 2.2 全數通過
- [x] 2.4 下載邏輯（依月份組網址、HTTP 下載、404/逾時/中斷處理）屬於 I/O 邊界，不納入本 seam 單元測試，改由 7.x/8.x 整合測試與人工 smoke test 涵蓋

## 3. Seam 3 — Google Books 查詢（`books/sources/google_books.py`）

- [ ] 3.1 建立 mock HTTP 回應 fixture：成功（含多個 `industryIdentifiers`）、查無資料、429、5xx、timeout
- [ ] 3.2 **(red)** 撰寫查詢函式測試（HTTP 以 mock 取代），涵蓋 3.1 各情境、`industryIdentifiers` 優先序、逾時與重試（僅 429/5xx/timeout 重試、4xx 不重試、最多 3 次、尊重 `Retry-After`）；確認測試先為紅燈
- [ ] 3.3 **(green)** 實作查詢函式、逾時（連線 5s／讀取 10s）、重試與退避＋jitter 邏輯，讓 3.2 全數通過

## 4. Seam 4 — 合併/欄位覆蓋規則（`books/services/merge.py`）

- [ ] 4.1 **(red)** 撰寫測試：書目欄位（title/publisher/authors）以 NCL 覆蓋、categories 聯集累加不刪除既有、cover_image 既有非空值不覆蓋、URL 升級 https 等 design.md 決策 5 的規則；確認測試先為紅燈
- [ ] 4.2 **(green)** 實作 `merge.py`，讓 4.1 全數通過

## 5. Seam 5 — Django ORM Upsert Repository（`books/services/ingest.py` 內的寫入邏輯）

- [ ] 5.1 **(red)**（使用 `pytest-django` 的資料庫測試，transaction rollback 隔離）撰寫測試：首次新增新書籍、同 ISBN 重複執行更新既有紀錄（非新增重複列）、categories 累加、authors 覆蓋重建、`cover_image` 既有值保留規則；確認測試先為紅燈
- [ ] 5.2 **(green)** 實作 upsert 邏輯（`select_for_update` + `transaction.atomic()` 包裹單筆寫入、失敗 rollback 該筆不影響同批其餘），讓 5.1 全數通過
- [ ] 5.3 **(red)** 撰寫測試：enrichment 冪等性判斷（已有非空封面/分類的 ISBN 應跳過 Google Books；尚未補齊的則應查詢）；確認測試先為紅燈
- [ ] 5.4 **(green)** 實作冪等性判斷邏輯，讓 5.3 全數通過
- [ ] 5.5 **(red)** 撰寫測試：`IngestionRun` 建立/更新（狀態、統計欄位、`trigger_type`）、`IngestionFailure` 寫入（含不得包含 API 金鑰/完整帶金鑰網址）；確認測試先為紅燈
- [ ] 5.6 **(green)** 實作對應寫入邏輯，讓 5.5 全數通過

## 6. Seam 6 — Management Command（手動觸發，`books/management/commands/ingest_books.py`）

- [ ] 6.1 **(red)** 撰寫測試：`--month` 格式驗證（含合法/不合法格式）、缺 Google Books API 金鑰情境、當月 404（手動觸發下視為明確錯誤）、對應 exit code（0 成功/部分成功、1 致命錯誤、2 參數錯誤）；確認測試先為紅燈
- [ ] 6.2 **(green)** 實作 management command，讓 6.1 全數通過

## 7. Seam 7 — `ingest_month` service 整合流程（端到端，框架無關）

- [ ] 7.1 **(red)** 撰寫整合測試：以 mock 的 NCL/Google Books adapter ＋ 測試用資料庫，驗證整體流程——成功案例、單筆失敗不中斷整批且正確寫入 `IngestionFailure`、enrichment 冪等性在完整流程中生效、當月 404 時回傳明確的「尚未公告」結果；確認測試先為紅燈
- [ ] 7.2 **(green)** 實作 `ingest_month(month, trigger_type)`（NCL 下載解析 → 逐筆檢查既有紀錄與冪等性 → 視需要呼叫 Google Books → upsert → 更新批次統計），讓 7.1 全數通過

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
