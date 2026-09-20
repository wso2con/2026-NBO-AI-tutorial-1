// Tiny theme manager. shadcn's pattern: toggle a `.dark` class on the
// <html> element; Tailwind picks it up because tailwind.config.js sets
// `darkMode: ["class"]`. We persist the choice in localStorage and fall
// back to the OS preference on first load.

import { useEffect, useState } from "react";

export type Theme = "light" | "dark";

const STORAGE_KEY = "lab1-theme";

function resolveInitial(): Theme {
  if (typeof window === "undefined") return "light";
  const stored = window.localStorage.getItem(STORAGE_KEY);
  if (stored === "light" || stored === "dark") return stored;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function applyTheme(theme: Theme) {
  const root = document.documentElement;
  root.classList.toggle("dark", theme === "dark");
}

export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(resolveInitial);

  useEffect(() => {
    applyTheme(theme);
    window.localStorage.setItem(STORAGE_KEY, theme);
  }, [theme]);

  const toggle = () => setTheme((t) => (t === "dark" ? "light" : "dark"));
  return [theme, toggle];
}
