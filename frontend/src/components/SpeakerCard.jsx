import React, { useRef } from "react";

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
  const meta = formatSpeakingMeta(total_speaking_sec, segment_count);

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

  return (
    <div
      className={`speaker-card ${isSelecting ? "is-selecting" : ""}`}
      data-testid={`speaker-card-${label}`}
    >
      <div>
        <p className="speaker-label" data-testid={`speaker-name-${label}`}>
          {displayName}
        </p>
        <p className="speaker-meta" data-testid={`speaker-meta-${label}`}>
          {meta}
        </p>
      </div>

      <div className="audio-player">
        {audioSrc ? (
          <audio
            controls
            preload="metadata"
            src={audioSrc}
            data-testid={`speaker-audio-${label}`}
          />
        ) : (
          <p className="audio-missing" data-testid={`speaker-audio-missing-${label}`}>
            No preview available
          </p>
        )}
      </div>

      <button
        type="button"
        className="primary-btn"
        onClick={() => onSelect(label)}
        disabled={disabled || isSelecting}
        data-testid={`speaker-select-btn-${label}`}
      >
        {isSelecting ? "Selecting…" : "This is me \u2713"}
      </button>
    </div>
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
  return `Spoke for ${timeStr}, ${segStr}`;
}
