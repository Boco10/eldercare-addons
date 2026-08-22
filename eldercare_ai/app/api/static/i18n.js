/* ElderCare add-on UI — localisation.
 *
 * English is the default, because the add-on is published to an international
 * audience. But the person reading this screen is a caregiver, and on a
 * Hungarian Home Assistant install they may not speak English at all — so the
 * interface follows Home Assistant's own language when it can.
 *
 * The order is deliberate:
 *
 *   1. what the user picked here (localStorage) — an explicit choice wins,
 *   2. Home Assistant's configured language, reported by /api/status,
 *   3. the browser's language,
 *   4. English.
 *
 * Adding a language: copy the `en` block, translate the values, and add the
 * code to LANGUAGES. Keys must match exactly — a missing key falls back to
 * English, so a partial translation degrades gracefully instead of showing
 * blanks.
 */

const LANGUAGES = { en: "English", hu: "Magyar" };

const TRANSLATIONS = {
  en: {
    "app.title": "ElderCare AI",
    "app.noscript":
      "JavaScript is disabled. Raw status is available at <a href=\"./api/status\">./api/status</a>, " +
      "entities at <a href=\"./api/entities\">./api/entities</a>.",

    "card.connections": "Connections",
    "conn.ha": "Home Assistant",
    "conn.mode": "Mode",
    "conn.cloud": "Cloud",
    "conn.paired": "Paired",
    "conn.connected": "connected",
    "conn.disconnected": "no connection",
    "conn.online": "online",
    "conn.offline": "offline",
    "conn.yes": "yes",
    "conn.no": "no",
    "conn.status_unavailable": "status unavailable",

    "card.data": "Data",
    "data.raw": "Raw states",
    "data.semantic": "Semantic events",
    "data.queue": "Waiting to upload",
    "data.mapped": "Mapped entities",

    "card.privacy": "Privacy",
    "privacy.image_mode": "Image upload",
    "privacy.mode.never": "never — images stay in the home",
    "privacy.mode.critical_only": "only on critical alerts",
    "privacy.mode.on_request": "when I ask for it",
    "privacy.mode.always": "always",
    "privacy.send_features": "Send daily summary",
    "privacy.send_raw": "Send raw events",
    "privacy.retention": "Keep raw data (days)",
    "privacy.save": "Save",
    "privacy.saving": "Saving…",
    "privacy.saved": "Saved — effective immediately.",
    "privacy.save_failed": "Could not save.",
    "privacy.supervisor_note": "Saving also updates the add-on Configuration tab.",
    "privacy.local_note": "Takes effect immediately and survives a restart.",

    "card.diagnostics": "Diagnostics",
    "diag.test_alert": "Send test alert",
    "diag.export": "Export mapping",
    "diag.sending": "Sending…",
    "diag.sent": "Sent",
    "diag.failed": "Failed",
    "diag.test_failed": "The test alert could not be sent.",

    "pairing.title": "Cloud connection",
    "pairing.loading": "Loading…",
    "pairing.paired": "paired",
    "pairing.not_paired": "not paired",
    "pairing.home": "home",
    "pairing.installation": "Installation ID",
    "pairing.backend": "backend",
    "pairing.unpair": "Disconnect",
    "pairing.unpair_confirm": "Disconnect from the cloud? Uploads will stop.",
    "pairing.unpair_note":
      "After disconnecting, uploads stop — but <strong>critical alerts keep working locally</strong>.",
    "pairing.step1": "Request a pairing code.",
    "pairing.step2": "Sign in on the web portal and enter the code there.",
    "pairing.step3": "Copy the device token you receive back here.",
    "pairing.code": "Code",
    "pairing.expires": "expires",
    "pairing.get_code": "Request pairing code",
    "pairing.new_code": "Request a new code",
    "pairing.save_token": "Save token",
    "pairing.reveal": "show",
    "pairing.hide": "hide",
    "pairing.checking": "Checking…",

    "analysis.title": "Routine analysis",
    "analysis.intro":
      "The add-on collects sensor states and recent events, sends them to the backend, " +
      "and shows the result. <strong>The evaluation happens on the backend.</strong> " +
      "Critical alerts (SOS, smoke, CO, fall) work locally, independently of this.",
    "analysis.camera": "Camera",
    "analysis.no_image": "Without image",
    "analysis.preview": "Preview",
    "analysis.run": "Request analysis from backend",
    "analysis.running": "Analysis in progress…",
    "analysis.none_yet": "No analysis has run yet.",
    "analysis.reasons": "Reasons",
    "analysis.action": "Suggested action",
    "analysis.sensors_sent": "sensors",
    "analysis.events_sent": "events sent",
    "analysis.image_analyzed": "image analysed",
    "analysis.credits": "credits",
    "analysis.quality": "data quality",
    "analysis.failed": "The analysis could not be started.",
    "analysis.mock": "MOCK response",
    "analysis.score": "score",

    "camera.none": "No camera in Home Assistant.",
    "camera.unavailable": "not available",
    "camera.upload_allowed": "Image upload: <strong>{mode}</strong> — an image may be attached.",
    "camera.upload_blocked":
      "Image upload: <strong>{mode}</strong> — the image will <strong>not</strong> be sent, " +
      "local preview only.",
    "camera.snapshot_failed": "Snapshot unavailable (disabled, or the camera is unreachable).",

    "feed.title": "Live sensor feed",
    "feed.intro":
      "Everything arriving from Home Assistant, and what the add-on makes of it. " +
      "If a sensor is missing here, the problem is upstream — not in the mapping.",
    "feed.states_title": "Incoming states",
    "feed.events_title": "Semantic events",
    "feed.col_time": "Time",
    "feed.col_entity": "Entity",
    "feed.col_change": "Change",
    "feed.col_processed": "Processing",
    "feed.col_type": "Event",
    "feed.col_room": "Room",
    "feed.col_synced": "Upload",
    "feed.pause": "Pause",
    "feed.resume": "Resume",
    "feed.filter_placeholder": "Filter by entity or room…",
    "feed.summary": "{states} states · {events} events stored · {ignored} ignored",
    "feed.no_states": "No state change yet.",
    "feed.no_events": "No semantic event yet.",
    "feed.sent": "sent",
    "feed.local_only": "local only",
    "feed.skip_unmapped": "no mapping",
    "feed.skip_unconfirmed": "not confirmed",
    "feed.skip_disabled": "sending off",
    "feed.skip_ignored": "not needed",

    "entities.title": "Entities and semantic mapping",
    "entities.intro":
      "The system suggests a meaning, but <strong>only confirmed mappings are used</strong> — " +
      "a wrong meaning would cause a wrong alert. Set the room, then save.",
    "entities.ready":
      "{count} entities confirmed — learning can start.",
    "entities.not_ready":
      "Currently <strong>{count}</strong> confirmed; at least 5 are recommended for reliable operation.",
    "entities.col_entity": "Entity",
    "entities.col_state": "State",
    "entities.col_meaning": "Meaning",
    "entities.col_room": "Room",
    "entities.col_device": "Device",
    "entities.col_send": "Send",
    "entities.col_note": "Note",
    "entities.col_status": "Status",
    "entities.tab_empty": "No entity of this type.",
    "entities.tab_ignored": "Not needed",
    "entities.group_unknown": "Not recognised",
    "entities.not_needed": "Not needed",
    "entities.not_needed_hint":
      "Move it to the \"Not needed\" tab. It stays out of the way and is never processed.",
    "entities.restore": "Put back",
    "entities.send_hint": "Send this entity's data to the backend",
    "entities.note_hint": "Your own note — stays local, never sent to the cloud",
    "entities.note_placeholder": "e.g. behind the TV, battery powered",
    "entities.room_placeholder": "e.g. bedroom",
    "entities.loading": "Loading…",
    "entities.empty": "No entities found. Is Home Assistant connected?",
    "entities.save": "Save",
    "entities.error": "Error",
    "entities.load_failed": "Could not load entities.",
    "entities.suggestion": "suggestion",
    "entities.sent": "sent",
    "entities.skipped": "skipped",
  },

  hu: {
    "app.title": "ElderCare AI",
    "app.noscript":
      "A JavaScript ki van kapcsolva. A nyers állapot a <a href=\"./api/status\">./api/status</a>, " +
      "az entitások az <a href=\"./api/entities\">./api/entities</a> végponton olvashatók.",

    "card.connections": "Kapcsolatok",
    "conn.ha": "Home Assistant",
    "conn.mode": "Mód",
    "conn.cloud": "Felhő",
    "conn.paired": "Párosítva",
    "conn.connected": "csatlakozva",
    "conn.disconnected": "nincs kapcsolat",
    "conn.online": "online",
    "conn.offline": "offline",
    "conn.yes": "igen",
    "conn.no": "nem",
    "conn.status_unavailable": "az állapot nem elérhető",

    "card.data": "Adatok",
    "data.raw": "Nyers állapotok",
    "data.semantic": "Szemantikus események",
    "data.queue": "Feltöltésre vár",
    "data.mapped": "Hozzárendelt entitások",

    "card.privacy": "Adatvédelem",
    "privacy.image_mode": "Képfeltöltés",
    "privacy.mode.never": "soha — a kép a lakásban marad",
    "privacy.mode.critical_only": "csak kritikus riasztásnál",
    "privacy.mode.on_request": "ha én kérem",
    "privacy.mode.always": "mindig",
    "privacy.send_features": "Napi összesítő küldése",
    "privacy.send_raw": "Nyers események küldése",
    "privacy.retention": "Nyers adat megőrzése (nap)",
    "privacy.save": "Mentés",
    "privacy.saving": "Mentés…",
    "privacy.saved": "Elmentve — azonnal érvényes.",
    "privacy.save_failed": "A mentés nem sikerült.",
    "privacy.supervisor_note": "A mentés a bővítmény Konfiguráció fülét is frissíti.",
    "privacy.local_note": "Azonnal érvényes, és újraindítás után is megmarad.",

    "card.diagnostics": "Diagnosztika",
    "diag.test_alert": "Tesztriasztás küldése",
    "diag.export": "Mapping exportálása",
    "diag.sending": "Küldés…",
    "diag.sent": "Elküldve",
    "diag.failed": "Sikertelen",
    "diag.test_failed": "A tesztriasztást nem sikerült elküldeni.",

    "pairing.title": "Felhőkapcsolat",
    "pairing.loading": "Betöltés…",
    "pairing.paired": "párosítva",
    "pairing.not_paired": "nincs párosítva",
    "pairing.home": "otthon",
    "pairing.installation": "Telepítésazonosító",
    "pairing.backend": "backend",
    "pairing.unpair": "Leválasztás",
    "pairing.unpair_confirm": "Leválasztod a felhőről? A feltöltés leáll.",
    "pairing.unpair_note":
      "Leválasztás után a feltöltés leáll — de <strong>a kritikus riasztás helyben " +
      "továbbra is működik</strong>.",
    "pairing.step1": "Kérj párosítási kódot.",
    "pairing.step2": "Lépj be a webes felületen, és írd be ott a kódot.",
    "pairing.step3": "Másold vissza ide a kapott eszköz-tokent.",
    "pairing.code": "Kód",
    "pairing.expires": "lejár",
    "pairing.get_code": "Párosítási kód kérése",
    "pairing.new_code": "Új kód kérése",
    "pairing.save_token": "Token mentése",
    "pairing.reveal": "mutasd",
    "pairing.hide": "elrejt",
    "pairing.checking": "Ellenőrzés…",

    "analysis.title": "Rutinelemzés",
    "analysis.intro":
      "A bővítmény összegyűjti a szenzorállapotokat és a friss eseményeket, elküldi a " +
      "backendnek, és megmutatja az eredményt. <strong>A kiértékelés a backenden " +
      "történik.</strong> A kritikus riasztás (SOS, füst, CO, esés) ettől függetlenül, " +
      "helyben működik.",
    "analysis.camera": "Kamera",
    "analysis.no_image": "Kép nélkül",
    "analysis.preview": "Előnézet",
    "analysis.run": "Elemzés kérése a backendtől",
    "analysis.running": "Elemzés folyamatban…",
    "analysis.none_yet": "Még nem futott elemzés.",
    "analysis.reasons": "Okok",
    "analysis.action": "Javasolt teendő",
    "analysis.sensors_sent": "szenzor",
    "analysis.events_sent": "esemény elküldve",
    "analysis.image_analyzed": "kép elemezve",
    "analysis.credits": "kredit",
    "analysis.quality": "adatminőség",
    "analysis.failed": "Az elemzést nem sikerült elindítani.",
    "analysis.mock": "MOCK válasz",
    "analysis.score": "pontszám",

    "camera.none": "Nincs kamera a Home Assistantban.",
    "camera.unavailable": "nem elérhető",
    "camera.upload_allowed":
      "Képfeltöltés: <strong>{mode}</strong> — kép csatolható.",
    "camera.upload_blocked":
      "Képfeltöltés: <strong>{mode}</strong> — a kép <strong>nem</strong> megy fel, " +
      "csak helyi előnézet.",
    "camera.snapshot_failed":
      "A pillanatkép nem érhető el (tiltva van, vagy a kamera nem válaszol).",

    "feed.title": "Élő szenzorfolyam",
    "feed.intro":
      "Minden, ami a Home Assistantból érkezik, és amit a bővítmény kezd vele. " +
      "Ha egy szenzor itt hiányzik, a gond feljebb van — nem a hozzárendelésben.",
    "feed.states_title": "Beérkező állapotok",
    "feed.events_title": "Szemantikus események",
    "feed.col_time": "Idő",
    "feed.col_entity": "Entitás",
    "feed.col_change": "Változás",
    "feed.col_processed": "Feldolgozás",
    "feed.col_type": "Esemény",
    "feed.col_room": "Szoba",
    "feed.col_synced": "Feltöltés",
    "feed.pause": "Megállítás",
    "feed.resume": "Folytatás",
    "feed.filter_placeholder": "Szűrés entitásra vagy szobára…",
    "feed.summary": "{states} állapot · {events} szemantikus esemény · {ignored} kihagyva",
    "feed.no_states": "Még nem érkezett állapotváltozás.",
    "feed.no_events": "Még nincs szemantikus esemény.",
    "feed.sent": "felküldve",
    "feed.local_only": "csak helyben",
    "feed.skip_unmapped": "nincs hozzárendelés",
    "feed.skip_unconfirmed": "nincs megerősítve",
    "feed.skip_disabled": "küldés kikapcsolva",
    "feed.skip_ignored": "nem kell",

    "entities.title": "Entitások és szemantikus hozzárendelés",
    "entities.intro":
      "A rendszer javasol jelentést, de <strong>csak a megerősített hozzárendelést " +
      "használja</strong> — a téves jelentés téves riasztást szülne. Add meg a szobát, " +
      "aztán mentsd el.",
    "entities.ready": "{count} entitás megerősítve — a tanulás indulhat.",
    "entities.not_ready":
      "Jelenleg <strong>{count}</strong> megerősített; a megbízható működéshez " +
      "legalább 5 ajánlott.",
    "entities.col_entity": "Entitás",
    "entities.col_state": "Állapot",
    "entities.col_meaning": "Jelentés",
    "entities.col_room": "Szoba",
    "entities.col_device": "Eszköz",
    "entities.col_send": "Küldés",
    "entities.col_note": "Jegyzet",
    "entities.col_status": "Állapot",
    "entities.tab_empty": "Ebből a típusból nincs entitás.",
    "entities.tab_ignored": "Nem kell",
    "entities.group_unknown": "Nem felismert",
    "entities.not_needed": "Nem kell",
    "entities.not_needed_hint":
      "Átteszi a „Nem kell” fülre. Ott nem lesz útban, és sosem dolgozzuk fel.",
    "entities.restore": "Visszavétel",
    "entities.send_hint": "Ennek az entitásnak az adata menjen a backendnek",
    "entities.note_hint": "A saját jegyzeted — helyben marad, sosem megy a felhőbe",
    "entities.note_placeholder": "pl. a TV mögött, elemes",
    "entities.room_placeholder": "pl. hálószoba",
    "entities.loading": "Betöltés…",
    "entities.empty": "Nem találtunk entitást. Csatlakozik a Home Assistant?",
    "entities.save": "Mentés",
    "entities.error": "Hiba",
    "entities.load_failed": "Nem sikerült betölteni az entitásokat.",
    "entities.suggestion": "javaslat",
    "entities.sent": "felküldve",
    "entities.skipped": "kihagyva",
  },
};

