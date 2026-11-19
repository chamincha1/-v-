# MARKET PULSE: unchanged Python app served by SnapDeploy's free container.
# Python 3.12 wheels exist for pandas/numpy/scikit-learn.
FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    MALLOC_ARENA_MAX=2 \
    MARKET_PULSE_CLOUD=1 \
    MARKET_PULSE_AUTO_OPEN=0 \
    PORT=8765
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd --system --create-home --uid 10001 marketpulse \
    && mkdir -p /app/data_cache \
    && chown -R marketpulse:marketpulse /app/data_cache
USER marketpulse
EXPOSE 8765
CMD ["python", "app_server.py"]
