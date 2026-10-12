#!/bin/sh
set -eu

: "${PORT:=8000}"
: "${MONGO_URI:=mongodb://127.0.0.1:27017}"
: "${MONGO_DB:=attendance_db}"

export PORT MONGO_URI MONGO_DB

echo "Starting HRONE API..."
echo "Database: $MONGO_DB"
echo "Port: $PORT"

# Start MongoDB only when using a local MongoDB URI
case "$MONGO_URI" in
    mongodb://localhost*|mongodb://127.0.0.1*)
        echo "Starting local MongoDB..."

        mkdir -p /data/db

        mongod \
            --dbpath /data/db \
            --bind_ip 127.0.0.1 \
            --port 27017 \
            --fork \
            --logpath /tmp/mongodb.log

        echo "Waiting for MongoDB..."

        until mongosh \
            --host 127.0.0.1 \
            --port 27017 \
            --quiet \
            --eval 'db.adminCommand("ping").ok' \
            >/dev/null 2>&1
        do
            sleep 1
        done

        echo "MongoDB is ready."
        ;;
    *)
        echo "Using the configured external MongoDB."
        ;;
esac

# Always seed sample data
echo "Seeding sample data..."
python /app/sample_seed.py

echo "Starting FastAPI..."

exec python -m uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "$PORT"