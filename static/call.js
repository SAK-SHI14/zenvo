/*
 * Zenvo Voice Call Client
 * Plain vanilla JavaScript with zero external libraries.
 * Talks to three FastAPI endpoints: /start_call, /turn, and /end_call.
 */

// Grab references to every element we need to update on the page.
const stateBadge = document.getElementById("state-badge");
const returningNote = document.getElementById("returning-note");
const errorBanner = document.getElementById("error-banner");
const voiceNote = document.getElementById("voice-note");
const micBtn = document.getElementById("mic-btn");
const micHint = document.getElementById("mic-hint");
const languageSelect = document.getElementById("language-select");
const phoneInput = document.getElementById("phone-input");
const startBtn = document.getElementById("start-btn");
const endBtn = document.getElementById("end-btn");
const textForm = document.getElementById("text-form");
const textInput = document.getElementById("text-input");
const sendBtn = document.getElementById("send-btn");
const sentimentValue = document.getElementById("sentiment-value");
const scoreValue = document.getElementById("score-value");
const scoreTrack = document.getElementById("score-track");
const scoreFill = document.getElementById("score-fill");
const transcriptList = document.getElementById("transcript-list");
const summaryCard = document.getElementById("summary-card");
const summarySentiment = document.getElementById("summary-sentiment");
const summaryText = document.getElementById("summary-text");
const summaryObjections = document.getElementById("summary-objections");
const summarySignals = document.getElementById("summary-signals");
const summaryNextAction = document.getElementById("summary-next-action");
const summaryScore = document.getElementById("summary-score");

// Browser SpeechRecognition API (Chrome exposes it as webkitSpeechRecognition).
const SpeechRecognitionAPI = window.SpeechRecognition || window.webkitSpeechRecognition || null;

// Map each supported UI language to its browser speech-recognition locale code.
const RECOGNITION_LANG_MAP = {
  en: "en-IN",
  hi: "hi-IN",
  hinglish: "en-IN",
};

// Track call ownership, mic teardown, speech playback, and error-recovery state.
let sessionId = null;
let callActive = false;
let callToken = 0;
let isRequestPending = false;
let isSpeaking = false;
let pendingAutoEnd = false;
let manualMicPaused = false;
let micPermissionDenied = false;
let turnLimitReached = false;

let recognition = null;
let recognitionAttemptId = 0;
let activeRecognitionId = 0;
let latestStartedRecognitionId = 0;
let isRecognizing = false;
let pendingRestartAfterTeardown = false;

let speechAttemptId = 0;
let speechWatchdogTimer = null;
let availableVoices = [];

// Show or clear the visible error banner in plain English.
function showError(message) {
  if (!message) {
    errorBanner.textContent = "";
    errorBanner.classList.add("hidden");
    return;
  }
  errorBanner.textContent = message;
  errorBanner.classList.remove("hidden");
}

// Show or clear a neutral status note (such as missing TTS voice).
function showVoiceNote(message) {
  if (!message) {
    voiceNote.textContent = "";
    voiceNote.classList.add("hidden");
    return;
  }
  voiceNote.textContent = message;
  voiceNote.classList.remove("hidden");
}

// Single decision helper checking whether the microphone is allowed to start or resume.
function canResumeListening() {
  return (
    callActive &&
    Boolean(SpeechRecognitionAPI) &&
    !isRequestPending &&
    !isSpeaking &&
    !pendingAutoEnd &&
    !manualMicPaused &&
    !micPermissionDenied &&
    !turnLimitReached
  );
}

