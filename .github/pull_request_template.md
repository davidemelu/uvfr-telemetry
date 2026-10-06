## Summary
<!-- What does this change and why? -->

## Changes
-

## Testing
- [ ] `make test` (unit tests) passes
- [ ] Integration test run on a vcan-capable host (`make test-integration`), if the change touches CAN, protocol, transport, pit receiver or InfluxDB
- [ ] Manual check (describe):

## Safety checklist
- [ ] The car node still never transmits on CAN (listen-only / receive-only)
- [ ] Any new CAN IDs are clearly labelled SIMULATED unless they come from the real UVFR DBC
- [ ] No secrets, `.env`, tokens or real test-day recordings in the diff
