# Bench checklist

CI runs against fakes and never shows how the hardware behaves. These checks
are run by hand on a reactor before a release. Record each one with the date,
the openreactor and library versions, and the firmware commit on each slice.

| Check | Date | openreactor | Libraries | Firmware | Result |
|---|---|---|---|---|---|
| Firmware commits flashed on each slice | 2026-10-12 | 2.0.0a1 (ade88dc) | crumbs-i2c 0.15.0, bread-crumbs-contracts 0.5.0, ezo-driver 0.6.0 | Slice_RLHT 4383bd9 (`gen1_nano`); 2× Slice_DCMT 44966b3 | RLHT flashed. The DCMTs are not exercised until #25 |
| Version and caps reported as expected | 2026-10-12 | 2.0.0a1 (ade88dc) | as above | Slice_RLHT 4383bd9 | Pass: RLHT 1.0.0, CRUMBS 0.12.5, caps 0x7F |
| Watchdog armed, and it trips when the process is killed | 2026-10-12 | 2.0.0a1 (ade88dc) | as above | Slice_RLHT 4383bd9 | Pass: armed at 5000 ms. After `kill -9` at ~400 ms on per 1000 ms, the relay went off ~5 s later, by eye. The trip count went 1, then 2, over two kills, and each restart re-armed the watchdog |
| Desired state re-asserted after a slice power-cycle | 2026-10-13 | 2.0.0a1 (1018584) | as above | Slice_RLHT 4383bd9 | Pass on 1018584, twice. With a 35 °C setpoint, an RLHT reset gave `slice-reboot` `re-asserted`, the setpoint went to 0 and the relay stayed off. A status read 1 s after the event showed closed loop, thermocouple 1, a 1000 ms period and the watchdog armed at 5000 ms. A new setpoint afterwards turned the relay on again |
| E-stop pressed and released resumes nothing | 2026-10-13 | 2.0.0a1 (1018584) | as above | Slice_RLHT 4383bd9 | Pass on 1018584 for an e-stop that cuts backplane power: at a 35 °C setpoint, release gave `slice-reboot` `re-asserted` and the relay stayed off (#62). No `slice-unreachable` event was logged while it was pressed; how long it was held was not timed. The ESTOP-pin e-stop is not tested on this rig |
| Stop-all from the UI, the API and `systemctl stop` | 2026-10-12 | 2.0.0a1 (ade88dc) | as above | Slice_RLHT 4383bd9 | Pass, all three. `systemctl --user stop` of a transient `systemd-run` unit (none ships yet, #29): stop-all 91 ms later, and the relay went off at once |
| Time from stop-all to the relay off, on a heating RLHT output (#51) | 2026-10-12 | 2.0.0a1 (ade88dc) | as above | Slice_RLHT 4383bd9 | Pass: relay off at once, by eye. The API answered in 91 ms, and a status read 1 s later showed `on_ms 0`. Relay only, no heater wired |
| Each EZO family reads, with temperature compensation applied | 2026-10-12 | 2.0.0a1 (ade88dc) | as above | EZO pH 2.15, DO 2.17 | pH and DO read, exit 0. Compensation not tested: no RTD on this rig |
| Each EZO family calibrated from the CLI | | | | | |
| A run recorded and exported from the CLI and the API | 2026-10-13 | 2.0.0a1 (1018584) | as above | Slice_RLHT 4383bd9 | Pass. CLI: a 28 s run ended `stopped` on Ctrl-C with a stop-all event; `export` wrote `readings.csv` (129 rows), `events.csv` and `run.json`. API: a 45 s run ended `stopped`, and its export held 204 readings (on ade88dc, a 93 s run held 524) |
| The API from another machine on the lab network, signed in | 2026-10-12 | 2.0.0a1 (ade88dc) | as above | Slice_RLHT 4383bd9 | Pass: a wrong password was refused (401). Signed in, the UI set a setpoint and ran Stop all |
| The controller lock refuses a second command | 2026-10-13 | 2.0.0a1 (1018584) | as above | | Pass: `read` while `serve` ran exited 1, naming the holder's host, pid and command. The `sudo` half of the check was not run |
| A 30-minute ramp-and-hold profile matches its `--dry-run` timeline | | | | | Waits on #28 |

## Before you start

Prepare the Pi and install openreactor as [install.md](install.md)
describes, with a configuration that lists the rig's devices. openreactor
does not drive DCMT slices until
[#25](https://github.com/uwo-fast/openreactor/issues/25).

## How to run the controller lock check

Run `openreactor read --follow` as one user, then `sudo openreactor read` in
another terminal. The second must refuse, naming the first. After the first
exits, `sudo openreactor read` must work.

## How to run the EZO checks

With every EZO circuit in I2C mode and listed in the config. Temperature
compensation uses the `RT` command, which needs pH firmware V2.12 or later and
DO firmware V2.13 or later; record each circuit's firmware (`i`) in the row.

1. `openreactor read` prints one line per channel, and exits 0. pH, EC
   and DO show `(compensated at … °C)` when their config names an RTD in
   `temp_comp`.
2. With an EZO-HUM, enable all three outputs (`O,T,1` and `O,Dew,1`) and
   check `openreactor read` prints humidity, air temperature and dew point. No
   vendor example shows the three-value reply, so only a real circuit can
   confirm ezo-driver parses it.
3. Check what the datasheets leave open: whether an RTD in °F or K expects its
   `Cal,t` reference in that scale, and that an EC calibrated at two and three
   points reports "two point" and "three point".
4. For each family, `openreactor ezo cal <device> status`, then one
   calibration point with a reference solution, then `status` again shows the
   new calibration. Record the points used.

## How to run the run and API checks

1. Record a short run from the CLI and export it:

   ```sh
   uv run openreactor run -c ~/openreactor.toml --name bench --notes "bench check"
   # Ctrl-C after a few readings
   uv run openreactor runs -c ~/openreactor.toml
   uv run openreactor export -c ~/openreactor.toml 1 -o bench-1.zip
   ```

   The run ends `stopped`, and `readings.csv` in the zip has a row per reading
   with its unit.

2. Serve on the Pi itself and drive the API from it:

   ```sh
   uv run openreactor serve -c ~/openreactor.toml
   curl -s localhost:8080/api/v1/status
   curl -s localhost:8080/api/v1/channels
   curl -s -X POST localhost:8080/api/v1/runs \
     -H 'Content-Type: application/json' -d '{"name": "api bench"}'
   curl -s -X POST localhost:8080/api/v1/runs/current/stop
   curl -s -o api-bench.zip localhost:8080/api/v1/runs/2/export
   curl -s localhost:8080/api/v1/ezo/ph/calibration
   ```

   Then stop the server with Ctrl-C: the run, if one is recording, ends
   `stopped`, with a stop-all event.

3. For the lab network, make a password hash, put it in the config with
   `host = "0.0.0.0"`, and serve again:

   ```sh
   uv run openreactor hash-password   # paste into server.password_hash
   uv run openreactor serve -c ~/openreactor.toml
   ```

   From another machine, `curl http://<pi>:8080/api/v1/status` answers 401,
   and with `-H 'Authorization: Bearer <password>'` it answers. Without the
   hash, `serve` refuses to bind `0.0.0.0`.

## How to run the RLHT checks

With an RLHT slice in the config and `openreactor serve` going: it prints
each event as it happens, and records it in a run while one is recording.
`openreactor status` shows each slice's state, from the same server. To heat
an output, set its setpoint:

```sh
curl -s -X PUT localhost:8080/api/v1/channels/jacket/setpoint \
  -H 'Content-Type: application/json' -d '{"value": 40}'
```

1. **Watchdog armed:** the start-up event reads `watchdog armed at <timeout> ms`.
2. **Watchdog trip:** stop openreactor without stop-all (`kill -9` the
   process) while a heater runs. The relay must go off within
   `watchdog_timeout_ms`. Don't run `openreactor status` or anything else
   that reads the slice meanwhile: every reply feeds its watchdog. Start it
   again: start-up arms the watchdog again.
3. **Trip noticed while running:** pause the bus for longer than the watchdog
   timeout (unplug the slice's I2C briefly). Within five polls of it answering
   again, a `slice-trip` event with result `re-asserted`.
4. **Re-assert after a power-cycle:** power-cycle the slice while openreactor
   runs with a setpoint set. A `slice-reboot` event with result
   `re-asserted`, the channel's setpoint reading stays at 0 and the relay
   stays off, and `openreactor status` shows closed loop and the configured
   thermocouples. This is for a default build, which boots disarmed. A build
   with `RLHT_WATCHDOG_BOOT_MS` gives a `slice-trip` instead, and only if it
   had tripped before (#53).
5. **E-stop:** press the slice's e-stop. An `e-stop` event with result `held`,
   then stop-all; the relays stay off. Release it: an `e-stop` event with
   result `released`, and the setpoints stay at 0. On a rig whose e-stop
   cuts the backplane's power instead, release gives step 4's event, and
   the relay stays off.
6. **Unreachable:** disconnect the slice's I2C. After 3 failed polls, a
   `slice-unreachable` event, and `openreactor status` shows it unreachable.
   Reconnect it: a `slice-reachable` event, and the watchdog is checked at
   once, so a slice that lost power meanwhile also gives step 4's event. A
   disconnect longer than `watchdog_timeout_ms` also gives a `slice-trip`
   event with result `re-asserted`.
