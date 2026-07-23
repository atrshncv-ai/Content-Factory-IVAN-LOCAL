# Tailscale proxy — TDD plan

## User journey

Node 2 must reach ComfyUI through the Tailscale daemon even when Happ overrides
the macOS route for `100.64.0.0/10`.

## Guarantees

| # | Guarantee | Test |
|---|---|---|
| 1 | Every local TCP connection starts `tailscale nc HOST PORT` | `tests/test_tailscale_proxy.py` |
| 2 | Target and listener settings come from environment variables | `tests/test_tailscale_proxy.py` |
| 3 | Bytes are relayed in both directions until EOF | `tests/test_tailscale_proxy.py` |

## E2E validation

Start the proxy and request `/system_stats` through its local port. A valid
ComfyUI JSON response is the required real-backend evidence.

## Evidence

- RED: `.venv/bin/pytest tests/test_tailscale_proxy.py -q` failed because
  `scripts.tailscale_proxy` did not exist.
- GREEN: full suite — `100 passed in 0.75s`.
- Types: `.venv/bin/mypy core scripts` — no issues in 14 source files.
- Real backend: `GET http://127.0.0.1:18188/system_stats` returned HTTP 200 from
  ComfyUI 0.27.0 on the Windows RTX 4070 Node 1.
- Installed harness: `cli-anything-comfyui ... queue history` returned valid JSON.
