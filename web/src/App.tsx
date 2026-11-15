import { ArrowRight, Lock, MessageSquareText, PhoneCall, ShieldCheck } from "lucide-react";
import { useEffect, useRef, useState, useSyncExternalStore, type FormEvent, type ReactNode } from "react";

import { Phone } from "./components/Phone";
import { AlertPreview, SafeWordPanel } from "./components/SafeWord";
import { CallController } from "./lib/controller";
import { speechRecognitionSupported } from "./lib/speech";

const EXAMPLES = [
  "tía Carmen, se ha dejado las llaves dentro, en 2 min",
  "my manager Dana, the client moved the call to right now",
];

const controller = new CallController();
const preview = new URLSearchParams(window.location.search).get("preview");
if (preview) controller.showPreview(preview);

function Eyebrow({ children }: { children: ReactNode }) {
  return (
    <span className="inline-flex rounded-full border border-line px-3 py-1 font-mono text-xs uppercase tracking-wider text-muted">
      {children}
    </span>
  );
}

function ModeNotice({ mode }: { mode: string | null }) {
  if (mode === "fixture")
    return <p className="text-center font-mono text-xs text-muted">Scripted call: this server runs without a model key.</p>;
  if (mode === "offline")
    return <p className="text-center font-mono text-xs text-muted">Offline preview: a scripted call played in your browser.</p>;
  return null;
}

function Hero() {
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
  const [excuse, setExcuse] = useState(EXAMPLES[0] ?? "");
  const busy = state.phase !== "idle" && state.phase !== "ended";
  const phoneRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // On a phone-sized screen the phone frame sits below the form; bring it into view when it rings.
    if (state.phase === "ringing") phoneRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [state.phase]);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    void controller.compile(excuse);
  };
  return (
    <section className="hero-wash relative overflow-hidden rounded-t-[28px]">
      <div className="hero-grid pointer-events-none absolute inset-0" aria-hidden />
      <div className="relative mx-auto grid max-w-6xl items-center gap-12 px-5 pb-20 pt-16 md:grid-cols-[1.1fr_0.9fr] md:px-10 md:pb-28 md:pt-24">
        <div>
          <Eyebrow>Voice agent · telephony</Eyebrow>
          <h1 className="mt-6 font-display text-[40px] font-medium leading-[1.02] tracking-[-0.03em] md:text-[80px]">
            <span className="block text-ink">Text an excuse.</span>
            <span className="block text-muted">
              Your phone <em className="font-serif font-normal italic text-accent-text">rings.</em>
            </span>
          </h1>
          <p className="mt-6 max-w-xl text-lg text-muted">
            Write who should call and why. A caller who answers back rings you, holds a five-turn conversation in Spanish
            or English, and gives you a believable reason to leave.
          </p>
          <form onSubmit={submit} className="mt-8 max-w-xl">
            <label htmlFor="excuse" className="sr-only">
              Your excuse
            </label>
            <div className="flex flex-col gap-3 rounded-[20px] border border-line bg-surface p-2 sm:flex-row sm:items-center sm:rounded-full">
              <input
                id="excuse"
                value={excuse}
                onChange={(e) => setExcuse(e.target.value)}
                maxLength={500}
                className="min-w-0 flex-1 bg-transparent px-4 py-2 text-ink placeholder:text-muted focus:outline-none"
                placeholder="who calls, why, and when"
              />
              <button
                type="submit"
                disabled={busy}
                className="inline-flex h-11 items-center justify-center gap-2 rounded-full bg-accent px-6 font-medium text-on-accent transition-opacity duration-150 disabled:opacity-50"
              >
                Call me <ArrowRight size={16} strokeWidth={1.5} />
              </button>
            </div>
          </form>
          <div className="mt-3 flex flex-wrap gap-2">
            {EXAMPLES.map((example) => (
              <button
                key={example}
                type="button"
                onClick={() => setExcuse(example)}
                className="rounded-full border border-line px-3 py-1 text-left font-mono text-xs text-muted transition-colors duration-150 hover:text-ink"
              >
                {example}
              </button>
            ))}
          </div>
          <p className="mt-4 max-w-xl text-xs leading-relaxed text-muted">
            By pressing Call me you agree to keep it harmless: a first name and an excuse, never a real person's or an
            organisation's identity, and nothing meant to deceive or bother anyone. The caller's voice is AI-generated.
            This is not an emergency service.
          </p>
          <SafeWordPanel controller={controller} />
          {state.notice && (
            <p role="status" className="mt-5 max-w-xl rounded-[20px] border border-line bg-surface px-4 py-3 text-sm text-ink">
              {state.notice}
            </p>
          )}
          {!speechRecognitionSupported() && (
            <p className="mt-5 max-w-xl text-sm text-muted">
              This browser has no speech recognition, so you will type your side of the call. Chrome gives you the full
              voice version.
            </p>
          )}
        </div>
        <div ref={phoneRef} className="relative space-y-3">
          <Phone state={state} controller={controller} />
          <ModeNotice mode={state.mode} />
          <AlertPreview frame={state.alertPreview} />
          {state.phase === "idle" && (
            <>
          <div className="pointer-events-none absolute -left-6 top-24 hidden rounded-full border border-line bg-surface px-4 py-2 font-mono text-xs text-ink shadow-float lg:block">
            Tía Carmen · mobile
          </div>
          <div className="pointer-events-none absolute -right-4 bottom-40 hidden max-w-[220px] rounded-[20px] border border-line bg-surface px-4 py-3 text-sm text-ink shadow-float lg:block">
            "Te espero en el portal."
          </div>
            </>
          )}
        </div>
      </div>
    </section>
  );
}

