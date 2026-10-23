#!/bin/sh
# Navigate to backend and start the Uvicorn ASGI server
export FINANCE_AGENT_STDIN_ENABLED=1
cd /app/backend
exec uvicorn main:app --host 0.0.0.0 --port 8000
