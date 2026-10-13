# Browser UI and HTTP API

`openreactor serve` runs the controller and serves two things from the same
process: a browser UI at `/`, and an HTTP API at `/api/v1`. It prints each
event as it happens, as `run` does, and holds the same lock as the other
device commands ([usage.md](usage.md#one-controller-at-a-time)).

```sh
openreactor serve -c ~/openreactor.toml
```

By default it listens on `127.0.0.1:8080`, so only the Pi itself can reach
it. [Serving on the lab network](#serving-on-the-lab-network) describes how to
open it to other machines.

## The browser UI

Every page except sign-in has a Stop all button, which sends stop-all to
every slice at once.
If any slice fails to go safe, the page names it.

| Page | Path | What it shows |
|---|---|---|
| Dashboard | `/` | Live values, a chart of the values since the page opened, each device's status, and the current run. |
| Controls | `/controls` | A setpoint form for each RLHT channel. |
| Runs | `/runs` | Start and stop a run, and export the recorded runs. |
| Devices | `/devices` | Each device's status, and EZO calibration. |
| About | `/about` | The version, the licence, and the included browser libraries. |

The UI loads nothing from the internet, so it works on a lab network with no
internet access. The few browser files it uses ship inside the package
([development.md](development.md#vendored-browser-files)).

## The HTTP API

All endpoints are under `/api/v1`, and all of them sit behind the same
sign-in as the UI. The OpenAPI description is at `/api/v1/openapi.json`.

| Method | Path | What it does |
|---|---|---|
| `GET` | `/status` | The version, the current run, each device's status, and each RLHT slice in detail under `slices`. |
| `GET` | `/channels` | The latest value of every channel. |
| `PUT` | `/channels/{channel}/setpoint` | Set an RLHT channel's setpoint. The body is `{"value": 40}`. |
| `POST` | `/stop-all` | Send stop-all, and return one event per slice. |
| `GET` | `/runs` | List the recorded runs. |
| `POST` | `/runs` | Start a run. The body is `{"name": "...", "notes": "..."}`; `notes` is optional. Answers 201 with the run's `id`. |
| `POST` | `/runs/current/stop` | Stop the recording run. Answers with its `id` and `status`. |
| `GET` | `/runs/{run}/export` | Download the run as `run-N.zip`. |
| `GET` | `/ezo/{device}/calibration` | Show a circuit's calibration. |
| `POST` | `/ezo/{device}/calibration` | Calibrate one point. The body is `{"point": "mid", "value": 7.0}`. |
| `DELETE` | `/ezo/{device}/calibration` | Erase a circuit's calibration. |

For example, from the Pi:

```sh
curl -s localhost:8080/api/v1/status
curl -s -X PUT localhost:8080/api/v1/channels/jacket/setpoint \
  -H 'Content-Type: application/json' -d '{"value": 40}'
curl -s -X POST localhost:8080/api/v1/stop-all
```

The calibration points for each family are in
[usage.md](usage.md#ezo-calibration).

## Setpoints

You set an RLHT channel's setpoint from the API or the Controls page, in °C.
The slice works in tenths of a degree, so openreactor rounds the value to the
nearest 0.1 °C. It accepts values from 0 up to the channel's `max_setpoint`,
or up to 3276.7 °C when the configuration sets none.

openreactor sends the setpoint to the slice at once, and records it as an
event in the run, whether the send worked or not.

A setpoint stays the one openreactor wants until one of these happens:

- You set it again.
- Stop-all, an e-stop, or a slice reboot sets it to 0.
- The slice is found running other setpoints with no trip or reboot to
  explain it, and openreactor sets it to 0.
- openreactor stops. It does not restore setpoints when it starts again.

After a plain watchdog trip, openreactor sends the setpoint again.
[safety.md](safety.md) describes each case.

A `PUT` to `/channels/{channel}/setpoint` answers with one of these:

| Status | When |
|---|---|
| 204 | The setpoint was sent. |
| 404 | No actuated channel has that name. |
| 409 | The channel is on a DCMT slice, which openreactor does not drive yet. |
| 409 | The slice's e-stop is held. Release it, then set the setpoint again. |
| 409 | The slice is read-only, because it has no command watchdog. |
| 409 | The slice is running setpoints other than those wanted, and openreactor is checking why. Try again a moment later. |
| 422 | The value is not a JSON number, is below 0, or is above the limit. |
| 502 | Sending failed. The slice may still have applied it, so the next polls compare what it runs with what is wanted. |
| 503 | The slice is unreachable, did not finish start-up, or is not in use. |
| 504 | The controller did not start the action within 30 seconds, or started it and did not finish within 30 more. |

Any endpoint can also answer 401 when you are not signed in, 403 when the
`Origin` check fails or, on a server with no password, when the request is
not addressed to a loopback name, 413 when the request body is over 64 KiB,
and 429 when another password check from the same address is running or
another client's check did not finish in time.

The other endpoints use the same codes for the same kinds of problem: 404
for an unknown device or run, 409 for an action that does not fit the
current state (such as starting a run while one records), 422 for a bad
value, 502 when an EZO circuit refuses a command or does not answer, 503 when
the database or the controller cannot be used, and 504 for a timeout.

## Serving on the lab network

Without `server.password_hash`, `serve` listens only on a loopback address,
and it answers only requests addressed to `localhost`, `127.0.0.1` or
`[::1]`.

To serve the lab network:

1. Make a password hash, and paste it into the configuration:

   ```sh
   openreactor hash-password
   ```

   ```toml
   [server]
   host = "0.0.0.0"
   password_hash = "scrypt$32768$8$1$..."
   ```

2. Start `openreactor serve` again.
3. Open `http://<pi>:8080/` from another machine, and sign in.

Plain HTTP carries the password and the session cookie unencrypted. Use it
only on a lab network you trust, or put a TLS proxy in front of
openreactor.

The proxy must run on the same machine, keep the original `Host` header, and
send `X-Forwarded-Proto`. Otherwise browser sign-in fails its `Origin` check.

Never put a proxy in front of a server that has no password. Such a server
trusts every request that reaches it under a loopback host name, and a proxy
on the same machine sends exactly those.

## Signing in

A browser signs in with the password on the sign-in page, which posts to
`POST /login`, and gets a session cookie. The session lasts until
`POST /logout`, 12 hours, or a server restart.

A script sends the password on each request instead:

```sh
curl -s -H 'Authorization: Bearer <password>' http://<pi>:8080/api/v1/status
```

openreactor checks one password at a time. Each client address has at most
one check in progress: a second guess from the same address gets 429 at
once, and other clients wait up to 2 seconds for their turn.

A request that changes something is refused if its `Origin` header names
another site. A browser request signed in by cookie must carry an `Origin`
header.
