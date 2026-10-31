FROM python:3.13-slim@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962 AS base
WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: --no-deps -r requirements.txt
COPY --chown=1000:1000 src/*.py ./
USER 1000:1000
EXPOSE 8080
CMD ["python", "-u", "inbox.py"]

FROM base AS importer
USER root
COPY requirements-import.txt ./requirements-import.txt
RUN pip install --no-cache-dir --require-hashes --no-deps -r requirements-import.txt
USER 1000:1000
ENTRYPOINT ["python", "-u", "import_images.py"]

FROM base AS server
