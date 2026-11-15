// Drives one demo call: compile -> ring -> accept -> speak/listen loop -> end.
//
// Turn-taking is half-duplex: the microphone is off while the caller speaks, because laptop
// speakers bleed into the mic and the recognizer would transcribe the caller. Typing while the
// caller is talking is a barge-in: synthesis stops and an `interrupt` frame carries what was heard.

import { OfflineEngine, SocketEngine, fixtureCall, fixtureScript, type CallEngine } from "./engine";
import type { CallScript, CompileResponse, Language, SafeWordFrame } from "./protocol";
import { Recognizer, Speaker, speechRecognitionSupported } from "./speech";

export type Phase = "idle" | "compiling" | "ringing" | "connecting" | "caller" | "listening" | "thinking" | "ended";
export type Mode = "live" | "fixture" | "offline";

export interface Line {
  speaker: "caller" | "you";
  text: string;
  interrupted?: boolean;
}

export interface SafetySetup {
  safeWord: string;
  name: string;
  location: { latitude: number; longitude: number; accuracy: number } | null;
}

export interface CallState {
  phase: Phase;
  mode: Mode | null;
  script: CallScript | null;
  excuse: string;
  lines: Line[];
  interim: string;
  callerTurns: number;
  startedAt: number | null;
  endedAt: number | null;
  endReason: "completed" | "hung-up" | "declined" | null;
  micAvailable: boolean;
  micError: string | null;
  notice: string | null;
  firstAudioMs: number[];
  safeWordArmed: boolean;
  alertPreview: SafeWordFrame | null;
}

const INITIAL: CallState = {
  phase: "idle",
  mode: null,
  script: null,
  excuse: "",
  lines: [],
  interim: "",
  callerTurns: 0,
  startedAt: null,
  endedAt: null,
  endReason: null,
  micAvailable: false,
  micError: null,
  notice: null,
  firstAudioMs: [],
  safeWordArmed: false,
  alertPreview: null,
};

// Only for the `?preview=safeword` screenshot; real previews come from the server's compose_alert.
const SAMPLE_MAP = "https://www.openstreetmap.org/?mlat=40.41678&mlon=-3.70379#map=17/40.41678/-3.70379";
const SAMPLE_ALERT = [
  "Mari ha dicho su palabra de seguridad durante una llamada a las 22:42 (Europe/Madrid). Te eligió como la persona a quien avisar.",
  `Última ubicación que compartió, a las 22:30, con un margen de unos 18 m: ${SAMPLE_MAP}`,
  "Este es un mensaje automático del bot I'm Busy. No es un servicio de emergencias y no se ha avisado a nadie más. Si te preocupa, llama a Mari. Si crees que está en peligro, llama al número de emergencias (112).",
].join("\n\n");

const SPANISH_HINTS = /[ñáéíóú¿¡]|\b(que|tía|tío|mamá|papá|del|curro|llama|tiene|jefa?)\b/i;

export function guessLanguage(text: string): Language {
  return SPANISH_HINTS.test(text) ? "es-ES" : "en-US";
}

export class CallController {
  private state: CallState = { ...INITIAL, micAvailable: speechRecognitionSupported() };
  private listeners = new Set<() => void>();
  private engine: CallEngine | null = null;
  private speaker: Speaker | null = null;
  private recognizer: Recognizer | null = null;
  private serverEnded = false;
  private speaking = false;
  private turnStartedAt = 0;
  private promptAt: number | null = null;
  private token: string | null = null;
  // Set from the Safe word panel. Kept only in this page's memory; sent with the next excuse.
  safety: SafetySetup | null = null;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = (): CallState => this.state;

  private set(patch: Partial<CallState>): void {
    this.state = { ...this.state, ...patch };
    this.listeners.forEach((l) => l());
  }

