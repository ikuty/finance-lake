import duckdb
con = duckdb.connect()

print("=== 全量: '株式数（株）、発行済株式、議決権の状況' のcontext_idパターン(上位30) ===")
rows = con.execute(
    """
    select context_id, count(*) as n, count(distinct doc_id) as n_docs
    from read_parquet('/data/cleansed/edinet_facts.parquet')
    where item_name = '株式数（株）、発行済株式、議決権の状況'
      and value_num is not null
    group by context_id
    order by n_docs desc
    limit 30
    """
).fetchall()
for r in rows:
    print(" ", r)

print()
print("=== 'TreasuryShares'を含むcontext_idパターン(上位30) ===")
rows2 = con.execute(
    """
    select context_id, count(*) as n, count(distinct doc_id) as n_docs
    from read_parquet('/data/cleansed/edinet_facts.parquet')
    where item_name = '株式数（株）、発行済株式、議決権の状況'
      and context_id like '%TreasuryShares%'
      and value_num is not null
    group by context_id
    order by n_docs desc
    limit 30
    """
).fetchall()
for r in rows2:
    print(" ", r)

print()
print("=== 直近の有報(doc_type=120)全件中、何%がTreasurySharesタグを持つか ===")
total = con.execute(
    """
    select count(distinct f.doc_id)
    from read_parquet('/data/cleansed/edinet_facts.parquet') f
    join read_parquet('/data/cleansed/edinet_report_periods.parquet') rp on rp.doc_id = f.doc_id
    where rp.doc_type_code = '120'
    """
).fetchall()[0][0]
with_treasury = con.execute(
    """
    select count(distinct f.doc_id)
    from read_parquet('/data/cleansed/edinet_facts.parquet') f
    join read_parquet('/data/cleansed/edinet_report_periods.parquet') rp on rp.doc_id = f.doc_id
    where rp.doc_type_code = '120'
      and f.item_name = '株式数（株）、発行済株式、議決権の状況'
      and f.context_id like '%TreasuryShares%'
      and f.value_num is not null
    """
).fetchall()[0][0]
print(f"有報総数={total}, 自己株式タグあり={with_treasury}, カバレッジ={with_treasury/total*100:.1f}%")

print()
print("=== 複数のTreasurySharesパターンを同時に持つ文書の例(優先順位の衝突確認) ===")
rows3 = con.execute(
    """
    select doc_id, count(distinct context_id) as n_patterns
    from read_parquet('/data/cleansed/edinet_facts.parquet')
    where item_name = '株式数（株）、発行済株式、議決権の状況'
      and context_id like '%TreasuryShares%'
      and value_num is not null
    group by doc_id
    having count(distinct context_id) > 1
    limit 10
    """
).fetchall()
for r in rows3:
    print(" ", r)
