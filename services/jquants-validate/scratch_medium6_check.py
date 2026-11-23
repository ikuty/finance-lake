import duckdb
con = duckdb.connect()

targets = [
    ("4056", "2025-12-31", -7.29, -6.76), ("7370", "2026-05-31", 33.96, 35.79),
    ("3823", "2026-04-30", -7.11, -6.90), ("9220", "2026-03-31", 187.51, 182.31),
    ("7462", "2026-03-31", 36.00, 36.73), ("9259", "2025-09-30", 71.65, 72.46),
]

for jpx, pe, dwh_v, jq_v in targets:
    row = con.execute(
        "select doc_id, edinet_code, filer_name from read_parquet('/data/mart/jpx_edinet_disclosed_fundamentals.parquet') where jpx_code = ? and period_end = ?",
        [jpx, pe],
    ).fetchall()
    doc_id, edinet_code, name = row[0]
    print(f"=== {jpx} ({name}) doc={doc_id} dwh={dwh_v} jq={jq_v} ===")

    facts = con.execute(
        """
        select item_name, context_id, value_num
        from read_parquet('/data/cleansed/edinet_facts.parquet')
        where doc_id = ?
          and (item_name like '%当期純利益%' or item_name = '株式数（株）、発行済株式、議決権の状況'
               or item_name like '%非支配株主持分%' or item_name like '%１株当たり%利益%')
          and context_id like 'CurrentYear%'
          and value_num is not null
        order by item_name, context_id
        """,
        [doc_id],
    ).fetchall()
    for f in facts:
        print(" ", f)

    # implied shares from dwh eps
    print(f"  (DWH EPS implies shares if net_income parent-attrib known: check manually)")
    print()
