#!/usr/bin/env python3
"""銷售資料管線：CSV -> MySQL -> 靜態網頁（docs/）

用法：
  python pipeline.py init [--reset]                 建立資料庫與資料表
  python pipeline.py load data/xxx.csv [--label L]  匯入／以 sale_id 更新為完整快照
  python pipeline.py verify                         以驗收數值檢查目前資料庫
  python pipeline.py build                          從 MySQL 重新產生 docs/index.html 與 docs/data.json
  python pipeline.py demo                           一次跑完：重建 -> original -> 驗收 -> updated -> 驗收 -> 產生網頁
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pymysql
from pymysql.cursors import DictCursor

ROOT = Path(__file__).resolve().parent
SCHEMA_SQL = ROOT / "sql" / "schema.sql"
TEMPLATE = ROOT / "templates" / "index.template.html"
EXPECTED_JSON = ROOT / "expected.json"
DOCS_DIR = ROOT / "docs"
DEFAULT_ORIGINAL = ROOT / "data" / "sales_original_300.csv"
DEFAULT_UPDATED = ROOT / "data" / "sales_updated_300.csv"
TZ = timezone(timedelta(hours=8))  # 臺灣時間

CSV_COLUMNS = [
    "sale_id", "sale_date", "product_id", "product_name", "category",
    "channel", "unit_price", "quantity", "returned_quantity",
]

DROP_ORDER = [
    ("VIEW", "v_channel_category_sales"), ("VIEW", "v_channel_sales"),
    ("VIEW", "v_category_sales"), ("VIEW", "v_product_sales"), ("VIEW", "v_daily_sales"),
    ("TABLE", "snapshot_daily"), ("TABLE", "sales_change_log"), ("TABLE", "sales"),
    ("TABLE", "sales_stage"), ("TABLE", "products"), ("TABLE", "snapshot_log"),
]


class PipelineError(Exception):
    """可預期的錯誤（資料格式、驗收失敗等），會以友善訊息結束程式。"""


# ----------------------------------------------------------------------------
# 設定與連線
# ----------------------------------------------------------------------------
def load_env_file() -> None:
    """讀取 .env（不覆蓋已存在的環境變數）。"""
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def db_settings() -> dict:
    name = os.environ.get("MYSQL_DATABASE", "sales_dashboard")
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise PipelineError("MYSQL_DATABASE 只能包含英數字與底線")
    return {
        "host": os.environ.get("MYSQL_HOST", "127.0.0.1"),
        "port": int(os.environ.get("MYSQL_PORT", "3306")),
        "user": os.environ.get("MYSQL_USER", "root"),
        "password": os.environ.get("MYSQL_PASSWORD", ""),
        "database": name,
    }


def connect() -> pymysql.connections.Connection:
    """連線並確保資料庫存在（utf8mb4）。"""
    s = db_settings()
    try:
        boot = pymysql.connect(host=s["host"], port=s["port"], user=s["user"],
                               password=s["password"], charset="utf8mb4",
                               ssl={"ssl": {}})
    except pymysql.err.OperationalError as e:
        raise PipelineError(f"無法連線 MySQL（{s['host']}:{s['port']}）：{e}") from e
    with boot.cursor() as cur:
        cur.execute(
            f"CREATE DATABASE IF NOT EXISTS `{s['database']}` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
    boot.close()
    return pymysql.connect(
        host=s["host"], port=s["port"], user=s["user"], password=s["password"],
        database=s["database"], charset="utf8mb4", cursorclass=DictCursor, autocommit=False,
        ssl={"ssl": {}},
    )


def init_schema(conn, reset: bool = False) -> None:
    with conn.cursor() as cur:
        if reset:
            for kind, name in DROP_ORDER:
                cur.execute(f"DROP {kind} IF EXISTS `{name}`")
        text = "\n".join(
            line for line in SCHEMA_SQL.read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith("--")
        )
        for stmt in (s.strip() for s in text.split(";")):
            if stmt:
                cur.execute(stmt)
    conn.commit()


# ----------------------------------------------------------------------------
# 讀取與檢查 CSV
# ----------------------------------------------------------------------------
def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        raise PipelineError(f"找不到檔案：{path}")
    rows: list[dict] = []
    with open(path, newline="", encoding="utf-8-sig") as f:  # utf-8-sig 會自動去除 BOM
        reader = csv.DictReader(f)
        missing = set(CSV_COLUMNS) - set(reader.fieldnames or [])
        if missing:
            raise PipelineError(f"{path.name} 缺少欄位：{', '.join(sorted(missing))}")
        for line_no, r in enumerate(reader, start=2):
            try:
                row = {
                    "sale_id": int(r["sale_id"]),
                    "sale_date": date.fromisoformat(r["sale_date"].strip()),
                    "product_id": int(r["product_id"]),
                    "product_name": r["product_name"].strip(),
                    "category": r["category"].strip(),
                    "channel": r["channel"].strip(),
                    "unit_price": int(r["unit_price"]),
                    "quantity": int(r["quantity"]),
                    "returned_quantity": int(r["returned_quantity"]),
                }
            except (ValueError, TypeError) as e:
                raise PipelineError(f"{path.name} 第 {line_no} 列格式錯誤：{e}") from e
            if min(row["unit_price"], row["quantity"], row["returned_quantity"]) < 0:
                raise PipelineError(f"{path.name} 第 {line_no} 列有負數")
            if row["returned_quantity"] > row["quantity"]:
                raise PipelineError(f"{path.name} 第 {line_no} 列退貨數量大於售出數量")
            rows.append(row)

    if not rows:
        raise PipelineError(f"{path.name} 沒有資料")
    ids = [r["sale_id"] for r in rows]
    if len(ids) != len(set(ids)):
        raise PipelineError(f"{path.name} 的 sale_id 有重複")
    products: dict[int, tuple[str, str]] = {}
    for r in rows:
        info = (r["product_name"], r["category"])
        if products.setdefault(r["product_id"], info) != info:
            raise PipelineError(f"{path.name} 的商品 {r['product_id']} 名稱或分類不一致")
    return rows


# ----------------------------------------------------------------------------
# 匯入／更新快照
# ----------------------------------------------------------------------------
CHANGE_LOG_SQL = """
INSERT INTO sales_change_log
  (snapshot_id, sale_id, old_sale_date, new_sale_date, old_product_id, new_product_id,
   old_channel, new_channel, old_unit_price, new_unit_price, old_quantity, new_quantity,
   old_returned_quantity, new_returned_quantity, old_net_revenue, new_net_revenue)
