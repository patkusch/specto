# specto in a container: the pipeline, OCR and the examples, no local Python needed.
#
#   docker build -t specto .
#   docker run --rm -v "$PWD:/work" specto demo
#   docker run --rm -v "$PWD:/work" -e ANTHROPIC_API_KEY specto run walkthrough.mp4 --transcript walkthrough.vtt
FROM python:3.12-slim

# ffmpeg comes inside the imageio-ffmpeg wheel. These two libraries are what
# OCR's onnxruntime and opencv load at import time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/specto
COPY pyproject.toml README.md LICENSE ./
COPY specto ./specto
COPY examples ./examples
# An editable install keeps examples/ next to the package, so `specto demo` finds it.
RUN pip install --no-cache-dir -e ".[ocr]"

RUN useradd --create-home --uid 1000 specto \
    && mkdir -p /work \
    && chown specto:specto /work
ENV SPECTO_EXAMPLES=/opt/specto/examples \
    PYTHONUNBUFFERED=1
USER specto
WORKDIR /work

ENTRYPOINT ["specto"]
CMD ["doctor"]
