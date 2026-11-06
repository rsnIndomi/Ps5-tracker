FROM python:3.12-alpine
RUN apk add --no-cache tzdata
WORKDIR /app
COPY tracker.py .
RUN mkdir /data && chown nobody /data
ENV PYTHONUNBUFFERED=1 STATE_FILE=/data/state.json TZ=Europe/Amsterdam
VOLUME ["/data"]
USER nobody
# Python is PID 1 here and handles SIGTERM itself, so `docker stop` exits cleanly.
ENTRYPOINT ["python3", "/app/tracker.py"]
