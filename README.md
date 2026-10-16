# UX evidence for #7638 — per-window bottom-terminal open state

Rendered from the real built SPA (website/dist) over loopback with /api mocked, gateway-free.

- `1-windowA-opened.png` — window A opens the bottom terminal (Ctrl+`); a terminal attaches.
- `2-windowB-still-closed.png` — window B, same origin, stays closed with no terminal and no takeover banner.
- `3-windowB-opened-independently.png` — window B opens its own panel without disturbing A.

Two same-origin browser windows, independent per-window `open` state. No code lives on this branch.