// Update the visible state pill (Idle, Listening, Thinking..., Speaking) and button states.
function setCallState(mode) {
  stateBadge.className = "state-badge";
  micBtn.classList.remove("is-listening");

  if (mode === "listening") {
    stateBadge.textContent = "Listening";
    stateBadge.classList.add("state-listening");
    micBtn.classList.add("is-listening");
    micHint.textContent = "Listening now... speak naturally, or click mic to pause";
  } else if (mode === "thinking") {
    stateBadge.textContent = "Thinking...";
    stateBadge.classList.add("state-thinking");
    micHint.textContent = "Thinking...";
  } else if (mode === "speaking") {
    stateBadge.textContent = "Speaking";
    stateBadge.classList.add("state-speaking");
    micHint.textContent = "Agent is speaking (mic paused so it cannot hear itself)";
  } else {
    stateBadge.textContent = "Idle";
    stateBadge.classList.add("state-idle");
    if (!callActive) {
      micHint.textContent = "Start a call to begin speaking";
    } else if (turnLimitReached) {
      micHint.textContent = "Turn limit reached — click End Call for your summary";
    } else if (manualMicPaused) {
      micHint.textContent = "Microphone paused — click mic to resume or type below";
    } else {
      micHint.textContent = "Click the mic or type a message below";
    }
  }

  refreshButtons();
}

// Enable or disable controls so users cannot double-click or send turns at invalid times.
function refreshButtons() {
  startBtn.disabled = callActive || isRequestPending;
  endBtn.disabled = !callActive || isRequestPending || pendingAutoEnd;
  languageSelect.disabled = callActive || isRequestPending;
  phoneInput.disabled = callActive || isRequestPending;

  const canSendTurn = callActive && !isRequestPending && !isSpeaking && !pendingAutoEnd && !turnLimitReached;
  textInput.disabled = !canSendTurn;
  sendBtn.disabled = !canSendTurn;
  micBtn.disabled = !canSendTurn || !SpeechRecognitionAPI || micPermissionDenied;
}

// Append a user or agent message bubble to the live transcript panel and return the element.
function addTranscriptBubble(speaker, text) {
  const emptyMsg = transcriptList.querySelector(".empty-state");
  if (emptyMsg) {
    emptyMsg.remove();
  }

  const bubble = document.createElement("div");
  bubble.className = speaker === "user" ? "bubble bubble-user" : "bubble bubble-agent";

  const label = document.createElement("span");
  label.className = "bubble-speaker";
  label.textContent = speaker === "user" ? "You" : "Zenvo";

  const body = document.createElement("p");
  body.className = "bubble-text";
  body.textContent = text;

  bubble.appendChild(label);
  bubble.appendChild(body);
  transcriptList.appendChild(bubble);
  transcriptList.scrollTop = transcriptList.scrollHeight;
  return bubble;
}

// Mark a failed user transcript bubble visibly so the caller knows it was not delivered.
function markBubbleFailed(bubble) {
  if (!bubble) {
    return;
  }
  bubble.classList.add("bubble-failed");
  const label = bubble.querySelector(".bubble-speaker");
  if (label) {
    label.textContent = "You (not delivered)";
  }
}

// Update the sentiment badge and animate the 0-100 lead score bar.
function updateMetrics(sentiment, score) {
  const safeSentiment = ["positive", "neutral", "negative"].includes(sentiment) ? sentiment : "neutral";
  sentimentValue.textContent = safeSentiment;
  sentimentValue.className = `sentiment-pill sentiment-${safeSentiment}`;

  const clamped = Math.max(0, Math.min(100, Number(score) || 0));
  scoreValue.textContent = `${clamped} / 100`;
  scoreFill.style.width = `${clamped}%`;
  scoreTrack.setAttribute("aria-valuenow", String(clamped));
}

// Populate and reveal the end-of-call summary card returned by POST /end_call.
function renderSummaryCard(summaryData) {
  summaryText.textContent = summaryData.summary || "Call completed.";

  const safeSentiment = ["positive", "neutral", "negative"].includes(summaryData.overall_sentiment)
    ? summaryData.overall_sentiment
    : "neutral";
  summarySentiment.textContent = safeSentiment;
  summarySentiment.className = `sentiment-pill sentiment-${safeSentiment}`;

  fillSummaryList(summaryObjections, summaryData.objections, "None noted");
  fillSummaryList(summarySignals, summaryData.buying_signals, "None noted");

  summaryNextAction.textContent = summaryData.next_best_action || "Follow up with caller.";
  summaryScore.textContent = `Final Lead Score: ${summaryData.final_score ?? 0} / 100`;
  summaryCard.classList.remove("hidden");
}

