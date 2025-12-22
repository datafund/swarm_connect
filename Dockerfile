# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file and install dependencies
COPY requirements.txt .
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Version is passed as build arg and written to VERSION file
ARG VERSION=0.0.0-unknown
RUN echo "${VERSION}" > VERSION

# Command to run the application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
