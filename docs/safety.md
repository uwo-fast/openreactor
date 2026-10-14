# Safety

openreactor drives heaters. A heater with a setpoint above 0 heats until the
setpoint goes to 0, stop-all is sent, the slice's e-stop is pressed, or the
slice's command watchdog trips. Read this page before you connect a heater
to an RLHT slice.

This page describes what openreactor does to keep RLHT heater slices safe,
and what happens to the setpoints after each kind of fault.

## Firmware

Flash [Slice_RLHT](https://github.com/feastorg/Slice_RLHT) commit `16a04dd`
or later on every RLHT slice. With it, stop-all turns a heating output off at
the slice's next control step. Earlier firmware keeps its PID integral
through stop-all, so a running heater decays to off over several seconds
([feastorg/Slice_RLHT#13](https://github.com/feastorg/Slice_RLHT/issues/13)).
openreactor cannot tell the two apart, because both report the same version.

## The slice and its watchdog

Each RLHT slice runs its own PID for its two outputs; a gen1 board has a
relay on output 1 only. openreactor does not switch the heaters itself: it
sends setpoints and reads the slice's state.

The slice has a command watchdog. When it hears nothing from openreactor for
`watchdog_timeout_ms` (5 seconds by default), it stops its outputs. So if
openreactor is killed, hangs, or the Pi loses power, the heaters still stop.

A slice without the watchdog capability stays read-only: openreactor reads
it and includes it in stop-all, but sends it no setpoints. Setting
`allow_unprotected = true` on the device lifts that. Such a slice keeps its
last output if openreactor stops or the Pi loses power.

## Start-up

`run` and `serve` start each RLHT slice in the configuration, in this order:

1. openreactor asks the slice for its version. The slice must answer as an
   RLHT, built with CRUMBS 0.12.0 or later, with a compatible RLHT module
   version.
2. It reads the slice's capabilities.
3. It sends the safe state: both setpoints to 0, then open-loop duty to 0.
   It sends the second command even if the first fails.
4. If the slice has a watchdog, openreactor arms it with
   `watchdog_timeout_ms` and reads it back to confirm. If the watchdog does
   not arm, the slice is not used.
5. It sets closed-loop mode, any periods the configuration sets, the
   thermocouple for each output, and any PID gains the configuration sets.
   It sends each one only if the slice has that capability.

A slice that answered as an RLHT is included in stop-all from step 1 on, even
if a later step fails. A slice that did not finish start-up is not polled,
and refuses setpoints.

`read` and `ezo cal` never start or touch a slice.

## Polling

openreactor polls each slice every `slice_poll_s` (1 second by default). The
poll reads each output's temperature, setpoint and duty, records them like
any other reading, and feeds the slice's watchdog.

- If a poll's read fails, openreactor retries it twice within the same poll.
  After 3 failed polls in a row, it marks the slice unreachable.
- After every fifth poll, openreactor also reads the slice's watchdog, to
  find trips and reboots.
- If a watchdog read fails, openreactor tries again after the next poll.
  After 3 failures in a row it logs a `slice-unchecked` event, and goes back
  to every fifth poll. Until a watchdog read succeeds, it cannot see trips or
  reboots.

## What happens after each event

openreactor records each event it sees in the run, and `run` and `serve`
print it as it happens.

| Event | What openreactor does | Setpoints afterwards |
|---|---|---|
| Watchdog trip: the slice stopped hearing from openreactor, for example while its I2C was disconnected. | Logs `slice-trip`, and sends the configuration again with the wanted setpoints. | Restored. |
| Reboot: the slice lost power and restarted, so its watchdog is disarmed. | Logs `slice-reboot`, and sends the configuration again with setpoints of 0. | 0 until you set them again. |
| The watchdog's trip count went down. This is how a reboot shows on a build that arms its watchdog at boot. | Treats it as a reboot, and logs `slice-trip`. | 0 until you set them again. |
| The watchdog's timeout is not `watchdog_timeout_ms`. This is how a reboot shows on a build that arms at boot with a different timeout, or someone changed it at the slice. | Treats it as a reboot, and logs `slice-reboot`. | 0 until you set them again. |
| E-stop pressed, on the slice's e-stop pin. | Logs `e-stop` `held`, and sends stop-all to every slice. While the e-stop is held, it sends the slice its safe state at each poll and refuses setpoints. | 0 on every slice until you set them again. |
| E-stop released. | Logs `e-stop` `released`. | Still 0 until you set them again. |
| E-stop that cuts the slice's power. | The slice reboots, so this is the reboot case. | 0 until you set them again. |
| Stop-all, from the Stop all button, the API, Ctrl-C or SIGTERM. | Sends every slice its safe state. | 0 on every slice until you set them again. |
| The slice runs other setpoints than wanted, with no trip or reboot behind it. | Reads the watchdog on the next tick. If it finds no trip or reboot, logs `slice-setpoints-changed` and sends the slice its safe state. A slice with no watchdog, or whose watchdog reads keep failing, gets the safe state without the check. | 0 until you set them again. |
| The slice is unreachable: 3 polls failed in a row. | Logs `slice-unreachable`, and refuses setpoints. When the slice answers again, logs `slice-reachable` and reads its watchdog at once. | Unchanged. If the slice tripped or rebooted meanwhile, the trip or reboot case applies. |
| openreactor is killed, or the Pi loses power. | Nothing. The slice's watchdog trips after `watchdog_timeout_ms` and stops its outputs. | openreactor sends the safe state at its next start-up. |

After a watchdog trip, a heater resumes on its own: the slice kept its power,
so openreactor sends the wanted setpoints again. A loose I2C cable that
trips the watchdog therefore pauses heating and does not end it. After a
reboot, an e-stop, stop-all or a drift, the setpoints stay at 0.

openreactor does not restore setpoints when it starts again. It always
starts a slice in its safe state.

### Sending the configuration again

After a trip or a reboot, openreactor sends the slice its wanted state, in
this order: closed-loop mode, the setpoints, the periods, the thermocouples,
the gains, and then the watchdog armed again. It never sends a setpoint above
0 while the slice's e-stop is held.

At the next watchdog read, openreactor checks that the state took. If it did
not, it logs the event with the result `error: the re-assert did not take`,
and tries again at the next scheduled watchdog read, or at the next poll if
a setpoint was refused meanwhile. If sending failed, it tries again after the
next poll.

### Before a setpoint is sent

The slice takes both outputs' setpoints in one command, so setting one
output sends the other's again. If the slice rebooted since the last poll,
for example because a power-cut e-stop was just released, that would restart
the other output's heater with the watchdog disarmed. So before it sends a
setpoint, openreactor reads the slice's watchdog and state. Unless the
watchdog has the configured timeout and no new trip, the e-stop is clear,
and the setpoints are the ones it last sent, it refuses with 409 and checks
the slice's watchdog at its next poll. A slice with no watchdog
(`allow_unprotected`) has no watchdog to check: if its setpoints changed,
the next poll sends it the safe state. If the slice cannot be read,
openreactor refuses with 503.

### Setpoints that changed without a trip or reboot

The drift check covers three cases: an e-stop pressed and released between
two polls, a setpoint that reached the slice although sending it failed, and
another controller on the bus. In each, nothing resumes until you set a
setpoint again. While openreactor checks the watchdog, it refuses new
setpoints for that slice with 409.

## Known limitations

- A slice built with `RLHT_WATCHDOG_BOOT_MS` arms its watchdog at boot. Its
  reboot shows as a trip only if the slice had tripped before, or as a
  reboot if its boot timeout differs from `watchdog_timeout_ms`. Otherwise,
  if a setpoint was above 0, openreactor sees only that the setpoints
  changed and sends the safe state; if the setpoints were 0, it sees
  nothing. Either way the slice keeps the mode, thermocouples, periods,
  gains and watchdog timeout it booted with, so restart `run` or `serve`
  before you set a setpoint again. Follow
  [#53](https://github.com/uwo-fast/openreactor/issues/53).
- openreactor cannot tell whether a slice runs firmware from before or after
  `16a04dd`. Follow
  [#51](https://github.com/uwo-fast/openreactor/issues/51) and
  [feastorg/bread-crumbs-contracts#23](https://github.com/feastorg/bread-crumbs-contracts/issues/23).
