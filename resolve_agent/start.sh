#!/bin/sh
# Seed the database on first boot (if the DB file doesn't exist yet)
if [ ! -f "$DB_PATH" ]; then
    echo "First boot: seeding sample data..."
    python seed.py
fi
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
