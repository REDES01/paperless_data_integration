FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The emulator is asyncio-based; a single long-running process is enough for
# demo-scale traffic (3 corrections/min + 2 searches/min). Both bots share
# one Postgres connection pool and one requests.Session.
CMD ["python", "-u", "main.py"]
