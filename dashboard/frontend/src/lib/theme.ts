// Light/dark theme: the stored choice (localStorage) or the system preference.
// `/theme-init.js` (loaded by index.html) sets the class before anything renders (no flash);
// this module handles switching at runtime afterwards.
import { useSyncExternalStore } from "react";

export type Theme = "light" | "dark";

function systemTheme(): Theme {
  return typeof matchMedia !== "undefined" && matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

export function currentTheme(): Theme {
  return document.documentElement.classList.contains("dark") ? "dark" : "light";
}

const listeners = new Set<() => void>();

function apply(theme: Theme): void {
  const root = document.documentElement;
  root.classList.toggle("dark", theme === "dark");
  root.classList.toggle("light", theme === "light");
  listeners.forEach((l) => l());
}

export function setTheme(theme: Theme): void {
  try {
    localStorage.setItem("theme", theme);
  } catch {
    /* localStorage may be blocked — the theme still applies for this session */
  }
  apply(theme);
}

export function toggleTheme(): void {
  setTheme(currentTheme() === "dark" ? "light" : "dark");
}

// Fallback in case `/theme-init.js` did not run: make sure one of the classes is set.
(function init() {
  const root = document.documentElement;
  if (!root.classList.contains("dark") && !root.classList.contains("light")) {
    let saved: string | null = null;
    try {
      saved = localStorage.getItem("theme");
    } catch {
      /* ignore */
    }
    apply(saved === "dark" || saved === "light" ? (saved as Theme) : systemTheme());
  }
})();

/** Reactive theme value for the toggle button. */
export function useTheme(): Theme {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    currentTheme,
    () => "light" as Theme,
  );
}
