FROM python:3.11-slim
RUN pip install --no-cache-dir numpy
CMD ["python", "-c", "import numpy; print('numpy', numpy.__version__)"]
