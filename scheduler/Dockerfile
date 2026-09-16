FROM jrottenberg/ffmpeg:6.1-alpine
USER root
RUN apk add --no-cache python3
COPY scheduler.py splice.py admin.py /
ENTRYPOINT ["python3", "/scheduler.py"]
