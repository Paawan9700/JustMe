import React, { useEffect, useState } from "react";
import { MotionConfig, motion } from "framer-motion";
import { AudioLines, Link2, Scissors } from "lucide-react";

// The three plain-language steps of what Alphavox does, in journey order.
// Each step lights up in turn, the connector below it fills as the signal
// travels to the next one, then the loop restarts. Lives on the home page,
// in the space to the right of the hero (it used to crowd the top bar).
const STEPS = [
  { label: "Paste a link", hint: "Any long YouTube video", icon: Link2 },
  { label: "Pick your voice", hint: "Hear each speaker, choose yours", icon: AudioLines },
  { label: "Get just you", hint: "One clean cut of only your words", icon: Scissors },
];
const STEP_MS = 2400;
const EASE = [0.22, 1, 0.36, 1];

export default function HowItWorks({ className = "" }) {
  const [active, setActive] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setActive((prev) => (prev + 1) % STEPS.length), STEP_MS);
    return () => clearInterval(id);
  }, []);

  return (
    <MotionConfig reducedMotion="user">
      <section className={`glass p-6 ${className}`} aria-label="How Alphavox works" data-testid="how-it-works">
        <span className="label-mono">How it works</span>
        <ol className="mt-5">
          {STEPS.map(({ label, hint, icon: Icon }, idx) => {
            const isActive = idx === active;
            const isDone = idx < active;
            return (
              <li key={label} className="flex gap-4">
                <div className="flex flex-col items-center">
                  <span
                    className={`relative grid h-10 w-10 shrink-0 place-items-center rounded-xl border transition-all duration-300 ${
                      isActive
                        ? "border-accent/60 bg-accent/15 text-white shadow-glow-accent"
                        : isDone
                          ? "border-accent/30 bg-accent/[0.06] text-accent-soft"
                          : "border-white/10 bg-white/[0.03] text-slate-500"
                    }`}
                  >
                    <Icon className="h-[18px] w-[18px]" />
                    {isActive && (
                      <span className="absolute -right-1 -top-1 h-2.5 w-2.5 rounded-full bg-accent motion-safe:animate-ping" />
                    )}
                  </span>
                  {idx < STEPS.length - 1 && (
                    <span aria-hidden="true" className="relative my-1.5 w-px flex-1 overflow-hidden rounded-full bg-white/10" style={{ minHeight: 28 }}>
                      <motion.span
                        className="absolute inset-0 origin-top bg-gradient-to-b from-accent to-accent-blue"
                        initial={false}
                        animate={{ scaleY: idx < active ? 1 : 0 }}
                        transition={{ duration: idx < active ? 0.55 : 0.3, ease: EASE }}
                      />
                    </span>
                  )}
                </div>
                <div className={idx < STEPS.length - 1 ? "pb-5" : ""}>
                  <p
                    className={`pt-1 text-sm font-semibold transition-colors duration-300 ${
                      isActive ? "text-white" : isDone ? "text-slate-300" : "text-slate-500"
                    }`}
                  >
                    {label}
                  </p>
                  <p className="mt-0.5 text-xs text-slate-500">{hint}</p>
                </div>
              </li>
            );
          })}
        </ol>
      </section>
    </MotionConfig>
  );
}
