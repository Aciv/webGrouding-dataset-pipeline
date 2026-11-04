import type { Browser, BrowserContext, Page } from "playwright";
import type { Config, Source, Viewport } from "../../domain/types.js";
import { withinScope } from "../../domain/discovery/url-policy.js";
import { observeNetwork, type NetworkEvent } from "../network.js";
import { BrowserInfrastructureError, requireBrowser } from "./browser-failure.js";

/** Per-source browser resources and per-visit network evidence; the shared Chromium is not owned here. */
export interface DiscoverySession {
  page: Page;
  network: NetworkEvent[];
  navigate(url: string): Promise<void>;
  close(): Promise<void>;
}

/** Open one isolated page, closing a partially initialized context if setup fails. */
export async function openDiscoverySession(browser: Browser, config: Config, source: Source,
    reference: Viewport, entry: string): Promise<DiscoverySession> {
  let context: BrowserContext | undefined;
  try {
    requireBrowser(browser);
    context = await browser.newContext({ viewport: reference, deviceScaleFactor: 1,
      locale: "en-US", timezoneId: "UTC", colorScheme: "light" });
    const page = await context.newPage();
    const network = observeNetwork(page);
    return { page, network,
      navigate: async url => {
        if (!withinScope(entry, source.pathPrefix ?? "/", url)) throw new Error("Requested URL outside source scope");
        await page.setViewportSize(reference);
        const response = await page.goto(url, { waitUntil: "domcontentloaded", timeout: config.navigationTimeoutMs });
        if (response && response.status() >= 400) throw new Error(`Navigation HTTP ${response.status()}`);
        if (!withinScope(entry, source.pathPrefix ?? "/", page.url()))
          throw new Error(`Redirect outside source scope: ${url} -> ${page.url()}`);
        await page.waitForTimeout(config.settleMs);
      },
      close: () => context!.close(),
    };
  } catch (error) {
    await context?.close().catch(() => {});
    throw new BrowserInfrastructureError(`Browser session creation failed: ${String(error)}`, { cause: error });
  }
}