function fillSummaryList(listEl, items, fallbackText) {
  listEl.innerHTML = "";
  const values = Array.isArray(items) && items.length > 0 ? items : [fallbackText];
  for (const item of values) {
    const li = document.createElement("li");
    li.textContent = item;
    listEl.appendChild(li);
  }
}

// Load available browser TTS voices (Chrome loads them asynchronously).
function loadVoices() {
  if ("speechSynthesis" in window) {
    availableVoices = window.speechSynthesis.getVoices() || [];
  }
}

if ("speechSynthesis" in window) {
  loadVoices();
  window.speechSynthesis.onvoiceschanged = loadVoices;
}

// Pick the best installed browser voice for en (en-IN), hi (hi-IN), or hinglish (en-IN).
function pickVoiceForLanguage(lang) {
  if (availableVoices.length === 0) {
    loadVoices();
  }
  const byTag = (tag) => availableVoices.find((v) => v.lang && v.lang.toLowerCase() === tag.toLowerCase());
  const byPrefix = (prefix) =>
    availableVoices.find((v) => v.lang && v.lang.toLowerCase().startsWith(prefix.toLowerCase()));

  if (lang === "hi") {
    return byTag("hi-IN") || byPrefix("hi");
  }
  if (lang === "hinglish") {
    return byTag("en-IN") || byPrefix("en") || byTag("hi-IN") || byPrefix("hi");
  }
  return byTag("en-IN") || byPrefix("en");
}

/*
 * IDEA 1: SpeechRecognition (webkitSpeechRecognition) turns speech to text.
 * Each attempt gets a unique callToken and attemptId so stale events from an
 * aborted or ended call are ignored and never submit or restart the mic.
 */
function setupRecognition() {
  if (!SpeechRecognitionAPI) {
    showError("Use Chrome: browser speech recognition is unsupported here. You can type your replies below.");
    return;
  }
  recognition = new SpeechRecognitionAPI();
  recognition.continuous = false;
  recognition.interimResults = true;
  recognition.maxAlternatives = 1;
}

