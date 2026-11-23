import duckdb
con = duckdb.connect()

targets = {
    "E00400": ("2540", "S100YK3W"), "E05218": ("4304", "S100W81F"),
    "E34712": ("4435", "S100W7KU"), "E04185": ("9070", "S100W6N2"),
}

for edinet_code, (jpx, doc_id) in targets.items():
    print(f"=== {jpx} ({edinet_code}) doc={doc_id} ===")
    facts = con.execute(
        """
        select item_name, element_id, context_id, value_num
        from read_parquet('/data/cleansed/edinet_facts.parquet')
        where doc_id = ?
          and (item_name like '%非支配株主%' or item_name like '%新株予約権%')
          and context_id like 'CurrentYearInstant%'
          and value_num is not null
        order by item_name, context_id
        """,
        [doc_id],
    ).fetchall()
    for f in facts:
        print(" ", f)
    print()
