# Installation

This walks through a complete setup, from an empty Home Assistant to an add-on
that alerts. Budget about twenty minutes; the sensor mapping is the part that
takes the time.

## 1. What you need first

**Home Assistant OS or Supervised.** The add-on runs under the Supervisor, so
Home Assistant Container and Core cannot install it. Check under **Settings →
System → About**: if there is no *Add-ons* entry in the Settings menu, you are on
an unsupported installation type.

**Sensors already working in Home Assistant.** The add-on does not talk to your
sensors directly — it reads what Home Assistant already knows. If a sensor does
not show up in Home Assistant, it will not show up here either.

The useful minimum:

| What | Why it matters |
|---|---|
| 3+ room presence or motion sensors | Without them there is no daily routine to learn |
| A front door sensor | Separates "left the house" from "stopped moving" |
| A power-metering plug on a routine appliance | A coffee maker is a reliable morning marker |
| A bed occupancy sensor | Wake-up and bedtime, the two anchors of the day |
| Optionally a camera with Frigate | Only used for fall confirmation, and only if you allow it |

Fewer sensors still work; the add-on says so instead of guessing. Below roughly
four confirmed entities it reports "insufficient data" rather than drawing
conclusions from too little.

## 2. Add the repository

Click this, or paste the repository URL under **Settings → Add-ons → ⋮ → Add
repository**:

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FBoco10%2Feldercare-addons)

Then **Settings → Add-ons → Add-on Store**, find **ElderCare AI** under this
repository, and press **Install**.

The first install builds the image on your machine and takes a few minutes on a
Raspberry Pi. That is normal — the add-on ships as source on purpose, so you can
read what you are running.

## 3. Start it

Press **Start**, then open the **Log** tab. A healthy start looks like this:

```
ElderCare AI 0.1.3 starting (mode: supervisor)
Database ready: /data/eldercare.db
Pairing state: not paired
Mappings loaded: 0 entries (0 confirmed)
Discovered notification channels: ['notify', 'mobile_app_...']
WebSocket connected: ws://supervisor/core/websocket
```

Two lines are worth reading:

- **Discovered notification channels** — this is where alerts will go. An empty
  list means Home Assistant has no notification service, and a critical alert
  would have nowhere to arrive. Set up the mobile app or another notifier first.
- **WebSocket connected** — without it the add-on sees no sensors at all.

## 4. Confirm the entities

Press **Open**. Your Home Assistant login protects this page; there is no
separate password.

Go to the **Entities** tab. The add-on lists what it found, grouped by type, and
suggests a meaning for each — presence, door, bed, appliance. Tabs with pending
work are marked, so you can see where to start.

For each sensor you care about:

1. Check the suggested **meaning** and correct it if wrong.
2. Fill in the **room**. The add-on cannot infer it; `binary_sensor.motion_2`
   tells it nothing.
3. Press **Save**.

**Nothing is processed until you confirm it.** This is deliberate: a wrong
meaning produces a wrong alert, and that is the fastest way to lose trust in a
monitoring system. A suggestion sits inert until a human agrees with it.

Sensors you do not want — the printer, the doorbell, a test switch — go to the
**Not needed** tab and stay out of the way.

Aim for five or more confirmed entities. The page tells you where you stand.

## 5. Check that alerting works

On the **Diagnostics** panel, press **Send test alert**.

It goes out through the real delivery path, on the real channels — it is not a
simulation. If it does not arrive on your phone, alerting is broken, and it is
better to learn that now than during an emergency.

At this point the add-on is useful on its own: it processes events locally and
raises critical alerts (SOS, smoke, CO, confirmed fall) with no internet and no
cloud involved.

## 6. Optional: pair with a backend

Pairing adds the learned daily routine, the caregiver rulebook and alert
escalation. It is not required, and the critical path above works without it.

1. On the **Cloud connection** panel press **Request pairing code**.
2. Sign in to the caregiver portal and enter the code there.
3. Paste the device token you get back into the add-on and press **Save token**.

The token is a credential. The field hides it, clears it after saving, and the
add-on never logs it or shows it again — only whether one exists.

If you self-host a different backend, point `cloud_api_url` at it in the add-on
Configuration tab. Changing the address requires pairing again.

## 7. What happens next

| Period | What to expect |
|---|---|
| Days 1–2 | Sensor check and data quality measurement. Alerts only for critical events. |
| Weeks 3–4 | The routine is being learned. Only unambiguous critical rules alert. |
| From week 4 | Deviations are scored and explained. |

The quiet first weeks are the design, not a fault. Alerting on a routine it has
not learned yet would mean false alarms, and a caregiver who has been woken
three times for nothing turns the system off.

## Troubleshooting

| Symptom | What to check |
|---|---|
| The add-on will not install | Home Assistant OS or Supervised only — Container and Core have no Supervisor. |
| The Entities tab is empty | The Log tab: is there a `WebSocket connected` line? |
| A sensor is missing from the list | Does it exist in **Developer tools → States**? If not, the problem is upstream. |
| A sensor is listed but nothing happens with it | Open the **Live sensor feed**. It states the reason: no mapping, not confirmed, sending off, or not needed. |
| The test alert does not arrive | The Log shows the channels found at startup. An empty list means Home Assistant has no notifier. |
| Cloud status is "offline" | Check the internet connection and `cloud_api_url`. Local operation continues regardless. |
| "insufficient data" every day | Too few confirmed entities, or a sensor is stuck in `unavailable`. The daily summary names the entity. |

Full option reference: [eldercare_ai/DOCS.md](./eldercare_ai/DOCS.md).

## Removing it

Uninstalling deletes the add-on's `/data` directory with everything in it:
mappings, events, learned habits. If you paired with a backend, deleting the
data held there is a separate request from the caregiver portal.
