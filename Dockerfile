FROM python:3.10-slim

ENV LANG C.UTF-8
ENV TZ Asia/Tokyo

RUN apt-get update && apt-get install -y \
    && apt-get clean \
    && apt-get install libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r /app/requirements.txt
RUN pip -V

COPY src /app