FROM jrottenberg/ffmpeg:6.1-alpine
USER root
RUN apk add --no-cache python3
COPY scheduler.py splice.py monitor.py /
COPY monitor-static /monitor-static
ENTRYPOINT ["python3", "/scheduler.py"]
