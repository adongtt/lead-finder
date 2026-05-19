FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create mount point for persistent volume
RUN mkdir -p /data

# Fly.io sets PORT env var; default to 8080
ENV PORT=8080
ENV DATA_DIR=/data
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8080"]