const STORAGE_KEY = "eldercare_lang";
const CHOSEN_KEY = "eldercare_lang_chosen";

/** "hu-HU" → "hu". Home Assistant and browsers both send regional variants. */
function normalise(code) {
  if (!code) return "";
  const base = String(code).toLowerCase().split(/[-_]/)[0];
  return TRANSLATIONS[base] ? base : "";
}

let currentLang = "en";
try {
  currentLang = normalise(localStorage.getItem(STORAGE_KEY))
    || normalise(navigator.language)
    || "en";
} catch (e) { /* private mode: stay with the default */ }

/**
 * Adopt Home Assistant's language.
 *
 * Only when the user has not chosen one here. An explicit choice must survive
 * a reload — otherwise the picker would look broken: you set English, the next
 * status poll would silently switch you back to Hungarian.
 */
function applyHomeAssistantLanguage(code) {
  const lang = normalise(code);
  if (!lang || lang === currentLang) return;
  try {
    if (localStorage.getItem(CHOSEN_KEY)) return;
  } catch (e) { /* private mode: no stored choice to respect */ }
  setLanguage(lang, { explicit: false });
}

/** Translate a key. Missing keys fall back to English, then to the key itself. */
function t(key, vars) {
  let text =
    TRANSLATIONS[currentLang]?.[key] ?? TRANSLATIONS.en[key] ?? key;
  if (vars) {
    for (const [name, value] of Object.entries(vars)) {
      text = text.replaceAll(`{${name}}`, String(value));
    }
  }
  return text;
}

function setLanguage(lang, options) {
  if (!TRANSLATIONS[lang]) return;
  currentLang = lang;
  try {
    localStorage.setItem(STORAGE_KEY, lang);
    // Only a click on the picker counts as a choice. Following Home Assistant
    // must not look like the user decided — otherwise we could never go back
    // to following it.
    if (!options || options.explicit !== false) {
      localStorage.setItem(CHOSEN_KEY, "1");
    }
  } catch (e) { /* private mode */ }
  document.documentElement.lang = lang;
  applyTranslations();
  document.dispatchEvent(new CustomEvent("languagechange"));
}

function getLanguage() { return currentLang; }

/** Replace the content of every [data-i18n] element. */
function applyTranslations(root) {
  const scope = root || document;
  scope.querySelectorAll("[data-i18n]").forEach((el) => {
    el.innerHTML = t(el.dataset.i18n);
  });
  scope.querySelectorAll("[data-i18n-title]").forEach((el) => {
    el.title = t(el.dataset.i18nTitle);
  });
  scope.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
    el.placeholder = t(el.dataset.i18nPlaceholder);
  });
}
