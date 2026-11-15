# Translator agent image (LiveKit Cloud or any Docker host).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

RUN useradd -m -u 1000 app
WORKDIR /home/app/voice-translator

COPY --chown=app requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

USER app
ENV HOME=/home/app
COPY --chown=app translator ./translator

# download the VAD / turn-detector model files
RUN python -m translator.agent download-files

CMD ["python", "-m", "translator.agent", "start"]
