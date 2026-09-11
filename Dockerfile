FROM python:3.11-slim

WORKDIR /app

COPY server/server.py /app/server.py
COPY server/users.sqlite3 /app/users.sqlite3

ENV RAILWAY_HOST=0.0.0.0
ENV PORT=8000

EXPOSE 8000

CMD ["python", "server.py"]
