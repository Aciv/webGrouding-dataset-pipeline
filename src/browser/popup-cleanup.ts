import type { Page } from "playwright";

/** Remove popup presentation before geometry/image capture; never activate consent or business actions.
 * Removes entire subtrees from both screenshot and raw DOM; quality metadata retains removal evidence.
 * Re-run after preparation to catch delayed overlays. Bottom floating UI is removed
 * only with hit-stack evidence of covered content; ordinary navigation is retained.
 */
export async function removeCapturePopups(page: Page) {
  return page.evaluate(() => {
    const removed: { tag: string; id: string; className: string; reason: string }[] = [];
    const nodes = [...document.querySelectorAll<HTMLElement>("body *")];
    const visible = (node: HTMLElement) => node.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true });
    const identity = (node: HTMLElement) => `${node.id} ${node.getAttribute("class") || ""}`;
    const popupName = /(?:^|[\s_-])(?:modal|popup|pop-up|lightbox|interstitial)(?:$|[\s_-])/i;
    const backdropName = /(?:^|[\s_-])(?:backdrop|overlay|scrim)(?:$|[\s_-])/i;
    const consentName = /cookie|consent|onetrust|didomi|cmpbox|qc-cmp|privacy-banner|gdpr/i;
    const floating = (node: HTMLElement) => {
      for (let current: HTMLElement | null = node; current && current !== document.body; current = current.parentElement) {
        if (/^(fixed|sticky|absolute)$/.test(getComputedStyle(current).position)) return true;
      }
      return false;
    };
    // Fixed elements follow the viewport by CSS definition; avoid scrolling here,
    // which could trigger lazy loading and change the page being measured.
    const coversBottomContent = (node: HTMLElement): boolean => {
      if (!/^(fixed|sticky)$/.test(getComputedStyle(node).position) ||
          node.matches('nav,header,[role="navigation"],[role="banner"]') ||
          node.querySelector('nav,[role="navigation"]')) return false;
      const rect = node.getBoundingClientRect();
      const left = Math.max(0, rect.left), right = Math.min(innerWidth, rect.right);
      const top = Math.max(0, rect.top), bottom = Math.min(innerHeight, rect.bottom);
      if (right - left < 10 || bottom - top < 10 ||
          top < innerHeight * .45 || bottom < innerHeight * .8) return false;
      let covered = 0;
      for (const x of [.15, .5, .85]) for (const y of [.2, .5, .8]) {
        const stack = document.elementsFromPoint(left + (right - left) * x, top + (bottom - top) * y);
        const front = stack.findIndex(element => element === node || node.contains(element));
        if (front < 0) continue;
        const underlying = stack.slice(front + 1).find(element =>
          element !== node && !node.contains(element) && !element.contains(node));
        if (!underlying) continue;
        // A body/main-sized empty wrapper is not evidence of painted content.
        const content = underlying.matches('img,svg,canvas,video,iframe,input,button,select,textarea') ||
          [...underlying.childNodes].some(child => child.nodeType === Node.TEXT_NODE && child.textContent?.trim());
        if (content) covered++;
      }
      return covered >= 2;
    };
    const remove = (node: HTMLElement, reason: string) => {
      if (!node.isConnected) return;
      removed.push({ tag: node.tagName.toLowerCase(), id: node.id,
        className: node.getAttribute("class") || "", reason });
      // A native modal remains in the top layer until close(), even when CSS hides its box.
      if (node instanceof HTMLDialogElement && node.open) node.close();
      node.remove();
    };
    for (const node of nodes) {
      if (!node.isConnected || !visible(node)) continue;
      const role = node.getAttribute("role");
      if (node.matches("dialog[open], [aria-modal='true'], :popover-open") || role === "dialog" || role === "alertdialog") {
        remove(node, "dialog");
      } else if (popupName.test(identity(node)) && floating(node)) {
        remove(node, "named_popup");
      } else if (consentName.test(identity(node)) && floating(node)) {
        remove(node, "consent_popup");
      } else if (coversBottomContent(node)) {
        remove(node, "bottom_float_covering_content");
      }
    }
    // Only remove named backdrop layers paired with an actual removed popup.
    if (removed.length) {
      for (const node of nodes) {
        const rect = node.getBoundingClientRect();
        if (node.isConnected && visible(node) && backdropName.test(identity(node)) && floating(node) &&
            rect.width >= innerWidth * .5 && rect.height >= innerHeight * .5)
          remove(node, "popup_backdrop");
      }
      for (const node of [document.documentElement, document.body]) {
        if (/hidden|clip/.test(getComputedStyle(node).overflowY))
          node.style.setProperty("overflow-y", "auto", "important");
        if (getComputedStyle(node).pointerEvents === "none")
          node.style.setProperty("pointer-events", "auto", "important");
      }
    }
    return removed;
  });
}