// Start listening on the microphone, waiting for any prior teardown to finish first.
function startListening() {
  if (!canResumeListening() || !recognition) {
    return;
  }
  if (isRecognizing) {
    pendingRestartAfterTeardown = true;
    return;
  }

  showError("");
  pendingRestartAfterTeardown = false;
  recognitionAttemptId += 1;
  const myAttemptId = recognitionAttemptId;
  const myCallToken = callToken;

  activeRecognitionId = myAttemptId;
  latestStartedRecognitionId = myAttemptId;
  isRecognizing = true;

  const chosenLang = languageSelect.value;
  recognition.lang = RECOGNITION_LANG_MAP[chosenLang] || "en-IN";

  recognition.onresult = (event) => {
    if (myCallToken !== callToken || myAttemptId !== activeRecognitionId || !callActive || manualMicPaused) {
      return;
    }

    let finalTranscript = "";
    let interimTranscript = "";
    for (let i = event.resultIndex; i < event.results.length; i += 1) {
      const piece = event.results[i][0].transcript;
      if (event.results[i].isFinal) {
        finalTranscript += piece;
      } else {
        interimTranscript += piece;
      }
    }

    if (interimTranscript) {
      micHint.textContent = `Hearing: "${interimTranscript.trim()}"`;
    }

    const cleaned = finalTranscript.trim();
    if (cleaned) {
      discardListening();
      sendTurn(cleaned);
    }
  };

  recognition.onerror = (event) => {
    if (myAttemptId === latestStartedRecognitionId) {
      isRecognizing = false;
    }
    if (myCallToken !== callToken || myAttemptId !== activeRecognitionId) {
      return;
    }
    activeRecognitionId = 0;

    if (event.error === "not-allowed" || event.error === "service-not-allowed") {
      micPermissionDenied = true;
      showError("Microphone permission denied. Allow mic access in Chrome or type below.");
      setCallState("idle");
      return;
    }
    if (event.error === "no-speech") {
      if (callActive && !isSpeaking && !isRequestPending) {
        setCallState("idle");
        micHint.textContent = "No speech detected — click the mic to speak again or type below.";
      }
      return;
    }
    if (event.error !== "aborted") {
      showError(`Microphone error (${event.error}). You can continue by typing below.`);
    }
    if (!isSpeaking && !isRequestPending) {
      setCallState("idle");
    }
  };

  recognition.onend = () => {
    if (myAttemptId === latestStartedRecognitionId) {
      isRecognizing = false;
    }
    if (myAttemptId === activeRecognitionId) {
      activeRecognitionId = 0;
    }
    if (myCallToken !== callToken) {
      pendingRestartAfterTeardown = false;
      return;
    }
    if (pendingRestartAfterTeardown) {
      pendingRestartAfterTeardown = false;
      if (canResumeListening()) {
        startListening();
        return;
      }
    }
    if (callActive && !isSpeaking && !isRequestPending && !pendingAutoEnd) {
      setCallState("idle");
    }
  };

  try {
    setCallState("listening");
    recognition.start();
  } catch (err) {
    isRecognizing = false;
    activeRecognitionId = 0;
    setCallState("idle");
    showError("Could not start microphone. Try clicking the mic again or typing below.");
  }
}

// Abort any active microphone capture immediately so discarded audio cannot trigger a turn.
function discardListening() {
  activeRecognitionId = 0;
  pendingRestartAfterTeardown = false;
  if (!recognition || !isRecognizing) {
    return;
  }
  try {
    recognition.abort();
  } catch (err) {
    isRecognizing = false;
  }
}

/*
 * IDEA 3 & IDEA 4:
 * IDEA 3: speechSynthesis.speak() reads the agent's reply aloud using an
 *         installed voice matching en-IN or hi-IN (falling back to text + a
 *         small note if no matching voice is installed).
 * IDEA 4: Abort the mic while the agent speaks and restart it only inside one
 *         guarded completion helper shared by onend, onerror, and a watchdog.
 */
function speakReply(text, lang) {
  // IDEA 4 (part 1): Discard microphone listening before speaking.
  discardListening();

  speechAttemptId += 1;
  const mySpeechId = speechAttemptId;
  const myCallToken = callToken;
  let completed = false;

  if (speechWatchdogTimer) {
    clearTimeout(speechWatchdogTimer);
    speechWatchdogTimer = null;
  }

  // Guarded completion helper: runs at most once per utterance and ignores stale calls.
  function completeSpeech(reason) {
    if (completed || mySpeechId !== speechAttemptId || myCallToken !== callToken) {
      return;
    }
    completed = true;
    if (speechWatchdogTimer) {
      clearTimeout(speechWatchdogTimer);
      speechWatchdogTimer = null;
    }
    isSpeaking = false;

    if (reason === "timeout") {
      if ("speechSynthesis" in window) {
        window.speechSynthesis.cancel();
      }
      showVoiceNote("Voice playback stalled — continuing in text mode.");
    } else if (reason === "error") {
      showVoiceNote("Voice playback had an error — continuing in text mode.");
    }

    if (pendingAutoEnd) {
      pendingAutoEnd = false;
      endCall();
      return;
    }

    if (canResumeListening()) {
      // IDEA 4 (part 2): Restart the microphone only after speech ends cleanly.
      startListening();
    } else {
      setCallState("idle");
    }
  }

  if (!("speechSynthesis" in window)) {
    showVoiceNote("Browser voice output is unavailable — showing text replies only.");
    completeSpeech("unavailable");
    return;
  }

  const matchedVoice = pickVoiceForLanguage(lang);
  if (!matchedVoice) {
    showVoiceNote(`No ${lang} voice is installed in this browser — showing text reply only.`);
    completeSpeech("no-voice");
    return;
  }

  showVoiceNote("");
  window.speechSynthesis.cancel();

  const utterance = new SpeechSynthesisUtterance(text);
  utterance.voice = matchedVoice;
  utterance.lang = matchedVoice.lang || RECOGNITION_LANG_MAP[lang] || "en-IN";
  utterance.rate = 1.0;

  isSpeaking = true;
  setCallState("speaking");

  const watchdogMs = Math.max(8000, Math.min(20000, String(text).length * 120 + 4000));
  speechWatchdogTimer = setTimeout(() => completeSpeech("timeout"), watchdogMs);

  utterance.onend = () => completeSpeech("end");
  utterance.onerror = () => completeSpeech("error");

  window.speechSynthesis.speak(utterance);
}

