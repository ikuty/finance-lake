import duckdb
con = duckdb.connect()

targets = ["2540", "4304", "4435", "9070"]

for jpx in targets:
    rows = con.execute(
        """
        select period_end, period_type, eps, bps
        from read_parquet('/data/mart/jpx_edinet_disclosed_fundamentals.parquet')
        where jpx_code = ?
        order by period_end
        """,
        [jpx],
    ).fetchall()
    name = con.execute(
        "select filer_name from read_parquet('/data/mart/jpx_edinet_disclosed_fundamentals.parquet') where jpx_code = ? limit 1",
        [jpx],
    ).fetchall()[0][0]
    print(f"=== {jpx} ({name}) ===")
    for r in rows:
        pe, pt, eps, bps = r
        flag = ""
        if eps is not None and abs(eps) > 1_000_000:
            flag += " <<< EPS異常"
        if bps is not None and abs(bps) > 1_000_000:
            flag += " <<< BPS異常"
        print(f"  {pe} {pt} eps={eps} bps={bps}{flag}")
    print()
