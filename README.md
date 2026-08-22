# ElderCare Home Assistant Add-ons

A Home Assistant add-on repository for behaviour-learning eldercare monitoring.

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FBoco10%2Feldercare-addons)

## Add-ons in this repository

### [ElderCare AI](./eldercare_ai)

Uses the sensors already in the home — presence, doors, power-metering plugs,
bed occupancy, optionally a camera — as a single data source. Nothing about the
resident's day is programmed in advance: over 3–4 weeks the system learns what a
typical day looks like, then flags the unusual departures from it.

- Discovers Home Assistant entities and assigns semantic meaning to them.
- Processes events locally — raw sensor data does not leave the home.
- **Critical alerts work without internet and without the cloud**: SOS, smoke,
  CO and a confirmed fall are evaluated on the device and go straight out
  through Home Assistant's notification channels.
- Optionally pairs with a backend that learns the routine and lets a caregiver
  describe, in plain language, what counts as unusual.

## Installation

**[Full installation guide → INSTALL.md](./INSTALL.md)** — prerequisites, sensor
mapping, verifying that alerts actually arrive, and troubleshooting.

The short version:

1. Click the button above, or go to **Settings → Add-ons → ⋮ → Add repository**
   and paste this repository's URL.
2. Install **ElderCare AI** from the repository and start it.
3. Press **Open** to reach the add-on's own interface. Your Home Assistant login
   protects it — there is no separate password.
4. On the **Entities** tab, confirm what the add-on found. Nothing is processed
   before you confirm.
5. Press **Send test alert** on the Diagnostics panel. If it does not arrive,
   alerting is broken — better to find out now than in an emergency.

Requires Home Assistant **OS or Supervised**; Container and Core have no
Supervisor and cannot install add-ons.

Option reference: [eldercare_ai/DOCS.md](./eldercare_ai/DOCS.md).

## What runs where

The add-on is useful on its own: entity discovery, local event processing and
critical alerting need nothing but Home Assistant.

Pairing with a backend adds the learned routine, the caregiver rulebook and the
alert escalation. That backend is a separate, closed-source service; this
repository contains only the add-on. If you self-host something else, point
`cloud_api_url` at it — the API contract is what the add-on speaks, and the
add-on does not care who answers.

## Privacy

- By default only semantic events and daily summaries reach the cloud. Raw
  sensor readings stay on the device.
- Camera image upload is **off** by default; at most one snapshot, on a
  high-risk alert. Continuous cloud video is not supported.
- Raw data is kept locally for a configurable period, then deleted.

## Not a medical device

This system supports wellbeing and safety monitoring. Its alerts are
probabilistic signals, not diagnoses, and they do not replace medical or
caregiver supervision.

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md). Bug reports and pull requests are
welcome.

## License

[MIT](./LICENSE).
