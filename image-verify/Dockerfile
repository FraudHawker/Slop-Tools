FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    exiftool \
    libmagic1 \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# C2PA support is required in the published Docker image.
RUN pip install --no-cache-dir c2pa-python

COPY app/ ./app/

ENV FLASK_APP=app.main
ENV PYTHONUNBUFFERED=1

EXPOSE 8085

CMD ["python", "-m", "flask", "run", "--host=0.0.0.0", "--port=8085"]
