// Voice input that runs on the device: Whisper (tiny, English) via transformers.js,
// with the browser's Web Speech API as a fallback. Audio is never uploaded.
// Transcripts only fill the composer; the user still presses send.
(() => {
  const TRANSFORMERS_URL = "https://cdn.jsdelivr.net/npm/@xenova/transformers@2.17.2/+esm";
  const WHISPER_MODEL = "Xenova/whisper-tiny.en";
  const SAMPLE_RATE = 16000;
  const FRAME_MS = 20;
  const MIN_SPEECH_MS = 300;
  const MAX_RECORD_MS = 15000;

  const DIGIT_WORDS = {
    oh: "0", zero: "0", one: "1", two: "2", three: "3", four: "4",
    five: "5", six: "6", seven: "7", eight: "8", nine: "9",
  };
  const DIGIT_RUN_RE = /(?:\b(?:oh|zero|one|two|three|four|five|six|seven|eight|nine)\b[\s,]*){3,}/gi;
  const BRACKETED_TAG_RE = /\[[^\]]*\]|\([^)]*\)/g;

  // Whisper transcribes spoken digits as words ("oh four one two"); turn runs of 3+ back into digits.
  function normalizeSpokenNumbers(text) {
    return text.replace(DIGIT_RUN_RE, (run) =>
      (run.match(/[a-zA-Z]+/g) || []).map((w) => DIGIT_WORDS[w.toLowerCase()]).join("")
    );
  }

  // Whisper's common hallucinations on noise: "[BLANK_AUDIO]", "(music)", or one word repeated in a loop.
  function cleanTranscript(raw) {
    const text = (raw || "").replace(BRACKETED_TAG_RE, " ").replace(/\s+/g, " ").trim();
    if (!text || !/[a-zA-Z]/.test(text)) return "";
    const words = text.toLowerCase().split(" ");
    let run = 1;
    for (let i = 1; i < words.length; i++) {
      run = words[i] === words[i - 1] ? run + 1 : 1;
      if (run >= 5) return "";
    }
    return normalizeSpokenNumbers(text);
  }

  // Keeps only the stretch of the clip that is clearly louder than its quietest 10% of frames.
  // Returns null when there is no real speech, so noise never reaches the model.
  function gateSpeech(samples) {
    const frameLen = Math.round((SAMPLE_RATE * FRAME_MS) / 1000);
    const rms = [];
    for (let i = 0; i + frameLen <= samples.length; i += frameLen) {
      let sum = 0;
      for (let j = i; j < i + frameLen; j++) sum += samples[j] * samples[j];
      rms.push(Math.sqrt(sum / frameLen));
    }
    if (!rms.length) return null;
    const sorted = [...rms].sort((a, b) => a - b);
    const threshold = Math.max(sorted[Math.floor(sorted.length * 0.1)] * 3, 0.01);

    let first = -1;
    let last = -1;
    rms.forEach((v, i) => {
      if (v > threshold) {
        if (first < 0) first = i;
        last = i;
      }
    });
    if (first < 0 || (last - first + 1) * FRAME_MS < MIN_SPEECH_MS) return null;
    return samples.slice(first * frameLen, (last + 1) * frameLen);
  }

  let asrPromise = null;
  function loadWhisper(onProgress) {
    if (!asrPromise) {
      asrPromise = (async () => {
        const transformers = await import(TRANSFORMERS_URL);
        transformers.env.allowLocalModels = false;
        return transformers.pipeline("automatic-speech-recognition", WHISPER_MODEL, {
          progress_callback: (p) => {
            if (p.status === "progress" && onProgress) onProgress(Math.round(p.progress));
          },
        });
      })();
      asrPromise.catch(() => {
        asrPromise = null;
      });
    }
    return asrPromise;
  }

  async function decodeToSamples(blob) {
    const ctx = new AudioContext({ sampleRate: SAMPLE_RATE });
    try {
      const buffer = await ctx.decodeAudioData(await blob.arrayBuffer());
      return buffer.getChannelData(0);
    } finally {
      ctx.close();
    }
  }

  function create({ micBtn, micStatus, textInput, onState }) {
    const webSpeechImpl = window.SpeechRecognition || window.webkitSpeechRecognition;
    const canRecord = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
    let whisperFailed = false;
    let recorder = null;
    let stream = null;
    let chunks = [];
    let autoStop = null;
    let recognizer = null;
    let webSpeechActive = false;

    function setStatus(text) {
      micStatus.textContent = text;
    }

    function fill(text) {
      textInput.value = text;
      textInput.focus();
    }

    function useWebSpeech() {
      if (!webSpeechImpl) return false;
      if (!recognizer) {
        recognizer = new webSpeechImpl();
        recognizer.continuous = false;
        recognizer.interimResults = false;
        recognizer.lang = "en-AU";
        recognizer.onresult = (e) => fill(e.results[0][0].transcript);
        recognizer.onerror = () => setStatus("Couldn't hear that. Try again, or type it.");
        recognizer.onend = () => {
          webSpeechActive = false;
          onState(false);
        };
      }
      recognizer.start();
      webSpeechActive = true;
      onState(true);
      setStatus("Listening (browser speech)…");
      return true;
    }

    async function startWhisperRecording() {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      chunks = [];
      recorder = new MediaRecorder(stream);
      recorder.ondataavailable = (e) => {
        if (e.data.size) chunks.push(e.data);
      };
      recorder.start();
      onState(true);
      setStatus("Listening… press the mic again to stop.");
      autoStop = setTimeout(() => stopAndTranscribe(), MAX_RECORD_MS);
      loadWhisper((pct) => {
        if (recorder) setStatus(`Downloading speech model… ${pct}%`);
      }).catch(() => {
        whisperFailed = true;
      });
    }

    function stopRecorder() {
      return new Promise((resolve) => {
        recorder.onstop = () => {
          stream.getTracks().forEach((t) => t.stop());
          resolve(new Blob(chunks, { type: recorder.mimeType }));
        };
        recorder.stop();
      });
    }

    async function stopAndTranscribe() {
      clearTimeout(autoStop);
      const blob = await stopRecorder();
      recorder = null;
      onState(false);
      setStatus("Transcribing on this device…");
      try {
        const asr = await loadWhisper();
        const samples = gateSpeech(await decodeToSamples(blob));
        if (!samples) {
          setStatus("I didn't catch any speech. Try again, or type it.");
          return;
        }
        const out = await asr(samples);
        const text = cleanTranscript(out.text);
        if (!text) {
          setStatus("I didn't catch that. Try again, or type it.");
          return;
        }
        setStatus("");
        fill(text);
      } catch (err) {
        whisperFailed = true;
        setStatus("Speech model unavailable. Using browser speech next time.");
      }
    }

    micBtn.addEventListener("click", async () => {
      if (webSpeechActive) {
        recognizer.stop();
        return;
      }
      if (recorder) {
        await stopAndTranscribe();
        return;
      }
      setStatus("");
      if (canRecord && !whisperFailed) {
        try {
          await startWhisperRecording();
          return;
        } catch (err) {
          setStatus("Microphone unavailable. Check browser permission.");
          return;
        }
      }
      if (!useWebSpeech()) {
        setStatus("Voice input is not supported in this browser.");
      }
    });

    if (!canRecord && !webSpeechImpl) {
      micBtn.disabled = true;
      micBtn.title = "Voice input not supported in this browser";
      micBtn.classList.add("opacity-40", "cursor-not-allowed");
    }

  }

  window.VocalVoice = { create, normalizeSpokenNumbers, cleanTranscript, gateSpeech };
})();
