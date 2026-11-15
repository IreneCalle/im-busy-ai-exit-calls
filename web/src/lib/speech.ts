// Web Speech API wrappers.
//
// Recognition: `SpeechRecognition` is not Baseline. Chrome (desktop and Android) exposes it as
// `webkitSpeechRecognition` and sends audio to a server; Safari has partial support; Firefox ships
// it disabled. Synthesis (`speechSynthesis`) is Baseline and works everywhere, with voices that
// vary by OS. The page always offers typed input, so the demo works without a microphone.

import type { Language } from "./protocol";

interface RecognitionAlternative {
  transcript: string;
}
interface RecognitionResultItem {
  readonly length: number;
  readonly isFinal: boolean;
  [index: number]: RecognitionAlternative;
}
interface RecognitionEvent {
  resultIndex: number;
  results: { readonly length: number; [index: number]: RecognitionResultItem };
}
interface NativeRecognition {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  maxAlternatives: number;
  start(): void;
  stop(): void;
  abort(): void;
  onresult: ((event: RecognitionEvent) => void) | null;
  onend: (() => void) | null;
  onerror: ((event: { error: string }) => void) | null;
}
type RecognitionCtor = new () => NativeRecognition;

function recognitionConstructor(): RecognitionCtor | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as { SpeechRecognition?: RecognitionCtor; webkitSpeechRecognition?: RecognitionCtor };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

export function speechRecognitionSupported(): boolean {
  return recognitionConstructor() !== null;
}

export function speechSynthesisSupported(): boolean {
  return typeof window !== "undefined" && "speechSynthesis" in window;
}

export interface RecognizerHandlers {
  onInterim(text: string): void;
  onFinal(text: string): void;
  onError(error: string): void;
}

/** Listens for one utterance at a time. `listen()` again after each turn. */
export class Recognizer {
  private native: NativeRecognition | null = null;
  private active = false;

  constructor(
    private readonly lang: Language,
    private readonly handlers: RecognizerHandlers,
  ) {}

  listen(): void {
    const Ctor = recognitionConstructor();
    if (Ctor === null || this.active) return;
    const recognition = new Ctor();
    recognition.lang = this.lang;
    recognition.continuous = false;
    recognition.interimResults = true;
    recognition.maxAlternatives = 1;
    let finalText = "";
    recognition.onresult = (event) => {
      let interim = "";
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i];
        const text = result?.[0]?.transcript ?? "";
        if (result?.isFinal) finalText += text;
        else interim += text;
      }
      if (interim) this.handlers.onInterim(interim);
    };
    recognition.onerror = (event) => {
      if (event.error !== "no-speech" && event.error !== "aborted") this.handlers.onError(event.error);
    };
    recognition.onend = () => {
      this.active = false;
      this.native = null;
      if (finalText.trim()) this.handlers.onFinal(finalText.trim());
    };
    this.native = recognition;
    this.active = true;
    recognition.start();
  }

  stop(): void {
    this.native?.abort();
    this.native = null;
    this.active = false;
  }

  get listening(): boolean {
    return this.active;
  }
}

/** Split a growing buffer into complete sentences and a remainder. Pure; used by the speaker. */
export function takeSentences(buffer: string): { sentences: string[]; rest: string } {
  const sentences: string[] = [];
  const pattern = /[^.!?]*[.!?]+(\s+|$)/g;
  let consumed = 0;
  for (const match of buffer.matchAll(pattern)) {
    if (match.index !== consumed) break;
    const sentence = match[0].trim();
    if (sentence) sentences.push(sentence);
    consumed += match[0].length;
  }
  return { sentences, rest: buffer.slice(consumed) };
}

function pickVoice(lang: Language): SpeechSynthesisVoice | null {
  const voices = window.speechSynthesis.getVoices();
  const base = lang.slice(0, 2);
  const exact = voices.filter((v) => v.lang.replace("_", "-") === lang);
  const sameLanguage = voices.filter((v) => v.lang.startsWith(base));
  const pool = exact.length ? exact : sameLanguage;
  return pool.find((v) => /natural|neural|google/i.test(v.name)) ?? pool[0] ?? null;
}

export interface SpeakerHandlers {
  onStart(): void;
  onIdle(): void;
}

/** Speaks streamed text sentence by sentence so the first sentence starts before the reply ends. */
export class Speaker {
  private buffer = "";
  private queued = 0;
  private spoken: string[] = [];
  private flushed = false;

  constructor(
    private readonly lang: Language,
    private readonly handlers: SpeakerHandlers,
  ) {}

  push(token: string): void {
    this.flushed = false;
    this.buffer += token;
    const { sentences, rest } = takeSentences(this.buffer);
    this.buffer = rest;
    sentences.forEach((s) => this.say(s));
  }

  /** Speak whatever is buffered (called on the `last` token). */
  flush(): void {
    this.flushed = true;
    const rest = this.buffer.trim();
    this.buffer = "";
    if (rest) this.say(rest);
    else if (this.queued === 0) this.handlers.onIdle();
  }

  /** Stop immediately; returns the text spoken so far for the interrupt frame. */
  cancel(): string {
    const heard = this.spoken.join(" ");
    this.buffer = "";
    this.queued = 0;
    if (speechSynthesisSupported()) window.speechSynthesis.cancel();
    return heard;
  }

  resetTurn(): void {
    this.spoken = [];
  }

  private say(text: string): void {
    if (!speechSynthesisSupported()) {
      this.handlers.onStart();
      this.spoken.push(text);
      if (this.flushed) this.handlers.onIdle();
      return;
    }
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = this.lang;
    const voice = pickVoice(this.lang);
    if (voice) utterance.voice = voice;
    utterance.onstart = () => this.handlers.onStart();
    utterance.onend = () => {
      this.spoken.push(text);
      this.queued = Math.max(0, this.queued - 1);
      if (this.queued === 0 && this.flushed && !this.buffer) this.handlers.onIdle();
    };
    utterance.onerror = utterance.onend;
    this.queued += 1;
    window.speechSynthesis.speak(utterance);
  }
}
