# ElderCare AI

A behaviour-learning eldercare agent for Home Assistant.

It uses the sensors already in the home — presence, doors, power-metering plugs, bed occupancy,
optionally a camera — as a single data source. Nothing about the resident's day is programmed in
advance: over 3–4 weeks the system **learns** what a typical day looks like, then flags the
unusual departures from it.

## What it does

- Discovers Home Assistant entities automatically and assigns semantic meaning to them.
- Processes events locally — raw sensor data does not leave the home.
- Learns the daily routine and reports deviations together with an explanation.
- Critical alerts (SOS, smoke, CO, confirmed fall) work **without internet and without the cloud**.
- The caregiver can give feedback: this was normal / false alarm / a real problem.

## Privacy

- By default only semantic events and daily summaries reach the cloud.
- Camera image upload is **off** by default; at most one snapshot, on a high-risk alert.
- Continuous cloud video is not supported.
- Raw data is kept locally for a configurable period, then deleted.

## Not a medical device

This system supports wellbeing and safety monitoring. Its alerts are probabilistic signals, not
diagnoses, and they do not replace medical or caregiver supervision.

## Installation

1. Add this repository to Home Assistant: **Settings → Add-ons → ⋮ → Add repository**
2. Install the *ElderCare AI* add-on.
3. Start it, then open the local interface with the **Open** button.
4. Confirm the discovered entities, then pair with your caregiver portal account.

Full documentation: [DOCS.md](DOCS.md).

## Language

The add-on speaks English everywhere it surfaces: interface, documentation and log. The
translation machinery is still in place (`translations/`, `app/api/static/i18n.js`) if a second
language is added later.
