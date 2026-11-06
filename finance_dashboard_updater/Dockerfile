FROM python:3.11-alpine

WORKDIR /app
COPY run.sh updater.py ./
RUN chmod a+x /app/run.sh

CMD ["/app/run.sh"]
