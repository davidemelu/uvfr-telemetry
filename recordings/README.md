# Recordings

CAN logs recorded with `make record` / `python -m replay.record` land here in
SocketCAN candump format (`candump -l`). Everything in this folder except this
README is git-ignored: recordings can be large, and real test-day data may be
team-confidential. Share recordings through the team's storage, not git.

Replay one onto vcan0 (stop the fake ECU first, or it will mix with the replay):

```bash
make replay LOG=recordings/vcan0-20261006-120000.log SPEED=2 LOOP=1
python -m replay.ctl pause | resume | speed 5 | status
```

Real UVFR recordings will usually be from `can0` with the real DBC. Replay
remaps them onto vcan0 automatically; point `car_node.dbc` and
`config/channels.yaml` at the real DBC's message and signal names.
