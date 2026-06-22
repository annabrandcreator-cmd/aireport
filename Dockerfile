FROM python:3.11-slim

# системные библиотеки для weasyprint (рендер PDF)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangocairo-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 libffi-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# по умолчанию заказы и PDF во временной папке (диск не обязателен).
# для постоянного хранения подключи том на /data и задай DB_PATH=/data/orders.db, REPORTS_DIR=/data/reports
ENV PORT=8000 DB_PATH=/tmp/orders.db REPORTS_DIR=/tmp/reports
CMD ["sh", "-c", "gunicorn -w 2 -b 0.0.0.0:${PORT} app:app --timeout 180"]
