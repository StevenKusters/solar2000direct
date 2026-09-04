# The base image is named here rather than in build.yaml, which the Supervisor reports as
# deprecated. It also cannot be taken from BUILD_FROM: the Supervisor passes its own
# Alpine base in that build-arg, and that image has no Python at all.
#
# Not one of the Home Assistant base images, because huawei-solar requires Python 3.12 or
# newer and the Alpine-based bases track whatever Python their Alpine release ships. This
# tag is a multi-architecture manifest, so the same line builds on aarch64 and amd64.
FROM python:3.12-slim-bookworm

WORKDIR /app

# The licence travels with the image. Distributing an AGPL program means the recipient
# gets its terms, and the image is what most people receive.
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/

RUN pip install --no-cache-dir .

# History and saved profiles live here. As an add-on this is persistent and included in
# Home Assistant backups; as a plain container, mount a volume over it.
VOLUME ["/data"]
EXPOSE 8480

ENTRYPOINT ["solar2000direct"]
