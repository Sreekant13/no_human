// A task stuck in awaiting_approval is invisible unless the tab is focused —
// the Needs-You badge already covers the title, but a backgrounded/pinned tab
// only shows its favicon. Overlay a small red dot on the existing SVG icon so
// "needs you" is visible from the tab strip alone. faviconHref stays pure (no
// DOM) so it's node --test'able; setFavicon is the only DOM-touching part.
//
// Note: index.html encodes the base icon with a hand-picked partial escaping
// (spaces/</> encoded, quotes literal). Here we keep the SVG as a plain string
// and encode it with encodeURIComponent, which escapes more characters (e.g.
// quotes). The result is a byte-different but equivalent data URI — still a
// valid `data:image/svg+xml,...` href rendering the same icon.

const BASE_SVG = `<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'><stop offset='0' stop-color='#4C9AFF'/><stop offset='1' stop-color='#0C66E4'/></linearGradient></defs><rect width='32' height='32' rx='8' fill='url(#g)'/><g stroke='white' stroke-width='2.6' stroke-linecap='round' stroke-linejoin='round' fill='none'><polyline points='9,13 16,9 23,13'/><polyline points='9,18.5 16,14.5 23,18.5'/><polyline points='9,24 16,20 23,24'/></g></svg>`;

const DOT = `<circle cx='25' cy='7' r='6' fill='#FF3B30' stroke='white' stroke-width='2'/>`;

export function faviconHref(showDot) {
  const svg = showDot ? BASE_SVG.replace("</svg>", `${DOT}</svg>`) : BASE_SVG;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

export function setFavicon(showDot) {
  let link = document.querySelector("link[rel='icon']");
  if (!link) {
    link = document.createElement("link");
    link.rel = "icon";
    link.type = "image/svg+xml";
    document.head.appendChild(link);
  }
  link.href = faviconHref(showDot);
}
