FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY scripts/fetch_jquants_data.py .

ENTRYPOINT ["python3", "fetch_jquants_data.py"]
