// The two ways the page can run a call. Both speak the ConversationRelay-shaped protocol:
// SocketEngine talks to the real CallSession over /demo/ws; OfflineEngine replays the scripted
// fixture in the browser when there is no backend at all (a static preview).

import fixtures from "../fixtures/demo-calls.json";
import { parseServerFrame, type CallScript, type ClientFrame, type Language, type SafeWordFrame } from "./protocol";

export interface EngineEvents {
  onToken(token: string, last: boolean): void;
  onEnd(): void;
  onError(message: string): void;
  onSafeWord(frame: SafeWordFrame): void;
}

export interface CallEngine {
  connect(events: EngineEvents): void;
  prompt(text: string, lang: Language): void;
  interrupt(heard: string, elapsedMs: number): void;
  metric(turn: number, firstAudioMs: number): void;
  close(): void;
}

export class SocketEngine implements CallEngine {
  private socket: WebSocket | null = null;
  private ended = false;

  constructor(private readonly token: string) {}

  connect(events: EngineEvents): void {
    const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(`${scheme}//${window.location.host}/demo/ws?token=${encodeURIComponent(this.token)}`);
    socket.onmessage = (event) => {
      const frame = parseServerFrame(String(event.data));
      if (frame.type === "text") events.onToken(frame.token, frame.last === true);
      else if (frame.type === "safeword") events.onSafeWord(frame);
      else {
        this.ended = true;
        events.onEnd();
      }
    };
    socket.onclose = () => {
      if (!this.ended) {
        this.ended = true;
        events.onEnd();
      }
    };
    socket.onerror = () => events.onError("The connection to the call server failed.");
    this.socket = socket;
  }

  private send(frame: ClientFrame): void {
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify(frame));
  }

  prompt(text: string, lang: Language): void {
    this.send({ type: "prompt", voicePrompt: text, lang, last: true });
  }

  interrupt(heard: string, elapsedMs: number): void {
    this.send({ type: "interrupt", utteranceUntilInterrupt: heard, durationUntilInterruptMs: Math.round(elapsedMs) });
  }

  metric(turn: number, firstAudioMs: number): void {
    this.send({ type: "metric", turn, firstAudioMs: Math.round(firstAudioMs) });
  }

  close(): void {
    this.ended = true;
    this.socket?.close();
  }
}

interface FixtureCall {
  excuse: string;
  draft: Omit<CallScript, "voice_id" | "forbidden" | "max_turns"> & { caller_gender: string; delay_seconds: number | null };
  lines: Record<"hook" | "escalate" | "ask" | "closing" | "wrap_up" | "nudge", string>;
  user_lines: string[];
}

export function fixtureCall(lang: Language): FixtureCall {
  return (fixtures.calls as unknown as Record<Language, FixtureCall>)[lang];
}

export function fixtureScript(lang: Language): CallScript {
  const { draft } = fixtureCall(lang);
  return {
    caller_name: draft.caller_name,
    relationship: draft.relationship,
    speech_register: draft.speech_register,
    language: lang,
    voice_id: "browser",
    user_alias: draft.user_alias,
    opening_line: draft.opening_line,
    situation: draft.situation,
    ask: draft.ask,
    urgency: draft.urgency,
    facts: draft.facts,
    forbidden: [],
    max_turns: 5,
  };
}

const ARC = ["hook", "escalate", "ask", "closing"] as const;

/** Plays the fixture call word by word, like a model streaming tokens. Ignores what is said. */
export class OfflineEngine implements CallEngine {
  private timers: number[] = [];
  private turn = 0;
  private events: EngineEvents | null = null;

  constructor(private readonly lang: Language) {}

  connect(events: EngineEvents): void {
    this.events = events;
    this.stream(fixtureCall(this.lang).draft.opening_line, false);
  }

  prompt(): void {
    const key = ARC[this.turn];
    if (key === undefined) return;
    this.turn += 1;
    this.stream(fixtureCall(this.lang).lines[key], key === "closing");
  }

  interrupt(): void {
    this.clear();
  }

  metric(): void {}

  close(): void {
    this.clear();
  }

  private stream(text: string, thenEnd: boolean): void {
    this.clear();
    const words = text.split(" ");
    words.forEach((word, i) => {
      const delay = 380 + i * 45;
      this.timers.push(
        window.setTimeout(() => this.events?.onToken(i === 0 ? word : ` ${word}`, i === words.length - 1), delay),
      );
    });
    if (thenEnd) this.timers.push(window.setTimeout(() => this.events?.onEnd(), 420 + words.length * 45));
  }

  private clear(): void {
    this.timers.forEach((t) => window.clearTimeout(t));
    this.timers = [];
  }
}
