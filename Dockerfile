# claude-token-lens service image (deliverable 2.a -- see docs/deploy.md
# for the full deployment picture: this is the third of three hosting
# paths, behind the native Windows Scheduled Task and systemd user unit).
#
# Deliberately minimal: the package has zero third-party dependencies
# (pyproject.toml's `dependencies = []`), so this image needs nothing
# beyond a Python interpreter and the package itself -- no build tools,
# no compiler, no lockfile.
FROM python:3.12-slim

# Build-context install: copies the repository in (see .dockerignore for
# what is deliberately left out -- tests, fixtures, VCS metadata, local
# caches) and installs the package the same way `pip install .` would
# from a checkout, so the image runs exactly the code that was built,
# never a PyPI release that might lag it.
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Non-root: the service only ever reads transcripts under
# --projects-root and reads/writes its own SQLite store under
# --config-dir, both bind-mounted by the operator (see
# docker-compose.yml) -- it never needs root inside the container.
RUN useradd --create-home --shell /usr/sbin/nologin --uid 10001 token-lens

# Review finding 4 (blocking): docker-compose.yml mounts the SQLite
# store's own directory (/data/token-lens) as a *named* volume. Docker
# only ever seeds a named volume's ownership/contents from what already
# exists at that path in the image at first-create time -- if the path
# doesn't exist yet, the daemon (running as root) creates the mount
# point owned by root, and the non-root `token-lens` user below gets
# "Permission denied" the first time it tries to create service.db.
# Pre-creating and chowning both mount points here (root, before USER
# switches away) means the *image* already owns them correctly, so a
# fresh named volume (or a bind mount an operator points at an
# already-token-lens-owned host directory) is writable from the first
# container start, not just after a manual `chown` on the host.
# /data/claude is read-only at the compose level (see
# docker-compose.yml), but is still created/chowned here for the same
# "mount point exists with the right owner before USER switches"
# consistency, and so a local `docker run` smoke test that bind-mounts
# a plain host directory there (docs/deploy.md's `--network none` test)
# doesn't depend on that directory happening to be world-readable.
RUN mkdir -p /data/token-lens /data/claude && chown -R token-lens:token-lens /data/token-lens /data/claude
USER token-lens
WORKDIR /home/token-lens

# --bind 0.0.0.0 is required here so the container's own port can be
# published to the host at all (a process bound to 127.0.0.1 inside a
# container is unreachable from outside it, container-local traffic
# included) -- docs/deploy.md explains why this is still safe: compose
# publishes that port to 127.0.0.1 on the host, so exposure stays
# loopback-only from the host's own perspective. --allow-remote is
# required alongside it because serve.run() refuses a non-loopback
# --bind otherwise (see docs/api.md's "Local only" section).
ENTRYPOINT ["claude-token-lens", "serve"]
CMD ["--projects-root", "/data/claude/projects", "--config-dir", "/data/token-lens", \
     "--bind", "0.0.0.0", "--allow-remote", "--port", "8765"]

EXPOSE 8765

# No curl/wget in this image on purpose (smaller attack surface, no
# extra apt-get network-tool install) -- python's own stdlib urllib
# does the healthcheck request instead. Exits non-zero (via the
# assertion, which raises) on anything but a 200 with `"ok": true`.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import json,urllib.request as u; r=u.urlopen('http://127.0.0.1:8765/api/health', timeout=4); assert json.load(r)['ok'] is True"]
