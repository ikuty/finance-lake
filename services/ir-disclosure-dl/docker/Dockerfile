FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY scripts/db.py scripts/http_client.py scripts/tdnet_client.py scripts/jpx_disclosure_client.py \
     scripts/status_report.py scripts/run_daily.py .
COPY seed/edinet_common_stock_issuers.csv seed/edinet_common_stock_issuers.csv
ENTRYPOINT ["python3", "run_daily.py"]
