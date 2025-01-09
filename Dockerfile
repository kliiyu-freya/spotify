# Dockerfile
FROM python:3.9-slim

# Install dependencies
RUN pip install spotipy websocket-client

# Set environment variables for your application
ARG CLIENT_ID
ARG CLIENT_SECRET
ENV CLIENT_ID=${CLIENT_ID}
ENV CLIENT_SECRET=${CLIENT_SECRET}

# Copy application files
COPY . /app
WORKDIR /app

# Default command
CMD ["python", "main.py"]