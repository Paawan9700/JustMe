/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./src/**/*.{js,jsx}", "./public/index.html"],
  theme: {
    extend: {
      colors: {
        // Deep slate / charcoal base — dark-mode-first financial theme
        ink: {
          950: "#06080d",
          900: "#0a0e16",
          850: "#0e131e",
          800: "#121826",
          700: "#1a2233",
          600: "#26314a",
        },
        // Action / CTA — electric violet → blue
        accent: {
          DEFAULT: "#7c5cff",
          soft: "#9d86ff",
          deep: "#5b3fe0",
          blue: "#3b82f6",
        },
        // Bullish / success
        bull: {
          DEFAULT: "#34d399",
          soft: "#6ee7b7",
          deep: "#059669",
        },
        // Bearish / warning / error
        bear: {
          DEFAULT: "#fb7185",
          soft: "#fda4af",
          deep: "#e11d48",
        },
      },
      fontFamily: {
        sans: [
          "Inter",
          "SF Pro Display",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "sans-serif",
        ],
        mono: [
          "JetBrains Mono",
          "SF Mono",
          "ui-monospace",
          "Cascadia Code",
          "Roboto Mono",
          "monospace",
        ],
      },
      borderRadius: {
        xl: "14px",
        "2xl": "20px",
        "3xl": "28px",
      },
      boxShadow: {
        glass: "0 8px 40px rgba(0,0,0,0.45), inset 0 1px 0 rgba(255,255,255,0.04)",
        "glow-accent": "0 10px 36px -6px rgba(124,92,255,0.55)",
        "glow-bull": "0 10px 36px -6px rgba(52,211,153,0.45)",
        "glow-bear": "0 10px 36px -6px rgba(251,113,133,0.4)",
      },
      backgroundImage: {
        "accent-grad": "linear-gradient(135deg, #7c5cff 0%, #3b82f6 100%)",
        "accent-grad-hi": "linear-gradient(135deg, #9d86ff 0%, #60a5fa 100%)",
        "bull-grad": "linear-gradient(135deg, #34d399 0%, #059669 100%)",
        "grid-faint":
          "linear-gradient(rgba(255,255,255,0.022) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,0.022) 1px, transparent 1px)",
      },
      keyframes: {
        "fade-up": {
          "0%": { opacity: "0", transform: "translateY(10px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        shimmer: {
          "100%": { transform: "translateX(100%)" },
        },
        "pulse-ring": {
          "0%": { boxShadow: "0 0 0 0 rgba(124,92,255,0.45)" },
          "70%": { boxShadow: "0 0 0 10px rgba(124,92,255,0)" },
          "100%": { boxShadow: "0 0 0 0 rgba(124,92,255,0)" },
        },
        float: {
          "0%,100%": { transform: "translateY(0)" },
          "50%": { transform: "translateY(-6px)" },
        },
      },
      animation: {
        "fade-up": "fade-up 0.5s cubic-bezier(0.22,1,0.36,1) both",
        shimmer: "shimmer 1.8s infinite",
        "pulse-ring": "pulse-ring 1.8s cubic-bezier(0.66,0,0,1) infinite",
        float: "float 4s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};
