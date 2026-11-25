const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const CALL_JS_PATH = path.resolve(__dirname, "..", "static", "call.js");
const CALL_JS_SOURCE = fs.readFileSync(CALL_JS_PATH, "utf-8");

function makeElement(id, initialValue = "") {
  const classes = new Set();
  const attrs = {};
  const listeners = {};
  const children = [];
  const el = {
    id,
    value: initialValue,
    disabled: false,
    textContent: "",
    innerHTML: "",
    style: {},
    scrollHeight: 100,
    scrollTop: 0,
    children,
    classList: {
      add: (...names) => names.forEach((n) => classes.add(n)),
      remove: (...names) => names.forEach((n) => classes.delete(n)),
      contains: (n) => classes.has(n),
    },
    set className(val) {
      classes.clear();
      String(val)
        .split(/\s+/)
        .filter(Boolean)
        .forEach((n) => classes.add(n));
    },
    get className() {
      return Array.from(classes).join(" ");
    },
    setAttribute: (k, v) => {
      attrs[k] = String(v);
    },
    getAttribute: (k) => attrs[k],
    addEventListener: (type, fn) => {
      listeners[type] = listeners[type] || [];
      listeners[type].push(fn);
    },
    dispatch: async (type, eventObj = {}) => {
      const ev = { preventDefault: () => {}, ...eventObj };
      for (const fn of listeners[type] || []) {
        await fn(ev);
      }
    },
    appendChild: (child) => children.push(child),
    querySelector: (selector) => {
      if (selector === ".empty-state") {
        return el.innerHTML.includes("empty-state")
          ? { remove: () => (el.innerHTML = "") }
          : null;
      }
      if (selector === ".bubble-speaker") {
        return children.find((c) => c.className && c.className.includes("bubble-speaker")) || null;
      }
      return null;
    },
  };
  return el;
}

function createHarness(fetchImpl, options = {}) {
  const elements = {
    "state-badge": makeElement("state-badge"),
    "returning-note": makeElement("returning-note"),
    "error-banner": makeElement("error-banner"),
    "voice-note": makeElement("voice-note"),
    "mic-btn": makeElement("mic-btn"),
    "mic-hint": makeElement("mic-hint"),
    "language-select": makeElement("language-select", options.language || "en"),
    "phone-input": makeElement("phone-input", ""),
    "start-btn": makeElement("start-btn"),
    "end-btn": makeElement("end-btn"),
    "text-form": makeElement("text-form"),
    "text-input": makeElement("text-input", ""),
    "send-btn": makeElement("send-btn"),
    "sentiment-value": makeElement("sentiment-value"),
    "score-value": makeElement("score-value"),
    "score-track": makeElement("score-track"),
    "score-fill": makeElement("score-fill"),
    "transcript-list": makeElement("transcript-list"),
    "summary-card": makeElement("summary-card"),
    "summary-sentiment": makeElement("summary-sentiment"),
    "summary-text": makeElement("summary-text"),
    "summary-objections": makeElement("summary-objections"),
    "summary-signals": makeElement("summary-signals"),
    "summary-next-action": makeElement("summary-next-action"),
    "summary-score": makeElement("summary-score"),
  };

  const recognitionHistory = [];
  class MockRecognition {
    constructor() {
      this.lang = "en-IN";
      this.startCount = 0;
      this.abortCount = 0;
      recognitionHistory.push(this);
    }
    start() {
      this.startCount += 1;
    }
    abort() {
      this.abortCount += 1;
    }
    stop() {
      throw new Error("stop() should not be called when discarding");
    }
  }

  const spokenUtterances = [];
  let cancelCount = 0;
  const voices = options.voices || [{ lang: "en-IN", name: "English India" }];
  const speechSynthesis = {
    getVoices: () => voices,
    cancel: () => {
      cancelCount += 1;
    },
    speak: (utt) => {
      spokenUtterances.push(utt);
    },
  };

  const timers = new Map();
  let nextTimerId = 1;
  const documentMock = {
    getElementById: (id) => elements[id],
    createElement: (tag) => makeElement(tag),
  };

  const context = {
    document: documentMock,
    fetch: fetchImpl,
    SpeechRecognition: MockRecognition,
    webkitSpeechRecognition: MockRecognition,
    SpeechSynthesisUtterance: function Utterance(text) {
      this.text = text;
    },
    speechSynthesis,
    setTimeout: (fn, ms) => {
      const id = nextTimerId++;
      timers.set(id, { fn, ms });
      return id;
    },
    clearTimeout: (id) => {
      timers.delete(id);
    },
    console,
    Boolean,
    Number,
    String,
    Array,
    Math,
  };
  context.window = context;

  vm.runInNewContext(CALL_JS_SOURCE, context, { filename: "call.js" });

  return {
    elements,
    recognition: recognitionHistory[0],
    spokenUtterances,
    timers,
    getCancelCount: () => cancelCount,
    flushWatchdogs: () => {
      for (const [id, entry] of Array.from(timers.entries())) {
        timers.delete(id);
        entry.fn();
      }
    },
  };
}

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  };
}

