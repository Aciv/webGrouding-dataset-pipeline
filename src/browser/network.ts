import type { Page } from "playwright";

/** Bounded resource diagnostics; HTTP errors are evidence, not automatically page failures. */
export interface NetworkEvent { kind: string; url: string; detail: string }

/** Track one page lifecycle. The owning page/context removes listeners when closed. */
export function observeNetwork(page: Page): NetworkEvent[] {
  const events: NetworkEvent[] = [];
  page.on("requestfailed", request => {
    if (events.length < 100) events.push({ kind: "requestfailed", url: request.url(), detail: request.failure()?.errorText ?? "" });
  });
  page.on("response", response => {
    if (response.status() >= 400 && events.length < 100)
      events.push({ kind: "http", url: response.url(), detail: String(response.status()) });
  });
  return events;
}
