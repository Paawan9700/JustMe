import React, { useRef, useState } from "react";
import { motion } from "framer-motion";
import { Check, Loader2, AudioLines, Clock, Hash, Play, Pause, Heart } from "lucide-react";

/**
 * One speaker option in the AWAITING_SELECTION state.
 *
 * Props:
 *   - speaker: { label, total_speaking_sec, segment_count, snippet_url, can_favorite }
 *   - displayName: "Speaker 1"
 *   - onSelect(label)
 *   - isSelecting: boolean
 *   - disabled: boolean (other card is selecting)
 *   - showHeart: render the favourite toggle at all
 *   - favorite: { favorite_id, name, in_box, strength } | null — current heart state
 *   - onToggleFavorite(speaker), favoriteBusy, heartDisabled
 */
export default function SpeakerCard({
  speaker,
  displayName,
  onSelect,
  isSelecting,
  disabled,
  showHeart = false,
  favorite = null,
  onToggleFavorite,
  favoriteBusy = false,
  heartDisabled = false,
}) {
  const { label, total_speaking_sec, segment_count, snippet_url, can_favorite } = speaker;
  // The user's own name for a favourite voice wins; nothing is ever named for them.
  const title = (favorite && favorite.name) || displayName;
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
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl border border-white/10 bg-white/[0.03] text-accent-soft">
            <AudioLines className="h-5 w-5" />
          </span>
          <div className="min-w-0">
            {/* The user's full name for a favourite must be readable, so it
                wraps (up to two lines) instead of being cut off. */}
            <p
              className="line-clamp-2 break-words text-base font-semibold leading-snug text-white"
              title={title}
              data-testid={`speaker-name-${label}`}
            >
              {title}
            </p>
            <p
              className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 font-mono text-xs text-slate-500"
              data-testid={`speaker-meta-${label}`}
            >
              <span className="inline-flex items-center gap-1 whitespace-nowrap">
                <Clock className="h-3 w-3" />
                {timeStr}
              </span>
              <span className="inline-flex items-center gap-1 whitespace-nowrap">
                <Hash className="h-3 w-3" />
                {segStr}
              </span>
            </p>
            {favorite && favorite.in_box && (
              <p className="mt-1.5 inline-flex items-center gap-1.5 rounded-full border border-rose-400/25 bg-rose-400/10 px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.12em] text-rose-200">
                <Heart className="h-3 w-3 fill-current" />
                {favorite.strength === "likely" ? "Possible match" : "Favourite"}
              </p>
            )}
          </div>
        </div>
        {showHeart && (
          <HeartButton
            filled={!!favorite}
            busy={favoriteBusy}
            disabled={heartDisabled || (!favorite && !can_favorite)}
            cannotSave={!favorite && !can_favorite}
            onClick={() => onToggleFavorite && onToggleFavorite(speaker)}
            label={label}
          />
        )}
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
        className="btn-primary mt-auto w-full"
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

function HeartButton({ filled, busy, disabled, cannotSave, onClick, label }) {
  const hint = cannotSave
    ? "Not enough clear speech to remember this voice"
    : filled
      ? "Remove from favourites"
      : "Save to favourites";
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled || busy}
      aria-pressed={filled}
      aria-label={hint}
      title={hint}
      className={`grid h-9 w-9 shrink-0 place-items-center rounded-xl border transition-all duration-200 disabled:cursor-not-allowed ${
        filled
          ? "border-rose-400/40 bg-rose-400/15 text-rose-300 hover:bg-rose-400/25"
          : "border-white/10 bg-white/[0.03] text-slate-400 hover:border-rose-400/40 hover:text-rose-300"
      } ${disabled && !busy ? "opacity-40" : ""}`}
      data-testid={`speaker-fav-btn-${label}`}
    >
      {busy ? (
        <Loader2 className="h-4 w-4 animate-spin" />
      ) : (
        <Heart className={`h-4 w-4 ${filled ? "fill-current" : ""}`} />
      )}
    </button>
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
