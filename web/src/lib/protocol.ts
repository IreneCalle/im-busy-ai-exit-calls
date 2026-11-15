// Frames exchanged with /demo/ws. Same shapes as Twilio ConversationRelay, plus `metric`.

export type Language = "es-ES" | "en-US";

export interface CallScript {
  caller_name: string;
  relationship: string;
  speech_register: "family" | "work" | "formal";
  language: Language;
  voice_id: string;
  user_alias: string | null;
  opening_line: string;
  situation: string;
  ask: string;
  urgency: number;
  facts: string[];
  forbidden: string[];
  max_turns: number;
}

export type CompileResponse =
  | { status: "ok"; mode: "live" | "fixture"; token: string; script: CallScript; safeWordArmed: boolean }
  | { status: "refused"; mode: "live" | "fixture"; refusal: string; message: string };

// Page -> server
export interface PromptFrame {
  type: "prompt";
  voicePrompt: string;
  lang: Language;
  last: true;
}
export interface InterruptFrame {
  type: "interrupt";
  utteranceUntilInterrupt: string;
  durationUntilInterruptMs: number;
}
export interface MetricFrame {
  type: "metric";
  turn: number;
  firstAudioMs: number;
}
export type ClientFrame = PromptFrame | InterruptFrame | MetricFrame;

// Server -> page
export interface TextFrame {
  type: "text";
  token: string;
  last?: boolean;
}
export interface EndFrame {
  type: "end";
  handoffData?: string;
}
/** Demo only: what the trusted contact would receive. The server sends nothing anywhere. */
export interface SafeWordFrame {
  type: "safeword";
  text: string;
  latitude?: number;
  longitude?: number;
  mapUrl?: string;
}
export type ServerFrame = TextFrame | EndFrame | SafeWordFrame;

export function parseServerFrame(raw: string): ServerFrame {
  const frame = JSON.parse(raw) as { type?: unknown };
  if (frame.type === "text" || frame.type === "end" || frame.type === "safeword") return frame as ServerFrame;
  throw new Error(`unexpected frame type: ${String(frame.type)}`);
}
