import { translate } from "./i18n";

/** Prefer stable, localized codes and never stringify a structured error object. */
export function apiErrorMessage(detail: unknown, fallback: string): string {
  if (typeof detail === "string") return translate(detail) || fallback;
  if (detail && typeof detail === "object") {
    const error = detail as { code?: unknown; message?: unknown };
    if (typeof error.code === "string") {
      const message = translate(error.code);
      if (message !== error.code) return message;
    }
    if (typeof error.message === "string") return error.message;
  }
  return fallback;
}
