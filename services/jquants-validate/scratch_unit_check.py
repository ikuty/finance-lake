import duckdb
con = duckdb.connect()

targets = [
    ("S100YK3W", "2540", "養命酒"), ("S100W81F", "4304", "Eストアー"),
    ("S100W7KU", "4435", "カオナビ"), ("S100W6N2", "9070", "トナミHD"),
]

for doc_id, jpx, name in targets:
    print(f"=== {jpx} ({name}) doc={doc_id} ===")
    facts = con.execute(
        """
        select item_name, element_id, context_id, unit_id, unit, value_num, value_text
        from read_parquet('/data/cleansed/edinet_facts.parquet')
        where doc_id = ?
          and item_name in (
            '１株当たり当期純利益又は当期純損失（△）、経営指標等',
            '１株当たり純資産額、経営指標等'
          )
        order by item_name, context_id
        """,
        [doc_id],
    ).fetchall()
    for f in facts:
        print(" ", f)
    print()
