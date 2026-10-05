# 2026 年 9 月銷售儀表板

CSV → MySQL → Python → 靜態網頁（GitHub Pages）。GitHub Pages 不會查詢 MySQL，
所以每次資料庫更新後，要重新執行 Python 產生 `docs/` 再推送。

## 專案結構

```
pipeline.py                 匯入、更新、驗收、產生網頁（唯一需要執行的程式）
sql/schema.sql              資料表與檢視表
templates/index.template.html   RWD 網頁範本
expected.json               驗收數值
data/                       sales_original_300.csv、sales_updated_300.csv
docs/                       產出的網站（index.html、data.json、vendor/chart.umd.js）
```

## 安裝

```bash
pip install -r requirements.txt
cp .env.example .env        # 填入 MySQL 帳號密碼
```

需要 MySQL 8.0+（或 MariaDB 10.5+）。第一次執行會自動建立資料庫（utf8mb4）與資料表。

## 一次跑完整流程

```bash
python pipeline.py demo
```

依序：重建資料表 → 匯入 original → 驗收 → 以 sale_id 更新為 updated → 驗收 → 產生網頁。
任何一項驗收不符，程式會以非 0 結束。

## 分步執行

```bash
python pipeline.py init --reset
python pipeline.py load data/sales_original_300.csv   # 300 筆
python pipeline.py load data/sales_updated_300.csv    # 更新後仍為 300 筆，異動 240、不變 60
python pipeline.py verify
python pipeline.py build
```

`load` 的做法：CSV 先進 `sales_stage` 暫存表，再與 `sales` 依 `sale_id` 比對，
差異寫入 `sales_change_log`，最後用 `INSERT ... ON DUPLICATE KEY UPDATE` 更新，
快照中不存在的 `sale_id` 會刪除。所以兩份檔案不會串接成 600 筆。

## 之後資料庫有更新

```bash
python pipeline.py build
git add docs && git commit -m "更新銷售資料" && git push
```

已開啟的頁面每 30 秒檢查一次 `data.json`，有新版就自動重畫並顯示通知。

## 發佈到 GitHub Pages

Repository → Settings → Pages → Deploy from a branch → `main` / `/docs`。
`.env` 已列在 `.gitignore`，不會被上傳。注意 `docs/data.json` 含全部 300 筆資料，會是公開的。

## 資料庫重點

- `sales.net_revenue` 是資料庫自動計算欄位：`unit_price × (quantity − returned_quantity)`
- `CHECK` 限制：數值不得為負、退貨不得大於售出
- `snapshot_log` 記錄每次匯入的總計與新增／異動／不變／刪除筆數
- `snapshot_daily` 保存每個快照的每日彙總，供圖表畫出前一版的對照虛線
- `v_daily_sales`、`v_product_sales`、`v_category_sales`、`v_channel_sales`、`v_channel_category_sales` 提供圖表資料

## 常見問題

- 連線失敗：檢查 `.env` 與 MySQL 是否在執行。MySQL 8 的 `caching_sha2_password` 需要 `cryptography` 套件（已列在 requirements.txt）。
- 圖表空白：確認 `docs/vendor/chart.umd.js` 有一起推送。
- 直接雙擊 `docs/index.html` 可以看到畫面，但自動更新只在 http(s) 網址下運作。
