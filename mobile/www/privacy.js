// Privacy mode, auto-lock, spoken replies and display settings. Stored on this device only.
(() => {
  const PREF_KEY = "vocalpay.prefs";
  const IDLE_LOCK_MS = 120000;
  const TEXT_SCALE = { standard: "100%", large: "118%", xlarge: "136%" };
  const defaults = { privateMode: false, speak: false, highContrast: false, textSize: "standard", dark: false };

  let prefs = { ...defaults };
  let idleTimer = null;
  let locked = false;

  function load() {
    try {
      prefs = { ...defaults, ...JSON.parse(localStorage.getItem(PREF_KEY) || "{}") };
    } catch (err) {
      prefs = { ...defaults };
    }
  }

  function save() {
    try {
      localStorage.setItem(PREF_KEY, JSON.stringify(prefs));
    } catch (err) {
      // Blocked storage means the choice only lasts for this page load.
    }
  }

  // Wraps a value that should be hidden in private mode. Visible normally; blurred and tap-to-reveal when private.
  function sens(html) {
    const tab = prefs.privateMode ? ' tabindex="0" role="button" aria-label="Hidden. Activate to reveal."' : "";
    return `<span class="sens"${tab}>${html}</span>`;
  }

  function applyPrefs() {
    document.body.classList.toggle("private", prefs.privateMode);
    document.documentElement.classList.toggle("hc", prefs.highContrast);
    document.documentElement.classList.toggle("dark", prefs.dark);
    document.documentElement.style.fontSize = TEXT_SCALE[prefs.textSize] || "100%";
    const toggle = document.getElementById("private-toggle");
    if (toggle) {
      toggle.setAttribute("aria-pressed", String(prefs.privateMode));
      toggle.classList.toggle("bg-slate-800", prefs.privateMode);
    }
    document.querySelectorAll(".sens").forEach((el) => {
      if (prefs.privateMode) {
        el.setAttribute("tabindex", "0");
        el.setAttribute("role", "button");
        el.setAttribute("aria-label", "Hidden. Activate to reveal.");
      } else {
        el.removeAttribute("tabindex");
        el.removeAttribute("role");
        el.removeAttribute("aria-label");
        el.classList.remove("revealed");
      }
    });
    if (!prefs.privateMode) unlock();
    resetIdle();
  }

  function set(key, value) {
    prefs[key] = value;
    save();
    applyPrefs();
  }

  function reveal(el) {
    el.classList.toggle("revealed");
  }

  function lock() {
    if (!prefs.privateMode || locked) return;
    locked = true;
    document.querySelectorAll(".sens.revealed").forEach((el) => el.classList.remove("revealed"));
    document.body.classList.add("locked");
    const banner = document.getElementById("lock-banner");
    if (banner) banner.classList.remove("hidden");
  }

  function unlock() {
    locked = false;
    document.body.classList.remove("locked");
    const banner = document.getElementById("lock-banner");
    if (banner) banner.classList.add("hidden");
  }

  function resetIdle() {
    clearTimeout(idleTimer);
    if (prefs.privateMode) idleTimer = setTimeout(lock, IDLE_LOCK_MS);
  }

  // Spoken replies: in private mode the spoken text never includes the hidden details.
  //
  // Android's System WebView (what this app runs in) exposes window.speechSynthesis
  // but on most devices speak() is a silent no-op — the Web Speech Synthesis API
  // isn't actually implemented there, only in the standalone Chrome app. The
  // @capacitor-community/text-to-speech plugin wraps Android's native TextToSpeech
  // service instead, which does work from a WebView. Use it when present (the
  // installed app); fall back to the Web Speech API otherwise (the server build,
  // opened in a real desktop/mobile browser, where it does work).
  function speak(text) {
    if (!prefs.speak) return;
    const line = prefs.privateMode ? "Reply hidden. Reveal it on screen to hear the details." : text;
    const NativeTTS = window.Capacitor?.Plugins?.TextToSpeech;
    if (NativeTTS) {
      NativeTTS.stop().catch(() => {});
      NativeTTS.speak({ text: line }).catch(() => {});
      return;
    }
    if ("speechSynthesis" in window) {
      window.speechSynthesis.cancel();
      window.speechSynthesis.speak(new SpeechSynthesisUtterance(line));
    }
  }

  // Private Mode's thread lock: gates revealing a hidden thread, or paying
  // someone whose thread is hidden, behind the device's own biometric/PIN
  // check. Native via @aparajita/capacitor-biometric-auth, bundled at
  // vendor/biometric-auth.js (see mobile/README.md for how/why) -- loaded
  // lazily, same pattern as voice.js's Whisper model. allowDeviceCredential
  // lets a phone/emulator with no fingerprint or face enrolled fall back to
  // its lock-screen PIN/pattern/password instead of failing outright.
  // Returns false (never throws) on cancellation, failure, or the server
  // build / a plain browser, where there's no native bridge to ask.
  let _biometricAuthPromise = null;
  function _loadBiometricAuth() {
    if (!_biometricAuthPromise) _biometricAuthPromise = import("vendor/biometric-auth.js").then((m) => m.BiometricAuth);
    return _biometricAuthPromise;
  }

  async function authenticateBiometric(reason) {
    if (!window.Capacitor?.isNativePlatform?.()) return false;
    try {
      const BiometricAuth = await _loadBiometricAuth();
      await BiometricAuth.authenticate({ reason: reason || "Unlock this chat", cancelTitle: "Cancel", allowDeviceCredential: true });
      return true;
    } catch (err) {
      return false;
    }
  }

  document.addEventListener("click", (e) => {
    if (locked) {
      unlock();
      resetIdle();
      return;
    }
    const el = e.target.closest(".sens");
    if (el && prefs.privateMode) reveal(el);
    resetIdle();
  });
  document.addEventListener("keydown", (e) => {
    if (locked) {
      unlock();
      resetIdle();
      return;
    }
    if ((e.key === "Enter" || e.key === " ") && e.target.classList && e.target.classList.contains("sens")) {
      e.preventDefault();
      reveal(e.target);
    }
    resetIdle();
  });
  document.addEventListener("pointermove", () => {
    if (!locked) resetIdle();
  });

  load();
  window.VocalPrivacy = {
    get: () => ({ ...prefs }),
    set,
    sens,
    speak,
    applyPrefs,
    isPrivate: () => prefs.privateMode,
    authenticateBiometric,
  };
  document.addEventListener("DOMContentLoaded", applyPrefs);
})();
