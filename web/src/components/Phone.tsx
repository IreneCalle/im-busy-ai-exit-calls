import { Mic, MicOff, Phone as PhoneIcon, PhoneOff, Send, Share2, Sparkles } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";

import type { CallController, CallState } from "../lib/controller";

function initials(name: string): string {
  return name
    .replace(/^(tía|tío|aunt|uncle)\s+/i, "")
    .split(/\s+/)
    .slice(0, 2)
    .map((w) => w[0]?.toUpperCase() ?? "")
    .join("");
}

function useClock(startedAt: number | null, endedAt: number | null): string {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (startedAt === null || endedAt !== null) return;
    const id = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(id);
  }, [startedAt, endedAt]);
  if (startedAt === null) return "0:00";
  const seconds = Math.max(0, Math.floor(((endedAt ?? now) - startedAt) / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function TurnDots({ done, total }: { done: number; total: number }) {
  return (
    <div className="flex items-center gap-1.5" aria-label={`Turn ${done} of ${total}`}>
      {Array.from({ length: total }, (_, i) => (
        <span
          key={i}
          className={`h-1.5 w-5 rounded-full transition-colors duration-200 ${i < done ? "bg-accent" : "bg-phone-ink/20"}`}
        />
      ))}
    </div>
  );
}

function StatusBar() {
  return (
    <div className="flex items-center justify-between px-6 pt-3 font-mono text-[11px] text-phone-muted">
      <span>9:41</span>
      <span className="h-5 w-24 rounded-full bg-phone-bezel" aria-hidden />
      <span>5G</span>
    </div>
  );
}

function Idle({ state }: { state: CallState }) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-4 px-8 text-center">
      <p className="font-display text-5xl font-light tracking-tight text-phone-ink">9:41</p>
      <p className="text-sm text-phone-muted">
        {state.phase === "compiling" ? "Writing the call..." : "Nobody is calling. Yet."}
      </p>
      {state.phase === "compiling" && (
        <span className="h-1 w-24 overflow-hidden rounded-full bg-phone-ink/10">
          <span className="block h-full w-1/2 animate-pulse rounded-full bg-accent" />
        </span>
      )}
    </div>
  );
}

function Ringing({ state, controller }: { state: CallState; controller: CallController }) {
  const script = state.script;
  if (script === null) return null;
  return (
    <div className="flex flex-1 flex-col items-center justify-between px-8 pb-10 pt-12 text-center">
      <div>
        <p className="font-mono text-xs uppercase tracking-widest text-phone-muted">Incoming call</p>
        <p className="mt-3 font-display text-4xl font-medium tracking-tight text-phone-ink">{script.caller_name}</p>
        <p className="mt-1 text-sm text-phone-muted">mobile</p>
      </div>
      <div className="ring-pulse relative grid h-28 w-28 place-items-center rounded-full">
        <span className="relative z-10 grid h-28 w-28 place-items-center rounded-full bg-accent font-display text-4xl text-on-accent">
          {initials(script.caller_name)}
        </span>
      </div>
      <div className="flex w-full justify-between px-4">
        <button
          type="button"
          onClick={() => controller.decline()}
          className="grid h-16 w-16 place-items-center rounded-full bg-negative text-on-deep transition-transform duration-150 hover:scale-105"
          aria-label="Decline"
        >
          <PhoneOff size={26} strokeWidth={1.5} />
        </button>
        <button
          type="button"
          onClick={() => controller.accept()}
          className="grid h-16 w-16 place-items-center rounded-full bg-positive text-on-deep transition-transform duration-150 hover:scale-105"
          aria-label="Accept"
        >
          <PhoneIcon size={26} strokeWidth={1.5} />
        </button>
      </div>
    </div>
  );
}

const PHASE_LABEL: Record<string, string> = {
  connecting: "connecting",
  caller: "speaking",
  listening: "your turn",
  thinking: "...",
};

