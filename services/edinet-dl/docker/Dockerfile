FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY scripts/fetch_documents.py scripts/backfill_report.py .
COPY seed/edinet_etf_fund_issuers.csv seed/edinet_etf_fund_issuers.csv

ENTRYPOINT ["python3", "fetch_documents.py"]
