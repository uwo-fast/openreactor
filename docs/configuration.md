# Configuration

openreactor reads one TOML file that lists the server, the controller timing
and every device. It discovers nothing: a device that is not in the file is
not used. [examples/openreactor.toml](../examples/openreactor.toml) is the
full reference, with a comment on every setting.

The commands that use a configuration read `/etc/openreactor/openreactor.toml`
unless you pass `-c FILE`. `profile validate` reads one only when you pass
`-c`.

## Checking a file

```sh
openreactor check-config ~/openreactor.toml
```

`check-config` validates the whole file before anything starts, and lists
every problem with its place in the file, such as
`device[1].address: 0x80 is outside the device range 0x08 to 0x77`. An
unknown key is an error, so a misspelt setting does not pass silently. Every
command that uses a configuration runs the same check first.

## `[server]`

These settings apply to `openreactor serve`.

| Key | Default | Meaning |
|---|---|---|
| `host` | `"127.0.0.1"` | The address to listen on. |
| `port` | `8080` | The port to listen on, 1 to 65535. |
| `password_hash` | none | The sign-in password's hash, from `openreactor hash-password`. |

Without `password_hash`, `serve` refuses any `host` that is not a loopback
address. `serve --host` and `--port` override the file.
[web.md](web.md#serving-on-the-lab-network) describes serving on the lab
network.

## `[controller]`

| Key | Default | Meaning |
|---|---|---|
| `slice_poll_s` | `1.0` | How often each slice is polled, in seconds. |
| `ezo_period_s` | `2.0` | How often the EZO sensors are read, in seconds. |
| `watchdog_timeout_ms` | `5000` | How long a slice waits without hearing from openreactor before it stops its outputs. |

Each poll also feeds the slice's watchdog, so the poll and the watchdog are
checked together:

- `slice_poll_s` must be at least 0.2 s, because a poll takes two 0.1 s
  controller ticks.
- `watchdog_timeout_ms` must be between 3 × `slice_poll_s` and 65535 ms.
  Three polls must fit inside it.

`ezo_period_s` must be at least the slowest configured EZO read plus 0.2 s.

The EZO read times are:

| Circuit | Read time | With temperature compensation |
|---|---|---|
| pH | 0.9 s | 0.9 s |
| ORP | 1.0 s | not available |
| RTD | 0.6 s | not available |
| EC | 0.6 s | 0.9 s |
| DO | 0.6 s | 0.9 s |
| HUM | 0.3 s | not available |

## `[storage]`

| Key | Default | Meaning |
|---|---|---|
| `database` | per user | The SQLite file that holds the recorded runs. It must be an absolute path. |

Left unset, each user gets `$XDG_STATE_HOME/openreactor/openreactor.db`, or
`~/.local/state/openreactor/openreactor.db` when `XDG_STATE_HOME` is not set.
A system-wide install would set a shared path, such as
`/var/lib/openreactor/openreactor.db`.

## `[[device]]`

Each device is one `[[device]]` table. Every device has these keys:

| Key | Meaning |
|---|---|
| `name` | The device's name. |
| `kind` | `rlht`, `dcmt`, `ezo-ph`, `ezo-orp`, `ezo-rtd`, `ezo-ec`, `ezo-do` or `ezo-hum`. |
| `bus` | The Linux I2C bus, such as `/dev/i2c-1`. |
| `address` | The 7-bit I2C address, `0x08` to `0x77`. |

A bus and address pair may be used only once. Device and channel names start
with a lowercase letter, use only `a-z`, `0-9` and `_`, and are unique across
the whole file: a device and a channel cannot share a name either.

### RLHT heater slices

An RLHT slice has two outputs, each with an onboard PID on one of two
thermocouples; a gen1 board has a heater relay on output 1 only. Its
`channels` table names the outputs you use, with at least one channel:

```toml
[[device]]
name = "heater"
kind = "rlht"
bus = "/dev/i2c-1"
address = 0x0A
channels.jacket = { label = "Jacket heater", output = 1, tc = 1 }
```

| Channel key | Required | Meaning |
|---|---|---|
| `label` | no | The name shown in the UI. It defaults to the channel's name. |
| `output` | yes | The heater output, 1 or 2. Each output takes one channel. |
| `tc` | yes | The thermocouple the output's PID uses, 1 or 2. |
| `period_ms` | no | The time-proportioning period, 100 to 10000 ms. |
| `kp`, `ki`, `kd` | no | The PID gains, set together. |
| `max_setpoint` | no | The highest setpoint the API and the UI accept, in °C. |

openreactor sends `period_ms` and the gains only when the file sets them.
Otherwise the slice keeps its own values.

The gains follow the slice's limits:

- Each gain is 0 to 25.5, in steps of 0.1.
- `ki` must be above 0. With `ki = 0` the slice's PID keeps its integral, and
  a heater can stay on after its setpoint goes to 0.
- The slice takes both outputs' gains in one command, so you set gains on
  every channel of the device, or on none.
- An output with no channel gets the other output's gains. A gen1 RLHT has
  one relay, so its one channel can set gains. The copy keeps `ki` above 0
  on both outputs, for the reason above. openreactor never sets a setpoint
  on an output with no channel.

`max_setpoint` must be above 0 and at most 3276.7 °C, the highest setpoint
the slice takes. Without it, the API accepts up to 3276.7 °C.

A slice without a command watchdog stays read-only unless the device sets
`allow_unprotected = true`. If openreactor stops or the Pi loses power, such
a slice keeps its last output. [safety.md](safety.md) explains the watchdog.

### DCMT motor slices

openreactor does not drive DCMT slices yet. It accepts them in the file;
`run` skips them with a note, and `serve` shows them as not supported. A
DCMT channel has a `label` and a `motor`, 1 or 2.

### EZO sensors

An EZO device's readings are named after the device: `ph` for a pH circuit
named `ph`. A circuit with several outputs names each one after it, such as
`do.mg_l` and `do.saturation`. pH, EC and DO
circuits can compensate for temperature with the reading from an `ezo-rtd`
device, which `temp_comp` names:

```toml
[[device]]
name = "ph"
kind = "ezo-ph"
bus = "/dev/i2c-1"
address = 0x63
temp_comp = "vessel_temp"
```

The factory default addresses are in
[install.md](install.md#2-put-the-ezo-circuits-in-i2c-mode).
