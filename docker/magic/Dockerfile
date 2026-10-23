# The ORFS image plus Magic (VLSI layout tool), for frame layouts published in
# Magic format, seal rings, and Magic DRC / GDS XOR (P1.5).
#   docker build --platform linux/amd64 -t sipath/orfs-magic:8.3.685 docker/magic
# on the ORFS image locked in sipath/toolchain.json (images.orfs): the same tools as every run, never "latest"
ARG BASE=openroad/orfs@sha256:751a77afcade9882b51427e6d9d079b8e270e7a8f4aa66df2d0659457d1c29fd
FROM ${BASE}
ARG MAGIC_TAG=8.3.685
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      tcsh tcl-dev tk-dev libx11-dev libcairo2-dev libglu1-mesa-dev libncurses-dev git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# serial make: magic's recursive Makefile breaks the jobserver under -j
RUN git clone --depth 1 --branch ${MAGIC_TAG} https://github.com/RTimothyEdwards/magic.git /tmp/magic \
 && cd /tmp/magic && ./configure --prefix=/usr/local \
 && make && make install \
 && rm -rf /tmp/magic \
 && magic -dnull -noconsole --version
