FROM python:3.9-slim-buster

WORKDIR /app

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "main:app"]
