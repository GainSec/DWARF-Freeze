# syntax=docker/dockerfile:1
# Builds a fully static x86-64 Amaru node with the Freeze agent compiled in, for use inside
# DWARF's devnet containers (which carry no system libraries). The compiler runs on the build
# machine's own architecture and cross-compiles, so this is not slowed by emulation.
# The build context is prepared by `freeze-debugger build`: `src/` is the instrumented tree.
# Network access during the build is intentional (toolchain and crates); it is not hermetic.
FROM --platform=$BUILDPLATFORM rust:bookworm AS toolchain
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3-venv clang libclang-dev libssl-dev pkg-config cmake ca-certificates \
 && rm -rf /var/lib/apt/lists/*
ARG TOOLCHAIN
RUN rustup toolchain install "${TOOLCHAIN}" --profile minimal --target x86_64-unknown-linux-musl \
 && rustup default "${TOOLCHAIN}" \
 && cargo install cargo-zigbuild --locked --version 0.23.4 \
 && python3 -m venv /opt/zig \
 && /opt/zig/bin/pip install --no-cache-dir ziglang==0.15.2 \
 && printf '#!/bin/sh\nexec /opt/zig/bin/python -m ziglang "$@"\n' > /usr/local/bin/python-zig \
 && chmod 755 /usr/local/bin/python-zig
ENV RUSTUP_TOOLCHAIN=${TOOLCHAIN} CARGO_ZIGBUILD_ZIG_COMMAND=/usr/local/bin/python-zig

FROM toolchain AS build
WORKDIR /src
COPY src/ /src/
# The node's build script reads version information from git, so the tree must be a repository.
RUN git init -q . \
 && git -c user.name=dwarf-freeze -c user.email=freeze@localhost add -A \
 && git -c user.name=dwarf-freeze -c user.email=freeze@localhost commit -q -m "freeze build"
RUN --mount=type=cache,target=/usr/local/cargo/registry,id=dwarf-freeze-cargo-registry \
    --mount=type=cache,target=/src/target,id=dwarf-freeze-amaru-target-static \
    cargo zigbuild --locked -p amaru --bin amaru --target x86_64-unknown-linux-musl \
 && cp target/x86_64-unknown-linux-musl/debug/amaru /amaru
RUN cp -r "$(rustc --print sysroot)/lib/rustlib/etc" /rust-lldb

# Built only for `freeze-debugger build --debug-info` (`--target debugger`): lldb, the source
# tree the node was built from, and the script that drives lldb for `freeze-debugger source`.
FROM debian:bookworm-slim AS debugger
# lldb 19: old enough to be in Debian, new enough for the toolchain's Rust formatters. The last
# line proves its Python module loads from where lldb says it is.
ARG LLDB_VERSION=19
RUN apt-get update \
 && apt-get install -y --no-install-recommends lldb-${LLDB_VERSION} python3-lldb-${LLDB_VERSION} python3 \
 && rm -rf /var/lib/apt/lists/* \
# Debian puts the module's native half in a neighbouring folder; put a link where the module looks.
 && ln -sf "$(ls /usr/lib/*/liblldb-${LLDB_VERSION}.so.1 | head -1)" "$(lldb-${LLDB_VERSION} -P)/lldb/_lldb.so" \
 && python3 -c "import sys, subprocess; sys.path.insert(0, subprocess.run(['lldb-${LLDB_VERSION}', '-P'], capture_output=True, text=True, check=True).stdout.strip()); import lldb; lldb.SBDebugger.Create()"
# How the toolchain that built the node shows Rust values.
COPY --from=build /rust-lldb/ /opt/rust-lldb/
COPY --from=build /amaru /usr/local/bin/amaru
COPY src/ /src/
COPY lldb_driver.py /opt/freeze/lldb_driver.py
ENTRYPOINT ["python3", "/opt/freeze/lldb_driver.py"]

# The image itself is built for linux/amd64 (`--platform` on the build command).
FROM debian:bookworm-slim
COPY --from=build /amaru /usr/local/bin/amaru
ARG CAPABILITIES=""
ENV DWARF_FREEZE_CAPABILITIES=${CAPABILITIES}
WORKDIR /data
ENTRYPOINT ["/usr/local/bin/amaru"]
