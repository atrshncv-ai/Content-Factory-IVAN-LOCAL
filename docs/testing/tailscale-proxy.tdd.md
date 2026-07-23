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
