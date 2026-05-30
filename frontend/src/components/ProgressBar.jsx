import React from "react";

/**
 * Linear progress bar. `percent` is 0-100.
 */
export default function ProgressBar({ percent, testId }) {
  const value = Math.max(0, Math.min(100, Number(percent) || 0));
  return (
    <div data-testid={testId || "progress-bar"}>
      <div
        className="progress-track"
        role="progressbar"
        aria-valuenow={value}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <div className="progress-fill" style={{ width: `${value}%` }} />
      </div>
      <p className="progress-percent" data-testid="progress-percent">
        {value.toFixed(0)}%
      </p>
    </div>
  );
}
