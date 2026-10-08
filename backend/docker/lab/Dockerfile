# Build from repository root. CPU and GPU images are separate immutable versions.
ARG DAI_LAB_BASE_IMAGE=python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36
FROM ${DAI_LAB_BASE_IMAGE}
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN pip install --no-cache-dir torch==2.14.0 --index-url ${TORCH_INDEX_URL}
RUN pip install --no-cache-dir ipykernel==6.29.5 jupyter-client==8.9.1 \
    numpy==2.1.3 pandas==2.2.3 pillow==12.3.0 scikit-learn==1.6.0 matplotlib==3.9.2
COPY backend/docker/kernel/kernel_runner.py /opt/dai/kernel_runner.py
RUN mkdir -p /.dai && ln -s /opt/dai/kernel_runner.py /.dai/kernel_runner.py
RUN useradd --uid 1000 --create-home --shell /bin/bash student \
    && mkdir -p /course /work && chown student:student /work
USER student
WORKDIR /work
CMD ["python", "-m", "ipykernel_launcher"]
