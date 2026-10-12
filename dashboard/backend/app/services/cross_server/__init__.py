"""Cross-server features: chat bridge, presence, leaderboard, announcements.

Each feature can be switched individually (`CrossServerSettings`). Every cross-server message
names its origin server. The chat bridge is OFF by default (loop risk); the structural loop
protection lives in `loop_guard`.
Single-worker invariant: the loop-guard state is process-local (exactly one uvicorn worker).
"""