// Start a call via POST /start_call, display returning caller info, and speak the greeting.
async function startCall() {
  if (callActive || isRequestPending) {
    return;
  }

  callToken += 1;
  const myCallToken = callToken;

  showError("");
  showVoiceNote("");
  returningNote.classList.add("hidden");
  summaryCard.classList.add("hidden");
  transcriptList.innerHTML = '<p class="empty-state">Starting call...</p>';
  updateMetrics("neutral", 0);

  manualMicPaused = false;
  turnLimitReached = false;
  pendingAutoEnd = false;
  isSpeaking = false;

  const phone = phoneInput.value.trim();
  const language = languageSelect.value;

  isRequestPending = true;
  setCallState("thinking");

  try {
    const resp = await fetch("/start_call", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        phone: phone || null,
        language,
      }),
    });

    const data = await resp.json().catch(() => ({}));
    if (myCallToken !== callToken) {
      return;
    }

    if (!resp.ok) {
      const detail = data.detail || `Failed to start call (HTTP ${resp.status})`;
      showError(typeof detail === "string" ? detail : "Check phone format (digits and '+' up to 15 chars).");
      callActive = false;
      isRequestPending = false;
      setCallState("idle");
      return;
    }

    sessionId = data.session_id;
    callActive = true;
    isRequestPending = false;
    transcriptList.innerHTML = "";

    if (data.returning_lead) {
      const namePart = data.returning_lead.name ? `, ${data.returning_lead.name}` : "";
      const needPart = data.returning_lead.need ? ` Last noted need: ${data.returning_lead.need}.` : "";
      returningNote.textContent = `Welcome back${namePart}!${needPart}`;
      returningNote.classList.remove("hidden");
    }

    addTranscriptBubble("agent", data.greeting);
    speakReply(data.greeting, data.language || language);
  } catch (err) {
    if (myCallToken !== callToken) {
      return;
    }
    isRequestPending = false;
    callActive = false;
    setCallState("idle");
    showError("Network error while starting call. Check that the server is running.");
  }
}

/*
 * IDEA 2: fetch("/turn") sends user text and gets JSON back.
 * Both the microphone transcript and the fallback text box call this same function.
 * On failure, failed turns restore the draft and mark the user bubble as not delivered.
 */
