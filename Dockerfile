FROM ultralytics/ultralytics:8.4.92-arm64

RUN apt-get -o Acquire::Retries=3 update \
    && apt-get -o Acquire::Retries=3 install -y --no-install-recommends --fix-missing ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app/ /app/
RUN mkdir -p /models /output

EXPOSE 8790
CMD ["python", "-u", "/app/web_server.py"]
