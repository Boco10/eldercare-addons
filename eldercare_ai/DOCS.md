# ElderCare AI

## Where each setting lives

This is the question that costs the most time, so it comes first. Three different places, and the
split is not arbitrary — each setting sits where the thing it controls actually is.

| I want to… | Where | Why there |
|---|---|---|
| Point the add-on at a backend, decide what may leave the house | **This Configuration tab** | These are properties of *this* installation. |
| Say which entity is the front door, the bed, the kitchen | **Open → Entities tab** | The add-on is the only component that can see your entities. |
| Define what counts as unusual, and what alerts vs. notifies | **Caregiver portal → Rulebook** | You write it once; it applies wherever you read the results. |

The Configuration tab deliberately has no learning period and no anomaly thresholds. Those used to
be here and did nothing — the decision moved to the backend. A slider that changes nothing is worse
than no slider, because you believe you have set something.

## Installation

1. **Settings → Add-ons → ⋮ → Add repository**, then enter the repository URL.
2. Install *ElderCare AI* and start it.
3. **Open** takes you to the local interface. Your Home Assistant login protects it — there is no
   separate password.
4. On the **Entities** tab confirm what the add-on found. Nothing is uploaded before you confirm.
5. Press **Pair** and type the code from the caregiver portal.

## Minimum hardware

- A Zigbee coordinator and at least 3 room presence or motion sensors
- A front door sensor
- A power-metering smart plug on at least one routine appliance (a coffee maker, for example)
- A bed occupancy sensor or a validated alternative
- Optionally an RTSP camera with Frigate

## Options

| Option | Default | What it does |
|---|---|---|
| `cloud_api_url` | — | Where the add-on sends data. Changing it requires pairing again. |
| `send_daily_features` | `true` | One summary per day. This is what habits are learned from. |
| `image_upload_mode` | `critical_only` | `never` / `critical_only` / `on_request` / `always`. |
| `send_raw_events` | `false` | Debugging only. Raw events identify rooms and habits in detail. |
| `local_raw_retention_days` | `30` | Raw readings are deleted locally after this many days. |
| `log_level` | `info` | Use `debug` only while chasing a problem. |

## The first weeks

| Period | What happens |
|---|---|
| Days 1–2 | Sensor check, data quality measurement. Alerts only for critical events. |
| Weeks 3–4 | Learning the routine. Only unambiguous critical rules alert. |
| From week 4 | Deviations are scored and explained. |
| Ongoing | Slow baseline updates, caregiver feedback folded in. |

This is deliberate, not a fault: instead of alerting prematurely, the system first learns what is
normal **in this home**.

## Worth knowing

- **Critical alerting works without internet.** SOS, smoke, CO and a confirmed fall are evaluated
  locally and go straight out through Home Assistant's notification channels.
- **During an outage events queue up** and sync when the connection returns. Nothing is lost.
- **On poor data quality the system does not conclude.** If a sensor died, it reports "insufficient
  data" rather than inventing an alert.
- **Everything the add-on says is English** — interface, this documentation, and the log. Source
  comments are still Hungarian; they are for developers and never reach you.

## Troubleshooting

| Symptom | What to do |
|---|---|
| The add-on does not start | Check the *Log* tab. |
| "No connection" to Home Assistant | Restart the add-on; the client reconnects on its own. |
| Cloud status is "offline" | Check the internet connection. Local operation continues regardless. |
| No alert arrives | Press *Test alert* on the diagnostics panel. |
| Too many false alerts | Adjust your rules in the caregiver portal's rulebook. |

## Deleting data

The add-on keeps everything in its own `/data` directory; uninstalling removes it. Deletion of data
held in the cloud can be requested from the caregiver portal.

