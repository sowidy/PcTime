export function detectBrowser(userAgent) {
  if (/Edg\//.test(userAgent)) return "edge";
  if (/Vivaldi\//.test(userAgent)) return "vivaldi";
  return "chrome";
}

export function originOf(tab) {
  if (!tab || tab.incognito || !tab.url) return null;
  try {
    const url = new URL(tab.url);
    if (!["http:", "https:"].includes(url.protocol)) return null;
    return {scheme: url.protocol.slice(0, -1), host: url.host};
  } catch { return null; }
}

export function trimQueue(events, now = Date.now()) {
  let queue = events.filter(e => e.started_at_ms >= now - 600000);
  while (queue.length > 1000 || JSON.stringify(queue).length > 1024 * 1024) queue.shift();
  return queue;
}
