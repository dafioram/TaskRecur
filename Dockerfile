FROM python:3.9-slim

# Set environment variables to improve Python performance in Docker
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app files
COPY . .

# Expose the Flask port
EXPOSE 5000

# Run the app
# This tells the container to use the PORT variable defined in your .env or docker-compose
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "main:app"]