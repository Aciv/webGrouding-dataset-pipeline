/** Frozen pre-cache extractor: test oracle only, never used by the collector. */
import type { Page } from "playwright";
import { randomUUID } from "node:crypto";
import type { RawSnapshot } from "../../domain/types.js";

/**
 * Adapted from view-collector/src/capture/dom.ts::inspect.
 * Retains selector, geometry and interaction-hint traversal; deliberately removes
 * scrollbar injection, recognition tokens, aria-hidden rejection and label selection.
 * Adds hidden-node identity, direct text rectangles and evidence for later labeling.
 */
export async function extractDomReference(page: Page, maxNodes: number, captureHeight?: number): Promise<RawSnapshot> {
  return page.evaluate(({ limit, captureHeight, documentId }) => {
    type IdentityStore = { documentId: string; next: number; nodes: WeakMap<Element, string> };
    const runtime = window as typeof window & { __uiDatasetIdentity?: IdentityStore };
    const store = runtime.__uiDatasetIdentity ??= {
      // HTTP pages need not expose the secure-context-only crypto.randomUUID API.
      // Node supplies the token; the document's store preserves it across observations.
      documentId, next: 0, nodes: new WeakMap(),
    };
    const id = (element: Element): string => {
      let value = store.nodes.get(element);
      if (!value) {
        value = `n${String(store.next++).padStart(6, "0")}`;
        store.nodes.set(element, value);
      }
      return value;
    };
    const box = (rect: DOMRect) => ({ x: rect.x, y: rect.y, width: rect.width, height: rect.height });
    const selector = (element: Element): string => {
      if (element.id && document.querySelectorAll(`#${CSS.escape(element.id)}`).length === 1)
        return `#${CSS.escape(element.id)}`;
      const parts: string[] = [];
      let current: Element | null = element;
      while (current) {
        const parent: Element | null = current.parentElement;
        const siblings: Element[] = parent ? [...parent.children].filter(item => item.tagName === current!.tagName) : [current];
        parts.unshift(`${current.tagName.toLowerCase()}:nth-of-type(${siblings.indexOf(current) + 1})`);
        current = parent;
      }
      return parts.join(" > ");
    };
    const all = [...document.querySelectorAll("*")];
    // Assign before truncation/visibility filtering. IDs have document lifetime only.
    all.forEach(id);
    const selected = all.slice(0, limit);
    const selectedSet = new Set(selected);
    const css = new Map<Element, CSSStyleDeclaration>();
    const getStyle = (element: Element) => {
      let result = css.get(element);
      if (!result) { result = getComputedStyle(element); css.set(element, result); }
      return result;
    };
    const nodes = selected.map(element => {
      const rect = element.getBoundingClientRect();
      const style = getStyle(element);
      const left = Math.max(0, rect.left), top = Math.max(0, rect.top);
      const right = Math.min(innerWidth, rect.right);
      const bottom = Math.min(captureHeight ?? innerHeight, rect.bottom);
      const intersectsViewport = Math.min(innerWidth, rect.right) > left && Math.min(innerHeight, rect.bottom) > top;
      const captureBbox = right > left && bottom > top
        ? { x: left, y: top, width: right - left, height: bottom - top } : null;
      const attrs: Record<string, string> = {};
      for (const key of ["id", "class", "role", "href", "src", "alt", "title", "type",
        "aria-label", "aria-labelledby", "aria-hidden", "aria-expanded", "aria-controls",
        "aria-selected", "aria-checked", "aria-disabled", "placeholder", "data-testid",
        "controls", "disabled", "open", "multiple", "size"])
        if (element.hasAttribute(key)) attrs[key] = element.getAttribute(key)!;
      const clippingAncestors: string[] = [];
      for (let parent = element.parentElement; parent; parent = parent.parentElement) {
        const computed = getStyle(parent);
        if (/hidden|clip|auto|scroll/.test(`${computed.overflowX} ${computed.overflowY}`))
          clippingAncestors.push(id(parent));
      }
      const directText: string[] = [];
      const textRects: ReturnType<typeof box>[] = [];
      let textRectsTruncated = false;
      for (const child of element.childNodes) {
        if (child.nodeType !== Node.TEXT_NODE || !child.textContent?.trim()) continue;
        if (/^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(element.tagName)) continue;
        directText.push(child.textContent);
        const range = document.createRange();
        range.selectNodeContents(child);
        for (const fragment of range.getClientRects()) {
          if (textRects.length < 100) textRects.push(box(fragment));
          else textRectsTruncated = true;
        }
      }
      const text = directText.join(" ").replace(/\s+/g, " ").trim();
      const paint = (pseudo: string) => getComputedStyle(element, pseudo).content;
      return {
        nodeId: id(element),
        parentId: element.parentElement && selectedSet.has(element.parentElement) ? id(element.parentElement) : null,
        tag: element.tagName.toLowerCase(), selector: selector(element), attributes: attrs,
        ownText: text.slice(0, 2000), textTruncated: text.length > 2000,
        bbox: box(rect), fragments: [...element.getClientRects()].map(box),
        documentBbox: { ...box(rect), x: rect.x + scrollX, y: rect.y + scrollY },
        ...(element instanceof HTMLElement && element.offsetWidth > 0 && element.offsetHeight > 0 ? {
          clientBbox: {
            x: rect.x + element.clientLeft * rect.width / element.offsetWidth,
            y: rect.y + element.clientTop * rect.height / element.offsetHeight,
            width: element.clientWidth * rect.width / element.offsetWidth,
            height: element.clientHeight * rect.height / element.offsetHeight,
          },
        } : {}),
        captureBbox, textRects, textRectsTruncated,
        style: {
          display: style.display, visibility: style.visibility, opacity: style.opacity,
          position: style.position, zIndex: style.zIndex, overflowX: style.overflowX,
          overflowY: style.overflowY, clip: style.clip, clipPath: style.clipPath,
          backgroundColor: style.backgroundColor, backgroundImage: style.backgroundImage,
          borderTop: style.borderTop, borderRight: style.borderRight,
          borderBottom: style.borderBottom, borderLeft: style.borderLeft,
          borderRadius: style.borderRadius, boxShadow: style.boxShadow,
          fontSize: style.fontSize, fontWeight: style.fontWeight, color: style.color,
          padding: style.padding, gap: style.gap, gridTemplateColumns: style.gridTemplateColumns,
          flexDirection: style.flexDirection, transform: style.transform,
          filter: style.filter, whiteSpace: style.whiteSpace, margin: style.margin,
        },
        interactiveHint: element.matches('a[href],button,summary,input,select,textarea,[role="button"],[role="tab"],[role="link"],[role="menuitem"],[aria-expanded],[onclick],[tabindex]') ||
          style.cursor === "pointer",
        visibility: {
          cssVisible: element.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }),
          intersectsViewport, clippingAncestors, occlusion: "not_measured" as const,
        },
        pseudo: { before: paint("::before"), after: paint("::after") },
      };
    });
    return {
      schema: "raw_dom_geometry_v1" as const, documentId: store.documentId,
      url: location.href, title: document.title, capturedAt: new Date().toISOString(),
      viewport: { width: innerWidth, height: innerHeight, dpr: devicePixelRatio },
      scroll: { x: scrollX, y: scrollY },
      document: { width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight },
      coordinateSpace: "viewport_css_pixels" as const, scope: "main_frame_light_dom" as const,
      totalNodes: all.length, truncated: all.length > limit, nodes,
    };
  }, { limit: maxNodes, captureHeight, documentId: randomUUID() });
}