function InCall({ state, controller }: { state: CallState; controller: CallController }) {
  const [draft, setDraft] = useState("");
  const clock = useClock(state.startedAt, state.endedAt);
  const scroller = useRef<HTMLDivElement>(null);
  const script = state.script;
  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
  }, [state.lines, state.interim]);
  if (script === null) return null;
  const submit = (event: FormEvent) => {
    event.preventDefault();
    controller.say(draft);
    setDraft("");
  };
  const suggestion = state.mode !== "live" ? controller.suggestedReply() : null;
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="px-6 pt-6 text-center">
        <p className="font-display text-2xl font-medium tracking-tight text-phone-ink">{script.caller_name}</p>
        <p className="mt-1 font-mono text-xs text-phone-muted">
          {clock} · {PHASE_LABEL[state.phase] ?? state.phase}
        </p>
        <div className="mt-3 flex justify-center">
          <TurnDots done={state.callerTurns} total={script.max_turns} />
        </div>
      </div>
      <div ref={scroller} className="mt-4 flex-1 space-y-3 overflow-y-auto px-5 pb-4" aria-live="polite">
        {state.lines.map((line, i) => (
          <p
            key={i}
            className={
              line.speaker === "caller"
                ? "max-w-[85%] rounded-2xl rounded-tl-sm bg-phone-ink/10 px-3.5 py-2 text-[15px] leading-snug text-phone-ink"
                : "ml-auto max-w-[85%] rounded-2xl rounded-tr-sm bg-accent px-3.5 py-2 text-[15px] leading-snug text-on-accent"
            }
          >
            {line.text}
            {line.interrupted && <span className="ml-1 font-mono text-[11px] opacity-60">(cut off)</span>}
          </p>
        ))}
        {state.interim && (
          <p className="ml-auto max-w-[85%] rounded-2xl px-3.5 py-2 text-[15px] italic text-phone-muted">{state.interim}</p>
        )}
      </div>
      <form onSubmit={submit} className="flex items-center gap-2 border-t border-phone-ink/10 px-4 py-3">
        <span className="text-phone-muted" title={state.micAvailable ? "Microphone on when it's your turn" : "No speech recognition here"}>
          {state.micAvailable && state.phase === "listening" ? <Mic size={18} strokeWidth={1.5} /> : <MicOff size={18} strokeWidth={1.5} />}
        </span>
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={state.micAvailable ? "Speak, or type your reply" : "Type your reply"}
          className="min-w-0 flex-1 rounded-full bg-phone-ink/10 px-4 py-2 text-sm text-phone-ink placeholder:text-phone-muted focus:outline-none focus:ring-1 focus:ring-accent"
        />
        <button type="submit" aria-label="Send" className="grid h-9 w-9 place-items-center rounded-full bg-accent text-on-accent">
          <Send size={16} strokeWidth={1.5} />
        </button>
      </form>
      <div className="flex items-center justify-between px-5 pb-6">
        {suggestion ? (
          <button
            type="button"
            onClick={() => controller.say(suggestion)}
            className="inline-flex items-center gap-1.5 rounded-full border border-phone-ink/20 px-3 py-1.5 text-xs text-phone-muted transition-colors duration-150 hover:text-phone-ink"
          >
            <Sparkles size={13} strokeWidth={1.5} /> Say "{suggestion}"
          </button>
        ) : (
          <span />
        )}
        <button
          type="button"
          onClick={() => controller.hangUp()}
          className="grid h-12 w-12 place-items-center rounded-full bg-negative text-on-deep"
          aria-label="Hang up"
        >
          <PhoneOff size={20} strokeWidth={1.5} />
        </button>
      </div>
    </div>
  );
}

function Ended({ state, controller }: { state: CallState; controller: CallController }) {
  const clock = useClock(state.startedAt, state.endedAt);
  const script = state.script;
  if (script === null) return null;
  const heading =
    state.endReason === "declined" ? "Declined" : state.endReason === "hung-up" ? "You hung up" : "Call ended";
  return (
    <div className="flex flex-1 flex-col justify-between px-6 pb-8 pt-10">
      <div className="text-center">
        <p className="font-mono text-xs uppercase tracking-widest text-phone-muted">{heading}</p>
        <p className="mt-2 font-display text-3xl font-medium tracking-tight text-phone-ink">{script.caller_name}</p>
        <p className="mt-1 font-mono text-xs text-phone-muted">
          {clock} · {state.callerTurns} turns
        </p>
      </div>
      <figure className="rounded-[20px] bg-accent p-5 text-on-accent">
        <p className="font-mono text-[11px] uppercase tracking-widest opacity-70">The excuse</p>
        <blockquote className="mt-2 font-display text-xl leading-tight tracking-tight">"{state.excuse}"</blockquote>
        <figcaption className="mt-4 text-sm leading-snug opacity-80">{script.situation}</figcaption>
      </figure>
      <div className="flex gap-2">
        <ShareButton excuse={state.excuse} caller={script.caller_name} />
        <button
          type="button"
          onClick={() => controller.reset()}
          className="h-11 flex-1 rounded-full border border-phone-ink/25 text-sm text-phone-ink transition-colors duration-150 hover:bg-phone-ink/10"
        >
          New excuse
        </button>
      </div>
    </div>
  );
}

function ShareButton({ excuse, caller }: { excuse: string; caller: string }) {
  const [copied, setCopied] = useState(false);
  const share = async () => {
    const text = `"${excuse}" and ${caller} called me. I'm Busy.`;
    try {
      if (navigator.share) await navigator.share({ text, url: window.location.origin });
      else {
        await navigator.clipboard.writeText(`${text} ${window.location.origin}`);
        setCopied(true);
      }
    } catch {
      // The visitor closed the share sheet; nothing to do.
    }
  };
  return (
    <button
      type="button"
      onClick={() => void share()}
      className="inline-flex h-11 flex-1 items-center justify-center gap-1.5 rounded-full bg-phone-ink text-sm text-phone transition-opacity duration-150 hover:opacity-90"
    >
      <Share2 size={15} strokeWidth={1.5} /> {copied ? "Copied" : "Share this excuse"}
    </button>
  );
}

export function Phone({ state, controller }: { state: CallState; controller: CallController }) {
  const inCall = ["connecting", "caller", "listening", "thinking"].includes(state.phase);
  return (
    <div className="mx-auto w-full max-w-[360px]">
      <div className="rounded-[48px] bg-phone-bezel p-2.5 shadow-float">
        <div className="flex h-[640px] flex-col overflow-hidden rounded-[40px] bg-phone">
          <StatusBar />
          {(state.phase === "idle" || state.phase === "compiling") && <Idle state={state} />}
          {state.phase === "ringing" && <Ringing state={state} controller={controller} />}
          {inCall && <InCall state={state} controller={controller} />}
          {state.phase === "ended" && <Ended state={state} controller={controller} />}
        </div>
      </div>
    </div>
  );
}
