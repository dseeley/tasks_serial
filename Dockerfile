ARG ANSIBLE_VERSION=12.0.0
FROM python:3.12-slim-bookworm

ARG ANSIBLE_VERSION
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && if [ -n "${ANSIBLE_VERSION}" ]; then \
         pip install --no-cache-dir "ansible==${ANSIBLE_VERSION}"; \
       else \
         pip install --no-cache-dir ansible; \
       fi

WORKDIR /workspace

COPY . /workspace/collection
RUN mkdir -p /workspace/collections/ansible_collections/dseeley \
    && cp -a /workspace/collection /workspace/collections/ansible_collections/dseeley/tasks_serial

COPY tests /workspace/tests
RUN chmod +x /workspace/tests/run_tests.sh

WORKDIR /workspace/tests
CMD ["/workspace/tests/run_tests.sh"]