  async compile(excuse: string): Promise<void> {
    const text = excuse.trim();
    if (text.length < 3 || this.state.phase === "compiling") return;
    this.reset();
    this.set({ phase: "compiling", excuse: text });
    const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    let body: CompileResponse | null = null;
    try {
      const response = await fetch("/demo/compile", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text,
          timezone,
          ...(this.safety ? { safeWord: this.safety.safeWord, name: this.safety.name, location: this.safety.location } : {}),
        }),
      });
      if (response.status === 429) {
        this.set({ phase: "idle", notice: "That's enough calls for this hour. Try again later." });
        return;
      }
      if (response.status === 422) {
        const detail = ((await response.json()) as { detail?: string }).detail;
        this.set({ phase: "idle", notice: detail ?? "That safe word cannot be used." });
        return;
      }
      if (response.ok) body = (await response.json()) as CompileResponse;
    } catch {
      body = null;
    }
    if (body === null) {
      // No backend (static preview): play the scripted call entirely in the browser.
      const lang = guessLanguage(text);
      this.set({
        phase: "ringing",
        mode: "offline",
        script: fixtureScript(lang),
        excuse: fixtureCall(lang).excuse,
        notice: this.safety ? "The safe word needs the demo server; this offline preview plays the call without it." : null,
      });
      return;
    }
    if (body.status === "refused") {
      this.set({ phase: "idle", notice: body.message });
      return;
    }
    this.token = body.token;
    this.set({ phase: "ringing", mode: body.mode, script: body.script, safeWordArmed: body.safeWordArmed });
  }

  accept(): void {
    const script = this.state.script;
    if (this.state.phase !== "ringing" || script === null) return;
    const lang = script.language;
    this.speaker = new Speaker(lang, {
      onStart: () => this.onSpeechStart(),
      onIdle: () => this.onSpeechIdle(),
    });
    if (this.state.micAvailable) {
      this.recognizer = new Recognizer(lang, {
        onInterim: (text) => this.set({ interim: text }),
        onFinal: (text) => this.say(text),
        onError: (error) =>
          this.set({ micError: error === "not-allowed" ? "Microphone blocked. You can type instead." : `Microphone: ${error}` }),
      });
    }
    this.engine = this.state.mode === "offline" || this.token === null ? new OfflineEngine(lang) : new SocketEngine(this.token);
    this.set({ phase: "connecting", startedAt: Date.now(), lines: [] });
    this.engine.connect({
      onToken: (token, last) => this.onToken(token, last),
      onEnd: () => this.onServerEnd(),
      onError: (message) => this.set({ notice: message }),
      onSafeWord: (frame) => this.set({ alertPreview: frame }),
    });
  }

  decline(): void {
    this.set({ phase: "ended", endReason: "declined", endedAt: Date.now() });
  }

  /** The user's side of the call, spoken (recognizer) or typed. */
  say(raw: string): void {
    const text = raw.trim();
    const { phase, script } = this.state;
    if (!text || script === null || this.engine === null) return;
    if (!["caller", "listening", "thinking", "connecting"].includes(phase)) return;
    if (this.speaking || phase === "caller") {
      const heard = this.speaker?.cancel() ?? "";
      this.engine.interrupt(heard, performance.now() - this.turnStartedAt);
      this.speaking = false;
      this.markLastCallerInterrupted(heard);
    }
    this.recognizer?.stop();
    this.promptAt = performance.now();
    this.speaker?.resetTurn();
    this.set({ phase: "thinking", interim: "", lines: [...this.state.lines, { speaker: "you", text }] });
    this.engine.prompt(text, script.language);
  }

  suggestedReply(): string | null {
    if (this.state.script === null) return null;
    const userLines = fixtureCall(this.state.script.language).user_lines;
    const answered = this.state.lines.filter((l) => l.speaker === "you").length;
    return userLines[answered] ?? null;
  }

  /** Static snapshots of the fixture call for screenshots: `?preview=ringing|in-call|ended`. */
  showPreview(kind: string): void {
    const lang: Language = "es-ES";
    const call = fixtureCall(lang);
    const script = fixtureScript(lang);
    const startedAt = Date.now() - 23_000;
    const lines: Line[] = [
      { speaker: "caller", text: call.draft.opening_line },
      { speaker: "you", text: call.user_lines[0] ?? "" },
      { speaker: "caller", text: call.lines.hook },
      { speaker: "you", text: call.user_lines[1] ?? "" },
      { speaker: "caller", text: call.lines.escalate },
    ];
    const base = { script, excuse: call.excuse, mode: null };
    if (kind === "ringing") this.set({ ...base, phase: "ringing" });
    if (kind === "in-call") this.set({ ...base, phase: "listening", lines, callerTurns: 3, startedAt });
    const sampleSafety: SafetySetup = {
      safeWord: "flamenco rosa",
      name: "Mari",
      location: { latitude: 40.416775, longitude: -3.70379, accuracy: 18 },
    };
    if (kind === "safeword-setup") this.safety = sampleSafety;
    if (kind === "safeword") {
      this.safety = sampleSafety;
      const safeLines: Line[] = [
        ...lines,
        { speaker: "you", text: "Uy. Flamenco rosa. ¿Y qué hacemos?" },
        { speaker: "caller", text: call.lines.wrap_up },
      ];
      this.set({
        ...base,
        phase: "listening",
        lines: safeLines,
        callerTurns: 4,
        startedAt,
        safeWordArmed: true,
        alertPreview: { type: "safeword", text: SAMPLE_ALERT, mapUrl: SAMPLE_MAP },
      });
    }
    if (kind === "ended")
      this.set({ ...base, phase: "ended", endReason: "completed", callerTurns: 5, startedAt: startedAt - 25_000, endedAt: Date.now() });
  }

  hangUp(): void {
    this.teardown();
    this.set({ phase: "ended", endReason: "hung-up", endedAt: Date.now() });
  }

  reset(): void {
    this.teardown();
    this.token = null;
    this.serverEnded = false;
    this.state = { ...INITIAL, micAvailable: this.state.micAvailable };
    this.listeners.forEach((l) => l());
  }

  private onToken(token: string, last: boolean): void {
    const lines = [...this.state.lines];
    const previous = lines[lines.length - 1];
    if (previous === undefined || previous.speaker !== "caller" || this.state.phase !== "caller") {
      lines.push({ speaker: "caller", text: token.trimStart() });
      this.turnStartedAt = performance.now();
    } else {
      lines[lines.length - 1] = { ...previous, text: previous.text + token };
    }
    this.speaker?.push(token);
    const patch: Partial<CallState> = { phase: "caller", lines };
    if (last) {
      this.speaker?.flush();
      patch.callerTurns = this.state.callerTurns + 1;
    }
    this.set(patch);
  }

  private onSpeechStart(): void {
    this.speaking = true;
    if (this.promptAt !== null) {
      const ms = performance.now() - this.promptAt;
      this.promptAt = null;
      this.engine?.metric(this.state.callerTurns + 1, ms);
      this.set({ firstAudioMs: [...this.state.firstAudioMs, ms] });
    }
  }

  private onSpeechIdle(): void {
    this.speaking = false;
    if (this.serverEnded) {
      this.finish();
      return;
    }
    if (this.state.phase === "caller") {
      this.set({ phase: "listening" });
      this.recognizer?.listen();
    }
  }

  private onServerEnd(): void {
    this.serverEnded = true;
    if (!this.speaking && this.state.phase !== "ended") this.finish();
  }

  private finish(): void {
    this.teardown();
    this.set({ phase: "ended", endReason: "completed", endedAt: Date.now() });
  }

  private markLastCallerInterrupted(heard: string): void {
    const lines = [...this.state.lines];
    for (let i = lines.length - 1; i >= 0; i--) {
      const line = lines[i];
      if (line?.speaker === "caller") {
        lines[i] = { ...line, text: heard || line.text, interrupted: true };
        break;
      }
    }
    this.set({ lines });
  }

  private teardown(): void {
    this.engine?.close();
    this.engine = null;
    this.speaker?.cancel();
    this.speaker = null;
    this.recognizer?.stop();
    this.recognizer = null;
    this.speaking = false;
  }
}
