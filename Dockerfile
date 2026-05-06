FROM python:3.12

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Run the webservice on port $PORT
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT:=8080}
