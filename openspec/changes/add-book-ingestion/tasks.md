> 本檔案依 CLAUDE.md 的 TDD 規範撰寫：每個 seam 先寫失敗測試（red）、確認為紅燈後，才寫最小實作讓它通過（green）。不得整批模組寫完才在最後補測試。已確認的 seam：ISBN 正規化、NCL CSV 解析、Google Books 查詢、merge 規則、DB upsert repository、CLI、main.py 整合流程。

## 0. 專案初始化與資料庫骨架（prefactor，非 TDD 循環）

- [ ] 0.1 建立 `crawler/` 套件結構（`sources/`、`storage/`、`db/`、`schema.py`、`merge.py`、`cli.py`、`main.py`、`tests/fixtures/`）
- [ ] 0.2 建立 `requirements.txt`（HTTP 請求、SQLAlchemy、Alembic、資料庫驅動、S3 上傳、測試框架）
- [ ] 0.3 撰寫 `docker-compose.yml` 啟動本機 PostgreSQL
- [ ] 0.4 設定 Google Books API 金鑰、S3 存取憑證、`DATABASE_URL` 的讀取方式（`.env`），確認 `.gitignore` 涵蓋範圍
- [ ] 0.5 初始化 Alembic；以 SQLAlchemy 定義 models（`publishers`、`authors`、`books`、`book_authors`、`categories`、`book_categories`、`ingestion_runs`、`ingestion_failures`，對應 design.md 決策 2）
- [ ] 0.6 產生並套用初始 migration，於本機 Docker Postgres 驗證建表成功

## 1. Seam 1 — ISBN 正規化（`schema.py`）

- [ ] 1.1 **(red)** 撰寫 `normalize_isbn` 測試：有效 ISBN-13、有效 ISBN-10（含轉換為 13 碼）、含連字號/空白、checksum 無效、空字串/None、非數字字元等邊界案例；確認測試先為紅燈
- [ ] 1.2 **(green)** 實作 `normalize_isbn`，讓 1.1 全數通過
- [ ] 1.3 依 CLAUDE.md 規則檢查：測試是否已涵蓋邊界案例，未涵蓋則回頭補（回到 1.1），不得為了讓實作簡單而刪減案例

## 2. Seam 2 — NCL CSV 解析（`sources/ncl.py` 的純解析函式）

- [ ] 2.1 手造最小 CSV 測試樣本（正常列、多 ISBN 列、缺 ISBN 列、缺分類欄位、空檔案），存至 `tests/fixtures/`（待 2.5 取得實際下載檔後替換/補充為真實樣本）
- [ ] 2.2 **(red)** 針對 CSV 解析函式（輸入 CSV bytes、輸出中介資料結構，不含網路下載）撰寫測試，涵蓋 2.1 的各種樣本；確認測試先為紅燈
- [ ] 2.3 **(green)** 實作解析邏輯（含編碼/BOM/分隔符號偵測、欄位對應、單列多 ISBN 拆解），讓 2.2 全數通過
- [ ] 2.4 下載邏輯（依 `--month` 組網址、HTTP 下載、404/逾時/中斷處理）屬於 I/O 邊界，不納入本 seam 單元測試，改由 8.x 整合測試與人工 smoke test 涵蓋
- [ ] 2.5 第一次實際下載月度 CSV 後，確認真實年月格式、編碼、分隔符號，回頭補齊/替換 2.1 的 fixture 與 2.2 的測試案例

## 3. Seam 3 — Google Books 查詢（`sources/google_books.py`）

- [ ] 3.1 建立 mock HTTP 回應 fixture：成功（含多個 `industryIdentifiers`）、查無資料、429、5xx、timeout
- [ ] 3.2 **(red)** 撰寫查詢函式測試（HTTP 以 mock 取代），涵蓋 3.1 各情境、`industryIdentifiers` 優先序（ISBN_13 > ISBN_10）、逾時與重試（僅 429/5xx/timeout 重試、4xx 不重試、最多 3 次、尊重 `Retry-After`）；確認測試先為紅燈
- [ ] 3.3 **(green)** 實作查詢函式、逾時（連線 5s／讀取 10s）、重試與退避＋jitter 邏輯，讓 3.2 全數通過

