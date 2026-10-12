import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { AuthProvider } from "./auth";
import { ErrorBoundary } from "./components/ErrorBoundary";
// Self-hosted fonts (no external requests):
import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/600.css";
import "@fontsource/inter/700.css";
// Display font: Archivo Variable, the `wdth` file — it carries the width AND the weight axis in
// ONE file (font-stretch 62–125%, font-weight 100–900). The per-game art style takes its label
// typography from it (`--g-label-*` in index.css) without loading a font file per game.
import "@fontsource-variable/archivo/wdth.css";
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/600.css";
import "./lib/theme"; // applies the stored/system theme before rendering
import "./i18n"; // picks the language before the first render
import "./index.css";

// The second ErrorBoundary, and the more important one: the one in `Layout.tsx` sits INSIDE the
// `FeedbackProvider` and by construction cannot catch that provider's own render error — React
// only catches errors of descendants (an object in a toast would end in a blank page). This one
// sits above everything and turns any provider error into a panel.
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <BrowserRouter>
        <AuthProvider>
          <App />
        </AuthProvider>
      </BrowserRouter>
    </ErrorBoundary>
  </React.StrictMode>,
);
