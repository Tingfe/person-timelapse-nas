# Keep dependency layers stable even if the upstream version tag is republished.
# Update this digest deliberately when upgrading the runtime.
FROM ultralytics/ultralytics:8.4.92-arm64@sha256:3d13d14e9edf6a70bd9d4d519c2113a2dd9e67028e77ebffee37980400656014

RUN apt-get -o Acquire::Retries=3 update \
    && apt-get -o Acquire::Retries=3 install -y --no-install-recommends --fix-missing ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/person-timelapse
RUN mkdir -p /models /output
COPY app/ /opt/person-timelapse/

EXPOSE 8790
CMD ["python", "-u", "/opt/person-timelapse/web_server.py"]
