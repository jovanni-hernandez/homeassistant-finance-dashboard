FROM python:3.11-alpine

WORKDIR /app

# Install backend dependencies
COPY backend/requirements.txt .
RUN apk add --no-cache libjpeg-turbo freetype \
    && apk add --no-cache --virtual .build-deps build-base jpeg-dev freetype-dev zlib-dev \
    && pip install --no-cache-dir -r requirements.txt \
    && apk del .build-deps

# Copy application files
COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY run.sh .

RUN chmod a+x run.sh

# Home Assistant Add-on entrypoint
CMD [ "./run.sh" ]
