# TEST ONLY (scripts/smoke.sh). Same pinned python:3.13-alpine as the gate.
FROM python@sha256:9ef1a599f863231c3d91e941389bfdc4a2d6070557243d54df1a4e57015b960e
COPY --chmod=0644 brain.py /opt/brain/brain.py
USER 65534:65534
ENTRYPOINT ["python3", "-I", "-u", "/opt/brain/brain.py"]
