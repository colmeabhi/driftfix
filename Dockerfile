FROM node:20-alpine AS ui-builder
WORKDIR /app/ui/customer-portal

COPY ui/customer-portal/package*.json ./
RUN npm ci

COPY ui/customer-portal/ ./
RUN npm run build

FROM python:3.11-slim AS runtime
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    AWS_REGION=us-east-1 \
    AWS_DEFAULT_REGION=us-east-1 \
    TENANTS_TABLE=drift-detector-tenants \
    PROJECT=drift-detector

COPY admin_ui/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY admin_ui/ /app/admin_ui/
COPY --from=ui-builder /app/ui/customer-portal/dist /app/ui/customer-portal/dist

EXPOSE 8000
CMD ["uvicorn", "admin_ui.app:app", "--host", "0.0.0.0", "--port", "8000"]
