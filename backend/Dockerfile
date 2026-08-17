# Base image with Python 3.11
FROM python:3.11-slim

# ffmpeg is required by pydub (main.py's preprocess_audio() shells out to it)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (better Docker layer caching -
# this layer only rebuilds when requirements.txt changes, not on every code edit)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the actual backend code
COPY main.py .

# Render (and most hosts) inject the port to listen on via $PORT -
# defaulting to 8000 so this also works fine for local `docker run` testing.
ENV PORT=8000
EXPOSE 8000

# Shell form so $PORT gets expanded at container start time
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT}
