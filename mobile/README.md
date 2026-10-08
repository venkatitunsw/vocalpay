# VocalPay — offline Android build

A standalone APK of VocalPay that needs no server, no internet connection, and
no backend at all once installed. Everything the FastAPI backend (`main.py`
and friends, one level up) used to do is ported here to run **on the phone**:
a JS engine against an on-device database, a local Whisper model for voice,
and the device's own contacts for payees.

This is a fork of `frontend/`, not a live mirror of it. Changes to the
server app's `frontend/` do **not** automatically appear here — see
"Keeping this in sync" below.

## What runs on-device

| Piece | How |
|---|---|
| Payments, BPAY, PayID directory, confirmations, audit log | `www/offline/offline-engine.js` — a JS port of `main.py` + its repo modules, against IndexedDB (`www/offline/idb.js`) instead of Postgres |
| Contacts | `@capacitor-community/contacts` reads the phone's own address book (Setup → "Import from phone contacts") |
| Voice-to-text | Whisper (`Xenova/whisper-base.en`, quantized) via `@xenova/transformers`, running through ONNX Runtime Web (WASM) — genuine on-device inference, not a server call |
| UI, Private mode, themes, BPAY, display settings | Unchanged from `frontend/` |

**Stripe is mocked.** A real payment needs Stripe's *secret* key, which can
never ship inside an app — anyone could extract it and move money through
your account. So `/pay/execute`, `/payees/add_contact` and friends generate
fake `acct_offline_…` / `pi_offline_…` IDs instead of calling Stripe. No
money moves, ever, in this build. BPAY settlement was already simulated in
the server app (no real BPAY test network exists) and stays that way here.

**Not ported** — calling these returns a clear "not available offline"
message rather than silently doing the wrong thing:
- Passkeys / WebAuthn (`/webauthn/*`, `/confirm/passkey`) — PIN step-up still
  works.
- Recurring payments (`/recurring/*`).
- Service-provider bills (`/services/*`).
- The support chatbot's actual LLM reasoning (`/support/chat`) — it replies
  with a static "not available offline" message. (It needed Ollama, which
  doesn't run on a phone.) Everything else — payments, PayIDs, BPAY — works
  without it.

## What was verified

- The APK **builds and installs successfully** (`mobile/dist/VocalPay-debug.apk`,
  ~60 MB, built with `gradlew assembleDebug`).
- The offline engine's response shapes were checked by hand against
  `main.py`'s route handlers field-by-field.
- The Whisper model files are confirmed present inside the built APK
  (`unzip -l`), not just in the source tree.

**What was *not* verified**: no Android emulator or physical device was
available in this environment, so the app has never actually been run — no
screenshots, no confirmed voice transcription, no confirmed contacts
permission flow, no confirmed payment walkthrough on-device. Install it on a
real phone (see below) and tell me what breaks; given the size of this port,
something will need a fix on first real run.

## Installing the APK

```
adb install mobile/dist/VocalPay-debug.apk
```

Or copy `VocalPay-debug.apk` to the phone and open it (enable "install from
unknown sources" for your file manager/browser once, since it isn't
signed for the Play Store — this is a debug build).

## Rebuilding

**The project must be built from a path with no spaces in it.** Gradle on
Windows has a long-standing bug resolving dependencies when the project path
contains a space (`java.io.IOException: Invalid file path` at
`compileDebugJavaWithJavac`) — and this repo lives under "Text based Bot".
A junction/symlink does **not** work around this; Java resolves it back to
the real, spaced path. The fix used here: a real copy at `C:\vocalpay-mobile`.

```powershell
# 1. From the real project, after editing www/ or android/:
cd "<repo>\mobile"
npx cap sync android

# 2. Copy to the space-free build location (first time: copy everything;
#    after that, node_modules/android don't need re-copying unless you
#    installed a new npm package or touched android/ directly):
robocopy "<repo>\mobile" "C:\vocalpay-mobile" /E /NFL /NDL /NJH /NJS /NC /NS /NP
# (robocopy's exit code 1-7 is normal/success; only 8+ is a real failure)

# 3. Make sure C:\vocalpay-mobile\android\local.properties uses forward
#    slashes (`sdk.dir=C:/Users/you/AppData/Local/Android/Sdk`) — backslashes
#    get mis-parsed by Java's .properties escaping and silently corrupt the
#    path (e.g. "\Users" -> "sers").

# 4. Build:
cd C:\vocalpay-mobile\android
$env:JAVA_HOME = "C:\Program Files\Android\Android Studio\jbr"  # bundled JDK
.\gradlew.bat assembleDebug
# APK: C:\vocalpay-mobile\android\app\build\outputs\apk\debug\app-debug.apk
```

`android/app/build.gradle` also excludes the legacy `kotlin-stdlib-jdk7`/
`-jdk8` artifacts (`configurations.all { exclude ... }`) — the contacts
plugin pulls an older Kotlin stdlib that duplicates classes already in the
newer merged one; without the exclude, `checkDebugDuplicateClasses` fails.

### Refreshing the bundled Whisper model

The model files under `www/models/Xenova/whisper-base.en/` and the ONNX
runtime under `www/vendor/` were downloaded once from Hugging Face
(`https://huggingface.co/Xenova/whisper-base.en/resolve/main/...`, quantized
encoder + merged decoder) and from
`node_modules/@xenova/transformers/dist/`. They're committed as regular
files, not fetched at build or run time — re-download only if you want to
change model/quantization.

## Keeping this in sync with `frontend/`

`mobile/www/{index.html,app.js,voice.js,privacy.js,styles.css}` started as a
copy of `../frontend/`, then diverged:
- `index.html`: local `tailwind.css` instead of the CDN `<script>` tag;
  added the offline engine's `<script>` tags; added the "Import from phone
  contacts" button.
- `app.js`: `api()` now dispatches to `window.VPOffline` instead of
  `fetch()`; added the contacts-import handler.
- `voice.js`: points at the bundled local model/WASM instead of jsDelivr/HF.

A future change to `frontend/` is **not** automatically reflected here —
re-apply it by hand, or re-copy and redo these three diffs.
