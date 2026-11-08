# RAHBAR indexer image: the Job that builds the index, then exits. Build context: indexer/
#
# OCR needs system packages, not just pip packages: tesseract-ocr runs the OCR and
# poppler-utils (pdftoppm) rasterises the scanned PDF pages for it.
#
# The documents are baked in (docs/PIPELINE.md, "How the documents reach the Job"):
# `make stage-corpus` copies corpus/raw/ into indexer/corpus/ and eval/fixtures/ into
# indexer/fixtures/ before the build. CORPUS_DIR picks one: /corpus or /fixtures.

FROM python:3.12.10-slim-bookworm AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

FROM python:3.12.10-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 CORPUS_DIR=/corpus
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng poppler-utils \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 rahbar \
 && useradd --system --uid 10001 --gid rahbar --no-create-home rahbar
COPY --from=build /install /usr/local
WORKDIR /app
COPY pipeline/ ./pipeline/
COPY corpus/ /corpus/
COPY fixtures/ /fixtures/
USER 10001
CMD ["python", "-m", "pipeline.run"]
