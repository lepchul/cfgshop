FROM python:3.12-slim
WORKDIR /app
COPY bot.py .
RUN useradd -u 10001 -m shop && mkdir /app/data && chown shop:shop /app/data
USER shop
CMD ["python", "-u", "bot.py"]
