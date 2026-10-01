FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY dotbot ./dotbot
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home dotbot && mkdir /data && chown dotbot:dotbot /data
USER dotbot
ENV DOTBOT_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8765
ENTRYPOINT ["dotbot"]
CMD ["run"]
