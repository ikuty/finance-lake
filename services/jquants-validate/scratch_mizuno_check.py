import duckdb
con = duckdb.connect()

doc_id = "S100VZPV"
facts = con.execute(
    """
    select item_name, element_id, context_id, value_num
    from read_parquet('/data/cleansed/edinet_facts.parquet')
    where doc_id = ?
      and (item_name like '%１株当たり%利益%' or item_name like '%当期純利益%'
           or item_name like '%発行済株式%' or item_name like '%株式数%')
      and context_id like 'CurrentYear%'
      and value_num is not null
    order by item_name, context_id
    """,
    [doc_id],
).fetchall()
for f in facts:
    print(f)
