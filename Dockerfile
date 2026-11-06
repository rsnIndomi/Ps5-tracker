FROM python:3.12-alpine
WORKDIR /app
COPY tracker.py .
RUN mkdir /data && chown nobody /data
ENV PYTHONUNBUFFERED=1 STATE_FILE=/data/state.json
VOLUME ["/data"]
USER nobody
ENTRYPOINT ["python3", "/app/tracker.py"]
