FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY memebot.py marketing.py setup.py marketing.example.json ./

VOLUME ["/app/data"]

CMD ["python", "memebot.py"]
