FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd --create-home --uid 10001 pulse && mkdir -p /app/data && chown pulse:pulse /app/data
USER pulse
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python","-m","pulse.launch","--host","0.0.0.0"]
