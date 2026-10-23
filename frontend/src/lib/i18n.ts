import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import en from "../locales/en.json";
import pt from "../locales/pt-BR.json";
import es from "../locales/es.json";
import type { Locale } from "./types";
export function resolveLocale(values: readonly string[]): Locale {
  for (const value of values) {
    const base = value.toLowerCase().split("-")[0];
    if (base === "pt") return "pt-BR";
    if (base === "en" || base === "es") return base;
  }
  return "en";
}
let stored: string | null = null;
try {
  stored = localStorage.getItem("brickpulse.locale");
} catch {
  /* Preferences may be disabled by browser policy. */
}
const initial =
  stored && ["en", "pt-BR", "es"].includes(stored)
    ? stored
    : resolveLocale(navigator.languages ?? [navigator.language]);
void i18n.use(initReactI18next).init({
  resources: {
    en: { translation: en },
    "pt-BR": { translation: pt },
    es: { translation: es },
  },
  lng: initial,
  fallbackLng: "en",
  interpolation: { escapeValue: false },
  returnNull: false,
});
export function setLocale(locale: Locale) {
  try {
    localStorage.setItem("brickpulse.locale", locale);
  } catch {
    /* Session-only selection remains usable. */
  }
  void i18n.changeLanguage(locale);
}
i18n.on("languageChanged", (locale) => {
  document.documentElement.lang = locale;
});
document.documentElement.lang = initial;
export default i18n;
