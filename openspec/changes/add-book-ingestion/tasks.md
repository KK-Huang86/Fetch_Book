## 1. 專案初始化

- [ ] 1.1 建立 `crawler/` 套件結構（`sources/`、`storage/`、`db/`、`schema.py`、`merge.py`、`cli.py`、`main.py`、`tests/fixtures/`）
- [ ] 1.2 建立 `requirements.txt`（HTTP 請求、SQLAlchemy、Alembic、資料庫驅動、S3 上傳所需套件）
- [ ] 1.3 撰寫 `docker-compose.yml` 啟動本機 PostgreSQL
- [ ] 1.4 設定 Google Books API 金鑰、S3 存取憑證、資料庫連線字串（`DATABASE_URL`）的讀取方式（`.env`），並確認 `.gitignore` 涵蓋範圍

## 2. 資料庫 Schema 與 Model

- [ ] 2.1 初始化 Alembic，設定其讀取 `DATABASE_URL` 的方式
- [ ] 2.2 以 SQLAlchemy 定義 models：`publishers`、`authors`、`books`、`book_authors`、`categories`、`book_categories`、`ingestion_runs`、`ingestion_failures`（對應 design.md 決策 2 的欄位與約束）
- [ ] 2.3 產生並套用初始 migration，於本機 Docker Postgres 驗證建表成功

## 3. 共用中介資料結構與 ISBN 正規化

- [ ] 3.1 在 `schema.py` 定義匯入流程中介用的書籍資料結構（尚未寫入 DB 前的暫存形狀）
- [ ] 3.2 實作 ISBN 正規化函式：移除連字號/空白、10 碼轉 13 碼、checksum 驗證

## 4. CLI 契約

- [ ] 4.1 實作 `cli.py`：`--month`（必填，格式驗證）
- [ ] 4.2 實作 exit code 邏輯（0 成功／部分成功、1 致命錯誤、2 參數錯誤）
- [ ] 4.3 缺少 Google Books API 金鑰時，僅略過 enrichment 並於摘要/`ingestion_failures` 註記，不中止整批

## 5. 國家圖書館（NCL）來源

- [ ] 5.1 依 `--month` 組出月度 CSV 網址並下載（確認實際年月格式）
- [ ] 5.2 處理下載錯誤（404、逾時、中斷）與空檔案情況
- [ ] 5.3 確認並記錄實際 CSV 編碼（BOM/UTF-8/Big5）與分隔符號，統一轉為 UTF-8 處理
- [ ] 5.4 建立實際欄位對應表（分類號、建議上架分類、封面欄位是否存在等），轉換為中介資料結構
- [ ] 5.5 處理單列多 ISBN 拆解、缺 ISBN 資料列（記錄失敗、不寫入 DB）、同 CSV 重複 ISBN
- [ ] 5.6 將一份實際下載的 CSV 樣本存為測試 fixture（`tests/fixtures/`）

## 6. Google Books 來源（enrichment only，含冪等性）

- [ ] 6.1 實作查詢前的冪等性檢查：讀取資料庫該 ISBN 現有的 `cover_image_url`／`google_books` 分類關聯，皆非空則跳過查詢
- [ ] 6.2 實作以單一 ISBN 查詢 Google Books API 的函式（僅查詢，不做搜尋/分頁）
- [ ] 6.3 依 design.md 的規則挑選 `industryIdentifiers`（優先 ISBN_13）
- [ ] 6.4 實作 timeout（連線 5s／讀取 10s）與重試策略（僅 timeout/429/5xx 重試、最多 3 次、指數退避＋jitter、尊重 `Retry-After`）
- [ ] 6.5 處理查無資料/請求失敗情況：記錄至 `ingestion_failures`，不中斷整批
- [ ] 6.6 建立 Google Books API mock 回應 fixture（成功、查無資料、429、5xx、timeout 各一）

## 7. 封面圖片儲存

- [ ] 7.1 實作 `storage/s3.py`：下載 NCL 提供的圖片並上傳至 S3，回傳 CloudFront 網址，標記 `cover_image_hosting = 'self'`
- [ ] 7.2 Google Books 提供封面時，僅保留原始網址，標記 `cover_image_hosting = 'hotlink'`，不呼叫上傳邏輯
- [ ] 7.3 兩個已核准來源皆無封面時，`cover_image_url`/`cover_image_hosting` 維持明確空值，不中斷批次
- [ ] 7.4（部署前置，非程式碼）確認 S3 bucket 與 CloudFront + OAC 設定就緒（此項可等 5.4 確認 NCL 是否真的提供封面欄位後再排優先序）

## 8. 資料庫寫入（Upsert 邏輯）

- [ ] 8.1 實作 `db/repository.py`：以 `isbn13` 做 `ON CONFLICT DO UPDATE` upsert `books`
- [ ] 8.2 實作 `publishers`／`authors` 依名稱查找或新增，並重建該書的 `book_authors` 關聯
- [ ] 8.3 實作 `categories` 依 `(source, type, code, label)` 查找或新增，並新增（非覆蓋）`book_categories` 關聯
- [ ] 8.4 實作 `cover_image_url`/`cover_image_hosting` 的既有值保留規則（非空不覆蓋）
- [ ] 8.5 實作單筆寫入以 savepoint 包裹，單筆失敗 rollback 該筆、不影響同批其餘書籍
- [ ] 8.6 執行開始/結束時建立與更新 `ingestion_runs`（狀態、統計欄位）
- [ ] 8.7 實作 `ingestion_failures` 寫入，確保不含 API 金鑰或帶金鑰的完整請求網址

## 9. 整合與進入點

- [ ] 9.1 實作 `main.py`：NCL 下載解析 → 逐筆檢查資料庫既有紀錄與冪等性 → 視需要呼叫 Google Books → upsert → 更新批次統計
- [ ] 9.2 串接 CLI exit code 與整體流程的成功/部分成功/失敗狀態
- [ ] 9.3 實作執行摘要輸出（成功/失敗筆數、enrichment 成功筆數）至 stdout/stderr

## 10. 測試

- [ ] 10.1 單元測試：ISBN 正規化（含 checksum 無效、10→13 轉換）
- [ ] 10.2 單元測試：NCL CSV 解析（使用 5.6 的 fixture，含缺欄位、多 ISBN 列、空檔案）
- [ ] 10.3 Mock-based 整合測試：Google Books client 對各 mock 回應（成功/查無資料/429/5xx/timeout）的重試與結果處理，不依賴真實網路請求
- [ ] 10.4 資料庫整合測試（使用測試用 schema 或每個測試以 transaction rollback 隔離）：首次寫入新書籍、重複執行更新既有紀錄、categories 聯集累加、authors 覆蓋重建
- [ ] 10.5 測試：enrichment 冪等性——已有非空封面/分類的 ISBN 重跑時不呼叫 Google Books
- [ ] 10.6 測試：單筆失敗（NCL 列解析失敗或 Google Books 查詢失敗）不中斷整批，且正確寫入 `ingestion_failures`，`ingestion_runs` 統計正確
- [ ] 10.7 測試：CLI 參數驗證（`--month` 格式錯誤、缺 API 金鑰）與對應 exit code
- [ ] 10.8 獨立的人工 smoke-test checklist（不屬於自動化測試）：以實際一份月度 CSV 與真實 Google Books API，對本機 Docker Postgres 執行整個流程，人工查詢資料庫驗證結果
