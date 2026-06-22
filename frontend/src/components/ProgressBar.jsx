import React from "react";
import { motion } from "framer-motion";

/**
 * Linear progress bar. `percent` is 0-100.
 */
export default function ProgressBar({ percent, testId }) {
  const value = Math.max(0, Math.min(100, Number(percent) || 0));
  return (
    <div data-testid={testId || "progress-bar"}>
      <div
        className="relative h-2 w-full overflow-hidden rounded-full border border-white/[0.06] bg-ink-950"
        role="progressbar"
        aria-valuenow={value}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <motion.div
          className="relative h-full rounded-full bg-accent-grad"
          initial={false}
          animate={{ width: `${value}%` }}
          transition={{ duration: 0.6, ease: [0.22, 1, 0.36, 1] }}
        >
          {/* moving sheen */}
          <div className="absolute inset-0 overflow-hidden rounded-full">
            <div className="absolute inset-y-0 -left-full w-full animate-shimmer bg-gradient-to-r from-transparent via-white/40 to-transparent" />
          </div>
        </motion.div>
      </div>
      <p
        className="mt-2.5 font-mono text-xs tabular-nums text-slate-500"
        data-testid="progress-percent"
      >
        {value.toFixed(0)}%
      </p>
    </div>
  );
}
