FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY memebot.py tradebot.py dexbot.py webui.py ./
COPY citadel ./citadel

VOLUME ["/app/data"]

# по умолчанию — мем-коин сканер; для торгового бота:
#   docker run ... citadel python tradebot.py trade
#   docker run ... citadel python dexbot.py trade
CMD ["python", "memebot.py"]
