## 1. 專案初始化

- [ ] 1.1 建立 `crawler/` 套件結構（`sources/`、`schema.py`、`merge.py`、`output.py`、`main.py`）
- [ ] 1.2 建立 `requirements.txt`，加入 HTTP 請求所需套件（供 Google Books API 使用）
- [ ] 1.3 設定 Google Books API 金鑰讀取方式（環境變數），並加入 `.gitignore` 避免金鑰誤入版控

## 2. 共用書籍紀錄格式

- [ ] 2.1 在 `schema.py` 定義共用書籍紀錄格式：`isbn`、`title`、`authors`、`publisher`、`categories`（含 `source`/`label`）、`cover_image_url`、`sources`
- [ ] 2.2 實作 ISBN 正規化函式（10 碼轉 13 碼、格式驗證）

## 3. 國家圖書館（NCL）來源

- [ ] 3.1 實作月度 CSV 下載（依年月組出 `opendata/[年月]_isbn.csv` 網址）
- [ ] 3.2 實作 CSV 解析，轉換為共用書籍紀錄格式（分類號／建議上架分類 → `categories`）
- [ ] 3.3 處理 CSV 缺欄位/格式異常的資料列（略過並記錄，不中斷整批）

## 4. Google Books 來源

- [ ] 4.1 實作以 ISBN 查詢 Google Books API 的函式，轉換為共用書籍紀錄格式
- [ ] 4.2 實作 API 請求的重試與退避機制（因應配額限制）
- [ ] 4.3 處理查無資料/請求失敗的情況（記錄失敗項目並繼續，不中斷整批）

## 5. 跨來源合併

- [ ] 5.1 實作以 ISBN-13 為 key 的合併邏輯（`merge.py`）
- [ ] 5.2 實作欄位衝突處理規則：書目欄位（書名/作者/出版社）以 NCL 為主，封面圖片/英文分類以 Google Books 補足
- [ ] 5.3 實作封面圖片補齊：NCL 缺封面時，以相同 ISBN 查詢 Google Books 補齊

## 6. 輸出

- [ ] 6.1 實作 JSON 輸出（`output.py`），寫入 `output/[年月].json`
- [ ] 6.2 輸出結果包含依分類分組的檢視方式（各來源原生分類，不做跨來源統一）

## 7. 整合與進入點

- [ ] 7.1 實作 `main.py`：依序執行 NCL 下載解析 → Google Books 補齊 → 合併 → 輸出
- [ ] 7.2 執行結束時輸出摘要（成功筆數、失敗筆數、失敗項目清單）

## 8. 驗證

- [ ] 8.1 撰寫測試：ISBN 正規化與比對合併邏輯
- [ ] 8.2 撰寫測試：NCL CSV 解析邏輯（含缺欄位情況）
- [ ] 8.3 以實際一份月度 CSV 執行整個流程，人工檢查輸出 JSON 是否符合 schema 與分類呈現預期
