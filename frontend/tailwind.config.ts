import type { Config } from "tailwindcss";

// The palette is PLAN.md §2: dark terminal, muted borders, no pure black. The three brand
// colours are fixed by the spec; the rest is the surrounding neutral scale.
const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        base: "#0d1117",        // page
        panel: "#12181f",       // cards
        raised: "#161d26",      // rows, inputs
        edge: "#232b36",        // borders
        edgeBright: "#303b48",  // hover borders
        ink: "#e6edf3",         // primary text
        muted: "#8b97a6",       // secondary text
        faint: "#5b6673",       // tertiary text / axes
        accent: "#ecad0a",      // PLAN §2 accent yellow
        brand: "#209dd7",       // PLAN §2 blue primary
        submit: "#753991",      // PLAN §2 purple, submit buttons
        up: "#3fb950",
        down: "#f85149",
      },
      fontFamily: {
        // System stacks only: no network fetch at build time, and a terminal should look
        // like the machine it runs on.
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
        sans: ["ui-sans-serif", "system-ui", "Segoe UI", "Roboto", "sans-serif"],
      },
      fontSize: {
        "2xs": ["0.6875rem", { lineHeight: "0.875rem" }],
      },
    },
  },
  plugins: [],
};

export default config;