SELECT %s, s.sale_id, s.sale_date, t.sale_date, s.product_id, t.product_id,
       s.channel, t.channel, s.unit_price, t.unit_price, s.quantity, t.quantity,
       s.returned_quantity, t.returned_quantity,
       s.net_revenue, t.unit_price * (t.quantity - t.returned_quantity)
FROM sales s JOIN sales_stage t ON t.sale_id = s.sale_id
WHERE NOT (s.sale_date <=> t.sale_date AND s.product_id <=> t.product_id
       AND s.channel <=> t.channel AND s.unit_price <=> t.unit_price
       AND s.quantity <=> t.quantity AND s.returned_quantity <=> t.returned_quantity)
"""

UPSERT_SALES_SQL = """
INSERT INTO sales (sale_id, sale_date, product_id, channel, unit_price, quantity, returned_quantity)
SELECT sale_id, sale_date, product_id, channel, unit_price, quantity, returned_quantity
FROM sales_stage
ON DUPLICATE KEY UPDATE
  sale_date = VALUES(sale_date), product_id = VALUES(product_id), channel = VALUES(channel),
  unit_price = VALUES(unit_price), quantity = VALUES(quantity),
  returned_quantity = VALUES(returned_quantity)
"""


def live_metrics(cur) -> dict:
    cur.execute(
        "SELECT COUNT(*) AS records, COALESCE(SUM(quantity),0) AS quantity, "
        "COALESCE(SUM(returned_quantity),0) AS returns_total, "
        "COALESCE(SUM(net_revenue),0) AS net_revenue FROM sales"
    )
    return {k: int(v) for k, v in cur.fetchone().items()}


def load_snapshot(conn, rows: list[dict], label: str, source: str) -> dict:
    """把完整快照同步到 sales：依 sale_id 新增／更新／刪除，不會把兩份資料串接。"""
    now = datetime.now(TZ).replace(tzinfo=None)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO snapshot_log (label, source_file, loaded_at) VALUES (%s, %s, %s)",
                (label, source, now),
            )
            snapshot_id = cur.lastrowid

            cur.execute("DELETE FROM sales_stage")
            cur.executemany(
                "INSERT INTO sales_stage (sale_id, sale_date, product_id, product_name, category, "
                "channel, unit_price, quantity, returned_quantity) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [(r["sale_id"], r["sale_date"], r["product_id"], r["product_name"], r["category"],
                  r["channel"], r["unit_price"], r["quantity"], r["returned_quantity"]) for r in rows],
            )

            cur.execute(
                "INSERT INTO products (product_id, product_name, category) "
                "SELECT DISTINCT product_id, product_name, category FROM sales_stage "
                "ON DUPLICATE KEY UPDATE product_name = VALUES(product_name), category = VALUES(category)"
            )

            cur.execute(
                "SELECT COUNT(*) AS n FROM sales_stage t "
                "LEFT JOIN sales s ON s.sale_id = t.sale_id WHERE s.sale_id IS NULL"
            )
            inserted = int(cur.fetchone()["n"])

            cur.execute(CHANGE_LOG_SQL, (snapshot_id,))
            changed = cur.rowcount
            unchanged = len(rows) - inserted - changed

            cur.execute(
                "DELETE s FROM sales s LEFT JOIN sales_stage t ON t.sale_id = s.sale_id "
                "WHERE t.sale_id IS NULL"
            )
            deleted = cur.rowcount

            cur.execute(UPSERT_SALES_SQL)

            m = live_metrics(cur)
            cur.execute(
                "UPDATE snapshot_log SET records=%s, total_quantity=%s, total_returns=%s, "
                "net_revenue=%s, inserted_rows=%s, changed_rows=%s, unchanged_rows=%s, "
                "deleted_rows=%s WHERE snapshot_id=%s",
                (m["records"], m["quantity"], m["returns_total"], m["net_revenue"],
                 inserted, changed, unchanged, deleted, snapshot_id),
            )
            cur.execute(
                "INSERT INTO snapshot_daily (snapshot_id, sale_date, records, quantity, returns_total, net_revenue) "
                "SELECT %s, sale_date, records, quantity, returns_total, net_revenue FROM v_daily_sales",
                (snapshot_id,),
            )
            cur.execute("DELETE FROM sales_stage")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"snapshot_id": snapshot_id, "label": label, "inserted": inserted, "changed": changed,
            "unchanged": unchanged, "deleted": deleted, **m}


def guess_label(path: Path) -> str:
    name = path.stem.lower()
    for key in ("original", "updated"):
        if key in name:
            return key
    return path.stem


# ----------------------------------------------------------------------------
# 驗收
# ----------------------------------------------------------------------------
def load_expected() -> dict:
    if not EXPECTED_JSON.exists():
        return {}
    return json.loads(EXPECTED_JSON.read_text(encoding="utf-8"))


def build_checks(label: str, metrics: dict, snap: dict, expected: dict) -> list[dict] | None:
    exp = expected.get(label)
    if not isinstance(exp, dict):
        return None
    pairs = [
        ("資料筆數", exp["records"], metrics["records"]),
        ("售出數量（含退貨）", exp["quantity"], metrics["quantity"]),
        ("退貨數量", exp["returns"], metrics["returns_total"]),
        ("淨銷售額（NT$）", exp["netRevenue"], metrics["net_revenue"]),
    ]
    if label == "updated" and "changed" in expected:
        pairs.append(("異動筆數", expected["changed"], snap["changed_rows"]))
        pairs.append(("不變筆數", exp["records"] - expected["changed"], snap["unchanged_rows"]))
    return [{"name": n, "expected": e, "actual": a, "ok": e == a} for n, e, a in pairs]


def print_checks(label: str, checks: list[dict]) -> bool:
    print(f"驗收（{label}）")
    for c in checks:
        mark = "通過" if c["ok"] else "不符"
        print(f"  {mark}  {c['name']}：預期 {c['expected']:,}，實際 {c['actual']:,}")
    return all(c["ok"] for c in checks)


def latest_snapshot(cur) -> dict | None:
    cur.execute("SELECT * FROM snapshot_log ORDER BY snapshot_id DESC LIMIT 1")
    return cur.fetchone()


def run_verify(conn) -> bool:
    with conn.cursor() as cur:
        snap = latest_snapshot(cur)
        if not snap:
            raise PipelineError("資料庫還沒有任何快照，請先執行 load")
        metrics = live_metrics(cur)
    checks = build_checks(snap["label"], metrics, snap, load_expected())
    if checks is None:
        print(f"快照「{snap['label']}」沒有對應的驗收數值（expected.json），略過驗收。")
        return True
    return print_checks(snap["label"], checks)


# ----------------------------------------------------------------------------
# 產生靜態網頁
# ----------------------------------------------------------------------------
def clean(value):
    if isinstance(value, Decimal):
        return int(value)
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    return value


def clean_rows(rows) -> list[dict]:
    return [{k: clean(v) for k, v in r.items()} for r in rows]


def snapshot_summary(snap: dict) -> dict:
    return {
        "label": snap["label"], "source_file": snap["source_file"],
        "loaded_at": clean(snap["loaded_at"]), "records": int(snap["records"]),
        "quantity": int(snap["total_quantity"]), "returns": int(snap["total_returns"]),
        "net_revenue": int(snap["net_revenue"]), "inserted": int(snap["inserted_rows"]),
        "changed": int(snap["changed_rows"]), "unchanged": int(snap["unchanged_rows"]),
        "deleted": int(snap["deleted_rows"]),
    }


def collect_site_data(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM snapshot_log ORDER BY snapshot_id DESC LIMIT 2")
        snaps = cur.fetchall()
        if not snaps:
            raise PipelineError("資料庫還沒有任何快照，請先執行 load 或 demo")
        current = snaps[0]
        previous = snaps[1] if len(snaps) > 1 else None

        cur.execute("SELECT sale_date, records, quantity, returns_total, net_revenue "
                    "FROM v_daily_sales ORDER BY sale_date")
        daily = clean_rows(cur.fetchall())
        cur.execute("SELECT product_id, product_name, category, records, quantity, returns_total, "
                    "net_quantity, net_revenue FROM v_product_sales ORDER BY net_revenue DESC")
        products = clean_rows(cur.fetchall())
        cur.execute("SELECT category, records, net_quantity, net_revenue "
                    "FROM v_category_sales ORDER BY net_revenue DESC")
        categories = clean_rows(cur.fetchall())
        cur.execute("SELECT channel, records, net_quantity, net_revenue "
                    "FROM v_channel_sales ORDER BY net_revenue DESC")
        channels = clean_rows(cur.fetchall())
        cur.execute("SELECT channel, category, net_quantity, net_revenue FROM v_channel_category_sales")
        channel_category = clean_rows(cur.fetchall())
        cur.execute(
            "SELECT s.sale_id, s.sale_date, p.product_name, p.category, s.channel, "
            "s.quantity - s.returned_quantity AS net_quantity, s.net_revenue "
            "FROM sales s JOIN products p ON p.product_id = s.product_id ORDER BY s.sale_id"
        )
        records = clean_rows(cur.fetchall())

        prev_block = None
        if previous:
            cur.execute("SELECT sale_date, net_revenue FROM snapshot_daily "
                        "WHERE snapshot_id = %s ORDER BY sale_date", (previous["snapshot_id"],))
            prev_block = {**snapshot_summary(previous), "daily": clean_rows(cur.fetchall())}

        metrics = live_metrics(cur)

    checks = build_checks(current["label"], metrics, current, load_expected())
    return {
        "meta": {
            "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
            "snapshot": snapshot_summary(current),
            "previous": prev_block,
        },
        "daily": daily, "products": products, "categories": categories,
        "channels": channels, "channel_category": channel_category, "records": records,
        "verification": None if checks is None else {
            "label": current["label"], "ok": all(c["ok"] for c in checks), "checks": checks},
    }


def build_site(conn) -> None:
    data = collect_site_data(conn)
    DOCS_DIR.mkdir(exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    (DOCS_DIR / "data.json").write_text(payload, encoding="utf-8")

    template = TEMPLATE.read_text(encoding="utf-8")
    marker = "/*__DATA__*/null"
    if marker not in template:
        raise PipelineError("範本缺少資料插入標記 /*__DATA__*/null")
    inline = payload.replace("</", "<\\/")  # 避免資料中出現 </script>
    (DOCS_DIR / "index.html").write_text(template.replace(marker, inline), encoding="utf-8")
    (DOCS_DIR / ".nojekyll").touch()
    s = data["meta"]["snapshot"]
    print(f"已產生 docs/index.html 與 docs/data.json（快照：{s['label']}，"
          f"{s['records']} 筆，淨銷售額 NT${s['net_revenue']:,}）")


# ----------------------------------------------------------------------------
# 指令列
# ----------------------------------------------------------------------------
def cmd_load(conn, path: Path, label: str | None) -> bool:
    label = label or guess_label(path)
    rows = read_csv(path)
    r = load_snapshot(conn, rows, label, path.name)
    print(f"已匯入「{label}」：新增 {r['inserted']}、異動 {r['changed']}、"
          f"不變 {r['unchanged']}、刪除 {r['deleted']}；資料表共 {r['records']} 筆")
    return run_verify(conn)


def main() -> int:
    parser = argparse.ArgumentParser(description="銷售資料管線：CSV -> MySQL -> 靜態網頁")
    sub = parser.add_subparsers(dest="command", required=True)
    p_init = sub.add_parser("init", help="建立資料庫與資料表")
    p_init.add_argument("--reset", action="store_true", help="先刪除既有資料表")
    p_load = sub.add_parser("load", help="匯入／更新為完整快照")
    p_load.add_argument("csv", type=Path)
    p_load.add_argument("--label")
    sub.add_parser("verify", help="以 expected.json 驗收")
    sub.add_parser("build", help="從 MySQL 產生靜態網頁")
    p_demo = sub.add_parser("demo", help="完整流程示範")
    p_demo.add_argument("--original", type=Path, default=DEFAULT_ORIGINAL)
    p_demo.add_argument("--updated", type=Path, default=DEFAULT_UPDATED)
    args = parser.parse_args()

    load_env_file()
    try:
        conn = connect()
        try:
            if args.command == "init":
                init_schema(conn, reset=args.reset)
                print("資料表已就緒")
                return 0
            init_schema(conn)
            if args.command == "load":
                return 0 if cmd_load(conn, args.csv, args.label) else 1
            if args.command == "verify":
                return 0 if run_verify(conn) else 1
            if args.command == "build":
                build_site(conn)
                return 0
            if args.command == "demo":
                init_schema(conn, reset=True)
                ok = cmd_load(conn, args.original, "original")
                ok = cmd_load(conn, args.updated, "updated") and ok
                build_site(conn)
                return 0 if ok else 1
        finally:
            conn.close()
    except PipelineError as e:
        print(f"錯誤：{e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
