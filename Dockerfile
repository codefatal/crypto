FROM python:3.12-slim

# 시스템 의존성
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 의존성 레이어 (캐시 최적화 — 소스 코드보다 먼저 복사)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 소스 복사
COPY config/ config/
COPY src/ src/
COPY main.py .

# 비루트 사용자 실행
RUN useradd -m -u 1001 appuser
USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

CMD ["python", "main.py"]
