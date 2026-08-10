FROM python:3.12-slim AS base
WORKDIR /app
# deps first (their own cached layer) so a source edit doesn't re-download PyPI
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir "click>=8.1" "requests>=2.31" "tldextract>=5.1"
COPY brokerscrub ./brokerscrub
RUN pip install --no-cache-dir --no-deps .
ENV BROKERSCRUB_HOME=/data
ENTRYPOINT ["brokerscrub"]
CMD ["run", "--interval", "900"]

FROM base AS test
RUN pip install --no-cache-dir pytest
COPY tests ./tests
ENTRYPOINT []
CMD ["pytest", "-v"]
