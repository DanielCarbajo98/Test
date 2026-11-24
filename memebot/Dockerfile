FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot
COPY research ./research
COPY config.example.yaml ./
# config.yaml y .env se montan desde fuera; el estado persiste en el volumen /app/state
CMD ["python", "-m", "bot.main", "--config", "config.yaml"]
