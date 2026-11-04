import type { Page } from "playwright";
import type { LinkCandidate, SiteDiscovery } from "../../domain/discovery/types.js";
import { candidateUrl, pageIdentity, safeMenuName, withinScope } from "../../domain/discovery/url-policy.js";

/** Read declared anchors including hidden menus, adapted from View URL navigation inventory.
 * This bounded reader does not extract dense DOM geometry or recurse into child pages.
 */
async function inventory(page: Page) {
  return page.evaluate(() => ({
    links: [...document.querySelectorAll<HTMLAnchorElement>("a[href]")].slice(0, 3000).map((a, index) => ({
      href: a.href, text: (a.textContent || "").trim().slice(0, 160),
      selector: `a >> nth=${index}`, download: a.hasAttribute("download"),
      navigation: Boolean(a.closest('nav,header,[role="navigation"]')),
    })),
    menus: [...document.querySelectorAll<HTMLElement>('button,[role="button"],summary,a[aria-expanded],a[aria-haspopup]')]
      .slice(0, 500).map((element, index) => ({
        selector: `button,[role="button"],summary,a[aria-expanded],a[aria-haspopup] >> nth=${index}`,
        name: [element.getAttribute("aria-label"), element.textContent, element.id, element.className].join(" ").trim().slice(0, 300),
        menu: element.matches('summary,[aria-haspopup],[aria-expanded]') || /menu|navigation|菜单|导航/i.test(
          `${element.getAttribute("aria-label")} ${element.id} ${element.className}`),
        visible: element.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }) &&
          element.getBoundingClientRect().width > 0 && element.getBoundingClientRect().height > 0,
      })),
  }));
}

/** Discover URLs on the entry page only; read hidden hrefs before attempting bounded menu actions. */
export async function discoverHomepage(page: Page, site: SiteDiscovery, options: {
    prefix: string; maxCandidates: number; menuInteractions: number; desiredPages: number;
    navigationTimeoutMs: number; settleMs: number;
  }): Promise<void> {
  const entry = site.entryUrl, seen = new Set(site.candidates.map(c => pageIdentity(c.url)));
  /** Rank nav anchors first, while identity/scope filters prevent external and fragment candidates. */
  const addLinks = (data: Awaited<ReturnType<typeof inventory>>, method: "href" | "menu_href", menuSelector?: string) => {
    for (const link of [...data.links].sort((a, b) => Number(b.navigation) - Number(a.navigation))) {
      if (site.candidates.length >= options.maxCandidates) break;
      const target = !link.download && candidateUrl(link.href, entry, entry, options.prefix);
      if (!target || seen.has(pageIdentity(target))) continue;
      seen.add(pageIdentity(target));
      site.candidates.push({ url: target, fromUrl: entry, method, selector: link.selector, text: link.text, menuSelector });
    }
  };
  const initial = await inventory(page);
  addLinks(initial, "href");
  // Plenty of declared destinations means no need to replay menus just to rediscover their links.
  if (site.candidates.length < options.desiredPages) {
    const menus = initial.menus.filter(m => m.menu && m.visible && safeMenuName(m.name)).slice(0, options.menuInteractions);
    for (const menu of menus) {
      try {
        await page.goto(entry, { waitUntil: "domcontentloaded", timeout: options.navigationTimeoutMs });
        if (!withinScope(entry, options.prefix, page.url())) throw new Error("Homepage redirected outside source scope");
        const control = page.locator(menu.selector).first();
        await control.hover({ timeout: 1500 });
        await page.waitForTimeout(options.settleMs);
        const count = site.candidates.length;
        addLinks(await inventory(page), "menu_href", menu.selector);
        if (site.candidates.length === count) {
          await control.click({ timeout: 1500 });
          await page.waitForTimeout(options.settleMs);
          const target = candidateUrl(page.url(), entry, entry, options.prefix);
          if (target && !seen.has(pageIdentity(target))) {
            seen.add(pageIdentity(target));
            site.candidates.push({ url: target, fromUrl: entry, method: "menu_navigation", menuSelector: menu.selector });
          }
          if (pageIdentity(page.url()) === pageIdentity(entry)) addLinks(await inventory(page), "menu_href", menu.selector);
        }
        site.menuEvents.push({ selector: menu.selector, status: "checked" });
      } catch (error) {
        site.menuEvents.push({ selector: menu.selector, status: "failed", error: error instanceof Error ? error.message : String(error) });
      }
      if (site.candidates.length >= options.desiredPages || site.candidates.length >= options.maxCandidates) break;
    }
  }
  site.candidates = site.candidates.slice(0, options.maxCandidates);
  site.inventoryComplete = true;
}
