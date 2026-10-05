-- 銷售資料庫結構（MySQL 8.0+ / MariaDB 10.5+），可重複執行
-- 流程：CSV -> sales_stage（暫存）-> 依 sale_id 同步到 sales，並留下異動紀錄

-- 每次匯入留下一筆紀錄：快照總表
CREATE TABLE IF NOT EXISTS snapshot_log (
  snapshot_id    INT UNSIGNED NOT NULL AUTO_INCREMENT,
  label          VARCHAR(40)  NOT NULL,
  source_file    VARCHAR(255) NOT NULL,
  loaded_at      DATETIME     NOT NULL,
  records        INT          NOT NULL DEFAULT 0,
  total_quantity INT          NOT NULL DEFAULT 0,
  total_returns  INT          NOT NULL DEFAULT 0,
  net_revenue    BIGINT       NOT NULL DEFAULT 0,
  inserted_rows  INT          NOT NULL DEFAULT 0,
  changed_rows   INT          NOT NULL DEFAULT 0,
  unchanged_rows INT          NOT NULL DEFAULT 0,
  deleted_rows   INT          NOT NULL DEFAULT 0,
  PRIMARY KEY (snapshot_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 商品主檔
CREATE TABLE IF NOT EXISTS products (
  product_id   SMALLINT    NOT NULL,
  product_name VARCHAR(60) NOT NULL,
  category     VARCHAR(40) NOT NULL,
  PRIMARY KEY (product_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 銷售彙總（每筆 = 單一商品在當日指定通路）；淨銷售額由資料庫自動計算
CREATE TABLE IF NOT EXISTS sales (
  sale_id           INT         NOT NULL,
  sale_date         DATE        NOT NULL,
  product_id        SMALLINT    NOT NULL,
  channel           VARCHAR(20) NOT NULL,
  unit_price        INT         NOT NULL,
  quantity          INT         NOT NULL,
  returned_quantity INT         NOT NULL DEFAULT 0,
  net_revenue       INT GENERATED ALWAYS AS (unit_price * (quantity - returned_quantity)) STORED,
  PRIMARY KEY (sale_id),
  KEY idx_sales_date (sale_date),
  KEY idx_sales_product (product_id),
  KEY idx_sales_channel (channel),
  CONSTRAINT fk_sales_product FOREIGN KEY (product_id) REFERENCES products (product_id),
  CONSTRAINT chk_sales_nonneg CHECK (unit_price >= 0 AND quantity >= 0 AND returned_quantity >= 0),
  CONSTRAINT chk_sales_returns CHECK (returned_quantity <= quantity)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- CSV 暫存表：每次匯入先整份放進來，再與 sales 比對
CREATE TABLE IF NOT EXISTS sales_stage (
  sale_id           INT         NOT NULL,
  sale_date         DATE        NOT NULL,
  product_id        SMALLINT    NOT NULL,
  product_name      VARCHAR(60) NOT NULL,
  category          VARCHAR(40) NOT NULL,
  channel           VARCHAR(20) NOT NULL,
  unit_price        INT         NOT NULL,
  quantity          INT         NOT NULL,
  returned_quantity INT         NOT NULL,
  PRIMARY KEY (sale_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 異動紀錄：快照更新時，哪些 sale_id 由舊值變成新值
CREATE TABLE IF NOT EXISTS sales_change_log (
  log_id                BIGINT       NOT NULL AUTO_INCREMENT,
  snapshot_id           INT UNSIGNED NOT NULL,
  sale_id               INT          NOT NULL,
  old_sale_date         DATE,
  new_sale_date         DATE,
  old_product_id        SMALLINT,
  new_product_id        SMALLINT,
  old_channel           VARCHAR(20),
  new_channel           VARCHAR(20),
  old_unit_price        INT,
  new_unit_price        INT,
  old_quantity          INT,
  new_quantity          INT,
  old_returned_quantity INT,
  new_returned_quantity INT,
  old_net_revenue       INT,
  new_net_revenue       INT,
  PRIMARY KEY (log_id),
  KEY idx_change_snapshot (snapshot_id),
  KEY idx_change_sale (sale_id),
  CONSTRAINT fk_change_snapshot FOREIGN KEY (snapshot_id) REFERENCES snapshot_log (snapshot_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 每個快照當下的每日彙總，網頁用來畫「前一版」對照線
CREATE TABLE IF NOT EXISTS snapshot_daily (
  snapshot_id   INT UNSIGNED NOT NULL,
  sale_date     DATE         NOT NULL,
  records       INT          NOT NULL,
  quantity      INT          NOT NULL,
  returns_total INT          NOT NULL,
  net_revenue   INT          NOT NULL,
  PRIMARY KEY (snapshot_id, sale_date),
  CONSTRAINT fk_daily_snapshot FOREIGN KEY (snapshot_id) REFERENCES snapshot_log (snapshot_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 圖表用檢視表
CREATE OR REPLACE VIEW v_daily_sales AS
SELECT sale_date,
       COUNT(*)               AS records,
       SUM(quantity)          AS quantity,
       SUM(returned_quantity) AS returns_total,
       SUM(net_revenue)       AS net_revenue
FROM sales
GROUP BY sale_date;

CREATE OR REPLACE VIEW v_product_sales AS
SELECT p.product_id, p.product_name, p.category,
       COUNT(*)                              AS records,
       SUM(s.quantity)                       AS quantity,
       SUM(s.returned_quantity)              AS returns_total,
       SUM(s.quantity - s.returned_quantity) AS net_quantity,
       SUM(s.net_revenue)                    AS net_revenue
FROM sales s JOIN products p ON p.product_id = s.product_id
GROUP BY p.product_id, p.product_name, p.category;

CREATE OR REPLACE VIEW v_category_sales AS
SELECT p.category,
       COUNT(*)                              AS records,
       SUM(s.quantity - s.returned_quantity) AS net_quantity,
       SUM(s.net_revenue)                    AS net_revenue
FROM sales s JOIN products p ON p.product_id = s.product_id
GROUP BY p.category;

CREATE OR REPLACE VIEW v_channel_sales AS
SELECT channel,
       COUNT(*)                          AS records,
       SUM(quantity - returned_quantity) AS net_quantity,
       SUM(net_revenue)                  AS net_revenue
FROM sales
GROUP BY channel;

CREATE OR REPLACE VIEW v_channel_category_sales AS
SELECT s.channel, p.category,
       SUM(s.quantity - s.returned_quantity) AS net_quantity,
       SUM(s.net_revenue)                    AS net_revenue
FROM sales s JOIN products p ON p.product_id = s.product_id
GROUP BY s.channel, p.category;