## 4. Seam 4 — 合併/欄位覆蓋規則（`merge.py`）

- [ ] 4.1 **(red)** 撰寫測試：書目欄位（title/publisher/authors）以 NCL 覆蓋、categories 聯集累加不刪除既有、cover_image 既有非空值不覆蓋、URL 升級 https 等 design.md 決策 4 的規則；確認測試先為紅燈
- [ ] 4.2 **(green)** 實作 `merge.py`，讓 4.1 全數通過

## 5. Seam 5 — DB Upsert Repository（`db/repository.py`）

- [ ] 5.1 **(red)** 撰寫測試（透過 repository 自身的公開查詢介面驗證，不直接下 SQL）：首次新增新書籍、同 ISBN 重複執行更新既有紀錄（非新增重複列）、categories 累加、authors 覆蓋重建、`cover_image` 既有值保留規則；確認測試先為紅燈
- [ ] 5.2 **(green)** 實作 upsert 邏輯（`ON CONFLICT (isbn13) DO UPDATE`、savepoint 包裹單筆寫入、失敗 rollback 該筆不影響同批其餘），讓 5.1 全數通過
- [ ] 5.3 **(red)** 撰寫測試：enrichment 冪等性判斷（已有非空封面/分類的 ISBN，查詢介面回報「應跳過 Google Books」；尚未補齊的則回報「應查詢」）；確認測試先為紅燈
- [ ] 5.4 **(green)** 實作冪等性判斷邏輯，讓 5.3 全數通過
- [ ] 5.5 **(red)** 撰寫測試：`ingestion_runs` 建立/更新（狀態、統計欄位）、`ingestion_failures` 寫入（含不得包含 API 金鑰/完整帶金鑰網址）；確認測試先為紅燈
- [ ] 5.6 **(green)** 實作對應寫入邏輯，讓 5.5 全數通過

## 6. Seam 6 — CLI 參數解析與 exit code（`cli.py`）

- [ ] 6.1 **(red)** 撰寫測試：`--month` 格式驗證（含合法/不合法格式）、缺 Google Books API 金鑰情境、對應 exit code（0 成功/部分成功、1 致命錯誤、2 參數錯誤）；確認測試先為紅燈
- [ ] 6.2 **(green)** 實作 `cli.py`，讓 6.1 全數通過

## 7. Seam 7 — main.py 整合流程（端到端）

- [ ] 7.1 **(red)** 撰寫整合測試：以 mock 的 NCL/Google Books adapter ＋ 測試用資料庫（transaction rollback 隔離），驗證整體流程——成功案例、單筆失敗（NCL 解析失敗或 Google Books 查詢失敗）不中斷整批且正確寫入 `ingestion_failures`、enrichment 冪等性在完整流程中生效；確認測試先為紅燈
- [ ] 7.2 **(green)** 實作 `main.py`（NCL 下載解析 → 逐筆檢查既有紀錄與冪等性 → 視需要呼叫 Google Books → upsert → 更新批次統計）與執行摘要輸出，讓 7.1 全數通過

## 8. 封面圖片儲存（`storage/s3.py`，未列為正式 TDD seam，維持一般任務拆分）

- [ ] 8.1 實作 NCL 圖片下載並上傳至 S3、標記 `cover_image_hosting = 'self'`
- [ ] 8.2 Google Books 提供封面時僅保留原始網址、標記 `hotlink`，不呼叫上傳邏輯
- [ ] 8.3 兩來源皆無封面時，欄位維持明確空值，不中斷批次
- [ ] 8.4（部署前置，非程式碼）確認 S3 bucket 與 CloudFront + OAC 設定就緒（可等 2.5 確認 NCL 是否真的提供封面欄位後再排優先序）

## 9. 人工 Smoke Test（不屬於自動化測試）

- [ ] 9.1 以實際一份月度 CSV 與真實 Google Books API，對本機 Docker Postgres 執行整個流程，人工查詢資料庫驗證結果（含重跑一次驗證冪等性與累積行為）
