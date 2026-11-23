import duckdb
con = duckdb.connect()

doc_id = "S100YZC8"
facts = con.execute(
    """
    select item_name, element_id, context_id, value_num
    from read_parquet('/data/cleansed/edinet_facts.parquet')
    where doc_id = ?
      and (item_name like '%１株当たり%利益%' or item_name like '%当期純利益%'
           or item_name like '%発行済株式%' or item_name like '%株式数%、発行済%'
           or item_name like '%非支配株主%')
      and context_id like 'CurrentYear%'
      and value_num is not null
    order by item_name, context_id
    """,
    [doc_id],
).fetchall()
for f in facts:
    print(f)

print()
print("=== history ===")
hist = con.execute(
    "select period_end, eps, period_end_cum_adj from read_parquet('/data/mart/jpx_edinet_disclosed_fundamentals.parquet') where jpx_code = '3810' order by period_end"
).fetchall()
for h in hist:
    print(h)

print()
print("=== J-Quants raw rows ===")
jq = con.execute(
    "select code, doc_type, disc_date, cur_per_en, eps from read_parquet('/data/cleansed/jquants_fin_summary.parquet') where left(code,4) = '3810' order by disc_date"
).fetchall()
for j in jq:
    print(j)
