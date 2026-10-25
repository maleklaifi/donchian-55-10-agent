FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data
COPY app.py engine.py monthly_top5.py dashboard.html ./
RUN mkdir -p /data
EXPOSE 8080
CMD ["python", "app.py"]
