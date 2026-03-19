FROM debian:bookworm AS ffmpeg-builder

ARG FFMPEG_VERSION=7.1
ARG FFMPEG_SHA256=40973d44970dbc83ef302b0609f2e74982be2d85916dd2ee7472d30678a7abe6
ARG FFMPEG_CFLAGS="-O3 -march=skylake"

RUN apt-get update && apt-get install -y \
    build-essential \
    pkg-config \
    yasm \
    nasm \
    wget \
    libssl-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

RUN wget -O ffmpeg.tar.xz "https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz" \
    && echo "${FFMPEG_SHA256}  ffmpeg.tar.xz" | sha256sum -c \
    && tar -xf ffmpeg.tar.xz \
    && cd "ffmpeg-${FFMPEG_VERSION}" \
    && ./configure \
        --prefix=/usr/local \
        --disable-debug \
        --disable-doc \
        --disable-ffplay \
        --enable-openssl \
        --extra-cflags="${FFMPEG_CFLAGS}" \
        --extra-cxxflags="${FFMPEG_CFLAGS}" \
    && make -j$(nproc) \
    && make install


FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y \
    libssl3 \
    ca-certificates \
    zlib1g \
    curl \
    inotify-tools \
    jq \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ffmpeg-builder /usr/local/bin/ffmpeg /usr/local/bin/ffmpeg

RUN groupadd -g 1000 recorder && useradd -u 1000 -g recorder -s /sbin/nologin recorder

WORKDIR /app
COPY entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh

USER 1000:1000
ENTRYPOINT ["/app/entrypoint.sh"]