async function run() {
  // 1. Stale callbacks, manual pause, teardown race, and Hinglish -> en-IN mapping
  {
    const fetchCalls = [];
    const h = createHarness(
      async (url, opts) => {
        const body = JSON.parse(opts.body);
        fetchCalls.push({ url, body });
        if (url === "/start_call") {
          return jsonResponse(200, { session_id: "s1", greeting: "Hello!", language: "hinglish" });
        }
        return jsonResponse(200, {
          reply: "Pro is 2499",
          sentiment: "positive",
          lead_score: 60,
          intent: "enquiry",
          language: "hinglish",
        });
      },
      { language: "hinglish" }
    );

    await h.elements["start-btn"].dispatch("click");
    assert.equal(h.spokenUtterances.length, 1);
    h.spokenUtterances[0].onend();

    // Verify Hinglish sets recognition.lang to en-IN and started mic once
    assert.equal(h.recognition.lang, "en-IN");
    assert.equal(h.recognition.startCount, 1);

    // Save reference to first attempt's onresult/onend, then manually pause mic
    const staleOnResult = h.recognition.onresult;
    const attempt1OnEnd = h.recognition.onend;
    await h.elements["mic-btn"].dispatch("click");
    assert.equal(h.recognition.abortCount, 1);

    // Stale recognition result after manual pause must NOT submit /turn
    staleOnResult({
      resultIndex: 0,
      results: [{ isFinal: true, 0: { transcript: "Ghost transcript" } }],
    });
    assert.equal(fetchCalls.filter((c) => c.url === "/turn").length, 0);

    // Clicking mic to resume BEFORE attempt1OnEnd fires must wait for teardown before calling start()
    await h.elements["mic-btn"].dispatch("click");
    assert.equal(h.recognition.startCount, 1, "Must not start new recognition before teardown finishes");
    attempt1OnEnd();
    assert.equal(h.recognition.startCount, 2, "Must start after teardown onend finishes");
  }

  // 2. Automatic goodbye: locks End Call during speech, watchdog cancels stalled speech and auto-calls /end_call
  {
    const fetchCalls = [];
    const h = createHarness(async (url) => {
      fetchCalls.push(url);
      if (url === "/start_call") {
        return jsonResponse(200, { session_id: "s2", greeting: "Hi!", language: "en" });
      }
      if (url === "/turn") {
        return jsonResponse(200, {
          reply: "Goodbye!",
          sentiment: "positive",
          lead_score: 75,
          intent: "end_call",
          language: "en",
        });
      }
      return jsonResponse(200, {
        summary: "Ended cleanly.",
        objections: [],
        buying_signals: ["Booked"],
        next_best_action: "None",
        overall_sentiment: "positive",
        final_score: 75,
      });
    });

    await h.elements["start-btn"].dispatch("click");
    h.spokenUtterances[0].onend();

    h.elements["text-input"].value = "Bye";
    await h.elements["text-form"].dispatch("submit");
    await new Promise((r) => setTimeout(r, 0));

    // Manual End Call must be disabled while goodbye speech is playing
    assert.equal(h.elements["end-btn"].disabled, true);
    assert.equal(h.spokenUtterances.length, 2);

    // Simulate stalled speech firing the watchdog -> cancels speech and auto-finalizes once
    const cancelsBefore = h.getCancelCount();
    h.flushWatchdogs();
    // Stale utterance onend firing afterwards must be ignored (no duplicate /end_call)
    h.spokenUtterances[1].onend();
    await new Promise((r) => setTimeout(r, 10));

    assert.ok(h.getCancelCount() > cancelsBefore);
    assert.deepEqual(fetchCalls, ["/start_call", "/turn", "/end_call"]);
    assert.equal(h.elements["summary-card"].classList.contains("hidden"), false);
  }

  // 3. Failed /end_call retains sessionId and allows retry without hiding summary prematurely
  {
    let endAttempts = 0;
    const h = createHarness(async (url) => {
      if (url === "/start_call") {
        return jsonResponse(200, { session_id: "s3", greeting: "Hi!", language: "en" });
      }
      endAttempts += 1;
      if (endAttempts === 1) {
        return jsonResponse(500, { detail: "Temporary error" });
      }
      return jsonResponse(200, {
        summary: "Recovered summary.",
        objections: [],
        buying_signals: [],
        next_best_action: "Follow up",
        overall_sentiment: "neutral",
        final_score: 50,
      });
    });

    await h.elements["start-btn"].dispatch("click");
    h.spokenUtterances[0].onend();

    await h.elements["end-btn"].dispatch("click");
    assert.equal(h.elements["summary-card"].classList.contains("hidden"), true);
    assert.equal(h.elements["end-btn"].disabled, false, "End Call must remain enabled to retry");

    await h.elements["end-btn"].dispatch("click");
    assert.equal(endAttempts, 2);
    assert.equal(h.elements["summary-card"].classList.contains("hidden"), false);
    assert.equal(h.elements["summary-text"].textContent, "Recovered summary.");
  }

  // 4. HTTP 429 disables turns while keeping End Call available; failed turn restores draft
  {
    let turnNum = 0;
    const h = createHarness(async (url) => {
      if (url === "/start_call") {
        return jsonResponse(200, { session_id: "s4", greeting: "Hi!", language: "en" });
      }
      turnNum += 1;
      if (turnNum === 1) {
        return jsonResponse(500, { detail: "Server hiccup" });
      }
      return jsonResponse(429, { detail: "Too many turns" });
    });

    await h.elements["start-btn"].dispatch("click");
    h.spokenUtterances[0].onend();

    // First failed turn restores text draft and marks bubble failed
    h.elements["text-input"].value = "Retry me";
    await h.elements["text-form"].dispatch("submit");
    await new Promise((r) => setTimeout(r, 0));
    assert.equal(h.elements["text-input"].value, "Retry me");
    const bubbles = h.elements["transcript-list"].children;
    assert.ok(bubbles[bubbles.length - 1].classList.contains("bubble-failed"));

    // HTTP 429 disables further turns while keeping End Call enabled
    await h.elements["text-form"].dispatch("submit");
    await new Promise((r) => setTimeout(r, 0));
    assert.equal(h.elements["text-input"].disabled, true);
    assert.equal(h.elements["send-btn"].disabled, true);
    assert.equal(h.elements["mic-btn"].disabled, true);
    assert.equal(h.elements["end-btn"].disabled, false);
  }

  console.log("ALL CALL UI TESTS PASSED (4/4)");
}

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
