# Changelog

A formátum a [Keep a Changelog](https://keepachangelog.com/hu/1.1.0/) ajánlását követi,
a verziózás a [Semantic Versioning](https://semver.org/lang/hu/) szerint történik.

## [0.1.5]

### Megváltozott
- **A `config.yaml` megtisztítva** a hivatalos Home Assistant add-on linter szerint.
  A `startup`, `boot`, `hassio_role`, `auth_api`, `apparmor` és `ingress_port`
  kiírt értéke pontosan az alapérték volt — a linter ezt hibaként jelzi, és
  jogosan: egy kiírt alapérték azt sugallja, hogy döntés van mögötte, és eltakarja
  azt a néhány sort, ahol tényleg van. Élesben ellenőrizve: a Supervisor
  ugyanazokat az értékeket alkalmazza (`ingress_port: 8099`, `startup: application`,
  `boot: auto`), és az AppArmor profil is érvényes marad az `apparmor.txt` miatt.
- **A `watchdog:` kulcs helyett natív Docker `HEALTHCHECK`.** A config-kulcs elavult;
  a Supervisor watchdogja a konténer health állapotát nézi. Élesben ellenőrizve:
  a konténer `healthy` állapotot jelent.

## [0.1.4]

### Javítva
- **A device token olvashatóan állt a képernyőn.** A párosítási mezője
  `type="text"` volt, és a beírt token a mentés után is ott maradt — vállon át
  vagy egy képernyőmegosztáson leolvasható. Mostantól rejtett mező (kérésre
  megmutatható, mert egy 40 karakteres tokent elgépelni könnyű), mentés után
  pedig kiürül, sikertelen próbánál is: egy elutasított tokennek sincs helye a
  képernyőn. `autocomplete="off"`, hogy a böngésző se jegyezze meg.

### Hozzáadva
- `INSTALL.md`: teljes telepítési útmutató a publikus repóhoz — előfeltételek,
  szenzor-hozzárendelés, a riasztás tényleges ellenőrzése, hibakeresés.
- `tools/leak-scan.py`, bekötve a publikálásba: jelszó-, token-, kulcs- és
  e-mail-mintákra keres a kimásolt fában. Amit egyszer felpusholunk, azt a git
  előzményből ténylegesen nem lehet eltüntetni.

## [0.1.3]

### Hozzáadva
- **Magyar nyelv visszatért, de nem alapértelmezésként.** A felület a Home Assistant
  saját nyelvét követi: angol az alapértelmezés, magyar HA-n magyar. A sorrend
  szándékos — (1) amit a felhasználó itt választott, (2) a Home Assistant nyelve,
  (3) a böngésző nyelve, (4) angol. Aki egyszer kézzel választ, annak a döntése
  megmarad; e nélkül a következő státuszlekérdezés visszaváltana.
- `translations/hu.yaml`: a Supervisor beállítólapja magyar HA-n magyarul jelenik meg.
- Az `/api/status` visszaadja a Home Assistant nyelvét (`ha_language`).

### Megváltozott
- **A teljes forráskód angol lett**: 949 komment- és docstring-sor 37 fájlban.
  Eddig csak a KIFELÉ menő szöveg volt angol; a forrás magyar maradt, ami egy
  publikus repóban külső fejlesztőnek akadály.

### Javítva
- Az `i18n.js` magyar blokkjában egy magyar záró idézőjel (`"`) lezárta volna a
  JavaScript stringet — a fájl nem futott volna le. A `node --check` fogta meg.

### Megváltozott
- **A bővítmény angol nyelvű lett.** Minden, ami elhagyja a kódot — a beállítólap, a
  felület, a dokumentáció, a napló, a gondozó telefonjára menő riasztásszöveg — angolul
  szól. Ez a Home Assistant bővítmények konvenciója, és feltétele annak, hogy a bővítmény
  nemzetközi közönségnek publikálható legyen.
- A magyar fordítás kikerült (`translations/hu.yaml`, az `i18n.js` `hu` blokkja), a
  fordítási gépezet viszont a helyén maradt: egy második nyelv az `en` blokk másolásával
  bármikor visszatehető.
- `DOCS.md` és `README.md` angolul.

### Megjegyzés
- A forráskód kommentjei és docstringjei magyarul maradtak. Azok fejlesztőnek szólnak,
  és soha nem jelennek meg a felhasználónak.

## [0.1.1]

### Megváltozott
- A beállítólap átírva: minden mezőnek beszédes neve és magyarázata van, angolul és
  magyarul (a Home Assistant nyelve dönti el, melyik látszik). A sorrend a lap tetejétől
  olvasható: kapcsolat → mit lát a felhő → meddig tároljuk → hibakeresés.
- A dokumentáció (DOCS.md) kétnyelvű lett: angol elöl, magyar utána. A Home Assistant
  bővítményenként egyetlen doksifájlt szolgál ki, ezért mindkét nyelv ugyanabban a fájlban van.
- A doksi elején egy táblázat mondja meg, melyik beállítás hol van: bővítmény-opció,
  entitás-fül vagy a portál szabálykönyve.

### Eltávolítva
- `learning_period_days`, `minimum_data_quality`, `anomaly_threshold_medium`,
  `anomaly_threshold_high`. Az A14 architektúraváltás óta ezek beolvasva álltak, hatás
  nélkül — a döntés a backendre került (szokásmodell + a gondozó szabálykönyve). Egy
  hatástalan csúszka rosszabb, mint a hiánya: azt hinnéd, beállítottad.

## [Unreleased]

### Hozzáadva
- Home Assistant WebSocket kliens exponenciális backoffal és watchdoggal.
- Replay eseményforrás JSONL fixture-ökből — fejlesztés HA nélkül.
- Esemény-normalizáló duplikációszűréssel.
- Lokális SQLite séma (nyers állapotok, szemantikus események, offline queue, riasztások).
- Cloud sync kliens idempotenciával és a szerződés szerinti hibakezeléssel.
- Ingress mögötti helyi UI diagnosztikai panellel.
- Smoke teszt options-scenariókkal (`minimal`, `offline`, `full`).

### Hiányzik még (következő fázisok)
- Szemantikus eseményképzés és entitás-mapping UI (1. fázis).
- Kritikus offline riasztási motor (1. fázis).
- Napi feature builder, baseline, anomáliapontszám (3. fázis).
- Cloud párosítás és device token kezelés (2. fázis).
- MQTT visszapublikálás (1. fázis).

## [0.1.0] - 2026-07-28

### Hozzáadva
- Első váz: app-manifest, s6 indítás, konténer-build, futtatható event pipeline.
