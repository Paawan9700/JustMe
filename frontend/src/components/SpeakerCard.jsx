import React from "react";

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
        {snippet_url ? (
          <audio
            controls
            preload="none"
            src={snippet_url}
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