const STEPS = [
  {
    icon: MessageSquareText,
    title: "You text the excuse",
    body: "On Telegram, or in this page. A strong model turns it into a call plan: who calls, what happened, what they need from you, and a few small details to stay consistent.",
  },
  {
    icon: PhoneCall,
    title: "Your phone rings",
    body: "Twilio calls your verified number. The first sentence is pre-written, so it plays the instant you pick up. After that a fast model answers whatever you say.",
  },
  {
    icon: ShieldCheck,
    title: "Five turns, then goodbye",
    body: "Hook, urgency, the ask, the goodbye. Each turn gets its own one-line instruction, so the call stays short and lands on a reason to leave.",
  },
];

function HowItWorks() {
  return (
    <section className="mx-auto max-w-6xl px-5 py-24 md:px-10 md:py-32">
      <Eyebrow>How it works</Eyebrow>
      <h2 className="mt-5 max-w-3xl font-display text-4xl font-medium leading-[1.05] tracking-[-0.03em] md:text-6xl">
        <span className="text-ink">Not a recording.</span>{" "}
        <span className="text-muted">
          A caller who <em className="font-serif font-normal italic text-accent-text">listens.</em>
        </span>
      </h2>
      <div className="mt-14 grid gap-5 md:grid-cols-3">
        {STEPS.map(({ icon: Icon, title, body }) => (
          <article key={title} className="rounded-[20px] border border-line bg-surface p-7">
            <Icon size={22} strokeWidth={1.5} className="text-accent-text" />
            <h3 className="mt-5 font-display text-2xl font-medium tracking-tight">{title}</h3>
            <p className="mt-3 text-muted">{body}</p>
          </article>
        ))}
      </div>
    </section>
  );
}

function Stat({ value, caption, className = "" }: { value: string; caption: string; className?: string }) {
  return (
    <div className={`flex flex-col justify-between rounded-[20px] p-7 ${className}`}>
      <p className="font-display text-[88px] font-light leading-none tracking-[-0.04em] md:text-[112px]">{value}</p>
      <p className="mt-6 max-w-xs">{caption}</p>
    </div>
  );
}

function Proof() {
  return (
    <section className="mx-auto max-w-6xl px-5 pb-24 md:px-10 md:pb-32">
      <Eyebrow>By design</Eyebrow>
      <div className="mt-8 grid auto-rows-[minmax(220px,auto)] gap-5 md:grid-cols-6">
        <Stat value="0 ms" caption="of model time before the first sentence: the greeting is written before the phone rings." className="accent-tile text-on-accent md:col-span-4" />
        <Stat value="1" caption="number it will ever call: the one you shared from your own Telegram account." className="bg-deep text-on-deep md:col-span-2" />
        <Stat value="5" caption="turns with an arc: hook, urgency, the ask, the goodbye." className="border border-line bg-surface md:col-span-2" />
        <Stat value="2" caption="languages in the MVP, Spanish from Spain and American English, with stock voices only." className="border border-line bg-surface md:col-span-2" />
        <div className="flex flex-col justify-between rounded-[20px] border border-line bg-surface p-7 md:col-span-2">
          <p className="font-mono text-xs uppercase tracking-wider text-muted">The demo you are using</p>
          <p className="mt-4">
            This page speaks the same WebSocket protocol as Twilio's ConversationRelay. Your browser does the listening and
            the speaking; the server runs the same call loop the phone uses.
          </p>
        </div>
      </div>
    </section>
  );
}

function Limits() {
  return (
    <section className="bg-deep text-on-deep">
      <div className="mx-auto max-w-6xl px-5 py-24 md:px-10 md:py-32">
        <span className="inline-flex rounded-full border border-on-deep/30 px-3 py-1 font-mono text-xs uppercase tracking-wider text-on-deep/70">
          Limits by design
        </span>
        <h2 className="mt-5 max-w-3xl font-display text-4xl font-medium leading-[1.05] tracking-[-0.03em] md:text-6xl">
          A way out, <span className="font-serif font-normal italic">not</span> a way to fool people.
        </h2>
        <div className="mt-12 grid gap-8 text-on-deep/80 md:grid-cols-3">
          <p>
            It only calls the number you shared with Telegram's own contact button. There is no field for a destination,
            so it cannot be pointed at someone else.
          </p>
          <p>
            Stock voices only. No cloning, no real people, no police, hospitals, courts or banks. The compiler refuses
            those and a second check rejects any script that slips through.
          </p>
          <p>
            No call transcripts are kept. A call record holds the caller's name, the outcome, the number of turns and the
            response time, and /forget deletes even that.
          </p>
        </div>
      </div>
    </section>
  );
}

export default function App() {
  return (
    <div className="frame min-h-screen p-3 md:p-4">
      <main className="overflow-hidden rounded-[28px] bg-paper">
        <Hero />
        <HowItWorks />
        <Proof />
        <Limits />
        <footer className="mx-auto flex max-w-6xl flex-col gap-2 px-5 py-10 font-mono text-xs text-muted md:flex-row md:justify-between md:px-10">
          <span>I'm Busy · MIT licence</span>
          <span className="inline-flex items-center gap-1.5">
            <Lock size={12} strokeWidth={1.5} /> Speech in this demo is processed by your browser.
          </span>
        </footer>
      </main>
    </div>
  );
}
