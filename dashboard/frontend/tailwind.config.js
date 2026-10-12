/** @type {import('tailwindcss').Config} */

// All colours are backed by CSS variables (RGB channels) so that both the light/dark switch
// (variables in index.css) and Tailwind's alpha modifier (e.g. `bg-surface-card/60`) work.
const v = (name) => `rgb(var(--${name}) / <alpha-value>)`;

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        // ONE scale, named semantically. The legacy names `ink`/`bone`/`blood`/`toxic`/`rust`
        // (the same variables under other names) must not be defined here again: two names for
        // one colour mean every change has to be thought of twice. `src/colour-scale.test.ts`
        // keeps the legacy names out.
        surface: {
          DEFAULT: v("ink"),        // app background
          raised: v("ink-900"),     // sidebar / top bar / inputs
          card: v("ink-800"),       // cards
          sunken: v("ink-700"),     // hover / sunken / skeleton
        },
        line: {
          DEFAULT: v("ink-600"),    // hairline
          soft: v("ink-700"),       // very subtle divider
          strong: v("ink-500"),     // emphasised edge / scrollbar
        },
        fg: {
          DEFAULT: v("bone"),       // main text
          muted: v("bone-muted"),   // secondary text
          dim: v("bone-dim"),       // tertiary / placeholder
        },
        primary: {
          DEFAULT: v("primary"),
          strong: v("primary-strong"),
          soft: v("primary-soft"),
          text: v("primary-text"),
          btn: v("btn-primary"),
          fg: v("on-primary"),
        },
        accent: { DEFAULT: v("accent"), soft: v("accent-soft") },
        success: { DEFAULT: v("toxic"), text: v("toxic-light"), soft: v("toxic-deep"), border: v("toxic-dark") },
        warning: { DEFAULT: v("rust"), text: v("rust-light") },
        danger: { DEFAULT: v("blood"), text: v("blood-light"), soft: v("blood-deep"), border: v("blood-dark") },
      },
      fontFamily: {
        // Display: Archivo Variable — ONE file with a width (62–125%) and a weight axis.
        // The per-game art style uses it through `font-stretch`/`font-weight`
        // (`--g-label-*` in index.css) instead of loading a second font file.
        display: ['"Archivo Variable"', "Inter", "system-ui", "sans-serif"],
        body: ["Inter", "system-ui", "sans-serif"],
        mono: ['"JetBrains Mono"', "ui-monospace", "SFMono-Regular", "monospace"],
      },
      // Tight radii: the tool should look like a tool, not like a landing page.
      // (`rounded-xl`/`rounded-2xl` are used everywhere, so the values are set here centrally
      //  instead of in every class name.)
      borderRadius: {
        DEFAULT: "0.25rem",
        md: "0.3125rem",
        lg: "0.375rem",
        xl: "0.5rem",
        "2xl": "0.625rem",
      },
      boxShadow: {
        // Hairline first, shadow only as a hint. The edge carries, not the blur.
        panel: "0 0 0 1px rgb(var(--shadow) / 0.05), 0 1px 2px 0 rgb(var(--shadow) / 0.05)",
        soft: "0 0 0 1px rgb(var(--shadow) / 0.06), 0 2px 8px -4px rgb(var(--shadow) / 0.16)",
        glow: "0 0 0 1px rgb(var(--primary) / 0.4)",
        ring: "0 0 0 1px rgb(var(--primary) / 0.22)",
      },
    },
  },
  plugins: [],
};
