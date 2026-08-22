# Kiadási folyamat

## Verziózás

Semantic Versioning. A `config.yaml` `version` mezője és a GHCR image tagje **mindig egyezik** —
a Supervisor ez alapján dönti el, hogy van-e frissítés.

| Változás | Verzió |
|---|---|
| Hibajavítás, nincs séma- vagy viselkedésváltozás | patch (0.1.**1**) |
| Új opció, új funkció, visszafelé kompatibilis | minor (0.**2**.0) |
| Opció eltávolítása/átnevezése, API-verzióváltás, adatmigráció | major (**1**.0.0) |

## Lépések

1. **CHANGELOG.md** — az `[Unreleased]` szakasz átnevezése a kiadott verzióra, dátummal.
2. **config.yaml** — `version` frissítése.
3. Ellenőrzés:
   ```bash
   bash test/smoke.sh              # minimal, offline, full scenario
   pytest                          # unit + replay
   ```
   Az `offline` scenariónak **kötelezően** zöldnek kell lennie — ez bizonyítja, hogy a helyi
   működés nem függ a felhőtől.
4. Commit + tag: `git tag eldercare_ai-0.2.0 ; git push --tags`
5. A GitHub Action multi-arch buildet készít és a GHCR-be pusholja.
6. **Ellenőrzés éles előtt:** telepítsd egy teszt-HA példányra, és győződj meg róla, hogy a
   meglévő `/data` adatbázis migrálódik, nem vész el.

## Opció eltávolítása

Ha egy opciót kiveszel a sémából, a már telepített felhasználóknál ez a figyelmeztetés jelenik meg:
`Option '<key>' does not exist in the schema`. Ezért az eltávolítást a `run` scriptben kell
kezelni `bashio::addon.option '<key>'` hívással (argumentum nélkül = törlés).

## Stabil és canary ág

- `main` → stabil kiadások
- `next` → canary; a felhasználó a repo URL végére írt `#next`-tel választja

## Visszavonás

Ha egy kiadás hibás: `config.yaml`-ben vissza az előző verzióra, új patch tag, és a
CHANGELOG-ban egyértelmű `### Visszavonva` szakasz. A GHCR-ből ne törölj image-et — a
felhasználók visszaállításához kellhet.
