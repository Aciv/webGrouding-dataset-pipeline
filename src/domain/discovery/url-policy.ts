/** Accept exact origins, HTTP→HTTPS upgrades and www aliases, but no other host/port or HTTPS downgrade.
 * Arbitrary subdomains and redirected external domains remain outside collection scope.
 */
export function withinScope(entry: string, prefix: string, value: string): boolean {
  try {
    const url = new URL(value, entry), start = new URL(entry);
    const sameOrigin = url.origin === start.origin;
    const canonicalAlias = /^https?:$/.test(start.protocol) &&
      url.hostname.replace(/^www\./, "") === start.hostname.replace(/^www\./, "") &&
      url.port === start.port && (url.protocol === start.protocol ||
        (start.protocol === "http:" && url.protocol === "https:"));
    return /^https?:$/.test(url.protocol) && !url.username && !url.password &&
      ![...url.searchParams.keys()].some(key => /token|password|secret|signature|api.?key/i.test(key)) &&
      (sameOrigin || canonicalAlias) && (prefix === "/" || url.pathname === prefix ||
        url.pathname.startsWith(prefix.endsWith("/") ? prefix : prefix + "/"));
  } catch { return false; }
}

/** Adapted from View route identity: index aliases retained; fragments are removed for page identity. */
export function pageIdentity(value: string): string {
  const url = new URL(value);
  url.hash = "";
  for (const key of [...url.searchParams.keys()]) if (/^utm_|^(gclid|fbclid)$/i.test(key)) url.searchParams.delete(key);
  url.pathname = url.pathname.replace(/\/index\.html?$/, "/");
  return url.href;
}

/** Only ordinary page URLs become candidates; no form submission or business-action replay. */
export function candidateUrl(href: string, base: string, entry: string, prefix: string): string | null {
  try {
    if (!href.trim() || /^(#|javascript:|mailto:|tel:|data:|blob:)/i.test(href.trim())) return null;
    const url = new URL(href, base);
    if (!withinScope(entry, prefix, url.href) ||
        /\.(?:png|jpe?g|gif|webp|svg|ico|pdf|zip|gz|mp[34]|webm|woff2?|css|js|json|xml)$/i.test(url.pathname) ||
        /\/(?:log[-_]?out|sign[-_]?out|log[-_]?in|sign[-_]?in|sign[-_]?up|register|checkout|delete|api)(?:[/.]|$)/i.test(url.pathname)) return null;
    url.hash = "";
    return url.href;
  } catch { return null; }
}

/** Menu exploration adapts View's unrelated/preference exclusions to a small DOM-only interface. */
export function safeMenuName(name: string): boolean {
  return !/log\s*(in|out)|sign\s*(in|up|out)|register|subscribe|download|cookie|consent|language|locale|currency|theme|appearance|country|region|delete|remove|cart|buy|donate|登录|注册|订阅|语言|地区|主题|购物|删除/i.test(name);
}
