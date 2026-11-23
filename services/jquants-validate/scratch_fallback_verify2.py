import duckdb
con = duckdb.connect()

targets = [("2540", "2026-03-31", -163.88, 3343.19), ("4304", "2025-03-31", 89.91, 650.14),
           ("4435", "2025-03-31", 25.37, 215.78), ("9070", "2025-03-31", 385.07, 10306.21)]

for jpx, pe, jq_eps, jq_bps in targets:
    row = con.execute(
        """
        select df.eps, df.bps, fi.diluted_eps
        from read_parquet('/data/mart/jpx_edinet_disclosed_fundamentals.parquet') df
        join read_parquet('/data/mart/edinet_financial_indicators.parquet') fi on fi.doc_id = df.doc_id
        where df.jpx_code = ? and df.period_end = ?
        """,
        [jpx, pe],
    ).fetchall()
    print(jpx, pe, "DWH=", row, " J-Quants eps/bps=", jq_eps, jq_bps)
