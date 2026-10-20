FROM python:3.11-alpine

WORKDIR /app

# Install backend dependencies
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY run.sh .

RUN chmod a+x run.sh

# Home Assistant Add-on entrypoint
CMD [ "./run.sh" ]
