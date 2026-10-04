# Changelog

All notable changes to this project are documented in this file.

The format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Releases before
2.0 are described in their [GitHub releases](https://github.com/uwo-fast/openreactor/releases).

## [Unreleased]

2.0 is a rewrite and is not compatible with 1.x: new configuration, new
database, and new device protocols. 1.x is maintained on the `1.x` branch.

### Added

- Python packaging, linting, type-checking, tests and CI.
- A TOML configuration file that lists every device, validated before
  anything starts: `openreactor check-config <file>`. A commented example is
  in `examples/openreactor.toml`.
- Atlas EZO pH, ORP, RTD, EC, DO and HUM circuits over ezo-driver: each
  circuit's type is checked against the config at startup, reads are
  split-phase so no circuit waits on another, and pH, EC and DO compensate
  with the last RTD temperature. `openreactor read` prints the readings, and
  `openreactor ezo cal` shows, sets and clears calibration.
- A controller that owns the bus: a 100 ms tick that never waits on a
  device, stop-all on the next tick that reaches every actuator whatever
  else fails, EZO commands that run between reads instead of during them,
  and a machine-wide lock so only one controller runs. `openreactor read`
  reads once by default; `read --follow` streams.
- Recorded runs in SQLite: `openreactor run --name X` records readings and
  events until stopped, `openreactor runs` lists them, and `openreactor
  export N` writes a zip of `readings.csv`, `events.csv` and `run.json`. A
  failing write stops recording and marks the run interrupted without
  stopping control.
- An HTTP API, `openreactor serve`: status, channels, setpoints, runs,
  export, stop-all and EZO calibration under `/api/v1`. It binds to a
  loopback address unless `server.password_hash` is set
  (`openreactor hash-password`); browsers sign in for a session cookie and
  scripts send `Authorization: Bearer`.
- A browser UI from `openreactor serve`: a dashboard with live values and
  charts, controls, runs, EZO calibration and an about page, with Stop all on
  every page and a sign-in page when a password is set. It works offline:
  Bootstrap, htmx and Chart.js are vendored, recorded with their hashes in
  `static/vendor/vendor.toml` and updated with `just vendor`.
- RLHT heater slices over CRUMBS, in `run` and `serve`: start-up checks
  type, CRUMBS version and module version, sends the safe state, arms and
  confirms the command watchdog when the slice has one (otherwise read-only
  unless `allow_unprotected`), and sets closed-loop mode, thermocouples and any
  configured periods and gains (new optional `kp`, `ki`, `kd` and `period_ms`
  per channel). GET_STATE every `slice_poll_s` keeps the watchdog fed and
  reports each channel's temperature, setpoint and duty. Stop-all sends both
  stop ops to every slice.
- RLHT supervision: GET_WATCHDOG after every fifth poll detects a trip
  (tripped, or a changed trip count) or a reboot (disarmed) and re-asserts
  the desired state, checked at the next poll; 3 failed watchdog reads in a
  row are logged; a slice e-stop or stop-all
  zeroes the setpoints and nothing resumes; a failed read is retried twice per poll, and 3 failed polls
  mark the slice unreachable. Each is an event in the run, and `/status`
  shows a slice that is unreachable or held by its e-stop.
- RLHT setpoints from the API and the Controls page, in °C to a tenth of a
  degree, up to an optional per-channel `max_setpoint`; refused while the
  slice's e-stop is held, it is read-only or it is unreachable, and
  recorded as an event. A slice found running other setpoints than wanted,
  with no trip or reboot behind it, is sent its safe state.
- `openreactor serve` prints each event as it happens, as `run` does.
- Run profiles: a TOML schedule of `set`, `ramp`, `hold` and `off` steps per
  channel. `openreactor profile validate` checks one and `--dry-run` prints
  its timeline; an example is in `examples/profiles/`.

### Removed

- The 1.x application. It stays on the `1.x` branch and its tags.
