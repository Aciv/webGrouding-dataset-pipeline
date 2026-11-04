import type { Browser } from "playwright";

/** Executor failure is retryable infrastructure state, never evidence that a website is invalid. */
export class BrowserInfrastructureError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "BrowserInfrastructureError";
  }
}

/** Gate context admission and results even before Playwright delivers its disconnected event. */
export function requireBrowser(browser: Browser): void {
  if (!browser.isConnected()) throw new BrowserInfrastructureError("Shared Chromium is disconnected");
}
