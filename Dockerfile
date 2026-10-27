# For Railway, Fly.io, Koyeb or any container host. The platform sets PORT; the server listens on it.
FROM python:3.12-slim
WORKDIR /app
COPY . .
ENV PYTHONUNBUFFERED=1 ELUNO_POS_HOST=0.0.0.0
EXPOSE 8000
CMD ["python", "run.py", "serve"]
