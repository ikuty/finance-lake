import duckdb
con = duckdb.connect()

targets = {"E05218": "4304", "E34712": "4435", "E04185": "9070", "E00400": "2540 (OK for comparison)"}

for edinet_code, label in targets.items():
    print(f"=== {label} ({edinet_code}) ===")
    row = con.execute(
        """
        select doc_id, eps, bps, net_income, net_assets, shares_outstanding
        from read_parquet('/data/intermediate/edinet_jgaap_financial_facts.parquet')
        where doc_id in (
            select doc_id from read_parquet('/data/cleansed/edinet_facts.parquet')
            where edinet_code = ?
            group by doc_id
        )
        order by doc_id desc
        limit 3
        """,
        [edinet_code],
    ).fetchall()
    for r in row:
        print(" ", r)

    ts = con.execute(
        """
        select f.doc_id, f.context_id, f.value_num, f.unit_id
        from read_parquet('/data/cleansed/edinet_facts.parquet') f
        where f.edinet_code = ?
          and f.item_name = '株式数（株）、発行済株式、議決権の状況'
          and f.context_id like 'CurrentYearInstant%TreasurySharesEtcMember'
        order by f.doc_id desc
        limit 5
        """,
        [edinet_code],
    ).fetchall()
    print("  treasury raw facts:", ts)
    print()
