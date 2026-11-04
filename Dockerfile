FROM python:3.12-slim

WORKDIR /app
# ffmpeg reads embedded subtitle tracks for the Cast receiver (/cast/subtitles).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
COPY app ./app
COPY static ./static

EXPOSE 8085
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8085"]
