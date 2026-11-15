# The translator agent, for LiveKit Cloud (`lk agent create`) or any Docker host.
# The web page is deployed separately (web/ folder -> Vercel).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

RUN useradd -m -u 1000 app
WORKDIR /home/app/voice-translator

COPY --chown=app requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

USER app
ENV HOME=/home/app
COPY --chown=app translator ./translator

# bake the VAD / end-of-sentence model files into the image
RUN python -m translator.agent download-files

CMD ["python", "-m", "translator.agent", "start"]
