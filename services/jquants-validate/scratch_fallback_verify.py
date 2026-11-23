import duckdb
con = duckdb.connect()

targets = [("2540", "2026-03-31", -163.88), ("4304", "2025-03-31", 89.91),
           ("4435", "2025-03-31", 25.37), ("9070", "2025-03-31", 385.07)]

for jpx, pe, jq_eps in targets:
    row = con.execute(
        "select eps, bps from read_parquet('/data/mart/jpx_edinet_disclosed_fundamentals.parquet') where jpx_code = ? and period_end = ?",
        [jpx, pe],
    ).fetchall()
    print(jpx, pe, "DWH eps/bps=", row, " J-Quants eps=", jq_eps)
