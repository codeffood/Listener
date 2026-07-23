FROM python:3.11-slim

WORKDIR /app

ARG HTTP_PROXY
ARG HTTPS_PROXY

RUN apt-get update && apt-get install -y \
    ffmpeg \
    libavformat-dev \
    libavcodec-dev \
    libavdevice-dev \
    libavutil-dev \
    libavfilter-dev \
    libswscale-dev \
    libswresample-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Trust proxy SSL interception for all pip calls (including spacy's internal pip)
RUN pip config set global.trusted-host "pypi.org files.pythonhosted.org download.pytorch.org"

# Install CPU-only torch first to avoid pulling full CUDA build (~6GB)
RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -r requirements.txt \
 && python -m spacy download en_core_web_sm \
 && python -c "from faster_whisper import WhisperModel; WhisperModel('base.en', device='cpu', compute_type='float32')"

COPY . .

RUN mkdir -p /app/data/uploads /app/data/cache/nas

EXPOSE 19080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "19080"]
