import React, { useRef, useState } from "react";
import { Pause, Play } from "lucide-react";

/**
 * Compact play/pause control for one voice sample, with the same dancing
 * equalizer as the speaker cards. Used by the remove-favourite dialog and the
 * Favourite voices page.
 */
export default function SamplePlayer({ src, testId }) {
  const audioRef = useRef(null);
  const [isPlaying, setIsPlaying] = useState(false);

  function toggle() {
    const el = audioRef.current;
    if (!el) return;
    if (el.paused) el.play().catch(() => {});
    else el.pause();
  }

  if (!src) {
    return (
      <p className="rounded-lg border border-dashed border-white/10 px-3 py-2.5 text-center font-mono text-xs text-slate-600">
        No sample available
      </p>
    );
  }

  return (
    <div className="flex items-center gap-3" data-testid={testId}>
      <button
        type="button"
        onClick={toggle}
        className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-accent-grad text-white shadow-glow-accent transition-transform duration-200 hover:scale-105 active:scale-95"
        aria-label={isPlaying ? "Pause voice sample" : "Play voice sample"}
      >
        {isPlaying ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4 translate-x-px" />}
      </button>
      <div className={`eq flex-1 ${isPlaying ? "eq-playing" : ""}`} aria-hidden="true">
        {Array.from({ length: 24 }).map((_, i) => (
          <span
            key={i}
            style={{
              animationDelay: `${(i % 6) * 80}ms`,
              animationDuration: `${640 + (i % 5) * 120}ms`,
            }}
          />
        ))}
      </div>
      <audio
        ref={audioRef}
        preload="metadata"
        src={src}
        onPlay={() => setIsPlaying(true)}
        onPause={() => setIsPlaying(false)}
        onEnded={() => setIsPlaying(false)}
        className="hidden"
      />
    </div>
  );
}
