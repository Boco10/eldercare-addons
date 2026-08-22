# Contributing

Thanks for looking. This is a small project; the fastest way to help is a clear
bug report with the add-on's log attached.

## Running the tests

Everything runs in Docker, so you do not need a local Python:

```bash
docker run --rm -v "$PWD:/addon" -w /addon python:3.12-slim \
  sh -c "pip install -q -r eldercare_ai/requirements.txt \
                       -r eldercare_ai/requirements-dev.txt && python -m pytest -q"
```

The replay fixtures (28 baseline days plus 10 anomaly days) live next to the
tests in `eldercare_ai/test/fixtures/`, so nothing has to be mounted or
generated first. `FIXTURES_DIR` overrides the location if you want to point the
suite at your own recordings; `test/fixtures/generate.py` regenerates them.

Linting uses [ruff](https://docs.astral.sh/ruff/):

```bash
docker run --rm -v "$PWD:/addon" -w /addon python:3.12-slim \
  sh -c "pip install -q ruff && ruff check ."
```

Both run in CI on every pull request.

## Building the add-on locally

Home Assistant builds local add-ons for you: drop the `eldercare_ai` folder into
`/addons` on your Home Assistant machine and it appears under **Local add-ons**.
`config.yaml` intentionally has no `image:` line, which is what tells the
Supervisor to build from source.

## Two things worth knowing before you change behaviour

**Critical alerting must never depend on the network.** SOS, smoke, CO and a
confirmed fall are decided in `app/alerts/local_engine.py` and go out through
Home Assistant. If a change makes that path wait on an HTTP call, it is the
wrong change — that path is the reason the add-on exists.

**Silence is not calm.** When the add-on cannot tell whether something is fine,
it says so rather than staying quiet. You will see this everywhere: an
unreachable backend reports "unreachable", a sensor that never fired reports
"no signal (a quiet day or a dead sensor — cannot be told apart)". Please keep
that habit; a monitoring system that fails quietly is worse than none.

## Language

The source, the comments and everything the add-on emits are English.

The interface is translated: `app/api/static/i18n.js` holds the UI strings and
`eldercare_ai/translations/` the add-on configuration labels. English is the
default; the UI follows Home Assistant's own language when a translation exists,
unless the reader picked one from the language selector.

Adding a language means copying the `en` block in `i18n.js`, translating the
values, adding the code to `LANGUAGES`, and dropping a `<code>.yaml` next to
`en.yaml`. Keys must match exactly — a missing key falls back to English, so a
partial translation degrades gracefully instead of showing blanks.

One trap worth knowing: a Hungarian closing quote (`”`) is a different character
from the ASCII `"`, and using the ASCII one inside a translated string ends the
JavaScript string early. `node --check app/api/static/i18n.js` catches it.

You will also see references like `docs/02-ADDON.md §4` in the comments. Those
point at the private design documents of the wider project and are not part of
this repository. Treat them as historical markers, not as required reading.

## Commit messages

Explain *why*, not *what* — the diff already shows what changed. If a change
fixes something subtle, say what would break without it.
