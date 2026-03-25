FROM python:3.12-slim-bookworm

WORKDIR /app
COPY requirements.txt .

# Install PyTorch CPU (smaller image)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu 2>/dev/null || true
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p data logs data/cache data/models data/rl_models
EXPOSE 8080

CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:8080", "--timeout", "120", "app:app"]
