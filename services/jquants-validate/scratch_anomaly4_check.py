import duckdb
con = duckdb.connect()

targets = [
    ("2540", "2026-03-31"), ("4304", "2025-03-31"),
    ("4435", "2025-03-31"), ("9070", "2025-03-31"),
]

for jpx, pe in targets:
    row = con.execute(
        "select doc_id, edinet_code, filer_name from read_parquet('/data/mart/jpx_edinet_disclosed_fundamentals.parquet') where jpx_code = ? and period_end = ?",
        [jpx, pe],
    ).fetchall()
    if not row:
        print(jpx, pe, "NOT FOUND")
        continue
    doc_id, edinet_code, name = row[0]
    print(f"=== {jpx} ({name}) doc={doc_id} pe={pe} ===")

    facts = con.execute(
        """
        select item_name, element_id, context_id, unit_id, value_num, value_text
        from read_parquet('/data/cleansed/edinet_facts.parquet')
        where doc_id = ?
          and (item_name like '%１株当たり%利益%' or item_name like '%当期純利益%'
               or item_name like '%発行済株式%' or item_name like '%純資産額%')
          and context_id like 'Current%'
        order by item_name, context_id
        """,
        [doc_id],
    ).fetchall()
    for f in facts:
        print(" ", f)
    print()
