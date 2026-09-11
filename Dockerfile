FROM python:3.11-slim

WORKDIR /app

COPY server/server.py .
COPY server/users.sqlite3 . 2>/dev/null || true

ENV PORT=8000
ENV RAILWAY_HOST=0.0.0.0

EXPOSE 8000

CMD ["python", "server.py"]
