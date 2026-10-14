FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir gtfs-realtime-bindings==3.0.0 numpy==2.5.4 pillow==12.3.0 playwright==1.63.0 pyyaml==6.0.3

CMD ["python", "server.py"]