async function sendTurn(userText) {
  if (!callActive || !sessionId || isRequestPending || turnLimitReached || pendingAutoEnd) {
    return;
  }

  const trimmed = userText.trim();
  if (!trimmed) {
    return;
  }

  const myCallToken = callToken;
  showError("");
  const userBubble = addTranscriptBubble("user", trimmed);
  isRequestPending = true;
  setCallState("thinking");

  const language = languageSelect.value;

  try {
    const resp = await fetch("/turn", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: sessionId,
        text: trimmed,
        language,
      }),
    });

    const data = await resp.json().catch(() => ({}));
    if (myCallToken !== callToken) {
      return;
    }
    isRequestPending = false;

    if (resp.status === 409) {
      markBubbleFailed(userBubble);
      callActive = false;
      sessionId = null;
      discardListening();
      setCallState("idle");
      showError("This call session has already ended (409). Start a new call to continue.");
      return;
    }

    if (resp.status === 429) {
      markBubbleFailed(userBubble);
      turnLimitReached = true;
      discardListening();
      setCallState("idle");
      showError("Turn limit reached for this session (429). Please click End Call for your summary.");
      return;
    }

    if (!resp.ok) {
      markBubbleFailed(userBubble);
      textInput.value = trimmed;
      setCallState("idle");
      const detail = data.detail || `Turn failed (HTTP ${resp.status})`;
      showError(typeof detail === "string" ? detail : "Please enter 1 to 500 characters.");
      return;
    }

    addTranscriptBubble("agent", data.reply);
    updateMetrics(data.sentiment, data.lead_score);

    // If the agent detected end_call, lock End Call while goodbye speaks, then auto-finalize.
    if (data.intent === "end_call") {
      pendingAutoEnd = true;
    }

    speakReply(data.reply, data.language || language);
  } catch (err) {
    if (myCallToken !== callToken) {
      return;
    }
    markBubbleFailed(userBubble);
    textInput.value = trimmed;
    isRequestPending = false;
    setCallState("idle");
    showError("Network error while sending turn. Your message was kept below to retry.");
  }
}

// End the active call via POST /end_call; keep sessionId on failure so the user can retry.
async function endCall() {
  if (!sessionId || isRequestPending) {
    return;
  }

  const myCallToken = callToken;
  discardListening();
  speechAttemptId += 1;
  if (speechWatchdogTimer) {
    clearTimeout(speechWatchdogTimer);
    speechWatchdogTimer = null;
  }
  if ("speechSynthesis" in window) {
    window.speechSynthesis.cancel();
  }
  isSpeaking = false;
  pendingAutoEnd = false;
  isRequestPending = true;
  setCallState("thinking");

  try {
    const resp = await fetch("/end_call", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId }),
    });

    const data = await resp.json().catch(() => ({}));
    if (myCallToken !== callToken) {
      return;
    }
    isRequestPending = false;

    if (!resp.ok) {
      setCallState("idle");
      showError(data.detail || `Could not end call cleanly (HTTP ${resp.status}). Click End Call to retry.`);
      return;
    }

    callToken += 1;
    callActive = false;
    sessionId = null;
    turnLimitReached = false;
    manualMicPaused = false;
    setCallState("idle");
    renderSummaryCard(data);
  } catch (err) {
    if (myCallToken !== callToken) {
      return;
    }
    isRequestPending = false;
    setCallState("idle");
    showError("Network error while ending call. Click End Call to retry.");
  }
}

// Wire up button clicks and the fallback text form.
startBtn.addEventListener("click", startCall);
endBtn.addEventListener("click", endCall);

micBtn.addEventListener("click", () => {
  if (!callActive || isRequestPending || isSpeaking || pendingAutoEnd || turnLimitReached) {
    return;
  }
  if (activeRecognitionId !== 0) {
    manualMicPaused = true;
    discardListening();
    setCallState("idle");
  } else {
    manualMicPaused = false;
    micPermissionDenied = false;
    startListening();
  }
});

textForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = textInput.value.trim();
  if (!text || !callActive || isRequestPending || isSpeaking || pendingAutoEnd || turnLimitReached) {
    return;
  }
  textInput.value = "";
  discardListening();
  sendTurn(text);
});

// Initialize speech recognition and initial UI button states when the page loads.
setupRecognition();
setCallState("idle");
