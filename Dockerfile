FROM public.ecr.aws/lambda/python:3.12 AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /build

RUN dnf install -y gcc python3-devel libffi-devel openssl-devel \
    && dnf clean all

COPY pyproject.toml uv.lock ./

RUN uv export --format requirements-txt --no-dev --frozen --output-file requirements.txt \
    && uv pip install --system --target /build/python -r requirements.txt


FROM public.ecr.aws/lambda/python:3.12

WORKDIR ${LAMBDA_TASK_ROOT}

COPY --from=builder /build/python/ ${LAMBDA_TASK_ROOT}/

COPY app/ ./app/

CMD ["app.main.handler"]