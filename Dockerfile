FROM ultralytics/ultralytics:8.4.92-arm64

RUN apt-get -o Acquire::Retries=3 update \
    && apt-get -o Acquire::Retries=3 install -y --no-install-recommends --fix-missing ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/person-timelapse
COPY app/ /opt/person-timelapse/
RUN mkdir -p /models /output

EXPOSE 8790
CMD ["python", "-u", "/opt/person-timelapse/web_server.py"]
