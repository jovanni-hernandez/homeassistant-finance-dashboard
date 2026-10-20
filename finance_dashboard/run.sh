#!/bin/sh
# Navigate to backend and start the Uvicorn ASGI server
cd /app/backend
exec uvicorn main:app --host 0.0.0.0 --port 8000
