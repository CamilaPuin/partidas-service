FROM python:3.12-slim-trixie

WORKDIR /workspace

COPY requirements.txt requirements-dev.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt -r requirements-dev.txt

COPY app ./app
COPY tests ./tests
COPY main.py ./main.py

CMD ["python", "-m", "pytest", "-q", "tests", "--ignore=tests/integration/test_h04_04_kafka_dlq.py"]
