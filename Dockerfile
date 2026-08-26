FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY memebot.py axiom_scout.py trader.py dashboard.py dashboard.html card.py ./
COPY assets ./assets

VOLUME ["/app/data"]

CMD ["python", "memebot.py"]
