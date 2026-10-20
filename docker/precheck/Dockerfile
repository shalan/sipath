# ChipFoundry's tapeout precheck (cf-precheck) with the tools it calls: KLayout and
# Magic from the SiPath Magic image, Netgen (LVS) and CVC (cvc_rv: the LVS and OEB
# power checks), every version pinned. Used by SiPath's precheck step.
#   docker build --platform linux/amd64 -t sipath/precheck:1.3.7 docker/precheck
# the SiPath Magic image, built locally (docker/magic): its tag, whose image ID the lock pins
# (sipath/toolchain.json images.magic: sha256:fb8376f6...); a local image cannot be named by digest here
ARG BASE=sipath/orfs-magic:8.3.685
FROM ${BASE}
ARG NETGEN_TAG=1.5.325
ARG CVC_TAG=v1.1.7
ARG CF_PRECHECK=1.3.7
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      autoconf automake libtool bison flex g++ python3-pip git ca-certificates gawk \
 && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch ${NETGEN_TAG} https://github.com/RTimothyEdwards/netgen.git /tmp/netgen \
 && cd /tmp/netgen && ./configure --prefix=/usr/local && make && make install \
 && rm -rf /tmp/netgen && netgen -batch quit
RUN git clone --depth 1 --branch ${CVC_TAG} https://github.com/d-m-bailey/cvc.git /tmp/cvc \
 && cd /tmp/cvc && autoreconf -i && ./configure --prefix=/usr/local --disable-nls && make && make install \
 && rm -rf /tmp/cvc && command -v cvc_rv
RUN python3 -m pip install --no-cache-dir "cf-precheck==${CF_PRECHECK}" && cf-precheck --version
