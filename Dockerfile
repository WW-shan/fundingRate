FROM python:3.10-slim

WORKDIR /app

# 复制 requirements 并安装 Python 依赖（ccxt 为纯 Python，无需编译工具链）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目文件
COPY . .

# 创建必要的目录
RUN mkdir -p data logs

# 设置环境变量
ENV PYTHONUNBUFFERED=1

# 暴露Web端口
EXPOSE 5000

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:5000/api/health', timeout=5)"

# 启动命令
CMD ["python", "main.py"]
