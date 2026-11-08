import "@fontsource-variable/inter";
import "@fontsource-variable/jetbrains-mono";
import "./styles/index.css";
import "./i18n";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import { ErrorBoundary } from "./components/ErrorBoundary";

let stored: string | null;
try {
  stored = window.localStorage.getItem("codepilot.theme");
} catch {
  stored = null;
}
const prefersDark = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
const dark = stored === "dark" || (stored !== "light" && prefersDark);
document.documentElement.classList.toggle("dark", dark);
document.documentElement.dataset.theme = dark ? "dark" : "light";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
);
