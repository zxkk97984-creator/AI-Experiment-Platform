ARG DAI_KERNEL_BASE_IMAGE=python:3.12-slim
FROM ${DAI_KERNEL_BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# 安装 ipykernel + 常用数学/数据科学库
# Keep the disposable kernel image reproducible at the Python package layer.
# A production build must still use a digest-pinned FROM.
RUN pip install --no-cache-dir \
    "ipykernel==6.29.5" \
    "numpy==2.1.3" \
    "pandas==2.2.3" \
    "scikit-learn==1.6.0" \
    "matplotlib==3.9.2"

# kernel runner（容器内连接 ipykernel 的可信脚本）
COPY kernel_runner.py /opt/dai/kernel_runner.py
RUN mkdir -p /.dai && ln -s /opt/dai/kernel_runner.py /.dai/kernel_runner.py

# 创建非 root 用户用于执行
RUN useradd --create-home --shell /bin/bash student

# 工作目录和临时目录
RUN mkdir -p /course /work /tmp && chown student:student /work /tmp

USER student
WORKDIR /work
