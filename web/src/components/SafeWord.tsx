import { MapPin, ShieldCheck, X } from "lucide-react";
import { useState, type FormEvent } from "react";

import type { CallController } from "../lib/controller";
import type { SafeWordFrame } from "../lib/protocol";

type LocationStatus = "off" | "asking" | "on" | "denied";

async function checkSafeWord(safeWord: string): Promise<string | null> {
  try {
    const response = await fetch("/demo/safeword", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ safeWord }),
    });
    if (!response.ok) return null;
    const body = (await response.json()) as { ok: boolean; message: string | null };
    return body.ok ? null : (body.message ?? "That phrase cannot be used.");
  } catch {
    // No backend (static preview): the server checks it again when the call is set up.
    return null;
  }
}

/** Landing-page panel: choose a safe word and, optionally, share the browser location for this demo. */
export function SafeWordPanel({ controller }: { controller: CallController }) {
  const initial = controller.safety;
  const [open, setOpen] = useState(initial !== null);
  const [safeWord, setSafeWord] = useState(initial?.safeWord ?? "");
  const [name, setName] = useState(initial?.name ?? "");
  const [location, setLocation] = useState(initial?.location ?? null);
  const [locationStatus, setLocationStatus] = useState<LocationStatus>(initial?.location ? "on" : "off");
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(initial !== null);

  const toggleLocation = () => {
    if (locationStatus === "on") {
      setLocation(null);
      setLocationStatus("off");
      return;
    }
    if (!("geolocation" in navigator)) {
      setLocationStatus("denied");
      return;
    }
    setLocationStatus("asking");
    navigator.geolocation.getCurrentPosition(
      (position) => {
        setLocation({
          latitude: position.coords.latitude,
          longitude: position.coords.longitude,
          accuracy: position.coords.accuracy,
        });
        setLocationStatus("on");
      },
      () => setLocationStatus("denied"),
      { enableHighAccuracy: false, timeout: 10_000, maximumAge: 60_000 },
    );
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    const problem = await checkSafeWord(safeWord);
    setError(problem);
    if (problem) return;
    controller.safety = { safeWord: safeWord.trim(), name: name.trim(), location };
    setSaved(true);
  };

  const remove = () => {
    controller.safety = null;
    setSafeWord("");
    setLocation(null);
    setLocationStatus("off");
    setSaved(false);
  };

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="mt-6 inline-flex items-center gap-2 rounded-full border border-line px-4 py-2 text-sm text-ink transition-colors duration-150 hover:bg-surface"
      >
        <ShieldCheck size={16} strokeWidth={1.5} className="text-accent-text" /> Add a safe word
      </button>
    );
  }

  return (
    <form onSubmit={(e) => void save(e)} className="mt-6 max-w-xl rounded-[20px] border border-line bg-surface p-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <p className="font-mono text-xs uppercase tracking-wider text-muted">Safe word</p>
          <p className="mt-2 text-sm text-muted">
            Say it during the call and the caller carries on as if nothing happened, then wraps up. Your trusted contact
            gets your location. In this demo nothing is sent: you see the message they would get.
          </p>
        </div>
        <button type="button" onClick={() => setOpen(false)} aria-label="Close" className="text-muted hover:text-ink">
          <X size={18} strokeWidth={1.5} />
        </button>
      </div>
      <div className="mt-4 grid gap-3 sm:grid-cols-[1.4fr_1fr]">
        <label className="text-sm">
          <span className="sr-only">Safe word</span>
          <input
            value={safeWord}
            onChange={(e) => {
              setSafeWord(e.target.value);
              setSaved(false);
            }}
            maxLength={60}
            placeholder="two to four words, e.g. pink flamingo"
            autoComplete="off"
            className="w-full rounded-full border border-line bg-paper px-4 py-2 text-ink placeholder:text-muted focus:outline-none focus:ring-1 focus:ring-accent"
          />
        </label>
        <label className="text-sm">
          <span className="sr-only">Your first name</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            maxLength={40}
            placeholder="your first name"
            className="w-full rounded-full border border-line bg-paper px-4 py-2 text-ink placeholder:text-muted focus:outline-none focus:ring-1 focus:ring-accent"
          />
        </label>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={toggleLocation}
          aria-pressed={locationStatus === "on"}
          className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs transition-colors duration-150 ${
            locationStatus === "on" ? "border-accent-text text-accent-text" : "border-line text-muted hover:text-ink"
          }`}
        >
          <MapPin size={13} strokeWidth={1.5} />
          {locationStatus === "on"
            ? "Location included"
            : locationStatus === "asking"
              ? "Asking the browser..."
              : locationStatus === "denied"
                ? "Location not available"
                : "Include my location"}
        </button>
        <span className="font-mono text-[11px] text-muted">kept only for this demo call</span>
      </div>
      {error && (
        <p role="alert" className="mt-3 text-sm text-accent-text">
          {error}
        </p>
      )}
      <div className="mt-4 flex items-center gap-2">
        <button
          type="submit"
          disabled={safeWord.trim().length === 0}
          className="h-10 rounded-full bg-ink px-5 text-sm font-medium text-paper transition-opacity duration-150 disabled:opacity-40"
        >
          {saved ? "Saved for the next call" : "Use this safe word"}
        </button>
        {saved && (
          <button type="button" onClick={remove} className="h-10 rounded-full px-4 text-sm text-muted hover:text-ink">
            Remove
          </button>
        )}
      </div>
      <p className="mt-4 text-xs text-muted">
        Not an emergency service. If you are in danger, call the local emergency number (112 in Europe, 911 in the US).
      </p>
    </form>
  );
}

/** Shown beside the phone when the safe word is heard. The caller on the phone shows nothing. */
export function AlertPreview({ frame }: { frame: SafeWordFrame | null }) {
  if (frame === null) return null;
  return (
    <aside aria-live="polite" className="rounded-[20px] border border-line bg-surface p-5 shadow-float">
      <div className="flex flex-wrap items-center gap-2">
        <span className="inline-flex rounded-full bg-accent px-3 py-1 font-mono text-[11px] uppercase tracking-wider text-on-accent">
          Demo: nothing was sent
        </span>
        <span className="font-mono text-[11px] uppercase tracking-wider text-muted">Safe word heard</span>
      </div>
      <p className="mt-3 font-display text-xl font-medium tracking-tight text-ink">What your trusted contact would receive</p>
      <p className="mt-3 whitespace-pre-line text-sm leading-relaxed text-ink">{frame.text}</p>
      {frame.mapUrl && (
        <a
          href={frame.mapUrl}
          target="_blank"
          rel="noreferrer"
          className="mt-3 inline-flex items-center gap-1.5 text-sm text-accent-text underline underline-offset-4"
        >
          <MapPin size={14} strokeWidth={1.5} /> Open the map pin
        </a>
      )}
    </aside>
  );
}
