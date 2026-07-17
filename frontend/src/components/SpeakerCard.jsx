import React, { useRef, useState } from "react";
import { motion } from "framer-motion";
import { Check, Loader2, AudioLines, Clock, Hash, Play, Pause } from "lucide-react";

/**
 * One speaker option in the AWAITING_SELECTION state.
 *
 * Props:
 *   - speaker: { label, total_speaking_sec, segment_count, snippet_url }
 *   - displayName: "Speaker 1"
 *   - onSelect(label)
 *   - isSelecting: boolean
 *   - disabled: boolean (other card is selecting)
 */
export default function SpeakerCard({
  speaker,
  displayName,
  onSelect,
  isSelecting,
  disabled,
}) {
  const { label, total_speaking_sec, segment_count, snippet_url } = speaker;
  const { timeStr, segStr } = formatSpeakingMeta(total_speaking_sec, segment_count);

  // Freeze the FIRST non-null snippet URL we ever receive and keep using it.
  //
  // JobStatus polls /api/jobs every 3s and the backend mints a brand-new
  // presigned URL on every response (fresh signature/expiry). Without this
  // freeze, the `snippet_url` string changes on each poll, React updates
  // <audio src>, and the browser tears down + reloads the element — which
  // resets playback. That was the "plays only 1-2s then stops" bug: audio
  // played until the next 3s poll swapped the src. The presigned URL is
  // valid for 1h, so reusing the first one is safe for the whole selection.
  const frozenSrcRef = useRef(null);
  if (snippet_url && !frozenSrcRef.current) {
    frozenSrcRef.current = snippet_url;
  }
  const audioSrc = frozenSrcRef.current;

  // Custom player: a hidden <audio> driven by a play/pause button, with a
  // decorative equalizer that dances while the sample plays. isPlaying is
  // sourced from the element's own events so it stays in sync no matter how
  // playback ends (natural end, pause, another element stealing focus).
  const audioRef = useRef(null);
  const [isPlaying, setIsPlaying] = useState(false);

  function togglePlay() {
    const el = audioRef.current;
    if (!el) return;
    if (el.paused) el.play().catch(() => {});
    else el.pause();
  }

  return (
    <motion.div
      variants={{
        hidden: { opacity: 0, y: 18 },
        show: { opacity: 1, y: 0 },
      }}
      className={`glass relative flex flex-col gap-4 p-5 transition-all duration-200 ${
        isSelecting
          ? "border-accent/50 shadow-glow-accent"
          : "hover:border-white/15"
      } ${disabled && !isSelecting ? "opacity-50" : ""}`}
      data-testid={`speaker-card-${label}`}
    >
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-3">
          <span className="grid h-10 w-10 place-items-center rounded-xl border border-white/10 bg-white/[0.03] text-accent-soft">
            <AudioLines className="h-5 w-5" />
          </span>
          <div>
            <p
              className="text-base font-semibold leading-tight text-white"
              data-testid={`speaker-name-${label}`}
            >
              {displayName}
            </p>
            <p
              className="mt-1 flex items-center gap-3 font-mono text-xs text-slate-500"
              data-testid={`speaker-meta-${label}`}
            >
              <span className="inline-flex items-center gap-1">
                <Clock className="h-3 w-3" />
                {timeStr}
              </span>
              <span className="inline-flex items-center gap-1">
                <Hash className="h-3 w-3" />
                {segStr}
              </span>
            </p>
          </div>
        </div>
      </div>

      <div className="rounded-xl border border-white/[0.06] bg-ink-950/60 p-3">
        {audioSrc ? (
          <div
            className="flex items-center gap-3"
            data-testid={`speaker-player-${label}`}
          >
            <button
              type="button"
              onClick={togglePlay}
              className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-accent-grad text-white shadow-glow-accent transition-transform duration-200 hover:scale-105 active:scale-95"
              aria-label={isPlaying ? "Pause voice sample" : "Play voice sample"}
              data-testid={`speaker-play-btn-${label}`}
            >
              {isPlaying ? (
                <Pause className="h-4 w-4" />
              ) : (
                <Play className="h-4 w-4 translate-x-px" />
              )}
            </button>
            <div
              className={`eq flex-1 ${isPlaying ? "eq-playing" : ""}`}
              aria-hidden="true"
            >
              {Array.from({ length: 28 }).map((_, i) => (
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
              src={audioSrc}
              onPlay={() => setIsPlaying(true)}
              onPause={() => setIsPlaying(false)}
              onEnded={() => setIsPlaying(false)}
              className="hidden"
              data-testid={`speaker-audio-${label}`}
            />
          </div>
        ) : (
          <p
            className="rounded-lg border border-dashed border-white/10 px-3 py-2.5 text-center font-mono text-xs text-slate-600"
            data-testid={`speaker-audio-missing-${label}`}
          >
            No preview available
          </p>
        )}
      </div>

      <motion.button
        type="button"
        whileHover={!disabled && !isSelecting ? { y: -2 } : {}}
        whileTap={!disabled && !isSelecting ? { scale: 0.98 } : {}}
        className="btn-primary w-full"
        onClick={() => onSelect(label)}
        disabled={disabled || isSelecting}
        data-testid={`speaker-select-btn-${label}`}
      >
        {isSelecting ? (
          <>
            <Loader2 className="h-4 w-4 animate-spin" />
            Selecting…
          </>
        ) : (
          <>
            <Check className="h-4 w-4" />
            This is me
          </>
        )}
      </motion.button>
    </motion.div>
  );
}

function formatSpeakingMeta(sec, count) {
  const seconds = Number(sec) || 0;
  let timeStr;
  if (seconds < 60) {
    const s = Math.max(1, Math.round(seconds));
    timeStr = `${s} second${s === 1 ? "" : "s"}`;
  } else {
    const m = Math.round(seconds / 60);
    timeStr = `${m} minute${m === 1 ? "" : "s"}`;
  }
  const segments = Number(count) || 0;
  const segStr = `${segments} segment${segments === 1 ? "" : "s"}`;
  return { timeStr, segStr };
}
