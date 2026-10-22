FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 DATABASE_PATH=/data/opportunity_hunter.db
WORKDIR /app
COPY app.py ./app.py
COPY static ./static
RUN mkdir -p /data && useradd --system --uid 10001 appuser && chown -R appuser:appuser /data /app
USER appuser
EXPOSE 8080
CMD ["python", "app.py"]
