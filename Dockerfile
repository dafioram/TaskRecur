FROM python:3.11-slim

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

# Buffer client requests before handing them to the single app thread.
# Flask's ProxyFix already handles forwarded headers.
CMD ["waitress-serve", "--listen=0.0.0.0:5000", "--threads=1", "--no-clear-untrusted-proxy-headers", "main:app"]